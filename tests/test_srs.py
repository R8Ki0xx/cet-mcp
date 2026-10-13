from datetime import date

import pytest

from cet_mcp import srs

D = date(2026, 10, 13)


def test_first_correct_schedules_one_day():
    s = srs.review(srs.ReviewState(), 4, D)
    assert (s.reps, s.interval, s.due_date) == (1, 1, "2026-10-14")
    assert s.ease == pytest.approx(2.5)  # q=4 时 EF 不变
    assert not s.in_wrongbook


def test_interval_progression():
    s = srs.ReviewState()
    intervals = []
    for _ in range(4):
        s = srs.review(s, 5, D)
        intervals.append(s.interval)
    assert intervals[:2] == [1, 6]
    assert intervals[2] == round(6 * 2.8)  # q=5 每次 EF+0.1，第三次后 EF=2.8
    assert intervals[3] > intervals[2]


def test_wrong_answer_resets_and_enters_wrongbook():
    s = srs.review(srs.review(srs.ReviewState(), 4, D), 4, D)
    s = srs.review(s, 1, D)
    assert (s.reps, s.interval, s.lapses) == (0, 1, 1)
    assert s.in_wrongbook and not s.mastered
    assert s.ease == pytest.approx(2.5 - 0.54)


def test_ease_floor():
    s = srs.ReviewState(ease=1.3)
    assert srs.review(s, 0, D).ease == 1.3


def test_mastery_after_streak():
    s = srs.review(srs.ReviewState(), 1, D)
    for i in range(srs.MASTERY_STREAK):
        assert s.in_wrongbook
        s = srs.review(s, 4, D)
    assert not s.in_wrongbook and s.mastered


def test_invalid_quality():
    with pytest.raises(ValueError):
        srs.review(srs.ReviewState(), 6, D)
