"""Recognize and operate HUMAN press-and-hold challenges through browser input.

The mouse action is only an attempt. The engine must verify that the challenge
has disappeared before it accepts cookies or a successful HTTP status.
"""

from __future__ import annotations

import re
import time
import logging
from dataclasses import dataclass, field
from html import unescape
from typing import Any

_BUTTON = re.compile(r"^\s*press\s*(?:&|and)\s*hold\s*$", re.IGNORECASE)
_PROMPT = re.compile(
    r"(?:confirm|verify)\s+(?:that\s+)?you\s+are\s+(?:a\s+)?human",
    re.IGNORECASE,
)
_INSTRUCTION = re.compile(
    r"press\s*(?:&|and)\s*hold.*?(?:confirm|verify).*?human",
    re.IGNORECASE | re.DOTALL,
)
_WIDGET = '#px-captcha:visible, iframe[title="Human verification challenge"]:visible'
_HOLD_SECONDS = 8.0
log = logging.getLogger("wraith.press_hold")


@dataclass
class PressHoldHandler:
    """Keep attempt limits and retry timing within one navigation."""

    enabled: bool
    deadline: float
    seen: bool = field(default=False, init=False)
    attempts: int = field(default=0, init=False)
    _retry_at: float = field(default=0.0, init=False)

    def poll(self, page: Any, html: str) -> bool:
        """Attempt eligible input and report whether this poll saw a challenge."""
        active, target = inspect_press_hold(page, html)
        self.seen = self.seen or active
        if not (active and self.enabled and target is not None):
            return active
        now = time.monotonic()
        if self.attempts >= 2 or not self._retry_at <= now < self.deadline:
            return active
        self.attempts += 1
        try:
            hold_button(page, target, deadline=self.deadline)
        except Exception as exc:
            log.debug("Press-and-hold attempt failed: %s", exc)
        self._retry_at = time.monotonic() + 1.0
        return active


def is_press_hold_challenge(html: str) -> bool:
    """Recognize challenge markup without matching ordinary hold controls."""
    markup = re.sub(
        r"<(script|style|noscript)\b[^>]*>.*?</\1\s*>",
        " ",
        html or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = unescape(re.sub(r"<[^>]+>", " ", markup))
    return bool(
        re.search(r"press\s*(?:&|and)\s*hold", text, re.IGNORECASE)
        and (_PROMPT.search(text) or "px-captcha" in markup.lower())
    )


def inspect_press_hold(page: Any, html: str) -> tuple[bool, Any]:
    """Find a visible challenge and its button, including child frames.

    Playwright locators also enter open shadow roots. A visible widget with no
    accessible button remains a challenge; the engine must not claim clearance.
    """
    active_frames = []
    targets = []
    host_target = None
    inspected = False
    for frame in getattr(page, "frames", [page]):
        try:
            widget = frame.locator(_WIDGET).count() > 0
            prompt = any(
                node.is_visible() for node in frame.get_by_text(_INSTRUCTION).all()
            )
            inspected = True
            if widget or prompt:
                active_frames.append(frame)
            hosts = frame.locator("#px-captcha:visible").all()
            if host_target is None and hosts:
                # _WIDGET includes this host selector, so the host identifies
                # its own challenge frame even when another frame has a prompt.
                # Some versions expose only a plain div or a closed-shadow host.
                # Hover uses its live box, without hard-coded screen coordinates.
                host_target = hosts[0]
            candidates = frame.get_by_role("button", name=_BUTTON).all()
            candidates += frame.get_by_text(_BUTTON).all()
            for candidate in candidates:
                if candidate.is_visible():
                    targets.append((frame, candidate))
        except Exception:
            # Frames can detach while the challenge redirects the page.
            continue
    active = bool(active_frames) if inspected else is_press_hold_challenge(html)
    target = next(
        (
            candidate
            for frame, candidate in targets
            if _in_challenge_frame(frame, active_frames)
        ),
        host_target,
    )
    return active, target if active else None


def _in_challenge_frame(frame: Any, active_frames: list[Any]) -> bool:
    """Allow targets in the challenge frame or its descendants, never siblings."""
    while frame is not None:
        if any(frame is active for active in active_frames):
            return True
        frame = getattr(frame, "parent_frame", None)
    return False


def hold_button(page: Any, target: Any, *, deadline: float) -> None:
    """Attempt one hold within the caller's deadline, always releasing input."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return
    # Hover performs visibility/actionability checks and resolves iframe offsets.
    target.hover(timeout=min(1000, remaining * 1000))
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return
    try:
        page.mouse.down(button="left")
        page.wait_for_timeout(min(_HOLD_SECONDS, remaining) * 1000)
    finally:
        page.mouse.up(button="left")
