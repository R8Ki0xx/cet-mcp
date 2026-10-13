"""SM-2 间隔重复算法（纯函数，无数据库依赖）。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta

# 错题自上次出错以来连续答对多少次视为"已掌握"
MASTERY_STREAK = 3

# 客观题的质量分映射
QUALITY_CORRECT = 4
QUALITY_WRONG = 1


@dataclass(frozen=True)
class ReviewState:
    ease: float = 2.5
    interval: int = 0
    reps: int = 0
    lapses: int = 0
    due_date: str | None = None  # ISO 日期
    in_wrongbook: bool = False
    mastered: bool = False


def next_ease(ease: float, quality: int) -> float:
    q = quality
    return max(1.3, ease + 0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))


def review(state: ReviewState, quality: int, today: date | None = None) -> ReviewState:
    """根据本次作答质量（0-5）计算新的复习状态。"""
    if not 0 <= quality <= 5:
        raise ValueError("quality 必须在 0-5 之间")
    today = today or date.today()
    ease = next_ease(state.ease, quality)

    if quality < 3:
        return replace(
            state,
            ease=ease,
            reps=0,
            interval=1,
            lapses=state.lapses + 1,
            due_date=(today + timedelta(days=1)).isoformat(),
            in_wrongbook=True,
            mastered=False,
        )

    reps = state.reps + 1
    if reps == 1:
        interval = 1
    elif reps == 2:
        interval = 6
    else:
        interval = max(1, round(state.interval * ease))

    in_wrongbook = state.in_wrongbook
    mastered = state.mastered
    if in_wrongbook and reps >= MASTERY_STREAK:
        in_wrongbook = False
        mastered = True

    return replace(
        state,
        ease=ease,
        reps=reps,
        interval=interval,
        due_date=(today + timedelta(days=interval)).isoformat(),
        in_wrongbook=in_wrongbook,
        mastered=mastered,
    )
