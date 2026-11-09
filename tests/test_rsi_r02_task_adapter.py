"""RSI-R0.2 T1 资产适配与接口合同测试（AC06–AC09 的可在本地复现部分）。

全部在 tmp 根下跑：合成来源 campaign，不读 /mnt、不触网、不调模型。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from orchestrator.rsi_r01 import observations as OBS
from orchestrator.rsi_r01 import schemas as S
from orchestrator.rsi_r01 import task_contract as TC
from orchestrator.rsi_r02 import campaign as camp
from orchestrator.rsi_r02 import task_adapter as TA

ALL_IDS = (*TC.DEV_IDS, *TC.VALIDATION_IDS)


@pytest.fixture(autouse=True)
def _roots(tmp_path, monkeypatch):
    for name in ("PHASE1", "PHASE2", "PHASE25", "PHASE26", "PHASE27", "PHASE281"):
        monkeypatch.setenv(f"SIMPRAXIS_{name}_ROOT", str(tmp_path / name.lower()))
    monkeypatch.setenv("SIMPRAXIS_RSI_R0_ROOT", str(tmp_path / "rsi_r0"))
    monkeypatch.setenv("SIMPRAXIS_RSI_R01_ROOT", str(tmp_path / "rsi_r01"))
    monkeypatch.setenv("SIMPRAXIS_RSI_R02_ROOT", str(tmp_path / "rsi_r02"))
    monkeypatch.setenv("SIMPRAXIS_RSI_R02_CAMPAIGN", "r02_T1")
    yield


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    env = dict(os.environ,
               GIT_AUTHOR_NAME="Junchuan Zhang",
               GIT_AUTHOR_EMAIL="zjunchuan@gmail.com",
               GIT_COMMITTER_NAME="Junchuan Zhang",
               GIT_COMMITTER_EMAIL="zjunchuan@gmail.com")
    subprocess.run(["git", "init", "-q"], cwd=r, check=True, env=env)
    (r / "x.txt").write_text("x\n")
    subprocess.run(["git", "add", "x.txt"], cwd=r, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=r, check=True, env=env)
    return r


@pytest.fixture
def ctx(repo, tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("# plan\n", encoding="utf-8")
    consent = tmp_path / "consent.txt"
    consent.write_text("authorized\n", encoding="utf-8")
    return camp.create_campaign(campaign_id="r02_T1", plan_path=plan,
                                consent_path=consent, repo_root=repo,
                                root=tmp_path / "rsi_r02")


def _public_doc(inst: str) -> dict:
    family = inst[0]
    kind = "pendulum" if family == "R" else "push"
    return {
        "schema": "simpraxis.task_pack.v1",
        "instance_id": inst,
        "family": family,
        "public": {
            "interface": {"kind": kind, "keys": ["gains"], "steps_max": 40,
                          "limits": {"gains": "0.0..1.0"}},
            "goal": "把系统修到目标行为" if family != "R" else "验证公开假设 h",
            "research": ({"band": [0.1, 0.5], "h": 1} if family == "R" else None),
        },
    }


def _private_doc(inst: str) -> dict:
    family = inst[0]
    if family == "R":
        return {"instance_id": inst, "family": family, "template": "pendulum",
                "role": "dev" if "-dev" in inst else "validation",
                "settle_band": {"theta": 0.02}, "expected_mechanism": "damping",
                "cells": [{"damping": 0.15}, {"damping": 0.45}]}
    return {"instance_id": inst, "family": family, "template": "push",
            "role": "dev" if "-dev" in inst else "validation",
            "tolerance": {"distance_m": 0.05},
            "reference_gains": [0.4, 0.6],
            "fault": {"kind": "steady_offset", "delta": 0.05}}


def _make_source(tmp_path, *, leak: "tuple[str, str] | None" = None,
                 drop: "str | None" = None) -> Path:
    src = tmp_path / "src"
    for inst in ALL_IDS:
        if inst == drop:
            continue
        pub = _public_doc(inst)
        if leak is not None and leak[0] == inst:
            pub["public"][leak[1]] = "leaked"
        pub_dir = src / "public" / "tasks" / inst
        pub_dir.mkdir(parents=True)
        (pub_dir / "task.json").write_text(
            json.dumps(pub, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        priv_dir = src / "private" / "instances"
        priv_dir.mkdir(parents=True, exist_ok=True)
        (priv_dir / f"{inst}.json").write_text(
            json.dumps(_private_doc(inst), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    (src / "private" / "task_matrix.json").write_text(
        json.dumps({"schema": "simpraxis.task_matrix.v1", "n": 9}) + "\n",
        encoding="utf-8")
    return src


def _tree_hashes(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


# ---- AC06：逐项绑定 hash（复制后读回比对） ----


def test_adapt_copies_byte_exact_and_binds_hashes(ctx, tmp_path):
    src = _make_source(tmp_path)
    before = _tree_hashes(src)
    out = TA.adapt(ctx=ctx, source_campaign=src)
    assert out["instances"] == 9
    # 来源只读
    assert _tree_hashes(src) == before
    # 公开 9 件 + 私有 9 件 + matrix
    assert len(out["copied"]["public"]) == 9
    assert len(out["copied"]["private"]) == 10
    for rec in out["copied"]["public"] + out["copied"]["private"]:
        copied = ctx.root / rec["path"]
        assert copied.is_file()
        assert copied.stat().st_mode & 0o777 == 0o600
        assert hashlib.sha256(copied.read_bytes()).hexdigest() == rec["sha256"]
    contract = json.loads((ctx.root / "contract" / "task_contract.json").read_text())
    assert contract["schema"] == TA.TASK_CONTRACT_SCHEMA
    assert [i["instance_id"] for i in contract["instances"]] == list(ALL_IDS)
    for inst in contract["instances"]:
        src_pub = src / inst["hashes"]["public_relpath"]
        assert inst["hashes"]["public"] == hashlib.sha256(src_pub.read_bytes()).hexdigest()


def test_missing_source_instance_rejected(ctx, tmp_path):
    src = _make_source(tmp_path, drop="D-val-2")
    with pytest.raises(TA.AdapterError):
        TA.adapt(ctx=ctx, source_campaign=src)


def test_missing_source_root_rejected(ctx, tmp_path):
    with pytest.raises(TA.AdapterError):
        TA.adapt(ctx=ctx, source_campaign=tmp_path / "nope")


# ---- AC07：公私隔离（公开件不得含私有真值键） ----


@pytest.mark.parametrize("key", ["fault", "tolerance", "reference_gains"])
def test_public_leak_rejected(ctx, tmp_path, key):
    src = _make_source(tmp_path, leak=("D-dev", key))
    with pytest.raises(TA.AdapterError) as ei:
        TA.adapt(ctx=ctx, source_campaign=src)
    assert key in str(ei.value)
    assert not (ctx.root / "contract" / "task_contract.json").exists()


def test_clean_public_docs_pass(tmp_path):
    src = _make_source(tmp_path)
    loaded = TA.load_source(src)
    for rec in loaded["instances"].values():
        assert TA.public_leaks(rec["public"], rec["private"]) == []


# ---- AC09：D 只判功能，诊断归因不声称 ----


def test_judge_binding_functional_only(ctx, tmp_path):
    src = _make_source(tmp_path)
    TA.adapt(ctx=ctx, source_campaign=src)
    contract = json.loads((ctx.root / "contract" / "task_contract.json").read_text())
    by_id = {i["instance_id"]: i for i in contract["instances"]}
    assert by_id["D-dev"]["judge"]["verdict_type"] == "repair_functional_success"
    assert "NOT_VERIFIED" in by_id["D-dev"]["judge"]["diagnosis_attribution"]
    assert by_id["E-dev"]["judge"]["layer"] == "L4"
    assert by_id["R-dev"]["judge"]["function"] == "judge_research"
    assert by_id["R-dev"]["judge"]["layer"] == "L5"
    assert "repair_functional_success" in contract["d_diagnosis_note"]
    # 判据模块 sha 与仓库实际文件一致
    real = Path(TA.REPO_ROOT) / "orchestrator/rsi_r01/evaluator.py"
    assert by_id["D-dev"]["judge"]["module_sha256"] == camp.sha256_file(real)
    assert contract["judge_modules"]["evaluator"]["sha256"] == camp.sha256_file(real)


def test_units_are_explicit(ctx, tmp_path):
    src = _make_source(tmp_path)
    TA.adapt(ctx=ctx, source_campaign=src)
    contract = json.loads((ctx.root / "contract" / "task_contract.json").read_text())
    by_id = {i["instance_id"]: i for i in contract["instances"]}
    assert by_id["E-dev"]["units"]["distance"] == "m"
    assert by_id["R-dev"]["units"]["theta"] == "rad"
    assert by_id["R-dev"]["units"]["damping"] == "N*m*s/rad"


# ---- 观测/分析合同常量与 R0.1 单一源一致 ----


def test_observation_contract_matches_schemas(ctx, tmp_path):
    src = _make_source(tmp_path)
    TA.adapt(ctx=ctx, source_campaign=src)
    contract = json.loads((ctx.root / "contract" / "task_contract.json").read_text())
    obs = contract["observation_contract"]
    assert obs["dt_s"] == S.DT_S
    assert obs["n_steps"] == S.N_STEPS
    assert obs["expected_rows"] == S.EXPECTED_ROWS
    assert obs["t_end_s"] == S.T_END_S
    assert obs["bands"] == {"theta_rad": S.THETA_BAND, "omega_rad_s": S.OMEGA_BAND,
                            "delta": S.DELTA}
    assert obs["kinds"] == {"push": S.OBS_KIND_PUSH, "pendulum": S.OBS_KIND_PENDULUM}
    trusted = obs["trusted_service"]
    assert trusted["image"] == OBS.IMAGE_REF
    assert trusted["image_digest"] == OBS.IMAGE_DIGEST
    assert trusted["service_script_sha256"] == camp.sha256_file(OBS.SERVICE_SCRIPT)


def test_analysis_contract_limits(ctx, tmp_path):
    src = _make_source(tmp_path)
    TA.adapt(ctx=ctx, source_campaign=src)
    contract = json.loads((ctx.root / "contract" / "task_contract.json").read_text())
    lim = contract["analysis_contract"]["limits"]
    assert lim["max_source_bytes"] == S.ANALYSIS_MAX_SOURCE_BYTES
    assert lim["max_stream_bytes"] == S.ANALYSIS_MAX_STREAM_BYTES
    assert lim["max_output_file_bytes"] == S.ANALYSIS_MAX_OUTPUT_FILE_BYTES
    assert lim["timeout_s"] == S.ANALYSIS_TIMEOUT_S
    assert lim["output_names"] == list(S.ANALYSIS_OUTPUT_NAMES)


# ---- 复用影响表（计划 T1 明确要求） ----


def test_reuse_impact_decisions(ctx, tmp_path):
    src = _make_source(tmp_path)
    TA.adapt(ctx=ctx, source_campaign=src)
    reuse = json.loads((ctx.root / "contract" / "reuse_impact.json").read_text())
    assert reuse["schema"] == TA.REUSE_IMPACT_SCHEMA
    deps = reuse["dependency_hashes"]
    assert set(deps["source_public"]) == set(ALL_IDS)
    assert set(deps["source_private"]) == set(ALL_IDS)
    decisions = {d["item"]: d["decision"] for d in reuse["decisions"]}
    assert decisions["R0.1 T6 长链测量仪器"] == "NOT_REUSED"
    assert decisions["候选 runner / 计费 / 状态机（R0.2 新代码）"] == "REVERIFY_IN_T3"
    assert decisions["任务/场景/故障定义（public/tasks + private/instances + task_matrix）"] \
        == "REUSED_UNCHANGED"
    assert "repair_functional_success" in reuse["d_verdict_scope"]
    assert reuse["revalidation_required"]


def test_public_api_static_only(ctx, tmp_path):
    src = _make_source(tmp_path)
    TA.adapt(ctx=ctx, source_campaign=src)
    api = json.loads((ctx.root / "contract" / "public_api.json").read_text())
    assert api["schema"] == TA.PUBLIC_API_SCHEMA
    assert len(api["instances"]) == 9
    assert api["instances"][0]["keys"] == ["gains"]
    assert api["action_protocol"]["allowed_actions"] == [
        "preregister", "run_experiment", "analyze", "submit"]
    assert api["sufficiency_status"].startswith("STATIC_ONLY")


# ---- 幂等 ----


def test_adapt_idempotent(ctx, tmp_path):
    src = _make_source(tmp_path)
    TA.adapt(ctx=ctx, source_campaign=src)
    first = json.loads((ctx.root / "contract" / "task_contract.json").read_text())
    TA.adapt(ctx=ctx, source_campaign=src)
    second = json.loads((ctx.root / "contract" / "task_contract.json").read_text())
    first.pop("recorded_at_utc")
    second.pop("recorded_at_utc")
    assert first == second
    # 副本不产生重复件
    files = [p for p in (ctx.root / "public").rglob("*") if p.is_file()]
    assert len(files) == 9


def test_cli_entry(ctx, tmp_path, monkeypatch):
    src = _make_source(tmp_path)
    monkeypatch.setenv("SIMPRAXIS_RSI_R02_CAMPAIGN", "r02_T1")
    rc = TA.main(["--source-campaign", str(src), "--campaign-id", "r02_T1",
                  "--root", str(tmp_path / "rsi_r02")])
    assert rc == 0
    assert (ctx.root / "contract" / "reuse_impact.json").is_file()
