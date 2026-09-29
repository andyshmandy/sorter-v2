import time
import threading
from dataclasses import dataclass
from typing import Optional


@dataclass
class DurationStat:
    count: int = 0
    total_ms: float = 0.0
    min_ms: float = float("inf")
    max_ms: float = 0.0
    last_ms: float = 0.0


@dataclass
class CounterStat:
    count: int = 0


@dataclass
class IntervalStat:
    count: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    last_ms: float = 0.0


class _TimerContext:
    def __init__(self, profiler: "Profiler", name: str):
        self.profiler = profiler
        self.name = name
        self.start: Optional[float] = None

    def __enter__(self):
        self.start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.start is None:
            return
        elapsed_ms = (time.perf_counter() - self.start) * 1000
        self.profiler.observeDuration(self.name, elapsed_ms)


class Profiler:
    def __init__(self, enabled: bool):
        self.enabled = enabled

        self._lock = threading.Lock()
        self._durations: dict[str, DurationStat] = {}
        self._counters: dict[str, CounterStat] = {}
        self._intervals: dict[str, IntervalStat] = {}
        self._last_mark_s: dict[str, float] = {}
        self._state_start_s: dict[str, float] = {}
        self._state_name: dict[str, str] = {}

    def timer(self, name: str) -> _TimerContext:
        return _TimerContext(self, name)

    def observeDuration(self, name: str, elapsed_ms: float) -> None:
        if not self.enabled:
            return
        with self._lock:
            stat = self._durations.get(name)
            if stat is None:
                stat = DurationStat()
                self._durations[name] = stat
            stat.count += 1
            stat.total_ms += elapsed_ms
            stat.last_ms = elapsed_ms
            if elapsed_ms < stat.min_ms:
                stat.min_ms = elapsed_ms
            if elapsed_ms > stat.max_ms:
                stat.max_ms = elapsed_ms

    def hit(self, name: str, count: int = 1) -> None:
        if not self.enabled:
            return
        with self._lock:
            stat = self._counters.get(name)
            if stat is None:
                stat = CounterStat()
                self._counters[name] = stat
            stat.count += count

    def mark(self, name: str) -> None:
        if not self.enabled:
            return
        now_s = time.perf_counter()
        with self._lock:
            prev_s = self._last_mark_s.get(name)
            self._last_mark_s[name] = now_s
            if prev_s is None:
                return
            interval_ms = (now_s - prev_s) * 1000
            stat = self._intervals.get(name)
            if stat is None:
                stat = IntervalStat()
                self._intervals[name] = stat
            stat.count += 1
            stat.total_ms += interval_ms
            stat.last_ms = interval_ms
            if interval_ms > stat.max_ms:
                stat.max_ms = interval_ms

    def enterState(self, group: str, state: str) -> None:
        if not self.enabled:
            return
        now_s = time.perf_counter()
        with self._lock:
            prev_state = self._state_name.get(group)
            prev_start = self._state_start_s.get(group)
            if prev_state is not None and prev_start is not None:
                elapsed_ms = (now_s - prev_start) * 1000
                self._addDurationUnlocked(
                    f"state_duration_ms.{group}.{prev_state}", elapsed_ms
                )
            self._state_name[group] = state
            self._state_start_s[group] = now_s
            self._addCounterUnlocked(f"state_entry_count.{group}.{state}", 1)
            if prev_state is not None and prev_state != state:
                self._addCounterUnlocked(
                    f"state_transition_count.{group}.{prev_state}->{state}", 1
                )

    def exitState(self, group: str) -> None:
        if not self.enabled:
            return
        now_s = time.perf_counter()
        with self._lock:
            prev_state = self._state_name.get(group)
            prev_start = self._state_start_s.get(group)
            if prev_state is None or prev_start is None:
                return
            elapsed_ms = (now_s - prev_start) * 1000
            self._addDurationUnlocked(
                f"state_duration_ms.{group}.{prev_state}", elapsed_ms
            )
            del self._state_name[group]
            del self._state_start_s[group]

    def _addDurationUnlocked(self, name: str, elapsed_ms: float) -> None:
        stat = self._durations.get(name)
        if stat is None:
            stat = DurationStat()
            self._durations[name] = stat
        stat.count += 1
        stat.total_ms += elapsed_ms
        stat.last_ms = elapsed_ms
        if elapsed_ms < stat.min_ms:
            stat.min_ms = elapsed_ms
        if elapsed_ms > stat.max_ms:
            stat.max_ms = elapsed_ms

    def _addCounterUnlocked(self, name: str, count: int) -> None:
        stat = self._counters.get(name)
        if stat is None:
            stat = CounterStat()
            self._counters[name] = stat
        stat.count += count
