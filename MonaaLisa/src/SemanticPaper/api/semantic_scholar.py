import time

import requests

from util.logger import Logger


"""
27-December-2025 - Lenio
Abstract: A client for the Semantic Scholar batch API (embeddings, citations, references).
"""
class SemanticScholarAPI:

    """
    Args:
        api_key (str | None): Optional API key for authenticated requests.
    Note: Without an API key, the client will operate in unauthenticated mode with limited rate limits.
    """
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key
        self.logger = Logger("SemanticScholarAPI")
        if api_key:
            self.logger.debug("SemanticScholarAPI initialized with API key (authenticated).")
        else:
            self.logger.debug("SemanticScholarAPI initialized without API key (unauthenticated).")

    # ------------------------------------------------------------------
    # Batch API methods – Feb 2026 – Basti, retries/splitting Nov 2026
    # ------------------------------------------------------------------

    BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"
    BATCH_FIELDS = "externalIds,embedding.specter_v2,citations.externalIds,references.externalIds"
    # Status codes worth retrying (rate limit / temporary server problems)
    RETRY_STATUS = {429, 500, 502, 503, 504}
    MAX_ATTEMPTS = 5
    BACKOFF_BASE_SECONDS = 2.0
    BACKOFF_MAX_SECONDS = 60.0

    """
    Abstract: Batch-fetches SPECTER v2 embeddings (768-D), citations, and references
        from the SemanticScholar REST API for a list of arXiv IDs.
        Kept for backwards compatibility: transport failures are merged into
        ``not_found``. New code should use ``fetch_batch_detailed`` which keeps
        them apart (a network error is not the same as "unknown to S2").
    Returns: tuple(found, not_found)
    """
    def fetch_batch(
        self,
        arxiv_ids: list[str],
        batch_size: int = 100,
        pause_seconds: float = 1.0,
    ) -> tuple[list[dict], list[str]]:
        found, not_found, failed = self.fetch_batch_detailed(arxiv_ids, batch_size, pause_seconds)
        return found, not_found + failed

    """
    Abstract: Like fetch_batch, but returns three lists:
        found:     list[dict] – papers with embedding; keys ``arxiv_id``, ``s2_id``,
                   ``embedding_768d``, ``citation_arxiv_ids``, ``reference_arxiv_ids``,
                   ``non_arxiv_citation_count``, ``non_arxiv_reference_count``
        not_found: list[str]  – S2 answered, but has no paper or no embedding (yet)
        failed:    list[str]  – the request itself failed after all retries
                   (rate limit, timeout, 5xx). These should NOT count as a retry
                   attempt of the paper.
        Each HTTP request is retried with exponential backoff (honours the
        Retry-After header). A chunk rejected with HTTP 400 (e.g. response too
        large because of long citation lists) is split in halves and retried.
    Args:
    - arxiv_ids: bare arXiv IDs (e.g. ["2302.01234"])
    - batch_size: papers per HTTP request (S2 max 500; 100 keeps responses small)
    - pause_seconds: sleep between batches for rate-limit safety
    """
    def fetch_batch_detailed(
        self,
        arxiv_ids: list[str],
        batch_size: int = 100,
        pause_seconds: float = 1.0,
    ) -> tuple[list[dict], list[str], list[str]]:
        found: list[dict] = []
        not_found: list[str] = []
        failed: list[str] = []
        batch_size = max(1, min(batch_size, 500))

        for start in range(0, len(arxiv_ids), batch_size):
            chunk = arxiv_ids[start:start + batch_size]
            self._fetch_chunk(chunk, found, not_found, failed)
            self.logger.info(
                f"Batch {start // batch_size + 1}: {len(chunk)} requested "
                f"(totals: {len(found)} found, {len(not_found)} not found, {len(failed)} failed)"
            )
            if start + batch_size < len(arxiv_ids):
                time.sleep(pause_seconds)

        self.logger.info(
            f"Batch fetch complete: {len(found)} found, {len(not_found)} not found, "
            f"{len(failed)} failed out of {len(arxiv_ids)} total"
        )
        return found, not_found, failed

    def _fetch_chunk(self, chunk: list[str], found: list, not_found: list, failed: list):
        results, status = self._post_with_retry(chunk)

        if results is None:
            # 400 = request rejected (often: response too large). Split and retry.
            if status == 400 and len(chunk) > 1:
                mid = len(chunk) // 2
                self.logger.warning(f"S2 rejected batch of {len(chunk)} (HTTP 400) – splitting")
                self._fetch_chunk(chunk[:mid], found, not_found, failed)
                self._fetch_chunk(chunk[mid:], found, not_found, failed)
            else:
                failed.extend(chunk)
            return

        if not isinstance(results, list):
            self.logger.error(f"Unexpected S2 response type {type(results).__name__}")
            failed.extend(chunk)
            return

        for i, arxiv_id in enumerate(chunk):
            item = results[i] if i < len(results) else None
            parsed = self._parse_item(arxiv_id, item)
            if parsed is None:
                # unknown to S2 or known without embedding (not processed yet)
                not_found.append(arxiv_id)
            else:
                found.append(parsed)

    def _post_with_retry(self, chunk: list[str]) -> tuple[list | None, int | None]:
        headers = {"x-api-key": self.api_key} if self.api_key else {}
        payload = {"ids": [f"ARXIV:{aid}" for aid in chunk]}
        delay = self.BACKOFF_BASE_SECONDS
        status: int | None = None

        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            retry_after: float | None = None
            try:
                # the semanticscholar library has no batch endpoint, so use requests directly
                resp = requests.post(
                    self.BATCH_URL,
                    json=payload,
                    params={"fields": self.BATCH_FIELDS},
                    headers=headers,
                    timeout=120,
                )
                status = resp.status_code if isinstance(resp.status_code, int) else 200
                if status in self.RETRY_STATUS:
                    # requests' headers are a CaseInsensitiveDict (not a dict subclass)
                    headers_obj = getattr(resp, "headers", None)
                    retry_after = _parse_retry_after(
                        headers_obj.get("Retry-After") if hasattr(headers_obj, "get") else None
                    )
                    raise requests.HTTPError(f"HTTP {status}")
                if 400 <= status < 500:
                    # client error: retrying the same request will not help
                    self.logger.error(f"SemanticScholar batch request rejected: HTTP {status}")
                    return None, status
                resp.raise_for_status()
                return resp.json(), status
            except Exception as e:
                if attempt == self.MAX_ATTEMPTS:
                    self.logger.error(
                        f"SemanticScholar batch request failed after {attempt} attempts: {e}"
                    )
                    return None, status
                wait = min(retry_after if retry_after is not None else delay, self.BACKOFF_MAX_SECONDS)
                self.logger.warning(
                    f"SemanticScholar batch request failed ({e}), attempt {attempt}/{self.MAX_ATTEMPTS}, "
                    f"retrying in {wait:.0f}s"
                )
                time.sleep(wait)
                delay *= 2
        return None, status

    @staticmethod
    def _parse_item(arxiv_id: str, item) -> dict | None:
        if item is None or not isinstance(item, dict):
            return None
        embedding_data = item.get("embedding")
        vector_768 = embedding_data.get("vector") if isinstance(embedding_data, dict) else None
        if not vector_768:
            return None

        citation_arxiv_ids: list[str] = []
        non_arxiv_citation_count = 0
        for cit in (item.get("citations") or []):
            cit_arxiv = ((cit or {}).get("externalIds") or {}).get("ArXiv")
            if cit_arxiv:
                citation_arxiv_ids.append(cit_arxiv)
            else:
                non_arxiv_citation_count += 1

        reference_arxiv_ids: list[str] = []
        non_arxiv_reference_count = 0
        for ref in (item.get("references") or []):
            ref_arxiv = ((ref or {}).get("externalIds") or {}).get("ArXiv")
            if ref_arxiv:
                reference_arxiv_ids.append(ref_arxiv)
            else:
                non_arxiv_reference_count += 1

        return {
            "arxiv_id": arxiv_id,
            "s2_id": item.get("paperId"),
            "embedding_768d": vector_768,
            "citation_arxiv_ids": citation_arxiv_ids,
            "reference_arxiv_ids": reference_arxiv_ids,
            "non_arxiv_citation_count": non_arxiv_citation_count,
            "non_arxiv_reference_count": non_arxiv_reference_count,
        }


def _parse_retry_after(value) -> float | None:
    """Retry-After in seconds (HTTP-date form is ignored)."""
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None
