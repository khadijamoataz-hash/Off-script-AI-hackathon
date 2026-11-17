"""The header shows the brand logo, served by the app itself."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import app


def test_logo_is_served_and_shown_in_the_header() -> None:
    client = TestClient(app)
    mark = client.get("/static/brand/logo-mark.png")
    assert mark.status_code == 200 and mark.headers["content-type"] == "image/png"
    page = client.get("/status").text
    # Symbol image plus the name as real text; the link reads "Foundry" and goes home.
    assert ('<a class="brand" href="/"><img class="brand-mark" src="/static/brand/logo-mark.png" alt=""'
            in page)
    assert '<span class="brand-word">Foundry</span></a>' in page
