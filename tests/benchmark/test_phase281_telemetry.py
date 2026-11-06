"""P281-4 三源遥测 / 预算帽 / 运行形态测试（AC-P281-4-07 … -18，计划 §8.7）。

这一组断言守护的是「**观测值不许来自候选自述**」这条主线：

* 帽（07/08）—— 第 11 次调用在**联网之前**被拦，终态落 ``CAP_HIT``，账本恰
  10 行且 seq 连续。拦晚一次就等于多花一次钱。
* 自述对账（09）—— ``observed`` 从账本重算；改一个 ``claimed`` 数字，评测器
  必须转 ``INVALID_ARTIFACT``，而不是采信自述。
* 隔离面（10/11/13/14）—— 缓存按 Trial 分目录、出生未过零调用、输入面不许
  被候选改、容器 argv 逐段合规。
* 冻结与常量面（12/15/16/17/18）—— 交付快照冻结、两臂同一缓存纪元、配置
  摘要单一来源、授予面可写、运行形态是兄弟容器（且探针件在场）。

**假到哪一步**：网关是假（0 触网，账行是真的），引擎是桩；其余（流水线、
账本读、评测器、容器 argv 构造、授予解析）全部是真实现。理由与边界见
``tests/benchmark/phase281_trial_fixtures.py`` 的模块 docstring。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.agentic.config import AgenticConfig
from orchestrator.agentic.forensics import cache_epoch_for
from orchestrator.agentic.image_pin import AGENTIC_IMAGE
from orchestrator.agentic.policy import GrantTable, PreToolUsePolicy
from orchestrator.agentic.tools.run import (
    LocalSubprocessBackend, build_docker_create_argv, check_backend)
from orchestrator.agentic.wiring import AgenticWiringError, parse_grant_strings
from orchestrator.benchmark import reference
from orchestrator.benchmark.phase281 import (
    arm_adapter, candidate_birth, constants, paths, sandbox, telemetry,
    trial_harness)
from tests.benchmark import phase281_parity as parity
from tests.benchmark import phase281_trial_fixtures as F

R1 = "icy_ground_dr_v1"
M1 = "mujoco_contact_push_v1"
L1 = "libero_env_adapter_v1"


def _gates(doc: dict) -> "dict[str, dict]":
    return {str(g["id"]): g for g in doc["gates"]}


# --------------------------------------------------------------------------
# AC-P281-4-07：第 11 次调用在**发出之前**被拦
# --------------------------------------------------------------------------

def test_ac_p281_4_07_cap_enforced_pre_call(tmp_path):
    """``CapGuardGateway`` 抛 ``TrialCapReached``，transport 计数停在 10。"""
    trial_id = constants.trial_id(R1, "legacy_v1", 0)
    ledger = tmp_path / "quota" / "ledger.jsonl"
    inner = F.FakeGateway(ledger, trial_id=trial_id, task_no=3)
    budget = telemetry.TrialBudget(cap=constants.CALL_CAP_PER_TRIAL,
                                   ledger=ledger, trial_id=trial_id)
    guarded = telemetry.CapGuardGateway(inner, budget)

    for index in range(constants.CALL_CAP_PER_TRIAL):
        guarded.call("coding_debug", [{"role": "user", "content": f"r{index}"}])
    assert inner.calls == 10
    assert budget.used() == 10 and budget.exhausted() is True

    # 第 11 次：抛在**联网之前**——内层的调用计数不再增长
    with pytest.raises(telemetry.TrialCapReached) as caught:
        guarded.call("coding_debug", [{"role": "user", "content": "over"}])
    assert (caught.value.trial_id, caught.value.cap, caught.value.used) == (
        trial_id, 10, 10)
    assert inner.calls == 10, "第 11 次调用漏到了 transport（帽拦晚了）"

    # 视觉入口同样过帽（视觉也计费）
    with pytest.raises(telemetry.TrialCapReached):
        guarded.call_visual("看图")
    assert inner.calls == 10

    # 透明代理面：非付费属性原样透传、可被识别为守卫（不是伪装成网关）
    assert guarded.model_id("coding_debug") == "fake-model"
    assert guarded.guard_of == "phase281.CapGuardGateway"

    # 权威值来自账本本身：外部再记一行，``used()`` 必须跟着变（不是内存计数）
    outer_row = dict(F.ledger_rows(ledger)[-1])
    outer_row["seq"] = outer_row["seq"] + 1
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(outer_row, ensure_ascii=False, sort_keys=True) + "\n")
    assert budget.used() == 11
    assert budget.exhausted() is True


# --------------------------------------------------------------------------
# AC-P281-4-08：帽用尽 → CAP_HIT，账本恰 10 行且 seq 连续
# --------------------------------------------------------------------------

def test_ac_p281_4_08_cap_hit_state(tmp_path):
    """引擎尝试 11 次 → 终态 ``CAP_HIT``、``used_calls=10``、seq 1..10。"""
    run = F.run_case(tmp_path, task_id=R1,
                     engine=F.Engine(calls=constants.CALL_CAP_PER_TRIAL + 1))
    trial = run.trial
    assert trial["status"] == "CAP_HIT", trial["status"]
    assert trial["used_calls"] == constants.CALL_CAP_PER_TRIAL
    rows = F.ledger_rows(run.ledger)
    assert len(rows) == constants.CALL_CAP_PER_TRIAL
    assert [row["seq"] for row in rows] == list(
        range(1, constants.CALL_CAP_PER_TRIAL + 1))
    assert all(row["event"] == "call" for row in rows)
    assert all(row["thread_id"] == trial["trial_id"] for row in rows)
    # 观测面与账本同源（不是引擎自报）
    assert trial["observed"]["calls"] == constants.CALL_CAP_PER_TRIAL
    assert trial["observed_sources"]["calls"] == "ledger"


# --------------------------------------------------------------------------
# AC-P281-4-09：claimed vs observed（正常相等 / 篡改即 INVALID_ARTIFACT）
# --------------------------------------------------------------------------

def test_ac_p281_4_09_claim_vs_observed(tmp_path):
    """自述从账本重算且逐值相等；改一个数字 → 优先级 3 判 INVALID_ARTIFACT。"""
    ctx = parity.build_root(tmp_path)
    root = ctx["root"]
    ledger = root / "quota" / "ledger.jsonl"
    run = F.run_case(tmp_path, task_id=R1, engine=F.Engine(calls=2),
                     root=root, ledger=ledger)

    doc = ctx["evaluator"].evaluate(run.base)
    checks = {entry["field"]: entry for entry in doc["manifest_checks"]}
    assert checks["claimed.paid_model_calls"]["observed"] == 2
    assert checks["claimed.paid_model_calls"]["claimed"] == 2
    assert all(entry["equal"] for entry in doc["manifest_checks"]), [
        e for e in doc["manifest_checks"] if not e["equal"]]
    assert _gates(doc)["GATE_SELF_REPORT"]["passed"] is True

    # 篡改：把自述从 2 改成 1（账本仍是 2 行）——评测器必须以账本为准
    manifest_path = run.base / "candidate_trial_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["claimed"]["paid_model_calls"] = 1
    tmp = manifest_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, manifest_path)

    tampered = ctx["evaluator"].evaluate(run.base)
    assert tampered["verdict"] == "INVALID_ARTIFACT", tampered["verdict"]
    gate = _gates(tampered)["GATE_SELF_REPORT"]
    assert gate["passed"] is False
    assert "claimed.paid_model_calls: 自述 1 ≠ 观测 2" in gate["detail"]

    # 反向证据：账本行数确实是 2（观测值的权威来源没被动过）
    assert len(F.ledger_rows(ledger)) == 2


# --------------------------------------------------------------------------
# AC-P281-4-10：call_cache 按 Trial 分目录，同名 key 不跨 Trial 命中
# --------------------------------------------------------------------------

def test_ac_p281_4_10_cache_isolation_per_trial(tmp_path):
    """两个 Trial 的 ``call_cache/`` 不同；B 取不到 A 写的结果。"""
    first = F.run_case(tmp_path, task_id=R1, rep=0,
                       engine=F.Engine(put_cache=("k1", "A 的结果")))
    second = F.run_case(tmp_path, task_id=R1, rep=1,
                        engine=F.Engine(get_cache=("k1", "f" * 64)))
    cache_a = Path(first.engine.seen["cache_dir"])
    cache_b = Path(second.engine.seen["cache_dir"])
    assert cache_a != cache_b
    files_a = F.sha_tree(cache_a)
    assert files_a, "A 没写进本 Trial 的 call_cache（测试前提不成立）"
    assert F.sha_tree(cache_b) == {}, "B 的 call_cache 里出现了件"
    assert set(F.sha_tree(cache_b).values()).isdisjoint(files_a.values())
    assert second.engine.seen["cache_hit"] is None, "同名 key 跨 Trial 命中了"
    # 目录权限与 Trial 同族（0600/0700 不必在别处重开）
    assert cache_a.stat().st_mode & 0o777 == 0o700


# --------------------------------------------------------------------------
# AC-P281-4-11：出生未过 → 不发任何调用
# --------------------------------------------------------------------------

def test_ac_p281_4_11_no_calls_before_birth(tmp_path):
    """``NOT_BORN`` → 零次 transport 调用、零账行、终态 ``FINISHED``。

    真实场景是「带一份过不了出生的入口起 Trial」：入口由 ``pre_seed`` 播种，
    第 4 步在**引擎之前**判定出生（不带入口时「未出生」不可判定，闸不生效——
    那是 ``_pre_birth`` 的刻意设计，不是本测试的捷径）。
    """
    spec = candidate_birth.load_task(R1)
    entry = trial_harness._entry_rel(spec)
    run = F.run_case(tmp_path, task_id=R1, birth="NOT_BORN",
                     engine=F.Engine(calls=3),
                     pre_seed={entry: "raise SystemExit(1)\n"})
    trial = run.trial
    assert run.gateway.calls == 0
    assert F.ledger_rows(run.ledger) == []
    assert trial["used_calls"] == 0
    assert trial["birth"]["status"] == "NOT_BORN"
    assert trial["birth_phase"] == "pre"
    assert run.engine.seen == {}, "出生未过却调用了引擎"
    assert trial["status"] == "FINISHED", trial["status"]


# --------------------------------------------------------------------------
# AC-P281-4-12：delivered/ 是运行结束时刻的冻结快照
# --------------------------------------------------------------------------

def test_ac_p281_4_12_delivered_snapshot_frozen(tmp_path):
    """运行后再改 ``workdir/``，``delivered/`` 的 sha256 集合不变。"""
    run = F.run_case(tmp_path, task_id=R1,
                     engine=F.Engine(write={"experiment_plan.yaml": "v1\n"}))
    delivered = run.base / "delivered"
    before = F.sha_tree(delivered)
    assert "experiment_plan.yaml" in before

    workdir = Path(run.trial["workdir"])
    (workdir / "experiment_plan.yaml").write_text("v2 篡改\n", encoding="utf-8")
    (workdir / "extra.txt").write_text("候选运行后才产生的件\n", encoding="utf-8")
    assert F.sha_tree(delivered) == before, "delivered/ 跟着 workdir 变了"
    # 反证：改动确实发生了（否则上面的相等是空转）
    assert F.sha_tree(workdir) != before


# --------------------------------------------------------------------------
# AC-P281-4-13：输入面跑前跑后 sha256 集合相等
# --------------------------------------------------------------------------

def test_ac_p281_4_13_inputs_unchanged(tmp_path):
    """候选篡改 ``input/`` → 运行器如实记账（不是静默接受）。"""
    clean = F.run_case(tmp_path, task_id=R1, rep=0, engine=F.Engine())
    assert clean.trial["problems"] == []

    dirty = F.run_case(tmp_path, task_id=R1, rep=1,
                       engine=F.Engine(tamper_input="task_snapshot.json"))
    problems = [p for p in dirty.trial["problems"] if "AC-13" in p]
    assert problems, dirty.trial["problems"]
    assert "task_snapshot.json" in problems[0]
    # 输入面在盘上确实被改了（与「检测器恒红」区分开）
    assert (dirty.base / "input" / "task_snapshot.json").read_text(
        encoding="utf-8") == "tampered\n"


# --------------------------------------------------------------------------
# AC-P281-4-14：L1 的 docker argv 通过容器 argv 校验
# --------------------------------------------------------------------------

def test_ac_p281_4_14_container_argv_valid(tmp_path):
    """``-v`` ⊆ 白名单、``:ro`` ≥ 3、``--network none``、镜像按 ``@sha256``。"""
    spec = candidate_birth.load_task(L1)
    workdir = tmp_path / "out"
    input_dir = tmp_path / "input"
    for path in (workdir, input_dir, input_dir / "skills",
                 input_dir / "assets"):
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    backend = sandbox.build_run_backend(spec, trial_workdir=workdir,
                                        input_dir=input_dir)
    assert isinstance(backend, sandbox.TaskImageBackend)
    argv = ["/usr/bin/python3", "adapter.py"]
    cmd = backend.build_argv(argv)

    assert reference.validate_container_argv(cmd) == []
    ro = [spec_ for spec_, mode in reference.parse_mount_flags(cmd)
          if mode == "ro"]
    assert len(ro) >= reference.MIN_RO_MOUNTS
    assert "--network" in cmd
    assert cmd[cmd.index("--network") + 1] == "none"
    assert cmd[cmd.index(backend.image)] == backend.image
    assert "@sha256:" in backend.image
    assert cmd[-2:] == argv

    # 构造期负例：非 digest 镜像 / 非 none 网络一律拒（不等到起容器）
    with pytest.raises(sandbox.SandboxError):
        sandbox.TaskImageBackend("sim-praxis/libero:task6", workdir=workdir,
                                 input_dir=input_dir)
    with pytest.raises(sandbox.SandboxError):
        sandbox.TaskImageBackend(backend.image, workdir=workdir,
                                 input_dir=input_dir, network="bridge")


# --------------------------------------------------------------------------
# AC-P281-4-15：两臂缓存纪元同一（全关 → 旧行为）
# --------------------------------------------------------------------------

def test_ac_p281_4_15_cache_epoch_all_off():
    """``cache_epoch_for(None) == cache_epoch_for(AgenticConfig())``。"""
    off = cache_epoch_for(AgenticConfig())
    assert cache_epoch_for(None) == off
    assert off == ("agentic-v2", "agentic-v2:", "agentic-v2-")
    # 两臂的 arm_adapter 都是 agentic_config=None（同一缓存纪元）
    assert arm_adapter.ARMS["legacy_v1"].to_json()["agentic_config"] is None
    assert arm_adapter.ARMS["agentic_v1"].to_json()["agentic_config"] is None
    assert cache_epoch_for(None) == off


# --------------------------------------------------------------------------
# AC-P281-4-16：清单里的 config_digest 与 ARMS 表重算值逐字相等
# --------------------------------------------------------------------------

def test_ac_p281_4_16_config_digest(tmp_path):
    """``engine.config_digest`` 只能来自 ``arm_config_digest`` 单一来源。"""
    digests: "dict[str, str]" = {}
    for arm in constants.ARMS:
        run = F.run_case(tmp_path, task_id=R1, arm=arm, rep=0,
                         engine=F.Engine(calls=1))
        engine_block = run.manifest["engine"]
        digests[arm] = engine_block["config_digest"]
        assert engine_block["entry"] == arm_adapter.ARMS[arm].engine
        assert engine_block["config_digest"] == arm_adapter.arm_config_digest(arm)
        # 逐字重算：规范化 JSON → sha256（不是又一次 canonical 之后取前缀）
        canonical = arm_adapter.canonical_json(
            arm_adapter.arm_params(arm_adapter.ARMS[arm]))
        assert engine_block["config_digest"] == hashlib.sha256(
            canonical.encode("utf-8")).hexdigest()
    assert digests["legacy_v1"] != digests["agentic_v1"], (
        "两臂配置摘要相同 ⇒ 清单无法证明跑的是哪条臂")


# --------------------------------------------------------------------------
# AC-P281-4-17：授予串可解析，且 write_file 在 workdir 内 allow / 外 deny
# --------------------------------------------------------------------------

def test_ac_p281_4_17_grants_parse_and_write_allowed(tmp_path):
    """``AGENTIC_GRANTS`` → GrantTable：写面覆盖整个 workdir，且不外溢。"""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    os.chmod(workdir, 0o700)

    grants = parse_grant_strings(arm_adapter.AGENTIC_GRANTS)   # 不抛
    assert grants.tools == {"apply_patch", "fail", "finish", "grep",
                            "list_dir", "read_file", "run_cmd", "write_file"}
    assert grants.write_roots == ("",)          # "" = 整个 workdir
    assert grants.write_roots_resolved(workdir) == [workdir]
    assert grants.run_argv == (("python",),)
    assert grants.probes == frozenset()          # 本实验不授予任何探针

    policy = PreToolUsePolicy(grants, workdir=workdir)
    inside = policy.check("write_file", {"content": "x = 1\n"},
                          resolved_path=workdir / "a.py")
    outside = policy.check("write_file", {"content": "x = 1\n"},
                           resolved_path=Path("/tmp/x.py"))
    assert inside.decision == "allow", inside.reason
    assert outside.decision == "deny"
    assert "不在" in outside.reason or "白名单" in outside.reason

    # 负例①：只授予 ``write_file`` 工具名、没有 ``write:`` 前缀串 ⇒
    # ``write_roots`` 空 ⇒ 不论目标在哪一律拒（工具在表里也写不了）
    bare = parse_grant_strings(("write_file", "read_file"))
    assert bare.write_roots == ()
    assert "write_file" in bare.tools
    denied = PreToolUsePolicy(bare, workdir=workdir).check(
        "write_file", {"content": "x = 1\n"}, resolved_path=workdir / "a.py")
    assert denied.decision == "deny"
    assert "可写路径前缀" in denied.reason

    # 负例②：偷渡路径形态在解析期就炸（不落到运行期）
    for bad in ("write:/", "write:/etc", "write:../x", "write:a/../../b"):
        with pytest.raises(AgenticWiringError):
            parse_grant_strings((bad,))


# --------------------------------------------------------------------------
# AC-P281-4-18：运行形态是兄弟容器，且探针件在场
# --------------------------------------------------------------------------

def _probe_doc(root: Path) -> dict:
    """读 ``analysis/run_form_probe.json``；缺件即红（不许静默跳过）。"""
    target = Path(root) / "analysis" / "run_form_probe.json"
    assert target.is_file(), (
        f"缺 {target}：付费 Trial 之前必须先跑 --probe-run-form"
        "（探针件缺失即付费 → 红，AC-P281-4-18 ③）")
    return json.loads(target.read_text(encoding="utf-8"))


def _probe_matches_image(doc: dict, spec) -> bool:
    """形态闸：探针记的镜像必须是该任务**预注册**的那个引用。"""
    expected = reference.libero_image_ref(spec)
    return doc.get("image") == expected


def _production_ctx(backend, *, nonprod: bool) -> SimpleNamespace:
    return SimpleNamespace(run_backend=backend, nonprod=bool(nonprod))


def test_ac_p281_4_18_run_form_is_container(tmp_path):
    """agentic 臂只跑兄弟容器；本地 NONPROD 后端在生产上下文被拒。"""
    workdir = tmp_path / "out"
    input_dir = tmp_path / "input"
    for path in (workdir, input_dir):
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)

    # ① 本地非生产后端：生产上下文 deny；它唯一被允许的 nonprod 上下文里写全灭
    local = LocalSubprocessBackend()
    denied = check_backend(_production_ctx(local, nonprod=False))
    assert getattr(denied, "denied", False) is True
    assert "生产上下文禁止使用本地 NONPROD 后端" in denied.decision.reason

    allowed = check_backend(_production_ctx(local, nonprod=True))
    assert allowed is local                              # nonprod 上下文放行
    grants = parse_grant_strings(arm_adapter.AGENTIC_GRANTS)
    blocked = PreToolUsePolicy(grants, workdir=workdir,
                               nonprod=True).check(
        "write_file", {"content": "x\n"}, resolved_path=workdir / "a.py")
    assert blocked.decision == "deny"
    assert "NONPROD" in blocked.reason

    # ② M1/R1 → 兄弟容器 AGENTIC_IMAGE，--network none，生产上下文放行
    for task_id in (M1, R1):
        spec = candidate_birth.load_task(task_id)
        backend = sandbox.build_run_backend(
            spec, trial_workdir=workdir, input_dir=input_dir)
        assert type(backend).__name__ == "DockerSiblingBackend"
        assert backend.form == "sibling_container"
        assert getattr(backend, "nonprod", False) is False
        assert backend.image == AGENTIC_IMAGE
        assert backend.network == "none"
        assert check_backend(_production_ctx(backend, nonprod=False)) is backend

    # ③ L1 → 任务镜像（不是 AGENTIC_IMAGE：它没有 libero 依赖）
    spec_l1 = candidate_birth.load_task(L1)
    l1_backend = sandbox.build_run_backend(
        spec_l1, trial_workdir=workdir, input_dir=input_dir)
    assert isinstance(l1_backend, sandbox.TaskImageBackend)
    assert l1_backend.form == "sibling_container"
    assert l1_backend.image == reference.libero_image_ref(spec_l1)
    assert l1_backend.image != AGENTIC_IMAGE
    assert check_backend(_production_ctx(l1_backend, nonprod=False)) is l1_backend

    # ④ 探针件在场，三格逐项可查；L1 的镜像与预注册一致
    root = paths.phase281_root()
    payload = _probe_doc(root)
    probes = payload["probes"]
    for task_id in constants.TASK_IDS:
        doc = probes[task_id]
        for key in ("python_v", "import_ok", "rc"):
            assert key in doc, (task_id, key)
        assert doc["rc"] == 0, (task_id, doc.get("stderr_tail"))
        assert doc["import_ok"] is True, (task_id, doc.get("mods"))
    assert probes[L1].get("runtime") == "docker"
    assert _probe_matches_image(probes[L1], spec_l1) is True
    # ② 的机判形态：若把镜像记成 AGENTIC_IMAGE，同一个判定必须转假
    assert _probe_matches_image({"image": AGENTIC_IMAGE}, spec_l1) is False

    # ③ 的机判形态：探针件缺失时读件即失败
    with pytest.raises(AssertionError, match="probe-run-form"):
        _probe_doc(tmp_path / "empty_root")

    # ④ venv_path=None：连 argv 都拼不出来（fail-closed 抛出，不是非法挂载）
    with pytest.raises(TypeError):
        build_docker_create_argv(image=l1_backend.image,
                                 argv=["/usr/bin/python3", "adapter.py"],
                                 workdir=workdir, venv_path=None)
