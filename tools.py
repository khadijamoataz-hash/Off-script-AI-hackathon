"""Agent tools: web search and page fetch.

Both tools only read public web pages. `fetch_url` records the page text in the evidence
ledger, which is what later allows a claim to cite that URL as a FACT. Neither tool falls
back to invented results: if search or a page is unreachable, the call raises ToolError.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from evidence import EvidenceLedger, FetchedPage

DEFAULT_SEARXNG_URL = "http://localhost:8080"
USER_AGENT = "FoundryResearchBot/0.1 (open-source research assistant)"
MAX_PAGE_BYTES = 1_500_000
MAX_PAGE_CHARS = 20_000
ALLOWED_CONTENT_TYPES = ("text/html", "text/plain", "application/xhtml+xml")


class ToolError(RuntimeError):
    """A tool could not do its job. Callers must surface this, never paper over it."""


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str = ""


def searxng_url() -> str:
    return os.getenv("FOUNDRY_SEARXNG_URL", DEFAULT_SEARXNG_URL).rstrip("/")


def search_status(timeout: float = 2.0) -> dict[str, object]:
    """Whether the search service answers. Used by the status page."""
    base = searxng_url()
    try:
        ok = httpx.get(f"{base}/healthz", timeout=timeout).status_code == 200
    except httpx.HTTPError:
        ok = False
    return {"searxng_url": base, "reachable": ok}


def web_search(
    query: str, *, max_results: int = 5, client: httpx.Client | None = None
) -> list[SearchResult]:
    """Search the web through the local SearXNG container. No API key, no account."""
    if not query.strip():
        raise ToolError("Empty search query.")
    owns_client = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        response = client.get(
            f"{searxng_url()}/search",
            params={"q": query, "format": "json", "safesearch": 1},
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ToolError(f"Web search is unavailable ({searxng_url()}): {exc}") from exc
    finally:
        if owns_client:
            client.close()

    results: list[SearchResult] = []
    seen: set[str] = set()
    for item in payload.get("results", []):
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        results.append(
            SearchResult(
                title=str(item.get("title") or "").strip(),
                url=url,
                snippet=str(item.get("content") or "").strip(),
            )
        )
        if len(results) >= max_results:
            break
    return results


class _TextExtractor(HTMLParser):
    """Pulls the title and visible text out of an HTML page using the standard library."""

    SKIP = {"script", "style", "noscript", "svg", "template", "head", "nav", "footer", "form"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        elif not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    """Return (title, visible text) for an HTML document."""
    parser = _TextExtractor()
    parser.feed(html)
    title = " ".join("".join(parser.title_parts).split())
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    return title, "\n".join(line for line in lines if line)


def _assert_public_url(url: str) -> None:
    """Refuse anything that is not a public http(s) address, so a page or a model cannot
    steer the fetcher at local services."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ToolError(f"Only http(s) URLs can be fetched: {url!r}")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ToolError(f"Cannot resolve host for {url!r}: {exc}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise ToolError(f"Refusing to fetch a non-public address: {url!r}")


def fetch_url(
    url: str,
    *,
    project_id: str,
    ledger: EvidenceLedger,
    client: httpx.Client | None = None,
    check_public: bool = True,
) -> FetchedPage:
    """Fetch a public page, extract its text and record it in the ledger for this project."""
    url = url.strip()
    owns_client = client is None
    client = client or httpx.Client(timeout=15.0, follow_redirects=False)
    try:
        current = url
        for _ in range(5):
            if check_public:
                _assert_public_url(current)
            with client.stream("GET", current, headers={"User-Agent": USER_AGENT}) as response:
                if response.is_redirect and response.headers.get("location"):
                    current = str(response.url.join(response.headers["location"]))
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if content_type and content_type not in ALLOWED_CONTENT_TYPES:
                    raise ToolError(f"Unsupported content type {content_type!r} at {url!r}")
                body = b""
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > MAX_PAGE_BYTES:
                        break
                encoding = response.encoding or "utf-8"
            break
        else:
            raise ToolError(f"Too many redirects for {url!r}")
    except httpx.HTTPError as exc:
        reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        raise ToolError(f"Could not fetch {url!r}: {reason}") from exc
    finally:
        if owns_client:
            client.close()

    raw = body.decode(encoding, errors="replace")
    if content_type == "text/plain":
        title, text = "", raw
    else:
        title, text = html_to_text(raw)
    if not text.strip():
        raise ToolError(f"No readable text at {url!r}")
    # The page is stored under the URL that was asked for, which is the URL a claim will cite.
    return ledger.record_fetch(
        project_id=project_id, url=url, title=title, text=text[:MAX_PAGE_CHARS]
    )
