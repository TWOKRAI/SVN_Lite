"""Оценка оставшегося времени скачивания. Без tkinter."""
from __future__ import annotations

import time
from collections import deque
from typing import Callable, Optional

ETA_WINDOW_SEC = 10.0     # скорость считаем по последним ~10 секундам
ETA_MIN_ELAPSED = 5.0     # раньше 5 секунд после старта оценку не показываем


class EtaTracker:
    """Скользящая скорость по последним ETA_WINDOW_SEC секундам → сколько секунд осталось."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._start = clock()
        self._samples: deque[tuple[float, int]] = deque()

    def update(self, count: int, total: Optional[int]) -> Optional[float]:
        """Запоминает отсчёт; возвращает секунды до конца или None, если оценка ещё ненадёжна."""
        now = self._clock()
        self._samples.append((now, count))
        while len(self._samples) > 2 and now - self._samples[1][0] >= ETA_WINDOW_SEC:
            self._samples.popleft()   # оставляем один отсчёт старше окна: он задаёт начало интервала
        if not total or count >= total or now - self._start < ETA_MIN_ELAPSED:
            return None
        t0, c0 = self._samples[0]
        if now - t0 < 1.0 or count <= c0:
            return None
        rate = (count - c0) / (now - t0)
        return (total - count) / rate


def format_eta(seconds: Optional[float]) -> str:
    """«осталось ~3 мин». Пустая строка, если оценки нет."""
    if seconds is None:
        return ""
    if seconds < 45:
        return "осталось меньше минуты"
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"осталось ~{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    return f"осталось ~{hours} ч {minutes} мин" if minutes else f"осталось ~{hours} ч"
