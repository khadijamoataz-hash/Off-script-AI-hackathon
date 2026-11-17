"""The header shows the brand logo, served by the app itself."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import app


def test_logo_is_served_and_shown_in_the_header() -> None:
    client = TestClient(app)
    logo = client.get("/static/brand/logo.png")
    assert logo.status_code == 200 and logo.headers["content-type"] == "image/png"
    page = client.get("/status").text
    assert '<a class="brand" href="/"><img class="brand-logo" src="/static/brand/logo.png" alt="Foundry"' in page
