"""P281-2 运行时证明（AC-P281-2-12/13/14）。

三件事各自独立取证：

- **粒度**（12）：每 Trial 一份 ``runtime_attestation.json``，``compare()`` 空列表
  才算相符；改一个包版本必须变成**非空**不符列表，否则「空列表」只是没比。
- **时序**（13）：采集发生在候选启动**之前**——用 CLI 端到端跑一次，比
  ``written_at`` 与事件流首事件 ``wall`` 的墙钟先后，而不是信代码里的注释。
- **判定归一**（14）：环境不符 ⇒ ``verdict=BLOCKED_INFRA``，既不是 ``FAIL``，
  也不许让 ``FingerprintError`` 逃逸成崩溃；CLI 边界上体现为 ``rc=3``。

容器 digest（AC-12 后半）落在 ``kb_integration`` 车道：那需要真 docker 与
``sim-praxis/libero:task6`` 镜像。**不**用 ``docker_integration``——那条车道的
测试自带 ``pytest.skip`` 自门，跳过会被读成「通过」，与硬规则 7 相悖。
"""
from __future__ import annotations

import json
import os
import stat as statmod
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from orchestrator.benchmark import runtime_fingerprint as rf
from orchestrator.benchmark.phase281 import birth_protocol as P
from orchestrator.benchmark.phase281 import candidate_birth as cb
from orchestrator.benchmark.phase281 import candidate_runtime as cr
from tests.benchmark import phase281_birth_fixtures as fx

REPO_ROOT = Path(__file__).resolve().parents[2]
BIRTH_CLI = REPO_ROOT / "scripts" / "benchmark" / "phase281_birth.py"

#: 计划 F10：L1 隔离镜像的冻结 digest（``task.yaml.runtime.image_digest`` 的
#: 来源，但这里**独立写死**——拿 yaml 和自己比证明不了任何事）。
F10_LIBERO_DIGEST = ("sha256:83304574517980a2b5f94d8e45b5c3f361182d0fa40fdd27"
                     "f6c1713040eca624")
F10_LIBERO_IMAGE = "sim-praxis/libero:task6"

M1_TASK = "mujoco_contact_push_v1"
L1_TASK = "libero_env_adapter_v1"


@pytest.fixture(autouse=True)
def _no_benchmark_root_env(monkeypatch):
    monkeypatch.delenv("SIMPRAXIS_BENCHMARK_ROOT", raising=False)


def _trial(tmp_path: Path, name: str = "trial") -> Path:
    directory = tmp_path / name
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    return directory


def _reseal(fingerprint: dict) -> dict:
    """改完指纹后重新封印（否则 ``validate_fingerprint`` 会判它无效）。"""
    fingerprint["fingerprint_sha256"] = rf.fingerprint_sha256(fingerprint)
    return fingerprint


