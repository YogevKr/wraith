"""DOM snapshotting for the Wraith agent layer (browser-use style).

This module turns a live Playwright page into a compact, indexed view of its
*interactive* elements — the perception half of the perceive/act loop that the
agent (:mod:`wraith.agent`) and the MCP server (:mod:`wraith.mcp`) drive.

The core idea, lifted from browser-use: an LLM doesn't need the raw DOM. It
needs a short, stable list of the things it can *act on*, each tagged with a
small integer index, e.g.::

    [12]<button role=button>Search</button>
    [13]<input role=textbox placeholder="Search query"/>

The agent then acts by index — ``page.locator('[data-wraith-index="12"]')`` —
because :func:`take_snapshot` stamps the matching ``data-wraith-index``
attribute onto every interactive element it returns. Indices are sequential and
only valid for the snapshot that produced them; re-snapshot after any action
that mutates the DOM.

Everything here is duck-typed against Playwright's *sync* API and contains no
hard imports of Playwright/Camoufox, so ``import wraith.snapshot`` succeeds even
without a browser installed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = ["Element", "Snapshot", "take_snapshot"]


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class Element:
    """A single interactive element discovered in a :class:`Snapshot`.

    Attributes:
        index: Sequential integer assigned during the snapshot. Act on this
            element via ``page.locator('[data-wraith-index="<index>"]')`` — the
            snapshot stamps a matching ``data-wraith-index`` attribute onto the
            live DOM node.
        tag: Lowercase tag name (``button``, ``a``, ``input``, ...).
        role: ARIA role — the explicit ``role`` attribute when present, else an
            implicit role derived from the tag (e.g. ``a`` -> ``link``). May be
            an empty string when none can be determined.
        text: Best-effort accessible label / visible text, trimmed.
        attributes: A curated dict of useful attributes (``placeholder``,
            ``aria-label``, ``href``, ``type``, ``value``, ``name``, ``id``,
            ``title``, ``alt``, ``checked``, ``disabled``, ...). Only present
            keys are included.
    """

    index: int
    tag: str
    role: str
    text: str
    attributes: dict = field(default_factory=dict)

    def to_text(self) -> str:
        """Render this element as one browser-use-style line.

        Example: ``[12]<button role=button aria-label="Search">Search</button>``.
        """
        parts = [self.tag]
        if self.role:
            parts.append(f"role={self.role}")

        # Surface a few high-signal attributes inline; keep the line short.
        for key in ("type", "name", "placeholder", "aria-label", "href",
                    "value", "checked", "title", "alt"):
            val = self.attributes.get(key)
            if val in (None, "", False):
                continue
            if val is True:
                parts.append(key)
            else:
                parts.append(f'{key}="{_clip(str(val), 60)}"')

        head = " ".join(parts)
        body = _clip(self.text, 120)
        if body:
            return f"[{self.index}]<{head}>{body}</{self.tag}>"
        return f"[{self.index}]<{head}/>"


@dataclass
class Snapshot:
    """An indexed, LLM-friendly view of a page at a moment in time.

    Attributes:
        url: The page URL when the snapshot was taken.
        title: The page title.
        elements: Interactive elements, in document/index order.
        screenshot: PNG bytes if ``highlight=True`` was passed to
            :func:`take_snapshot`, else ``None``.
    """

    url: str
    title: str
    elements: list[Element]
    screenshot: Optional[bytes] = None

    def to_text(self) -> str:
        """Render the snapshot as browser-use-style text for an LLM.

        One line per interactive element (with its action index); a small
        header carries the URL and title so the model has page context without
        an index it might try to act on.
        """
        lines = [f"URL: {self.url}", f"Title: {self.title}", ""]
        if self.elements:
            lines.extend(el.to_text() for el in self.elements)
        else:
            lines.append("(no interactive elements found)")
        return "\n".join(lines)

    def by_index(self, i: int) -> Optional[Element]:
        """Return the element with index ``i``, or ``None`` if absent."""
        for el in self.elements:
            if el.index == i:
                return el
        return None

    def __len__(self) -> int:
        return len(self.elements)

    def __iter__(self):
        return iter(self.elements)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def take_snapshot(
    page: Any,
    *,
    viewport_only: bool = True,
    highlight: bool = False,
    max_elements: int = 200,
) -> Snapshot:
    """Walk ``page``'s DOM and return an indexed :class:`Snapshot`.

    Runs :data:`_BUILD_DOM_TREE_JS` in the page to find visible interactive
    elements, stamp each with a sequential ``data-wraith-index`` attribute, and
    return their metadata. Act on a returned element by index via
    ``page.locator('[data-wraith-index="<index>"]')``.

    Args:
        page: A Playwright sync ``Page``.
        viewport_only: When ``True`` (default), only elements intersecting the
            current viewport are indexed; when ``False``, the whole document is
            considered.
        highlight: When ``True``, draw labeled boxes over each indexed element
            and capture a PNG screenshot into :attr:`Snapshot.screenshot`.
        max_elements: Cap on how many elements to index (keeps the text view
            and the LLM context bounded).

    Returns:
        A :class:`Snapshot`. ``elements`` is empty if the JS fails or nothing
        interactive is visible; this never raises for an empty page.
    """
    try:
        url = page.url
    except Exception:
        url = ""
    try:
        title = page.title()
    except Exception:
        title = ""

    args = {
        "viewportOnly": bool(viewport_only),
        "highlight": bool(highlight),
        "maxElements": int(max_elements),
    }

    raw: Any
    try:
        raw = page.evaluate(_BUILD_DOM_TREE_JS, args)
    except Exception:
        raw = []

    elements = _parse_elements(raw)

    screenshot: Optional[bytes] = None
    if highlight:
        try:
            screenshot = page.screenshot()
        except Exception:
            screenshot = None

    return Snapshot(url=url, title=title, elements=elements, screenshot=screenshot)


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _parse_elements(raw: Any) -> list[Element]:
    """Coerce the JS payload (list of dicts, or a JSON string) into Elements."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if not isinstance(raw, list):
        return []

    out: list[Element] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        attrs = item.get("attributes")
        if not isinstance(attrs, dict):
            attrs = {}
        out.append(
            Element(
                index=index,
                tag=str(item.get("tag") or "").lower(),
                role=str(item.get("role") or ""),
                text=str(item.get("text") or "").strip(),
                attributes=attrs,
            )
        )
    return out


