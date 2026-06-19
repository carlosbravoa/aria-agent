"""
Browser humanization planners — pure-function tests (no Chrome, no network).

The CDP dispatch (_move_to/_human_click_at/...) needs a live browser, but the
geometry/timing logic lives in pure planners which we can verify offline:
easing, mouse-path generation, click-target jitter, typing cadence, scroll splits.
"""

from __future__ import annotations

import pytest

from aria.tools import browser as B


def test_ease_endpoints_and_midpoint():
    assert B._ease(0.0) == 0.0
    assert B._ease(1.0) == 1.0
    assert B._ease(0.5) == pytest.approx(0.5)
    # monotonic increasing
    vals = [B._ease(i / 20) for i in range(21)]
    assert all(b >= a for a, b in zip(vals, vals[1:]))


def test_mouse_path_starts_moves_and_lands_exactly():
    start, end = (100.0, 100.0), (400.0, 250.0)
    pts = B._mouse_path(start, end)
    assert len(pts) >= 4
    assert pts[-1] == end                      # lands exactly on target
    # stays in a bounded envelope around the straight line (jitter is small)
    for x, y in pts:
        assert 100 - 5 <= x <= 400 + 5
        assert 100 - 5 <= y <= 250 + 5


def test_mouse_path_zero_distance_is_safe():
    pts = B._mouse_path((50.0, 50.0), (50.0, 50.0))
    assert pts and pts[-1] == (50.0, 50.0)     # no div-by-zero, lands on point


def test_mouse_path_step_count_scales_with_distance():
    short = B._mouse_path((0.0, 0.0), (20.0, 0.0))
    longp = B._mouse_path((0.0, 0.0), (1000.0, 0.0))
    assert len(longp) > len(short)
    assert len(longp) <= 14                      # capped, never sluggish


def test_target_point_inside_rect():
    rect = {"x": 200.0, "y": 300.0, "width": 80.0, "height": 24.0}
    for _ in range(50):
        x, y = B._target_point(rect)
        assert 200 <= x <= 280
        assert 300 <= y <= 324


def test_target_point_centre_when_humanize_off(monkeypatch):
    monkeypatch.setattr(B, "_HUMANIZE", False)
    rect = {"x": 0.0, "y": 0.0, "width": 100.0, "height": 50.0}
    assert B._target_point(rect) == (50.0, 25.0)


def test_type_plan_length_and_bounds():
    text = "hello world, this is a test"
    plan = B._type_plan(text)
    assert len(plan) == len(text)
    assert all(d > 0 for d in plan)
    assert all(d < 0.25 for d in plan)           # base + max thinking pause


def test_scroll_plan_sums_exactly_and_splits():
    for total in (500, -500, 333, 1000):
        chunks = B._scroll_plan(total)
        assert sum(chunks) == total              # no drift
        assert 2 <= len(chunks) <= 4             # humanized split (default on)


def test_scroll_plan_single_when_humanize_off(monkeypatch):
    monkeypatch.setattr(B, "_HUMANIZE", False)
    assert B._scroll_plan(420) == [420]
