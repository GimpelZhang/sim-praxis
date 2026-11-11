"""RSI-R1 T4：独立物理复跑证据（AC22/AC23 的机判面）。

全部离线：合成 raw + 合成执行身份；不启容器、不触网。
"""
from __future__ import annotations

import copy

import pytest

from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import judge as J
from orchestrator.rsi_r1 import replay as RP
from orchestrator.rsi_r1 import rubric as R
from tests import rsi_r1_helpers as H

KEEPER = "b" * 64


def _plan_a() -> object:
    doc = H.plan_doc("A")
    doc["conditions"] = [c for c in doc["conditions"] if c["id"] != "heavy"]
    return H.doc_of(doc)


def _a_record(spec: E.ExperimentSpec, *, displacement: float,
              contact_dims: "list[int] | None" = None,
              vx: float = 0.5) -> dict:
    times = [i * 0.001 for i in range(6)]
    dims = contact_dims if contact_dims is not None else [3] * len(times)
    return {
        "time_s": times,
        "qpos": [[displacement if i == len(times) - 1 else 0.0, 0.0]
                 for i in range(len(times))],
        "qvel": [[vx, 0.0] for _ in times],
        "contact_dim_per_step": list(dims),
        "run_id": spec.run_id, "spec_hash": spec.spec_hash,
        "block_id": spec.block_id,
    }


def _meta(spec: E.ExperimentSpec, *, tag: str, root: str, **over: object) -> dict:
    meta = {"run_id": spec.run_id, "spec_hash": spec.spec_hash,
            "condition_id": spec.condition_id,
            "initial_x_m": 0.0,
            "execution_id": f"exec-{tag}-{spec.run_id}",
            "container_id": f"ctr-{tag}-{spec.run_id}",
            "started_at_utc": f"2026-11-11T0{tag[-1]}:00:00Z",
            "raw_sha256": f"{tag}" * 64,
            "root": root,
            "cache_hit": False}
    meta.update(over)
    return meta


def _pair(spec: E.ExperimentSpec, *, displacement: float = 0.0,
          replay_displacement: float | None = None,
          original_root: str = "/mnt/replay_test/orig",
          replay_root: str = "/mnt/replay_test/replay",
          original_meta: "dict | None" = None,
          replay_meta: "dict | None" = None,
          original_record: "dict | None" = None,
          replay_record: "dict | None" = None) -> "tuple[dict, dict]":
    if replay_displacement is None:
        replay_displacement = displacement
    original = {"record": original_record if original_record is not None
                else _a_record(spec, displacement=displacement),
                "meta": original_meta if original_meta is not None
                else _meta(spec, tag="orig1", root=original_root)}
    replay = {"record": replay_record if replay_record is not None
              else _a_record(spec, displacement=replay_displacement),
              "meta": replay_meta if replay_meta is not None
              else _meta(spec, tag="repl2", root=replay_root)}
    return original, replay


def _one_spec():
    plan = _plan_a()
    manifest = E.allocate_confirm(plan, KEEPER)
    specs = J.build_specs(plan, manifest)
    return plan, manifest, specs


# ---------------------------------------------------------------------------
# 独立性：copy / 缓存 / 同身份 / 同根都拒
# ---------------------------------------------------------------------------

def test_clean_replay_passes_independence() -> None:
    _, _, specs = _one_spec()
    spec = specs[sorted(specs)[0]]
    original, replay = _pair(spec, displacement=0.0, replay_displacement=1e-6)
    ev = RP.verify_independence(original=original, replay=replay)
    assert ev["execution_id"].startswith("exec-repl2-")
    assert ev["record_sha256"]


def test_copied_raw_is_rejected() -> None:
    """抄=复用同一份 raw 文件（raw_sha256 相同），而不是"数值恰好复现"。"""
    _, _, specs = _one_spec()
    spec = specs[sorted(specs)[0]]
    original, replay = _pair(spec, displacement=0.0, replay_displacement=1e-6)
    replay["meta"]["raw_sha256"] = original["meta"]["raw_sha256"]
    with pytest.raises(RP.ReplayError) as exc:
        RP.verify_independence(original=original, replay=replay)
    assert exc.value.code == "COPIED_RAW"