def _clip(s: str, n: int) -> str:
    """Collapse whitespace and truncate ``s`` to ``n`` chars with an ellipsis."""
    s = " ".join(s.split())
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)].rstrip() + "…"


# --------------------------------------------------------------------------- #
# The in-page DOM walker.
#
# Self-contained, dependency-free JS executed via page.evaluate. It:
#   * walks the document (optionally piercing open shadow roots),
#   * keeps elements that are INTERACTIVE (tag, handler, tabindex, or role),
#   * filters to visible (and, when viewportOnly, in-viewport) elements,
#   * assigns a sequential integer index and stamps data-wraith-index="<i>",
#   * optionally overlays labeled highlight boxes,
#   * returns [{index, tag, role, text, attributes}, ...].
#
# Robustness notes: never throws out of the top level (per-node work is wrapped
# in try/catch), tolerates detached/odd nodes, and re-running it first clears
# any stale data-wraith-index / highlight overlay from a previous snapshot.
# --------------------------------------------------------------------------- #

_BUILD_DOM_TREE_JS = r"""
(args) => {
  args = args || {};
  const viewportOnly = args.viewportOnly !== false;
  const highlight = !!args.highlight;
  const maxElements = (typeof args.maxElements === 'number' && args.maxElements > 0)
    ? args.maxElements : 200;

  const HIGHLIGHT_CONTAINER_ID = '__wraith_highlight_container__';
  const INDEX_ATTR = 'data-wraith-index';

  // ---- cleanup any state left by a previous snapshot --------------------- //
  try {
    document.querySelectorAll('[' + INDEX_ATTR + ']').forEach((el) => {
      el.removeAttribute(INDEX_ATTR);
    });
  } catch (e) {}
  try {
    const old = document.getElementById(HIGHLIGHT_CONTAINER_ID);
    if (old) old.remove();
  } catch (e) {}

  // ---- which roles count as interactive ---------------------------------- //
  const INTERACTIVE_ROLES = new Set([
    'button', 'link', 'checkbox', 'menuitem', 'menuitemcheckbox',
    'menuitemradio', 'tab', 'switch', 'radio', 'combobox', 'textbox',
    'searchbox', 'slider', 'spinbutton', 'option', 'listbox',
  ]);
  const INTERACTIVE_TAGS = new Set([
    'a', 'button', 'input', 'select', 'textarea', 'summary', 'details',
    'label', 'option',
  ]);
  // Implicit ARIA role from tag (best-effort, enough to label the line).
  const IMPLICIT_ROLE = {
    a: 'link', button: 'button', select: 'combobox', textarea: 'textbox',
    summary: 'button', option: 'option',
  };
  const INPUT_TYPE_ROLE = {
    checkbox: 'checkbox', radio: 'radio', range: 'slider',
    number: 'spinbutton', submit: 'button', button: 'button',
    reset: 'button', image: 'button', search: 'searchbox',
  };

  const win = window;

  function implicitRole(el) {
    const tag = el.tagName.toLowerCase();
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      return INPUT_TYPE_ROLE[t] || 'textbox';
    }
    return IMPLICIT_ROLE[tag] || '';
  }

  function elementRole(el) {
    const explicit = (el.getAttribute('role') || '').trim().toLowerCase();
    if (explicit) return explicit.split(/\s+/)[0];
    return implicitRole(el);
  }

  function isInteractive(el) {
    let tag;
    try { tag = el.tagName.toLowerCase(); } catch (e) { return false; }
    if (INTERACTIVE_TAGS.has(tag)) {
      // A bare <a> with no href is usually not actionable; keep it only if it
      // has an href, a click handler, or an interactive role.
      if (tag === 'a' && !el.hasAttribute('href') && !el.onclick
          && !el.getAttribute('role') && !el.hasAttribute('tabindex')) {
        return false;
      }
      return true;
    }
    if (el.isContentEditable === true) return true;
    if (el.hasAttribute('onclick') || typeof el.onclick === 'function') return true;
    if (el.hasAttribute('tabindex') && el.getAttribute('tabindex') !== '-1') return true;
    const role = (el.getAttribute('role') || '').trim().toLowerCase().split(/\s+/)[0];
    if (role && INTERACTIVE_ROLES.has(role)) return true;
    // Cursor:pointer is a soft signal commonly used for custom clickables.
    try {
      const cur = win.getComputedStyle(el).cursor;
      if (cur === 'pointer' && (el.getAttribute('role') || el.hasAttribute('tabindex'))) {
        return true;
      }
    } catch (e) {}
    return false;
  }

  function isVisible(el) {
    try {
      const style = win.getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden') return false;
      if (parseFloat(style.opacity || '1') === 0) return false;
    } catch (e) {}
    const rect = el.getBoundingClientRect();
    if (!rect || rect.width <= 0 || rect.height <= 0) return false;
    if (el.hasAttribute('disabled')) {
      // Still include disabled controls (the agent may want to know about them)
      // but they remain "visible"; just don't filter on disabled here.
    }
    if (el.getAttribute('aria-hidden') === 'true') return false;
    return true;
  }

  function inViewport(el) {
    const rect = el.getBoundingClientRect();
    const vw = win.innerWidth || document.documentElement.clientWidth;
    const vh = win.innerHeight || document.documentElement.clientHeight;
    return rect.bottom > 0 && rect.right > 0 && rect.top < vh && rect.left < vw;
  }

  function bestText(el) {
    // Prefer an explicit accessible name, then visible text, then value/alt.
    let t = (el.getAttribute('aria-label') || '').trim();
    if (t) return t;
    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      const parts = labelledby.split(/\s+/)
        .map((id) => { const n = document.getElementById(id); return n ? n.textContent : ''; })
        .join(' ').trim();
      if (parts) return parts;
    }
    const tag = el.tagName.toLowerCase();
    if (tag === 'input') {
      const ty = (el.getAttribute('type') || 'text').toLowerCase();
      if (ty === 'submit' || ty === 'button' || ty === 'reset') {
        return (el.value || '').trim();
      }
    }
    t = (el.textContent || '').replace(/\s+/g, ' ').trim();
    if (t) return t;
    t = (el.getAttribute('placeholder') || el.getAttribute('title')
         || el.getAttribute('alt') || el.value || '').trim();
    return t;
  }

  const ATTR_KEYS = [
    'type', 'name', 'id', 'placeholder', 'aria-label', 'href', 'title',
    'alt', 'value', 'role', 'aria-expanded', 'aria-checked',
  ];

  function collectAttrs(el) {
    const out = {};
    for (const k of ATTR_KEYS) {
      try {
        if (el.hasAttribute(k)) {
          let v = el.getAttribute(k);
          if (v != null && v !== '') out[k] = (v.length > 200 ? v.slice(0, 200) : v);
        }
      } catch (e) {}
    }
    // Booleans surfaced as real booleans for to_text().
    try { if (el.hasAttribute('disabled')) out['disabled'] = true; } catch (e) {}
    try {
      if (el.tagName.toLowerCase() === 'input'
          && (el.type === 'checkbox' || el.type === 'radio')) {
        out['checked'] = !!el.checked;
      }
    } catch (e) {}
    return out;
  }

  // ---- walk the DOM (incl. open shadow roots) ---------------------------- //
  const results = [];
  let counter = 0;
  const queue = [document.documentElement];

  while (queue.length && counter < maxElements) {
    const node = queue.shift();
    if (!node) continue;

    let children;
    try { children = node.children ? Array.from(node.children) : []; } catch (e) { children = []; }
    for (const c of children) queue.push(c);
    try {
      if (node.shadowRoot && node.shadowRoot.children) {
        for (const c of Array.from(node.shadowRoot.children)) queue.push(c);
      }
    } catch (e) {}

    try {
      if (node.nodeType !== 1) continue;
      if (!isInteractive(node)) continue;
      if (!isVisible(node)) continue;
      if (viewportOnly && !inViewport(node)) continue;

      const index = counter++;
      node.setAttribute(INDEX_ATTR, String(index));
      results.push({
        index: index,
        tag: node.tagName.toLowerCase(),
        role: elementRole(node),
        text: bestText(node),
        attributes: collectAttrs(node),
      });
    } catch (e) { /* skip pathological node */ }
  }

  // ---- optional highlight overlay --------------------------------------- //
  if (highlight && results.length) {
    try {
      const container = document.createElement('div');
      container.id = HIGHLIGHT_CONTAINER_ID;
      container.style.cssText =
        'position:fixed;top:0;left:0;width:0;height:0;z-index:2147483647;pointer-events:none;';
      const palette = ['#FF0000', '#00AA00', '#0000FF', '#FF8800',
                       '#AA00AA', '#008888', '#888800', '#AA0044'];
      results.forEach((r) => {
        let el;
        try { el = document.querySelector('[' + INDEX_ATTR + '="' + r.index + '"]'); }
        catch (e) { el = null; }
        if (!el) return;
        const rect = el.getBoundingClientRect();
        const color = palette[r.index % palette.length];
        const box = document.createElement('div');
        box.style.cssText =
          'position:fixed;pointer-events:none;box-sizing:border-box;'
          + 'border:2px solid ' + color + ';'
          + 'top:' + rect.top + 'px;left:' + rect.left + 'px;'
          + 'width:' + rect.width + 'px;height:' + rect.height + 'px;';
        const label = document.createElement('div');
        label.textContent = String(r.index);
        label.style.cssText =
          'position:fixed;pointer-events:none;font:bold 11px monospace;'
          + 'color:#fff;background:' + color + ';padding:0 3px;line-height:14px;'
          + 'top:' + Math.max(0, rect.top - 14) + 'px;left:' + rect.left + 'px;';
        container.appendChild(box);
        container.appendChild(label);
      });
      document.body.appendChild(container);
    } catch (e) { /* overlay is best-effort */ }
  }

  return results;
}
"""