def _attestation(trial: Path) -> dict:
    return json.loads((trial / cr.ATTESTATION_NAME).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# AC-P281-2-12：每 Trial 一份 + compare 空列表
# --------------------------------------------------------------------------

def test_ac_p281_2_12_attestation_matches(tmp_path: Path):
    """相符 ⇒ ``PASS`` 且不符列表为空；改一个包版本 ⇒ 列表非空（且码对得上）。"""
    spec = cb.load_task(M1_TASK)
    trial = _trial(tmp_path)
    outcome = cr.RuntimeAttestation.gate(spec, trial)

    assert outcome["verdict"] == cr.VERDICT_PASS, outcome["problems"]
    assert outcome["problems"] == [], outcome["problems"]

    path = trial / cr.ATTESTATION_NAME
    assert path.is_file() and not path.is_symlink()
    assert statmod.S_IMODE(path.stat().st_mode) == 0o600
    doc = _attestation(trial)
    assert doc["schema"] == cr.ATTESTATION_SCHEMA
    assert doc["trial_id"] == trial.name          # 粒度是 Trial，不是任务
    assert doc["task_id"] == spec.task_id
    assert doc["written_at"].endswith("Z")
    assert doc["fingerprint_sha256"] == doc["fingerprint"]["fingerprint_sha256"]
    assert cr.RuntimeAttestation.check(spec, doc) == []

    # 幂等：同一 Trial 重采一次不换条件、不刷新时点
    again = cr.RuntimeAttestation.gate(spec, trial)
    assert again["verdict"] == cr.VERDICT_PASS, again["problems"]
    assert again["written_at"] == outcome["written_at"]

    # 每 Trial 一份：另一份 Trial 有另一份文件（内容指纹相同——同一台机器）
    trial2 = _trial(tmp_path, "trial-2")
    other = cr.RuntimeAttestation.gate(spec, trial2)
    assert other["verdict"] == cr.VERDICT_PASS, other["problems"]
    doc2 = _attestation(trial2)
    assert doc2["trial_id"] == "trial-2"
    assert doc2["fingerprint_sha256"] == doc["fingerprint_sha256"]

    # 负例：改一个包版本 ⇒ 非空不符列表（按码精确到 mujoco_version）。
    # 改的是 ``python.packages``（``compare()`` 的比对面），不是 ``simulator.version``
    # ——后者是身份描述段，不参与比对（改它仍相符，正好说明这条负例钉的是真判据）。
    tampered = deepcopy(doc)
    tampered["fingerprint"]["python"]["packages"]["mujoco"] = "9.99.9"
    _reseal(tampered["fingerprint"])
    problems = cr.RuntimeAttestation.check(spec, tampered)
    assert problems, "改了一个包版本却仍报相符——compare 没在比"
    assert rf.MismatchCode.MUJOCO_VERSION in [p["code"] for p in problems], problems

    # 同一份篡改**不**重新封印 ⇒ 指纹自身无效（如实报 invalid，不拿去比）
    unsealed = deepcopy(doc)
    unsealed["fingerprint"]["python"]["packages"]["mujoco"] = "9.99.9"
    bad = cr.RuntimeAttestation.check(spec, unsealed)
    assert [p["code"] for p in bad] == ["attestation_invalid"], bad


@pytest.mark.kb_integration
def test_ac_p281_2_12b_container_digest_matches_f10(tmp_path: Path):
    """L1 容器 digest 与 F10 严格相等，且容器内实测与任务要求逐项相符。

    两段独立：① ``docker image inspect`` 拿到的镜像 Id 必须**逐字**等于 F10
    字面量（这一条不依赖任何 yaml）；② 容器内探针采集出的指纹 ``compare()``
    必须为空列表（包版本/解释器也都得对上）。
    """
    probe = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", F10_LIBERO_IMAGE],
        capture_output=True, text=True, timeout=120)
    assert probe.returncode == 0, probe.stderr[-2000:]
    assert probe.stdout.strip() == F10_LIBERO_DIGEST, probe.stdout.strip()

    spec = cb.load_task(L1_TASK)
    assert spec.runtime.get("image_digest") == F10_LIBERO_DIGEST, spec.runtime

    trial = _trial(tmp_path)
    outcome = cr.RuntimeAttestation.gate(spec, trial)
    assert outcome["verdict"] == cr.VERDICT_PASS, outcome["problems"]
    assert outcome["problems"] == [], outcome["problems"]
    doc = _attestation(trial)
    assert doc["fingerprint"]["container"]["image_digest"] == F10_LIBERO_DIGEST
    assert doc["fingerprint"]["captured_in"] == "container"


# --------------------------------------------------------------------------
# AC-P281-2-13：采集早于候选启动
# --------------------------------------------------------------------------

def test_ac_p281_2_13_capture_precedes_trial(tmp_path: Path):
    """CLI 端到端：``written_at`` 严格早于事件流首个 ``start`` 事件的 ``wall``。

    跑 **B2** 而不是 B1：M1 的 ``birth_contract`` 里 ``import_required=false``，
    所以 B1 是 ``NOT_APPLICABLE``——不起子进程、也就没有事件流可对时序。选一层
    「确实会 spawn」的（B2 构造）才验得到「采集早于启动」。
    """
    work = fx.write_workdir(tmp_path / "work", {"solution.py": fx.CANDIDATE_OK})
    trial = _trial(tmp_path, "cli-trial")

    from scripts.benchmark import phase281_birth as cli    # noqa: PLC0415 - CLI 引导

    rc = cli.main(["--trial-dir", str(trial), "--task", M1_TASK,
                   "--workdir", str(work), "--level", "B2"])
    assert rc == cli.EXIT_OK, rc

    attestation = _attestation(trial)
    events, problems = P.read_events(trial / cb.EVENTS_NAME)
    assert problems == [], problems
    start = next(event for event in events if event.get("ev") == P.EV_START)

    written = fx.parse_wall(attestation["written_at"])
    launched = fx.parse_wall(start["wall"])
    assert written < launched, (attestation["written_at"], start["wall"])

    # 出生件照常在：这一条只证时序，不替代 AC-05 的能力判定
    birth = cb.load_birth(trial / cb.BIRTH_NAME)
    assert birth["status"] == "BORN", (birth["status"], birth["reason"])


# --------------------------------------------------------------------------
# AC-P281-2-14：环境不符 ⇒ BLOCKED_INFRA（不是 FAIL，也不是崩溃）
# --------------------------------------------------------------------------

