"""
Shared test setup. Every test runs OFFLINE and WITHOUT an API key:
  - any attempt to open a network connection raises an error,
  - ANTHROPIC_API_KEY is removed, so a real model call could not succeed,
  - the pause between site requests is set to 0.
Model calls and web pages are replaced by fakes inside each test.
"""

import socket

import pytest

import brochure.sources as sources_module
from brochure.config import Company


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise RuntimeError("Network access is blocked in tests.")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(sources_module, "FETCH_DELAY_SECONDS", 0)


@pytest.fixture
def acme() -> Company:
    return Company("acme", "Acme", "https://acme.com", "A")


@pytest.fixture
def beta() -> Company:
    return Company("beta", "Beta", "https://beta.com", "B")


@pytest.fixture
def acme_home() -> dict:
    """A fetched home page (as returned by scraper.fetch_page) that redirected to www."""
    return {
        "requested_url": "https://acme.com",
        "final_url": "https://www.acme.com/",
        "http_status": 200,
        "content_type": "text/html",
        "title": "Acme",
        "text": "Acme builds tools. " * 40,
        "links": ["/about", "/careers", "/products", "/privacy", "/pricing"],
        "error": None,
    }


class FetchRecorder:
    """A fake scraper.fetch_page that serves prepared pages and records every requested URL."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.calls: list[str] = []

    def __call__(self, url, timeout=15):
        self.calls.append(url)
        page = {"requested_url": url, "final_url": url, "http_status": 200, "content_type": "text/html",
                "title": url.rsplit("/", 1)[-1], "text": "", "links": [], "error": None}
        page.update(self.pages.get(url, {"http_status": 404, "error": "HTTP 404"}))
        return page


@pytest.fixture
def fetch_recorder():
    return FetchRecorder
