"""P281-1 清单契约测试（AC-P281-1-01/02/03）。

被测对象是**手写严格校验器** ``candidate_manifest``：这里逐类构造非法清单，
要求它点名拒收；同时钉住「``claimed.paid_model_calls > 0`` 必须被接受」——
付费候选与零付费候选走同一条评测路径，这是本阶段的前提，不是宽容。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from orchestrator.benchmark.phase281 import candidate_manifest as cm
from orchestrator.benchmark.phase281 import constants

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "benchmarks" / "research_engineering_v1" / "schemas"
#: 本阶段两个新 schema 的落点。**不在** ``schemas/`` 里——那个目录是 Phase 2.8.0
#: 已结案的冻结面：``tests/benchmark/test_task1_task_spec.py`` 把件数钉成 4、
#: ``specs/schemas_sha256.json`` 逐件登记，且该测试文件本身在 2.8.0 证据包
#: manifest 里带 sha256。往那儿加件等于改写已结案阶段的证据（锁内适配，见
#: Plan_Complete）。``reg.root / "schemas"`` 的通配也只看本层，不会扫到子目录。
PHASE281_SCHEMA_DIR = (REPO_ROOT / "benchmarks" / "research_engineering_v1"
                       / "phase281" / "schemas")

#: ``run_manifest.v1.schema.json`` 在 82a1427 时点的逐字节 sha256（冻结契约，H3）。
#: 这个常量是**钉子**：本阶段任何提交只要动了旧 schema 一个字节，本测试即红。
RUN_MANIFEST_V1_SHA256 = (
    "96d418e27e28980ecc204a44e16737acc5556eb7c4695948a9327dfabffa87df")


def _valid_manifest() -> dict:
    return {
        "schema": cm.MANIFEST_SCHEMA,
        "experiment_id": constants.EXPERIMENT_ID,
        "trial_id": constants.trial_id("mujoco_contact_push_v1", "legacy_v1", 0),
        "arm": "legacy_v1",
        "task_id": "mujoco_contact_push_v1",
        "runtime": {"mode": "host", "fingerprint_sha256": "a" * 64},
        "inputs": [{"relpath": "input/task_snapshot.json",
                    "sha256": "b" * 64, "bytes": 12}],
        "deliverables": [{"relpath": "delivered/metrics.json",
                          "sha256": "c" * 64, "bytes": 34}],
        "claimed": {"paid_model_calls": 0, "coding_rounds": 0,
                    "first_birth_round": 1, "tool_steps": 0,
                    "tokens_in": 0, "tokens_out": 0},
        "engine": {"entry": "agent_runner", "config_digest": "d" * 64},
        "created_at": "2026-11-06T00:00:00Z",
    }


def test_ac_p281_1_01_schemas_present_and_p0_untouched():
    """两个新 schema 在场且可解析；旧 ``run_manifest.v1`` 逐字节未动（H3）。"""
    for name in ("candidate_trial_manifest.v1.schema.json",
                 "candidate_evaluation_result.v1.schema.json"):
        path = PHASE281_SCHEMA_DIR / name
        assert path.is_file(), f"缺新 schema：{path}"
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["$schema"].startswith("http"), name
        assert doc.get("type") == "object", name
        assert doc.get("additionalProperties") is False, (
            f"{name}: 顶层必须 additionalProperties=false（自报字段不许夹带）")

    legacy = SCHEMA_DIR / "run_manifest.v1.schema.json"
    assert legacy.is_file()
    import hashlib
    actual = hashlib.sha256(legacy.read_bytes()).hexdigest()
    assert actual == RUN_MANIFEST_V1_SHA256, (
        "run_manifest.v1.schema.json 被改动（冻结契约，H3）："
        f"{actual} ≠ {RUN_MANIFEST_V1_SHA256}")


def test_ac_p281_1_02_manifest_validator():
    """接受 1 份合法清单；12 类非法逐一拒收并**点名**原因。"""
    good = _valid_manifest()
    assert cm.manifest_problems(good) == []
    assert cm.validate_manifest(good)["trial_id"] == good["trial_id"]

    def drop(obj, key):
        obj.pop(key)

    def extra(obj):
        obj["note"] = "夹带"

    def bad_trial_id(obj):
        obj["trial_id"] = "P281-~~not-a-trial~~"

    def bad_arm(obj):
        obj["arm"] = "baseline"

    def bad_sha(obj):
        obj["deliverables"][0]["sha256"] = "XYZ"

    def negative_bytes(obj):
        obj["deliverables"][0]["bytes"] = -1

    def bad_runtime(obj):
        obj["runtime"]["mode"] = "vm"

    def missing_claim(obj):
        obj["claimed"].pop("tokens_out")

    def empty_deliverables(obj):
        obj["deliverables"] = []

    def bad_time(obj):
        obj["created_at"] = "2026-11-06T00:00:00+08:00"

    def bad_experiment(obj):
        obj["experiment_id"] = "roadmap_phase0_v1"

    def absolute_input(obj):
        obj["inputs"][0]["relpath"] = "/etc/passwd"

    cases = [
        ("缺字段", drop, "arm", "缺字段"),
        ("多字段", extra, None, "多字段"),
        ("trial_id 格式错", bad_trial_id, None, "trial_id"),
        ("arm 非法", bad_arm, None, "arm"),
        ("sha256 非 64hex", bad_sha, None, "sha256"),
        ("bytes 负", negative_bytes, None, "bytes"),
        ("runtime.mode 非法", bad_runtime, None, "runtime.mode"),
        ("claimed 缺键", missing_claim, None, "claimed"),
        ("deliverables 空", empty_deliverables, None, "不得为空"),
        ("时间非 Z", bad_time, None, "created_at"),
        ("experiment_id 不符", bad_experiment, None, "experiment_id"),
        ("inputs.relpath 绝对路径", absolute_input, None, "relpath"),
    ]
    assert len(cases) == 12
    for label, mutate, arg, keyword in cases:
        bad = copy.deepcopy(good)
        if arg is None:
            mutate(bad)
        else:
            mutate(bad, arg)
        problems = cm.manifest_problems(bad)
        assert problems, f"{label}：非法清单被判合法"
        assert any(keyword in item for item in problems), (
            f"{label}：拒收原因未点名 {keyword!r}，实得 {problems}")
        with pytest.raises(cm.ManifestError) as info:
            cm.validate_manifest(bad)
        assert keyword in str(info.value), label


def test_ac_p281_1_03_claimed_paid_allowed():
    """``claimed.paid_model_calls > 0`` 必须**接受**（拒绝它等于让付费候选无法评测）。"""
    doc = _valid_manifest()
    doc["claimed"]["paid_model_calls"] = 7
    doc["claimed"]["tokens_in"] = 12345
    doc["claimed"]["tokens_out"] = 6789
    assert cm.manifest_problems(doc) == []
    accepted = cm.validate_manifest(doc)
    assert accepted["claimed"]["paid_model_calls"] == 7

    negative = _valid_manifest()
    negative["claimed"]["paid_model_calls"] = -1
    assert any("paid_model_calls" in item
               for item in cm.manifest_problems(negative))


def test_manifest_validator_collects_every_problem():
    """撞到第一条不抛：一次报全部问题（评测与修复都靠这一点）。"""
    doc = _valid_manifest()
    doc["arm"] = "baseline"
    doc["task_id"] = "nope"
    doc["created_at"] = "昨天"
    problems = cm.manifest_problems(doc)
    assert len(problems) >= 3, problems


def test_trial_id_must_round_trip_through_constants():
    """三段各自合法但组合不合法（未登记 rep）也要拒收。"""
    doc = _valid_manifest()
    doc["trial_id"] = "mujoco_contact_push_v1__legacy_v1__rep9"
    problems = cm.manifest_problems(doc)
    assert any("trial_id" in item for item in problems), problems
