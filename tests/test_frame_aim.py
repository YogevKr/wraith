"""click_frame aims in the frame's own coordinates, also on a tilted frame."""

from __future__ import annotations

import pytest

import wraith.agent as ag


class _TiltedFrame:
    """A frame whose page->frame mapping is a perspective (projective) map."""

    def __init__(self, mouse, size=(480, 90)):
        self.mouse, self.size, self.pointer = mouse, size, None

    def to_local(self, x, y):
        # A homography: rotation, skew, and perspective foreshortening.
        w = 1 + 0.0004 * (x - 600) - 0.0002 * (y - 100)
        lx = (0.92 * (x - 600) + 0.25 * (y - 100)) / w
        ly = (-0.18 * (x - 600) + 1.1 * (y - 100)) / w
        return lx, ly

    def evaluate(self, js):
        if js == ag._POINTER_PROBE_JS:
            self.pointer = None
            return list(self.size)
        if js == ag._POINTER_RESET_JS:
            self.pointer = None
            return None
        assert js == ag._POINTER_READ_JS
        return self.pointer

    def moved(self, x, y):
        lx, ly = self.to_local(x, y)
        inside = 0 <= lx <= self.size[0] and 0 <= ly <= self.size[1]
        self.pointer = [lx, ly] if inside else None


class _Mouse:
    def __init__(self):
        self.at, self.frame, self.moves = None, None, 0

    def move(self, x, y):
        if (x, y) != self.at:  # like Firefox: no event for a move in place
            self.frame.moved(x, y)
        self.at, self.moves = (x, y), self.moves + 1


def _page_and_frame():
    mouse = _Mouse()
    frame = _TiltedFrame(mouse)
    mouse.frame = frame
    return type("P", (), {"mouse": mouse})(), frame


INSIDE = (1000.0, 200.0)  # inside the frame, but about 95 px off the button
CORNER = (1000.0, 120.0)  # a corner of the on-screen box, off the rotated frame


@pytest.mark.parametrize(
    "goal, start, center, settled",
    [
        pytest.param((440, 45), INSIDE, None, False, id="off-target"),
        pytest.param((440, 45), INSIDE, None, True, id="cursor-already-at-aim"),
        pytest.param((440, 45), CORNER, (900.0, 200.0), False, id="aim-misses-frame"),
        pytest.param((479, 89), INSIDE, None, False, id="probe-at-frame-edge"),
    ],
)
def test_refinement_lands_on_the_frame_fraction(goal, start, center, settled):
    page, frame = _page_and_frame()
    if settled:
        page.mouse.move(*start)  # human_move ended exactly here
    fraction = (goal[0] / 480, goal[1] / 90)
    x, y = ag._refine_in_frame(
        page, frame, fraction, start, center and (lambda: center)
    )
    lx, ly = frame.to_local(x, y)
    assert abs(lx - goal[0]) <= 1.5 and abs(ly - goal[1]) <= 1.5
    assert page.mouse.moves < 40


def test_refinement_keeps_the_aim_when_the_frame_cannot_be_read():
    page, frame = _page_and_frame()
    off_frame = (100.0, 900.0)
    assert frame.to_local(*CORNER)[1] < 0
    assert ag._refine_in_frame(page, frame, (0.5, 0.5), off_frame) == off_frame
    nowhere = ag._refine_in_frame(
        page, frame, (0.5, 0.5), off_frame, lambda: (0.0, 0.0)
    )
    assert nowhere == off_frame

    class _Unreadable:
        def evaluate(self, js):
            raise RuntimeError("frame detached")

    assert ag._refine_in_frame(page, _Unreadable(), (0.5, 0.5), (1.0, 2.0)) == (
        1.0,
        2.0,
    )
