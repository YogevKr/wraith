"""Opt-in checks for the input tools: WRAITH_LIVE_TEST=1 uv run pytest tests/test_live_input_tools.py.

A local page on 127.0.0.1 has a <select>, a file input, an upload button that
opens a file chooser, a modal dialog, text that loads late, and a second page.
"""

from __future__ import annotations

import os

import pytest

if os.environ.get("WRAITH_LIVE_TEST") != "1":
    pytest.skip(
        "set WRAITH_LIVE_TEST=1 to run the input tool checks", allow_module_level=True
    )

from tests._local_server import html_pages, serve
from wraith.agent import AgentBrowser
from wraith.engine import launch
from wraith.uploads import UploadNotAllowedError

PAD = "<p>" + "Fixture text for a page with real content. " * 8 + "</p>"
FORM = f"""<!doctype html><title>Form</title><body>{PAD}
<select name=country><option value=us>United States</option><option value=uk>United Kingdom</option><option value=il>Israel</option></select>
<script>document.querySelector('[name=country]').addEventListener('change', e => {{
  document.body.dataset.change = (document.body.dataset.change || '') + e.isTrusted + ' ';
}});</script>
<input type=file name=doc>
<button onclick="document.getElementById('hidden').click()">Attach file</button>
<input type=file id=hidden style="display:none">
<button onclick="document.getElementById('dlg').showModal()">Open dialog</button>
<dialog id=dlg><p>Dialog text</p><button>Inside</button></dialog>
<button onclick="setTimeout(() => out.textContent = 'Loaded late', 1200)">Load later</button>
<p id=out></p>
<a href="/second">Second page</a>
</body>"""
SECOND = f"<!doctype html><title>Second</title><body><h1>Second</h1>{PAD}</body>"


_Handler = html_pages({"/second": SECOND}, FORM)


@pytest.fixture(scope="module")
def base():
    with serve(_Handler) as server:
        yield f"http://127.0.0.1:{server.server_port}"


def _index(ab, text=None, **attrs):
    for el in ab.last_snapshot.elements:
        if (text is None or el.text == text) and all(
            el.attributes.get(k) == v for k, v in attrs.items()
        ):
            return el.index
    raise AssertionError(f"no element {text!r} {attrs} in {ab.last_snapshot.to_text()}")


@pytest.mark.parametrize("engine", ["camoufox", "chromium"])
def test_input_tools_on_a_real_page(base, engine, tmp_path):
    uploads, outside = tmp_path / "uploads", tmp_path / "outside"
    uploads.mkdir()
    outside.mkdir()
    (uploads / "cv.txt").write_text("cv")
    (outside / "key.txt").write_text("key")
    js = lambda script: session.page.evaluate(script)  # noqa: E731

    with launch(engine=engine, headless=True, geoip=False) as session:
        ab = AgentBrowser(session=session, upload_dirs=[str(uploads)])
        ab.navigate(f"{base}/form")

        ab.select_option(_index(ab, name="country"), "United Kingdom")
        assert js("() => document.querySelector('[name=country]').value") == "uk"
        ab.select_option(_index(ab, name="country"), "il")
        assert js("() => document.querySelector('[name=country]').value") == "il"
        changes = js("() => document.body.dataset.change").split()
        assert changes and set(changes) == {"true"}, changes  # typed, not set by script

        ab.upload_file(_index(ab, name="doc"), str(uploads / "cv.txt"))
        assert (
            js("() => document.querySelector('[name=doc]').files[0].name") == "cv.txt"
        )
        ab.upload_file(_index(ab, "Attach file"), [str(uploads / "cv.txt")])
        assert js("() => document.getElementById('hidden').files[0].name") == "cv.txt"
        with pytest.raises(UploadNotAllowedError):
            ab.upload_file(_index(ab, name="doc"), str(outside / "key.txt"))

        ab.click(_index(ab, "Open dialog"))
        assert js("() => document.getElementById('dlg').open")
        ab.press_key("Escape")
        assert not js("() => document.getElementById('dlg').open")

        ab.click(_index(ab, "Load later"))
        ab.wait_for("Loaded late", seconds=5)
        with pytest.raises(TimeoutError):
            ab.wait_for("Never shown", seconds=1)

        ab.click(_index(ab, "Second page"))
        assert session.page.url.endswith("/second")
        assert ab.go_back().url.endswith("/form")
        assert ab.go_forward().url.endswith("/second")
