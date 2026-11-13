from datetime import date, datetime, timezone
from unittest.mock import Mock, patch

import pytest

from MonaaLisa.src.SemanticPaper.api.arxiv_oai import (
    ArxivOAIClient,
    BadResumptionToken,
    OAIError,
    parse_list_records,
)

MOD = "MonaaLisa.src.SemanticPaper.api.arxiv_oai"

# Shortened copy of a real arXivRaw response (Nov 2026)
PAGE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <responseDate>2026-11-11T15:45:20Z</responseDate>
  <ListRecords>
    <record>
      <header>
        <identifier>oai:arXiv.org:1709.07021</identifier>
        <datestamp>2026-10-01</datestamp>
        <setSpec>math:math:GR</setSpec>
      </header>
      <metadata>
        <arXivRaw xmlns="http://arxiv.org/OAI/arXivRaw/">
          <id>1709.07021</id>
          <submitter>Kirill Zainoulline</submitter>
          <version version="v2"><date>Tue, 29 Sep 2026 15:47:12 GMT</date><size>715kb</size></version>
          <version version="v1"><date>Wed, 20 Sep 2017 18:16:48 GMT</date><size>235kb</size></version>
          <title>Uniquely labelled geodesics
            of Coxeter groups</title>
          <authors>Elisabeth Fink, Anna Example and Kirill Zainoulline</authors>
          <categories>math.GR math.CO</categories>
          <license>http://arxiv.org/licenses/nonexclusive-distrib/1.0/</license>
          <journal-ref>J. Example 1 (2027)</journal-ref>
          <doi>10.1090/jams/1082</doi>
          <abstract>  Studying geodesics in
            Cayley graphs.  </abstract>
        </arXivRaw>
      </metadata>
    </record>
    <record>
      <header status="deleted">
        <identifier>oai:arXiv.org:2101.00001</identifier>
        <datestamp>2026-10-01</datestamp>
      </header>
    </record>
    <resumptionToken expirationDate='2026-11-13T00:00:00Z'>verb%3DListRecords%26skip%3D1300</resumptionToken>
  </ListRecords>
</OAI-PMH>"""


def _error_xml(code):
    return f"""<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
      <error code='{code}'>Reason: something</error></OAI-PMH>"""


class TestParseListRecords:

    def test_parses_record_fields(self):
        page = parse_list_records(PAGE_XML)
        assert len(page.records) == 1
        r = page.records[0]
        assert r["entry_id"] == "1709.07021"
        assert r["title"] == "Uniquely labelled geodesics of Coxeter groups"   # whitespace collapsed
        assert r["authors"] == "Elisabeth Fink, Anna Example, Kirill Zainoulline"
        assert r["abstract"] == "Studying geodesics in Cayley graphs."
        assert r["categories"] == "math.GR math.CO"
        assert r["doi"] == "10.1090/jams/1082"
        assert r["journal_ref"] == "J. Example 1 (2027)"
        assert r["url"] == "https://arxiv.org/pdf/1709.07021"

    def test_published_is_v1_and_updated_is_latest_version(self):
        r = parse_list_records(PAGE_XML).records[0]
        assert r["published"] == datetime(2017, 9, 20, 18, 16, 48, tzinfo=timezone.utc)
        assert r["updated"] == datetime(2026, 9, 29, 15, 47, 12, tzinfo=timezone.utc)

    def test_collects_deleted_records_and_token(self):
        page = parse_list_records(PAGE_XML)
        assert page.deleted_ids == ["2101.00001"]
        assert page.resumption_token == "verb%3DListRecords%26skip%3D1300"

    def test_no_records_match_is_an_empty_page(self):
        page = parse_list_records(_error_xml("noRecordsMatch"))
        assert page.records == [] and page.resumption_token is None

    def test_bad_resumption_token_raises_specific_error(self):
        with pytest.raises(BadResumptionToken):
            parse_list_records(_error_xml("badResumptionToken"))

    def test_other_errors_raise(self):
        with pytest.raises(OAIError) as exc:
            parse_list_records(_error_xml("badArgument"))
        assert exc.value.code == "badArgument"


def _resp(status=200, text=PAGE_XML, headers=None):
    r = Mock()
    r.status_code = status
    r.text = text
    r.headers = headers or {}
    r.raise_for_status = Mock()
    return r


class TestArxivOAIClient:

    @patch(f"{MOD}.Logger")
    @patch(f"{MOD}.time.sleep")
    @patch(f"{MOD}.requests.get")
    def test_first_request_uses_arxivraw_and_dates(self, mock_get, mock_sleep, mock_logger):
        mock_get.return_value = _resp()
        ArxivOAIClient().list_records_page(date(2026, 10, 1), date(2026, 10, 2), "cs")
        params = mock_get.call_args.kwargs["params"]
        assert params == {"verb": "ListRecords", "metadataPrefix": "arXivRaw",
                          "from": "2026-10-01", "until": "2026-10-02", "set": "cs"}

    @patch(f"{MOD}.Logger")
    @patch(f"{MOD}.time.sleep")
    @patch(f"{MOD}.requests.get")
    def test_follow_up_request_only_sends_token(self, mock_get, mock_sleep, mock_logger):
        mock_get.return_value = _resp()
        ArxivOAIClient().list_records_page(date(2026, 10, 1), resumption_token="tok")
        assert mock_get.call_args.kwargs["params"] == {"verb": "ListRecords", "resumptionToken": "tok"}

    @patch(f"{MOD}.Logger")
    @patch(f"{MOD}.time.sleep")
    @patch(f"{MOD}.requests.get")
    def test_retries_503_with_retry_after(self, mock_get, mock_sleep, mock_logger):
        mock_get.side_effect = [_resp(503, "", {"Retry-After": "42"}), _resp()]
        page = ArxivOAIClient().list_records_page(date(2026, 10, 1))
        assert len(page.records) == 1
        mock_sleep.assert_called_once_with(42.0)

    @patch(f"{MOD}.Logger")
    @patch(f"{MOD}.time.sleep")
    @patch(f"{MOD}.requests.get")
    def test_iter_pages_follows_tokens_and_pauses_between_pages(self, mock_get, mock_sleep, mock_logger):
        last_page = PAGE_XML.replace(
            "<resumptionToken expirationDate='2026-11-13T00:00:00Z'>verb%3DListRecords%26skip%3D1300</resumptionToken>",
            "<resumptionToken/>")
        mock_get.side_effect = [_resp(), _resp(text=last_page)]
        pages = list(ArxivOAIClient(delay_seconds=3).iter_pages(date(2026, 10, 1)))
        assert len(pages) == 2
        assert mock_get.call_args_list[1].kwargs["params"]["resumptionToken"] == "verb%3DListRecords%26skip%3D1300"
        mock_sleep.assert_called_once_with(3)
