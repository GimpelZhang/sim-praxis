"""RSI-R1.3 T5：并行批执行（维护专用）与执行语义（零物理）。"""

from __future__ import annotations

import threading
import time

from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import task_specs as TS


class _FakeBackend:
    def __init__(self):
        self.seen = []
        self.lock = threading.Lock()

    def run(self, spec):
        time.sleep(0.01)
        with self.lock:
            self.seen.append(spec.run_id)
        return {"run_id": spec.run_id, "time_s": [0.0, 1.0],
                "qpos": [0.0, 0.1], "qvel": [0.0, 0.0]}


def _specs(n=6):
    task = TS.build_new_a()
    out = []
    for i in range(n):
        out.append(E.build_host_spec(
            plan_sha256="a" * 64, study_id="A",
            condition_id=task.conditions[0].id, block_id=f"b{i + 1:02d}",
            scene_id=task.conditions[0].scene_id,
            params=dict(task.conditions[0].params)))
    return out


def test_parallel_batch_preserves_order_and_counts():
    specs = _specs(6)
    backend = _FakeBackend()
    book = E.ResourceBook(max_runs=6, max_steps=10**9)
    ev = E.execute_batch(specs, backend, book, workers=4)
    assert not ev.blocked
    assert ev.n_ok == 6
    assert [o.run_id for o in ev.outcomes] == [s.run_id for s in specs]
    assert book.used_runs == 6


def test_parallel_batch_infra_failure_blocks_without_refill():
    class Bad(_FakeBackend):
        def run(self, spec):
            if spec.block_id == "b03":
                raise E.BackendInfraError("BOOM")
            return super().run(spec)

    specs = _specs(6)
    ev = E.execute_batch(specs, Bad(), E.ResourceBook(6, 10**9), workers=3)
    assert ev.blocked
    assert any(o.error_code == "BOOM" for o in ev.outcomes)
    assert ev.n_ok < 6


def test_sequential_default_unchanged():
    specs = _specs(3)
    backend = _FakeBackend()
    ev = E.execute_batch(specs, backend, E.ResourceBook(3, 10**9))
    assert ev.n_ok == 3 and not ev.blocked