def test_ac_p281_2_14_mismatch_is_blocked_infra(tmp_path: Path):
    spec = cb.load_task(M1_TASK)
    trial = _trial(tmp_path)
    assert cr.RuntimeAttestation.gate(spec, trial)["verdict"] == cr.VERDICT_PASS

    tampered = deepcopy(_attestation(trial))
    tampered["fingerprint"]["python"]["packages"]["mujoco"] = "9.99.9"
    _reseal(tampered["fingerprint"])

    # gate 恒返回判决件，不抛
    outcome = cr.RuntimeAttestation.gate(spec, trial, attestation=tampered)
    assert outcome["verdict"] == cr.VERDICT_BLOCKED, outcome["verdict"]
    assert outcome["verdict"] not in ("FAIL", "NOT_BORN"), outcome["verdict"]
    assert rf.MismatchCode.MUJOCO_VERSION in [p["code"]
                                              for p in outcome["problems"]]

    # require 抛的是可捕获的 InfraBlocked（不是未分类异常），且判决件形状正确
    with pytest.raises(cr.InfraBlocked) as excinfo:
        cr.RuntimeAttestation.require(spec, tampered)
    blocked = excinfo.value.to_json()
    assert blocked["verdict"] == cr.VERDICT_BLOCKED
    assert blocked["reason"] == "blocked_infra"
    assert blocked["problems"], blocked

    # 指纹缺失 / 被换过身份 / 落在符号链接上：同为 BLOCKED_INFRA，各有独立码
    empty = _trial(tmp_path, "empty")
    with pytest.raises(cr.InfraBlocked) as missing:
        cr.RuntimeAttestation.load(empty)
    assert missing.value.problems[0]["code"] == "attestation_missing"

    frozen = _trial(tmp_path, "frozen")
    (frozen / cr.ATTESTATION_NAME).write_text(json.dumps({
        "schema": cr.ATTESTATION_SCHEMA, "task_id": spec.task_id,
        "trial_id": frozen.name, "written_at": P.now_wall(),
        "fingerprint": {}, "fingerprint_sha256": "0" * 64,
        "capture": {}}, ensure_ascii=False), encoding="utf-8")
    frozen_outcome = cr.RuntimeAttestation.gate(spec, frozen)
    assert frozen_outcome["verdict"] == cr.VERDICT_BLOCKED
    assert frozen_outcome["problems"][0]["code"] == "fingerprint_frozen"

    linked = _trial(tmp_path, "linked")
    target = tmp_path / "outside.json"
    target.write_text("{}\n", encoding="utf-8")
    os.symlink(target, linked / cr.ATTESTATION_NAME)
    linked_outcome = cr.RuntimeAttestation.gate(spec, linked)
    assert linked_outcome["verdict"] == cr.VERDICT_BLOCKED
    assert linked_outcome["problems"][0]["code"] == "symlink"


def test_ac_p281_2_14b_cli_exit_code_is_blocked(tmp_path: Path):
    """CLI 边界：运行时被冻结成别的身份 ⇒ ``rc=3``，出生结论不被记成 ``FAIL``。"""
    work = fx.write_workdir(tmp_path / "work", {"solution.py": fx.CANDIDATE_OK})
    trial = _trial(tmp_path, "blocked-trial")
    (trial / cr.ATTESTATION_NAME).write_text(json.dumps({
        "schema": cr.ATTESTATION_SCHEMA, "task_id": M1_TASK,
        "trial_id": trial.name, "written_at": P.now_wall(),
        "fingerprint": {}, "fingerprint_sha256": "f" * 64,
        "capture": {}}, ensure_ascii=False), encoding="utf-8")

    from scripts.benchmark import phase281_birth as cli    # noqa: PLC0415 - CLI 引导

    out = tmp_path / "outcome.json"
    rc = cli.main(["--trial-dir", str(trial), "--task", M1_TASK,
                   "--workdir", str(work), "--level", "B1", "--out", str(out)])
    assert rc == cli.EXIT_BLOCKED, rc
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["verdict"] == cr.VERDICT_BLOCKED
    assert saved["runtime"]["problems"][0]["code"] == "fingerprint_frozen"
    assert statmod.S_IMODE(out.stat().st_mode) == 0o600

    # --runtime-only 也一样：被阻塞就是 3，不会被当成 0
    only = tmp_path / "runtime-only.json"
    rc2 = cli.main(["--trial-dir", str(trial), "--task", M1_TASK,
                    "--runtime-only", "--out", str(only)])
    assert rc2 == cli.EXIT_BLOCKED, rc2