def test_identical_record_is_flagged_not_rejected() -> None:
    """确定性物理逐位复现是合法的；只作事实标记给 reviewer。"""
    _, _, specs = _one_spec()
    spec = specs[sorted(specs)[0]]
    original, replay = _pair(spec, displacement=0.0)
    ev = RP.verify_independence(original=original, replay=replay)
    assert ev["record_identical"] is True


def test_cache_hit_is_rejected() -> None:
    _, _, specs = _one_spec()
    spec = specs[sorted(specs)[0]]
    original, replay = _pair(spec, replay_displacement=1e-6)
    replay["meta"]["cache_hit"] = "raw"
    with pytest.raises(RP.ReplayError) as exc:
        RP.verify_independence(original=original, replay=replay)
    assert exc.value.code == "REPLAY_CACHE_HIT"


def test_same_execution_identity_is_rejected() -> None:
    _, _, specs = _one_spec()
    spec = specs[sorted(specs)[0]]
    original, replay = _pair(spec, replay_displacement=1e-6)
    replay["meta"]["execution_id"] = original["meta"]["execution_id"]
    with pytest.raises(RP.ReplayError) as exc:
        RP.verify_independence(original=original, replay=replay)
    assert exc.value.code == "SAME_EXECUTION"


def test_nested_or_shared_root_is_rejected() -> None:
    _, _, specs = _one_spec()
    spec = specs[sorted(specs)[0]]
    original, replay = _pair(spec, replay_displacement=1e-6,
                             replay_root="/mnt/replay_test/orig/child")
    with pytest.raises(RP.ReplayError) as exc:
        RP.verify_independence(original=original, replay=replay)
    assert exc.value.code == "ROOT_NOT_INDEPENDENT"


def test_missing_execution_identity_fails_closed() -> None:
    _, _, specs = _one_spec()
    spec = specs[sorted(specs)[0]]
    original, replay = _pair(spec, replay_displacement=1e-6)
    del replay["meta"]["container_id"]
    with pytest.raises(RP.ReplayError) as exc:
        RP.verify_independence(original=original, replay=replay)
    assert exc.value.code == "EXECUTION_IDENTITY_MISSING"


# ---------------------------------------------------------------------------
# 数值容差与暴露类别
# ---------------------------------------------------------------------------

def test_numeric_tolerance_is_abs_plus_rel() -> None:
    plan, manifest, specs = _one_spec()
    spec = specs[sorted(specs)[0]]
    tol_abs, tol_rel = R.replay_tolerance("displacement_x_m")
    orig = 0.05
    allowed = float(tol_abs) + float(tol_rel) * abs(orig)
    ok_original, ok_replay = _pair(spec, displacement=orig,
                                   replay_displacement=orig + allowed * 0.5)
    cmp = RP.compare_run(study_id="A", spec=spec, original=ok_original,
                         replay=ok_replay)
    assert cmp["ok"] is True, cmp["problems"]
    bad_original, bad_replay = _pair(spec, displacement=orig,
                                     replay_displacement=orig + allowed * 2.0)
    cmp2 = RP.compare_run(study_id="A", spec=spec, original=bad_original,
                          replay=bad_replay)
    assert cmp2["ok"] is False
    assert any(p.startswith("NUMERIC_DRIFT") for p in cmp2["problems"])


def test_exposure_class_change_is_rejected() -> None:
    _, _, specs = _one_spec()
    spec = specs[sorted(specs)[0]]
    original, replay = _pair(spec, replay_displacement=1e-6)
    replay["record"]["contact_dim_per_step"] = [3, 4, 3, 3, 3, 3]
    cmp = RP.compare_run(study_id="A", spec=spec, original=original, replay=replay)
    assert cmp["ok"] is False
    assert "EXPOSURE_CLASS_MISMATCH" in cmp["problems"]
    assert cmp["exposure"]["ok"] is False


