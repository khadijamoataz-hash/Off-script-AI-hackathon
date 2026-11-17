"""Search and fetch tools: real parsing, mocked network transport."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

import tools
from evidence import ClaimType, EvidenceLedger
from tools import ToolError, fetch_url, html_to_text, web_search

PAGE = """<html><head><title>Invoice survey</title><style>p{color:red}</style></head>
<body><nav>Home</nav><script>track()</script>
<h1>Survey</h1><p>Freelancers spend <b>five hours</b> a month chasing late invoices.</p>
</body></html>"""


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_html_to_text_drops_scripts_and_navigation() -> None:
    title, text = html_to_text(PAGE)
    assert title == "Invoice survey"
    assert "Freelancers spend five hours a month chasing late invoices." in text
    assert "track()" not in text and "Home" not in text


def test_fetch_records_page_and_makes_a_quoted_claim_a_fact(tmp_path: Path) -> None:
    ledger = EvidenceLedger(tmp_path / "db.sqlite3")
    client = _client(lambda request: httpx.Response(200, html=PAGE))
    url = "https://example.org/survey"

    page = fetch_url(url, project_id="p1", ledger=ledger, client=client, check_public=False)
    assert page.title == "Invoice survey"

    claim = ledger.add_claim(
        "Late invoices cost freelancers time.", project_id="p1", source_url=url,
        quote="five hours a month chasing late invoices",
    )
    assert claim.claim_type == ClaimType.FACT


def test_fetch_refuses_local_addresses_and_binary_content(tmp_path: Path) -> None:
    ledger = EvidenceLedger(tmp_path / "db.sqlite3")
    with pytest.raises(ToolError, match="non-public"):
        fetch_url("http://127.0.0.1:11434/api/tags", project_id="p1", ledger=ledger)
    with pytest.raises(ToolError, match="http"):
        fetch_url("file:///etc/passwd", project_id="p1", ledger=ledger)

    pdf = _client(lambda request: httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"}))
    with pytest.raises(ToolError, match="content type"):
        fetch_url("https://example.org/a.pdf", project_id="p1", ledger=ledger, client=pdf, check_public=False)
    assert ledger.list_fetches(project_id="p1") == []


def test_fetch_failure_raises_and_records_nothing(tmp_path: Path) -> None:
    ledger = EvidenceLedger(tmp_path / "db.sqlite3")
    client = _client(lambda request: httpx.Response(404))
    with pytest.raises(ToolError):
        fetch_url("https://example.org/missing", project_id="p1", ledger=ledger, client=client, check_public=False)
    assert ledger.list_fetches(project_id="p1") == []


def test_web_search_parses_results_and_fails_loudly_when_down() -> None:
    payload = {"results": [
        {"title": "A", "url": "https://a.example/1", "content": "first"},
        {"title": "A again", "url": "https://a.example/1", "content": "duplicate"},
        {"title": "Bad", "url": "javascript:alert(1)"},
        {"title": "B", "url": "https://b.example/2", "content": "second"},
    ]}
    results = web_search("invoice tools", client=_client(lambda request: httpx.Response(200, json=payload)))
    assert [r.url for r in results] == ["https://a.example/1", "https://b.example/2"]

    with pytest.raises(ToolError, match="unavailable"):
        web_search("invoice tools", client=_client(lambda request: httpx.Response(403)))


@pytest.mark.parametrize("operation", ["search", "fetch"])
def test_slow_drip_responses_exhaust_budget_without_partial_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    now = [0.0]
    monkeypatch.setattr(tools, "monotonic", lambda: now[0])

    class SlowBody(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield b'{"results": [' if operation == "search" else b"Some source text."
            now[0] = 100.0
            yield b"]}" if operation == "search" else b" More source text."

        def close(self):
            self.closed = True

    body = SlowBody()
    ledger = EvidenceLedger(tmp_path / "db.sqlite3")
    client = _client(lambda request: httpx.Response(200, stream=body))
    with pytest.raises(ToolError, match="latency budget"):
        if operation == "search":
            web_search("invoice tools", client=client)
        else:
            fetch_url(
                "https://example.org/slow", project_id="p1", ledger=ledger,
                client=client, check_public=False,
            )
    assert body.closed
    assert ledger.list_fetches(project_id="p1") == []


def test_redirects_share_one_fetch_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr(tools, "monotonic", lambda: now[0])
    timeouts = []

    def redirect(request):
        timeouts.append(request.extensions["timeout"])
        now[0] += 6.0
        return httpx.Response(302, headers={"location": "/next"})

    ledger = EvidenceLedger(tmp_path / "db.sqlite3")
    with pytest.raises(ToolError, match="latency budget"):
        fetch_url(
            "https://example.org/first", project_id="p1", ledger=ledger,
            client=_client(redirect), check_public=False,
        )
    assert len(timeouts) == 2
    assert timeouts[1]["read"] < timeouts[0]["read"]
    assert ledger.list_fetches(project_id="p1") == []


def test_injected_redirect_following_client_cannot_skip_public_url_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked = []
    requested = []

    def check(url):
        checked.append(url)
        if "127.0.0.1" in url:
            raise ToolError("Refusing to fetch a non-public address.")

    def redirect(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    monkeypatch.setattr(tools, "_assert_public_url", check)
    ledger = EvidenceLedger(tmp_path / "db.sqlite3")
    client = httpx.Client(transport=httpx.MockTransport(redirect), follow_redirects=True)
    with pytest.raises(ToolError, match="non-public"):
        fetch_url("https://example.org/start", project_id="p1", ledger=ledger, client=client)
    assert checked == ["https://example.org/start", "http://127.0.0.1/private"]
    assert requested == ["https://example.org/start"]
    assert ledger.list_fetches(project_id="p1") == []


def test_oversized_source_is_rejected_instead_of_recording_partial_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tools, "MAX_PAGE_BYTES", 5)
    ledger = EvidenceLedger(tmp_path / "db.sqlite3")
    client = _client(lambda request: httpx.Response(200, text="too long"))
    with pytest.raises(ToolError, match="byte budget"):
        fetch_url(
            "https://example.org/large", project_id="p1", ledger=ledger,
            client=client, check_public=False,
        )
    assert ledger.list_fetches(project_id="p1") == []


@pytest.mark.parametrize("payload", [{}, [], {"results": {}}, {"results": [None]}])
def test_invalid_search_payload_is_an_explicit_failure(payload) -> None:
    client = _client(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(ToolError, match="unavailable"):
        web_search("invoice tools", client=client)


def test_search_timeout_without_message_reports_exception_type() -> None:
    def timeout(request):
        raise httpx.ReadTimeout("")

    with pytest.raises(ToolError, match="ReadTimeout"):
        web_search("invoice tools", client=_client(timeout))
