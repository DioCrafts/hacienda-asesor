from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass
class StageLatency:
    stage: str
    ms: float


class MetricsCollector:
    def __init__(self) -> None:
        self.stage_latencies: list[StageLatency] = []
        self.uncertainty_events = 0
        self.total_turns = 0
        self.human_handoff_events = 0
        self.module_errors: dict[str, int] = {}

    @contextmanager
    def timed_stage(self, stage: str):
        start = time.perf_counter()
        try:
            yield
        except Exception:
            self.module_errors[stage] = self.module_errors.get(stage, 0) + 1
            raise
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            self.stage_latencies.append(StageLatency(stage=stage, ms=elapsed_ms))

    def track_turn(self, uncertainty_count: int, human_handoff: bool) -> None:
        self.total_turns += 1
        if uncertainty_count > 0:
            self.uncertainty_events += 1
        if human_handoff:
            self.human_handoff_events += 1

    def snapshot(self) -> dict:
        uncertainty_rate = self.uncertainty_events / self.total_turns if self.total_turns else 0.0
        handoff_rate = self.human_handoff_events / self.total_turns if self.total_turns else 0.0
        return {
            "latency_by_stage_ms": [vars(item) for item in self.stage_latencies],
            "uncertainty_rate": round(uncertainty_rate, 4),
            "human_handoff_rate": round(handoff_rate, 4),
            "errors_by_module": self.module_errors,
            "total_turns": self.total_turns,
        }


def emit_structured_log(event: str, payload: dict) -> None:
    logging.info(json.dumps({"event": event, **payload}, ensure_ascii=False))