def _slow_record(spec: E.ExperimentSpec, *, vx: float, displacement: float = 0.0) -> dict:
    """dt=0.02 s、21 步：够 0.2 s 保持窗，能真正判"停/不停"。"""
    times = [i * 0.02 for i in range(21)]
    return {
        "time_s": times,
        "qpos": [[displacement if i == len(times) - 1 else 0.0, 0.0]
                 for i in range(len(times))],
        "qvel": [[vx, 0.0] for _ in times],
        "contact_dim_per_step": [3] * len(times),
        "run_id": spec.run_id, "spec_hash": spec.spec_hash,
        "block_id": spec.block_id,
    }


def test_censoring_change_is_rejected() -> None:
    _, _, specs = _one_spec()
    spec = specs[sorted(specs)[0]]
    original, replay = _pair(spec, replay_displacement=1e-6)
    original["record"] = _slow_record(spec, vx=0.0)                  # 立刻停
    replay["record"] = _slow_record(spec, vx=0.5, displacement=1e-6)  # 4 s 不停
    cmp = RP.compare_run(study_id="A", spec=spec, original=original, replay=replay)
    assert cmp["ok"] is False
    assert any(p.startswith("CENSORING_MISMATCH") for p in cmp["problems"])
    assert cmp["exposure"]["ok"] is False


# ---------------------------------------------------------------------------
# 整 study
# ---------------------------------------------------------------------------

def _full_study(plan, manifest, specs, *, drift_run: "str | None" = None) -> dict:
    originals, replays = {}, {}
    for run_id, spec in specs.items():
        original, replay = _pair(
            spec,
            displacement=-0.01 if spec.condition_id == "low_mu" else 0.0,
            replay_displacement=(-0.01 if spec.condition_id == "low_mu" else 0.0)
            + (1e-3 if run_id == drift_run else 0.0))
        originals[run_id] = original
        replays[run_id] = replay
    return RP.compare_study(plan=plan, specs=specs,
                            originals=originals, replays=replays)


def test_full_study_replay_is_ok() -> None:
    plan, manifest, specs = _one_spec()
    out = _full_study(plan, manifest, specs)
    assert out["ok"] is True, out["problems"][:3]
    assert out["n_expected"] == out["n_replayed"] == len(specs)
    assert all(r["independence"] for r in out["runs"])


def test_single_drifting_run_fails_study() -> None:
    plan, manifest, specs = _one_spec()
    victim = sorted(specs)[0]
    out = _full_study(plan, manifest, specs, drift_run=victim)
    assert out["ok"] is False
    assert any(victim in p for p in out["problems"])


def test_unreplayed_run_fails_study() -> None:
    plan, manifest, specs = _one_spec()
    originals, replays = {}, {}
    for run_id, spec in specs.items():
        original, replay = _pair(spec, replay_displacement=1e-6)
        originals[run_id] = original
        if run_id != sorted(specs)[0]:
            replays[run_id] = replay
    out = RP.compare_study(plan=plan, specs=specs,
                           originals=originals, replays=replays)
    assert out["ok"] is False
    assert any(p.startswith("NOT_REPLAYED") for p in out["problems"])


# ---------------------------------------------------------------------------
# 判决稳定性
# ---------------------------------------------------------------------------

def _report(status: str, *, n: int = 32) -> dict:
    return {"study_id": "A", "layers": {"scientific": [
        {"id": "primary", "status": status, "n_blocks": n},
        {"id": "negative", "status": "SUPPORTED", "n_blocks": n},
    ]}}


def test_identical_judgement_is_stable() -> None:
    out = RP.judge_disagreement(_report("SUPPORTED"), _report("SUPPORTED"))
    assert out["ok"] is True, out["problems"]
    assert [c["contrast_id"] for c in out["contrasts"]] == ["negative", "primary"]


def test_flipped_judgement_is_replay_disagreement() -> None:
    out = RP.judge_disagreement(_report("SUPPORTED"), _report("INCONCLUSIVE"))
    assert out["ok"] is False
    assert any(p.startswith("REPLAY_DISAGREEMENT:primary")
               for p in out["problems"])


def test_denominator_change_is_rejected() -> None:
    out = RP.judge_disagreement(_report("SUPPORTED"), _report("SUPPORTED", n=31))
    assert out["ok"] is False
    assert any(p.startswith("DENOMINATOR_CHANGED") for p in out["problems"])
