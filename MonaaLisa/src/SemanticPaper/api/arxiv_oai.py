"""
Abstract: Client for arXiv's OAI-PMH interface (https://oaipmh.arxiv.org/oai).

    OAI-PMH is arXiv's official interface for harvesting metadata in bulk.
    Unlike the search API it has no result cap: ``ListRecords`` returns every
    record whose datestamp (= last change) lies in the requested date range,
    1300 records per page, continued via ``resumptionToken``.

    We use the ``arXivRaw`` format because it contains the date of every
    version (v1 date = first publication) plus DOI, journal-ref and license.

    Notes from testing the live endpoint (Nov 2026):
    - errors come back as HTTP 200 with <error code='...'> in the XML
    - an empty date range is reported as error ``noRecordsMatch``
    - expired/invalid tokens are reported as ``badResumptionToken``
    - flow control: HTTP 503 with Retry-After
"""

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterator

import requests

from util.logger import Logger

OAI_BASE_URL = "https://oaipmh.arxiv.org/oai"
NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "raw": "http://arxiv.org/OAI/arXivRaw/",
}


class OAIError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


class BadResumptionToken(OAIError):
    pass


@dataclass
class OAIPage:
    records: list[dict] = field(default_factory=list)
    deleted_ids: list[str] = field(default_factory=list)
    resumption_token: str | None = None


def _text(el, path: str) -> str | None:
    found = el.find(path, NS)
    if found is None or found.text is None:
        return None
    value = " ".join(found.text.split())  # collapse line breaks / indentation
    return value or None


def _parse_version_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _normalize_authors(raw: str | None) -> str | None:
    """'A, B and C' -> 'A, B, C' (same format as the rest of the DB)."""
    if not raw:
        return None
    return re.sub(r",?\s+and\s+", ", ", raw).strip()


def parse_record(record) -> tuple[dict | None, str | None]:
    """Returns (paper dict, None) for a normal record or (None, id) for a deleted one."""
    header = record.find("oai:header", NS)
    identifier = _text(header, "oai:identifier") if header is not None else None
    if header is not None and header.get("status") == "deleted":
        return None, (identifier or "").replace("oai:arXiv.org:", "") or None

    meta = record.find("oai:metadata/raw:arXivRaw", NS)
    if meta is None:
        return None, None
    entry_id = _text(meta, "raw:id")
    if not entry_id:
        return None, None

    versions = sorted(
        (
            (v.get("version", ""), _parse_version_date(_text(v, "raw:date")))
            for v in meta.findall("raw:version", NS)
        ),
        key=lambda item: int(item[0].lstrip("v") or 0) if item[0].lstrip("v").isdigit() else 0,
    )
    dates = [d for _, d in versions if d is not None]

    return {
        "entry_id": entry_id,
        "title": _text(meta, "raw:title"),
        "authors": _normalize_authors(_text(meta, "raw:authors")),
        "abstract": _text(meta, "raw:abstract"),
        "categories": _text(meta, "raw:categories"),
        "published": dates[0] if dates else None,   # v1
        "updated": dates[-1] if dates else None,    # latest version
        "doi": _text(meta, "raw:doi"),
        "journal_ref": _text(meta, "raw:journal-ref"),
        "license": _text(meta, "raw:license"),
        "url": f"https://arxiv.org/pdf/{entry_id}",
    }, None


def parse_list_records(xml_text: str | bytes) -> OAIPage:
    root = ET.fromstring(xml_text)
    error = root.find("oai:error", NS)
    if error is not None:
        code = error.get("code", "unknown")
        message = " ".join((error.text or "").split())
        if code == "noRecordsMatch":
            return OAIPage()
        if code == "badResumptionToken":
            raise BadResumptionToken(code, message)
        raise OAIError(code, message)

    page = OAIPage()
    list_records = root.find("oai:ListRecords", NS)
    if list_records is None:
        return page
    for record in list_records.findall("oai:record", NS):
        paper, deleted_id = parse_record(record)
        if paper:
            page.records.append(paper)
        elif deleted_id:
            page.deleted_ids.append(deleted_id)
    token_el = list_records.find("oai:resumptionToken", NS)
    if token_el is not None and token_el.text and token_el.text.strip():
        page.resumption_token = token_el.text.strip()
    return page


class ArxivOAIClient:
    """
    Abstract: Harvests arXiv metadata page by page with polite pacing and retries.
    Args:
    - base_url: OAI endpoint
    - delay_seconds: pause between pages (arXiv asks for ≥ 3 s)
    - max_attempts: retries per page on network errors, 5xx and 503 flow control
    """
    RETRY_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, base_url: str = OAI_BASE_URL, delay_seconds: float = 3.0,
                 timeout: float = 120.0, max_attempts: int = 6,
                 user_agent: str = "MonaaLisa-Harvester/1.0 (+https://github.com/Monaa-Lisa/Monaa-Lisa)"):
        self.base_url = base_url
        self.delay_seconds = delay_seconds
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.headers = {"User-Agent": user_agent}
        self.logger = Logger("ArxivOAI")

    def _get(self, params: dict) -> str:
        delay = 5.0
        for attempt in range(1, self.max_attempts + 1):
            wait = delay
            try:
                resp = requests.get(self.base_url, params=params, headers=self.headers, timeout=self.timeout)
                if resp.status_code in self.RETRY_STATUS:
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and str(retry_after).isdigit():
                        wait = float(retry_after)
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.text
            except requests.RequestException as e:
                if attempt == self.max_attempts:
                    raise
                wait = min(wait, 300.0)
                self.logger.warning(f"OAI request failed ({e}), attempt {attempt}/{self.max_attempts}, "
                                    f"retrying in {wait:.0f}s")
                time.sleep(wait)
                delay = min(delay * 2, 300.0)
        raise RuntimeError("unreachable")

    def list_records_page(self, from_date: date | None = None, until_date: date | None = None,
                          set_spec: str | None = None, resumption_token: str | None = None) -> OAIPage:
        if resumption_token:
            params = {"verb": "ListRecords", "resumptionToken": resumption_token}
        else:
            params = {"verb": "ListRecords", "metadataPrefix": "arXivRaw"}
            if from_date:
                params["from"] = from_date.isoformat()
            if until_date:
                params["until"] = until_date.isoformat()
            if set_spec:
                params["set"] = set_spec
        return parse_list_records(self._get(params))

    def iter_pages(self, from_date: date | None = None, until_date: date | None = None,
                   set_spec: str | None = None, resumption_token: str | None = None) -> Iterator[OAIPage]:
        """
        Yields one page after another. ``page.resumption_token`` is the token for
        the NEXT page – persist it after processing a page to be able to resume.
        """
        token = resumption_token
        first = True
        while True:
            if not first:
                time.sleep(self.delay_seconds)
            first = False
            page = self.list_records_page(from_date, until_date, set_spec, token)
            yield page
            token = page.resumption_token
            if not token:
                return
