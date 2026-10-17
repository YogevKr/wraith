"""read() returns the main content; find() returns passages with a term."""

from __future__ import annotations

import pytest

import wraith.agent as ag
from wraith.agent import AgentBrowser

ARTICLE = """# Tel Aviv

| Country | Israel |
| Population (2024) | 494,900 |
| Area | 52 km2 |

Tel Aviv is the most populous city in the Gush Dan area, with a population of 494,900.

The city was founded in 1909."""


def test_find_passages_returns_matching_rows_and_paragraphs():
    found = ag.find_passages(ARTICLE, "POPULATION")
    assert found == [
        "| Population (2024) | 494,900 | | Area | 52 km2 |",  # a row and the next ones
        "Tel Aviv is the most populous city in the Gush Dan area, with a population of 494,900.",
    ]
    assert ag.find_passages(ARTICLE, "founded", limit=1) == [
        "The city was founded in 1909."
    ]
    assert ag.find_passages(ARTICLE, "Jaffa") == []


def test_find_passages_clips_around_the_match():
    long = "word " * 200 + "needle " + "word " * 200
    (clip,) = ag.find_passages(long, "needle", width=60)
    assert (
        clip.startswith("…")
        and clip.endswith("…")
        and "needle" in clip
        and len(clip) <= 62
    )


class _Page:
    url = "https://a.test/"

    def __init__(self, main):
        self.main = main

    def evaluate(self, js):
        assert js == ag._MAIN_CONTENT_JS
        return self.main

    def content(self):
        return '<nav>Menu Menu Menu</nav><main><p>Article <a href="https://x.test/body">body</a></p></main>'

    def inner_text(self, selector):
        return "Menu Menu Menu Article body"


def _browser(main):
    return AgentBrowser(session=type("S", (), {"page": _Page(main), "context": None})())


def test_read_prefers_the_main_content():
    browser = _browser(
        {"html": "<h1>T</h1><main><p>Article body</p></main>", "text": "Article body"}
    )
    assert "Menu" not in browser.read() and "Article body" in browser.read()
    assert "Menu" in browser.read(full=True)
    assert "Menu" in _browser(None).read()  # no main element: the whole page


def test_find_needs_a_term():
    with pytest.raises(ValueError, match="word or phrase"):
        _browser(None).find("  ")


def test_read_drops_link_urls_unless_asked():
    browser = _browser(None)
    assert "x.test" not in browser.read() and "Article body" in browser.read()
    assert "(https://x.test/body)" in browser.read(links=True)
