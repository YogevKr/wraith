"""Human-like behavior helpers for Wraith.

WHY THIS EXISTS (reCAPTCHA-v3 reputation, empirically verified vs. EL AL's
Reblaze/Link11 + reCAPTCHA-v3 + Akamai + SiteMinder stack):

    reCAPTCHA-v3 scores a session's *reputation* on [0.0, 1.0] from a blend
    of account history, IP, and on-page interaction signals. Two behavioral
    facts fell out of testing:

      * Synthetic mouse-movement entropy HELPS the score. A page with zero
        pointer movement and an instant, atomic credential fill reads as a
        script; a few jittered moves and human-paced keystrokes read as a
        person and nudge the score up.

      * But do NOT over-inject. Obviously-synthetic, perfectly-periodic
        events (straight-line moves, fixed inter-key delays, teleporting the
        cursor) are themselves a tell. The goal is *plausible* entropy, not a
        firehose of fake events.

    These helpers add just enough human texture. They are the supporting act,
    not the headliner — the real win against reputation defenses is borrowing
    a warmed identity (see wraith.identity); behavior tuning only moves the
    needle at the margins and matters most on flows you can't pre-authenticate.

All functions are duck-typed against Playwright's sync API (Page / Locator)
so this module imports without Playwright/Camoufox installed.
"""

from __future__ import annotations

import math
import random
import time
from typing import Any

__all__ = ["human_move", "human_type", "human_drag", "drag_path", "dwell"]


# A person holds a mouse button or a key down for tens of milliseconds;
# automation releases it in under one, which input-timing sensors record.
_CLICK_HOLD_MS = (45.0, 110.0)
_KEY_HOLD_MS = (35.0, 95.0)


def key_gap_seconds() -> float:
    """Seconds between two keystrokes of a quick typist.

    Short enough for a ``<select>`` list's type-ahead, which forgets the typed
    prefix after about one second without a key.
    """
    return random.uniform(0.03, 0.09)


def click_hold_ms() -> float:
    """Milliseconds to hold the mouse button down during one click."""
    return random.uniform(*_CLICK_HOLD_MS)


def key_hold_ms() -> float:
    """Milliseconds to hold a key down during one keystroke."""
    return random.uniform(*_KEY_HOLD_MS)


def _ease_in_out(t: float) -> float:
    """Cubic ease-in-out; cursors accelerate then decelerate, not linear."""
    return 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2


def human_move(
    page: Any,
    steps: int = 24,
    *,
    target: tuple[float, float] | None = None,
    start: tuple[float, float] | None = None,
    jitter: float = 1.5,
) -> tuple[float, float]:
    """Move the mouse along a curved, eased, jittered path.

    Real pointers travel in arcs with variable speed and small tremor, never
    in a single straight teleport. We build a quadratic Bezier between ``start``
    and ``target`` (random points within the viewport if unspecified), walk it
    over ``steps`` with an ease-in-out velocity profile, and add a little
    per-step ``jitter`` so the line isn't geometrically perfect.

    Returns the final (x, y). ``steps`` controls how many discrete
    ``mouse.move`` events are emitted — more steps == smoother but slower.
    """
    width, height = _viewport_size(page)

    if start is None:
        start = (random.uniform(0, width), random.uniform(0, height))
    if target is None:
        target = (random.uniform(0, width), random.uniform(0, height))

    sx, sy = start
    tx, ty = target

    # Control point offset to the side of the straight line -> an arc.
    mx, my = (sx + tx) / 2, (sy + ty) / 2
    bow = random.uniform(-1, 1) * (abs(tx - sx) + abs(ty - sy)) * 0.15
    cx, cy = mx + bow, my - bow

    steps = max(2, int(steps))
    last = (sx, sy)
    for i in range(1, steps + 1):
        t = _ease_in_out(i / steps)
        # Quadratic Bezier.
        u = 1 - t
        x = u * u * sx + 2 * u * t * cx + t * t * tx
        y = u * u * sy + 2 * u * t * cy + t * t * ty
        if i < steps:  # don't jitter the final landing point
            x += random.uniform(-jitter, jitter)
            y += random.uniform(-jitter, jitter)
        x = _clamp(x, 0, width)
        y = _clamp(y, 0, height)
        try:
            page.mouse.move(x, y, steps=1)
        except TypeError:
            page.mouse.move(x, y)
        last = (x, y)
        # Sub-perceptual pauses; humans don't emit moves at a fixed rate.
        time.sleep(random.uniform(0.004, 0.018))
    return last


