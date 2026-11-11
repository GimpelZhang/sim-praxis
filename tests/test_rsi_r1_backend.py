"""RSI-R1 真物理 backend（``backend.py``）离线测试：假容器写 npz/reply，不触网。

覆盖：宿主重建 raw + 自报互证 + 落盘拒绝覆盖 + manifest 单 study 约束，
以及 ``execute_batch`` 遇基础设施失败整批 blocked、不补样本。
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import numpy as np
import pytest

from orchestrator.rsi_r1 import backend as B
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import metrics as M
from orchestrator.rsi_r1 import sandbox as SB
from orchestrator.rsi_r1 import scenes as S

PLAN = "a" * 64
IMAGE = "simpraxis/agentic:test"

A_PARAMS = {"mass_kg": "0.5", "v0_mps": "0.4",
            "sliding_friction": "0.4", "torsional_friction_m": "0.01"}
B_PARAMS = {"inertia_ratio": "1.0", "target_amplitude_rad": "0.2",
            "target_freq_hz": "1.0", "tc_s": "0.008", "dt_s": "0.001",
            "controller": "PD", "kp": "5", "kd": "0.1"}


# ---------------------------------------------------------------------------
# 假容器
# ---------------------------------------------------------------------------

class Runner:
    """按序吐出预置输出；argv 里找 /out:rw 挂载写入。"""

    def __init__(self, payloads, *, exit_code=0, timeout=False):
        self.payloads = list(payloads)
        self.argv = []
        self.removed = []
        self.exit_code = exit_code
        self.timeout = timeout

    def _out_dir(self, argv) -> Path:
        for i, arg in enumerate(argv):
            if arg == "-v" and argv[i + 1].endswith(":/out:rw"):
                return Path(argv[i + 1][: -len(":/out:rw")])
        raise AssertionError("argv 没有 /out:rw 挂载")

    def run(self, argv, *, timeout_s, container):
        assert timeout_s == SB.PHYSICS_WALL_CLOCK_S
        self.argv.append(list(argv))
        out = self._out_dir(argv)
        payload = self.payloads.pop(0)
        for name, data in payload.items():
            target = out / name
            if isinstance(data, str):
                target.write_text(data, encoding="utf-8")
            else:
                target.write_bytes(data)
        return SB.RunResult(exit_code=self.exit_code, stdout_tail="ok",
                            stderr_tail="", timed_out=self.timeout)

    def residue(self, container):
        return False

    def force_remove(self, container):
        self.removed.append(container)


def spec_a(*, block="b1", cond="base", params=None):
    return E.build_host_spec(plan_sha256=PLAN, study_id="A", condition_id=cond,
                             block_id=block, scene_id=S.SCENE_A,
                             params=dict(A_PARAMS if params is None else params))


def spec_b(*, block="b1", cond="base", params=None):
    return E.build_host_spec(plan_sha256=PLAN, study_id="B", condition_id=cond,
                             block_id=block, scene_id=S.SCENE_B,
                             params=dict(B_PARAMS if params is None else params))


def arrays_a(spec, *, n=None, x0=0.5, mu=None, tors=None):
    n = spec.nsteps if n is None else n
    dt = float(spec.dt_s)
    mu = float(spec.physical_params["sliding_friction"]) if mu is None else mu
    tors = float(spec.physical_params["torsional_friction_m"]) if tors is None else tors
    times = np.asarray([(i + 1) * dt for i in range(n)], dtype=np.float64)
    x = x0 - 0.02 * (np.arange(n) + 1) / n
    qpos = np.zeros((n, 7), dtype=np.float32)
    qpos[:, 0] = x
    qpos[:, 2] = 0.05
    qpos[:, 3] = 1.0
    qvel = np.zeros((n, 6), dtype=np.float32)
    qvel[:, 0] = 0.4
    return {
        "time_s": times, "qpos": qpos, "qvel": qvel,
        "contact_dim": np.full(n, 3, dtype=np.int8),
        "friction_sliding": np.full(n, mu, dtype=np.float32),
        "friction_torsional": np.full(n, tors, dtype=np.float32),
        "contact_force_n": np.full(n, 4.9, dtype=np.float32),
        "initial_x_m": np.asarray([x0], dtype=np.float64),
        "initial_z_m": np.asarray([0.05], dtype=np.float64),
    }


def arrays_b(spec, *, n=None, q_max=0.0):
    n = spec.nsteps if n is None else n
    dt = float(spec.dt_s)
    stride = int(round(float(spec.tc_s) / dt))
    times = np.asarray([(i + 1) * dt for i in range(n)], dtype=np.float64)
    q = np.linspace(0.0, q_max, n)
    return {
        "time_s": times,
        "qpos": q.reshape(-1, 1).astype(np.float32),
        "qvel": np.zeros((n, 1), dtype=np.float32),
        "target_q": np.zeros(n, dtype=np.float32),
        "command_torque": np.ones(n, dtype=np.float32),
        "applied_torque": np.ones(n, dtype=np.float32),
        "control_update_index": np.asarray([i // stride for i in range(n)],
                                           dtype=np.int32),
    }


def payload(spec, arrays, *, status="ok", extra=None, reply_patch=None,
            arrays_meta_patch=None):
    buf = io.BytesIO()
    np.savez_compressed(buf, **arrays)
    raw = buf.getvalue()
    if extra is None:
        extra = ({"x0_m": float(arrays["initial_x_m"][0])}
                 if spec.study_id == "A" else
                 {"saturation_count": 0, "instability_events": []})
    reply = {
        "schema": "simpraxis.rsi_r1.experiment_reply.v1",
        "schema_version": 1,
        "run_id": spec.run_id,
        "spec_hash": spec.spec_hash,
        "status": status,
        "code": None,
        "nsteps": int(len(arrays["time_s"])),
        "nsteps_requested": spec.nsteps,
        "stop_index": None,
        "solver_warning_count": 0,
        "mujoco_version": "3.15.0",
        "raw_file": "raw.npz",
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "extra": extra,
        "arrays": {k: [list(v.shape), str(v.dtype)] for k, v in arrays.items()},
    }
    if arrays_meta_patch:
        reply["arrays"].update(arrays_meta_patch)
    if reply_patch:
        reply.update(reply_patch)
    return {"raw.npz": raw,
            "reply.json": json.dumps(reply, sort_keys=True) + "\n"}


def make_backend(tmp_path, payloads, *, batch="cal", exit_code=0):
    return B.PhysicsBackend(observations_root=tmp_path / "obs",
                            evidence_root=tmp_path / "evidence",
                            work_root=tmp_path / "work",
                            runner=Runner(payloads, exit_code=exit_code),
                            image=IMAGE, batch_id=batch)


# ---------------------------------------------------------------------------
# A
# ---------------------------------------------------------------------------

def test_backend_a_happy_path_writes_raw_and_manifest(tmp_path):
    spec = spec_a()
    be = make_backend(tmp_path, [payload(spec, arrays_a(spec))])
    record = be.run(spec)
    assert record["nsteps"] == spec.nsteps
    assert record["contact_pair"] == "box_floor"
    assert set(record["contact_dim_per_step"]) == {3}
    assert record["nonfinite_count"] == 0
    base = tmp_path / "obs" / "cal"
    assert {p.name for p in base.iterdir()} >= {
        f"{spec.run_id}.jsonl", f"{spec.run_id}.npz", f"{spec.run_id}.meta.json"}
    meta = json.loads((base / f"{spec.run_id}.meta.json").read_text())
    assert meta["initial_x_m"] == 0.5
    assert meta["study_id"] == "A"
    m = M.compute_study_a(record, initial_x_m=be.metas[spec.run_id]["initial_x_m"])
    assert float(m.values[M.DISPLACEMENT]) == pytest.approx(
        float(record["qpos"][-1][0]) - 0.5, rel=1e-6)
    man = be.write_manifest()
    assert man["count"] == 1 and man["study"] == "A"
    assert man["total_steps"] == spec.nsteps


def test_backend_refuses_to_overwrite_existing_raw(tmp_path):
    spec = spec_a()
    be = make_backend(tmp_path, [payload(spec, arrays_a(spec)),
                                 payload(spec, arrays_a(spec))])
    be.run(spec)
    with pytest.raises(E.BackendInfraError) as exc:
        be.run(spec)
    assert exc.value.code == "RAW_EXISTS"


def test_backend_rejects_reply_spec_hash_mismatch(tmp_path):
    spec = spec_a()
    p = payload(spec, arrays_a(spec), reply_patch={"spec_hash": "b" * 64})
    be = make_backend(tmp_path, [p])
    with pytest.raises(E.BackendInfraError) as exc:
        be.run(spec)
    assert exc.value.code == "REPLY_SPEC_HASH"


def test_backend_rejects_array_set_mismatch(tmp_path):
    spec = spec_a()
    arrays = arrays_a(spec)
    arrays["junk"] = np.zeros(3)
    p = payload(spec, arrays)
    reply = json.loads(p["reply.json"])
    reply["arrays"].pop("junk")
    p["reply.json"] = json.dumps(reply)
    be = make_backend(tmp_path, [p])
    with pytest.raises(E.BackendInfraError) as exc:
        be.run(spec)
    assert exc.value.code == "ARRAY_SET"


def test_backend_a_rejects_non_ok_status(tmp_path):
    spec = spec_a()
    arrays = arrays_a(spec, n=spec.nsteps - 10)
    be = make_backend(tmp_path, [payload(spec, arrays, status="instability",
                                         extra={"x0_m": 0.5,
                                                "instability_events": []})])
    with pytest.raises(E.BackendInfraError) as exc:
        be.run(spec)
    assert exc.value.code == "A_STATUS"


def test_backend_a_rejects_friction_drift(tmp_path):
    spec = spec_a()
    arrays = arrays_a(spec, mu=0.3)
    be = make_backend(tmp_path, [payload(spec, arrays)])
    with pytest.raises(E.BackendInfraError) as exc:
        be.run(spec)
    assert exc.value.code == "FRICTION_MISMATCH"


def test_backend_a_rejects_x0_channel_mismatch(tmp_path):
    spec = spec_a()
    p = payload(spec, arrays_a(spec, x0=0.5), extra={"x0_m": 0.6})
    be = make_backend(tmp_path, [p])
    with pytest.raises(E.BackendInfraError) as exc:
        be.run(spec)
    assert exc.value.code == "X0_CHANNEL_MISMATCH"


def test_backend_a_rejects_step_short_on_ok(tmp_path):
    spec = spec_a()
    p = payload(spec, arrays_a(spec, n=spec.nsteps - 1))
    be = make_backend(tmp_path, [p])
    with pytest.raises(E.BackendInfraError) as exc:
        be.run(spec)
    assert exc.value.code == "STEP_SHORT"


# ---------------------------------------------------------------------------
# B
# ---------------------------------------------------------------------------

def test_backend_b_happy_path(tmp_path):
    spec = spec_b()
    be = make_backend(tmp_path, [payload(spec, arrays_b(spec))])
    record = be.run(spec)
    assert record["saturation_count"] == 0
    assert record["instability_events"] == []
    assert record["nsteps"] == spec.nsteps
    m = M.compute_study_b(record, amplitude_rad=0.2)
    assert m.details["loss_applied"] is False


def test_backend_b_rejects_self_reported_saturation_mismatch(tmp_path):
    spec = spec_b()
    p = payload(spec, arrays_b(spec),
                extra={"saturation_count": 3, "instability_events": []})
    be = make_backend(tmp_path, [p])
    with pytest.raises(E.BackendInfraError) as exc:
        be.run(spec)
    assert exc.value.code == "SATURATION_MISMATCH"


def test_backend_b_instability_is_valid_data_with_loss_cap(tmp_path):
    spec = spec_b()
    n = spec.nsteps - 100
    arrays = arrays_b(spec, n=n, q_max=4.5)
    events = [{"time_s": float(arrays["time_s"][-1]), "reason": "ABS_Q_GT_4"}]
    p = payload(spec, arrays, status="instability",
                extra={"saturation_count": 0, "instability_events": events})
    be = make_backend(tmp_path, [p])
    record = be.run(spec)
    assert record["nsteps"] == n < spec.nsteps
    assert record["instability_events"] == events
    m = M.compute_study_b(record, amplitude_rad=0.2)
    from orchestrator.rsi_r1 import rubric as R
    assert m.values[M.TRACKING_RMSE] == R.LOSS_CAP


def test_backend_b_nonfinite_event_is_recorded(tmp_path):
    spec = spec_b()
    n = spec.nsteps - 50
    arrays = arrays_b(spec, n=n)          # 记录到的样本仍全有限
    events = [{"time_s": float(arrays["time_s"][-1]), "reason": "NONFINITE"}]
    p = payload(spec, arrays, status="nonfinite",
                extra={"saturation_count": 0, "instability_events": events})
    be = make_backend(tmp_path, [p])
    record = be.run(spec)
    assert record["instability_events"] == events
    assert record["nonfinite_count"] == 0


def test_manifest_refuses_multi_study(tmp_path):
    a, b = spec_a(), spec_b()
    be = make_backend(tmp_path, [payload(a, arrays_a(a)),
                                 {"raw.npz": b"x", "reply.json": "{}"}])
    be.run(a)
    # 直接塞一条 B 的 meta，模拟同批混入两 study（manifest 必须拒绝）
    be.metas[b.run_id] = {"study_id": "B"}
    with pytest.raises(E.BackendInfraError) as exc:
        be.write_manifest()
    assert exc.value.code == "MANIFEST_MULTI_STUDY"


# ---------------------------------------------------------------------------
# execute_batch 集成（同 dispatcher / 资源账）
# ---------------------------------------------------------------------------

def test_execute_batch_blocks_on_infra_failure_without_padding(tmp_path):
    s1, s2 = spec_a(block="b1"), spec_a(block="b2")
    bad = payload(s1, arrays_a(s1), reply_patch={"schema": "wrong"})
    be = make_backend(tmp_path, [bad])
    book = E.ResourceBook(max_runs=10, max_steps=10_000_000)
    ev = E.execute_batch([s1, s2], be, book)
    assert ev.blocked is True
    assert ev.n_ok == 0
    assert len(ev.outcomes) == 1
    assert book.to_dict()["used_runs"] == 1