def drag_path(
    start: tuple[float, float], end: tuple[float, float]
) -> list[tuple[float, float, float]]:
    """Points ``(x, y, wait_ms)`` for a human drag from ``start`` to ``end``.

    Slider challenges score the trajectory, so it copies what a hand does:
    a fast start that slows down near the target (ease-out), a small vertical
    wobble, uneven event timing, and, on longer drags, a short overshoot that
    is corrected before release. The last point is exactly ``end``.
    """
    sx, sy = start
    ex, ey = end
    dx, dy = ex - sx, ey - sy
    distance = math.hypot(dx, dy)
    count = int(_clamp(distance / 5, 18, 90))
    overshoot = random.uniform(0.02, 0.06) if distance > 60 else 0.0
    wobble = random.uniform(0.6, 2.4) * random.choice((-1, 1))
    bend = random.uniform(0.6, 1.6)
    total_ms = random.uniform(420, 750) + distance * random.uniform(1.1, 1.9)
    points: list[tuple[float, float, float]] = []
    for i in range(1, count + 1):
        t = i / count
        progress = 1 - (1 - t) ** 3  # ease-out: fast start, slow finish
        if overshoot:
            # Late in the drag, pass the target by a few percent, then settle.
            late = _clamp((t - 0.55) / 0.45, 0.0, 1.0)
            progress += overshoot * math.sin(math.pi * late)
        x = sx + dx * progress
        y = sy + dy * progress + wobble * math.sin(math.pi * t * bend)
        if i < count:
            x += random.gauss(0, 0.35)
            y += random.gauss(0, 0.35)
        points.append((x, y, total_ms / count * random.uniform(0.6, 1.4)))
    points.append((ex, ey, random.uniform(40, 90)))
    return points


def human_drag(page: Any, start: tuple[float, float], end: tuple[float, float]) -> None:
    """Press at ``start``, move along :func:`drag_path` to ``end``, release.

    The cursor first travels to ``start`` (:func:`human_move`), rests, and
    holds the button a moment before moving, as a person does on a slider.
    The button is always released.
    """
    human_move(page, target=start)
    page.wait_for_timeout(random.uniform(90, 260))
    page.mouse.down()
    try:
        page.wait_for_timeout(random.uniform(60, 160))
        for x, y, wait_ms in drag_path(start, end):
            page.mouse.move(x, y)
            page.wait_for_timeout(wait_ms)
        page.wait_for_timeout(random.uniform(60, 180))
    finally:
        page.mouse.up()


def human_type(
    locator: Any,
    text: str,
    *,
    click: bool = True,
    delay: float = 0.11,
    jitter: float = 0.06,
    mistake_rate: float = 0.0,
) -> None:
    """Type ``text`` into ``locator`` one key at a time with jittered cadence.

    Instant ``locator.fill(...)`` of credentials is the single most robotic
    thing you can do — it sets the value atomically with no keydown/keyup
    rhythm. We press keys individually with a per-key delay of roughly
    ``delay`` seconds, varied by +/-``jitter`` so the cadence isn't a metronome.
    Slightly longer natural pauses are inserted after spaces/punctuation.

    ``mistake_rate`` (0..1, default 0) optionally injects a wrong character
    followed by a Backspace correction, the most human signal of all. Keep it
    low; over-injecting is itself detectable.
    """
    if click:
        locator.click(delay=click_hold_ms())
    for ch in text:
        if mistake_rate and random.random() < mistake_rate:
            wrong = random.choice("abcdefghijklmnopqrstuvwxyz")
            _press_char(locator, wrong)
            time.sleep(_key_delay(delay, jitter))
            _press_key(locator, "Backspace")
            time.sleep(_key_delay(delay, jitter))
        _press_char(locator, ch)
        d = _key_delay(delay, jitter)
        if ch in " \t.,;:!?\n":
            d += random.uniform(0.04, 0.16)  # humans pause at word boundaries
        time.sleep(d)


def dwell(min_seconds: float = 0.4, max_seconds: float = 1.8) -> float:
    """Sleep a random "thinking" pause and return the duration slept.

    Use between discrete actions (after a page settles, before clicking
    submit) so the session has human-scale gaps instead of firing actions
    back-to-back in the same millisecond.
    """
    if max_seconds < min_seconds:
        min_seconds, max_seconds = max_seconds, min_seconds
    d = random.uniform(min_seconds, max_seconds)
    time.sleep(d)
    return d


# --------------------------------------------------------------------------- #
# Internal helpers (duck-typed against Playwright)
# --------------------------------------------------------------------------- #

def _key_delay(delay: float, jitter: float) -> float:
    return max(0.0, random.uniform(delay - jitter, delay + jitter))


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _viewport_size(page: Any) -> tuple[float, float]:
    """Best-effort viewport size; fall back to a common desktop size.

    Note Wraith runs with no_viewport=True on the Chromium path (the default
    1280x720 viewport is itself a bot tell), so we probe the real window via
    the DOM when possible.
    """
    try:
        vp = page.viewport_size
        if vp:
            return float(vp["width"]), float(vp["height"])
    except Exception:
        pass
    try:
        dims = page.evaluate("() => [window.innerWidth, window.innerHeight]")
        if dims and dims[0] and dims[1]:
            return float(dims[0]), float(dims[1])
    except Exception:
        pass
    return 1366.0, 768.0


def _press_char(locator: Any, ch: str) -> None:
    """Insert a single character with realistic key events.

    ``press`` holds the key for :func:`key_hold_ms` between keydown and keyup.
    A character outside the keyboard layout (an accent, another script) has
    no key to press; ``press_sequentially`` / ``type`` insert it instead.
    """
    try:
        locator.press(ch, delay=key_hold_ms())
        return
    except Exception as exc:
        if "Unknown key" not in str(exc):
            raise
    try:
        locator.press_sequentially(ch, delay=0)
        return
    except AttributeError:
        pass
    try:
        locator.type(ch, delay=0)  # older Playwright
        return
    except AttributeError:
        pass
    locator.press(ch)


def _press_key(locator: Any, key: str) -> None:
    locator.press(key)
