"""P281-6 干跑测试（计划 §10.6 的 AC-01/02/03/04/09；**0 付费、0 触网**）。

这五条 AC 都在验同一件事的两面：**干跑必须真的走生产流水线**，而干跑产物
**必须不可能**被当成真证据读。前者靠「注入假引擎/假出生/假运行器但用真
``run_trial``」来测（与 ``phase281_trial_fixtures`` 同一先例），后者靠
``load_real`` / ``assert_real`` 两道闸来测。

不测的部分（如实登记）：`--all` 的三任务六试次全跑（含真容器与真出生五层）由
计划 §10.5 步骤 1 的 CLI 跑一次留证，不放进单测——那会把「流水线通不通」变成
「docker 在不在」。
"""
from __future__ import annotations

import ast
import hashlib
import json
import sys
from pathlib import Path

import pytest

from orchestrator.benchmark.phase281 import candidate_birth, constants, dryrun
from orchestrator.benchmark.phase281 import trial_harness

sys.path.insert(0, str(Path(__file__).resolve().parent))
from phase281_trial_fixtures import (  # noqa: E402  同目录夹具

    birth_runner,
    capture_for,
    skills_rows,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
M1 = "mujoco_contact_push_v1"


_ENTRIES = ("solution.py", "adapter.py")


def _runner(argv, cwd=None, timeout=None, env=None, **kwargs):
    """假运行器：不起子进程，但**如实**落一份合法的合成 telemetry/metrics。

    关键在「如实」二字：入口件**不在**就 rc=1（这正是真候选出生时的样子——
    工作目录里还没有候选写的入口）。只有补丁真的把入口件落进工作目录之后，
    这一跑才 rc=0 并产出遥测。若不分这两态，旧引擎的第一跑就撞上
    「rc==0 且 telemetry 在场」的停机条件，``coding_round`` 恒 0、罐头网关
    一次都调不到——那测的就不是流水线，而是假运行器本身。

    落的是合成件（干跑产物全部会被 :func:`dryrun.stamp_tree` 打上 ``simulated``），
    它与真候选的唯一区别是「没跑过物理引擎」。
    """
    args = [str(item) for item in (argv or ())]
    entry = next((name for name in _ENTRIES
                  if any(item.endswith(name) for item in args)), None)
    base = Path(cwd) if cwd is not None else Path(".")
    if entry is None or not (base / entry).is_file():
        class _Missing:
            returncode = 1
            stdout = ""
            stderr = f"no such file: {entry or '<no entry>'}\n"
        return _Missing()

    rows = [{"step": index, "t": round(index * 0.002, 6),
             "cube_pos": [0.05 + 0.01 * index, 0.02, 0.02],
             "target_pos": [0.30, 0.20, 0.02],
             "max_penetration": 0.0, "contact_force_norm": 0.0}
            for index in range(8)]
    (base / "telemetry.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8")
    (base / "metrics.json").write_text(json.dumps(
        {"endpoint_distance_m": 0.19, "max_penetration_m": 0.0,
         "contact_force_peak_n": 0.0, "steps": 8}, sort_keys=True) + "\n",
        encoding="utf-8")

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""
    return _Done()


def _dryrun(tmp_path, **kwargs):
    """在 ``tmp_path`` 下跑一次最小干跑（M1 × legacy），返回 summary。

    **不注入 ``engine_runner``**：旧引擎是真的（它自己去调罐头网关、自己落补丁），
    这样才能验到「罐头回复真的被吃下去了」；假的只有运行器与出生判定。
    """
    spec = candidate_birth.load_task(M1)
    return dryrun.run_all(
        root=tmp_path, tasks=[M1], arms=["legacy_v1"], rep=0,
        stamp="20260101T000000Z", runner=_runner,
        birth_runner=birth_runner("BORN"),
        skills_rows=skills_rows(tmp_path), capture=capture_for(spec, tmp_path),
        assets_dir=tmp_path / "assets", **kwargs)


# --------------------------------------------------------------------------
# AC-01 零付费
# --------------------------------------------------------------------------

def test_ac_p281_6_01_zero_paid(tmp_path):
    """干跑全程 0 实呼：账本视图是**一条不存在的路径**，真账一个字节没动。"""
    summary = _dryrun(tmp_path)
    assert summary["ledger_rows"] == 0
    assert [t["used_calls"] for t in summary["trials"]] == [0]
    # 账本视图故意不创建：不存在 ⇒ read_lines 返回空账，CapGuard 看到 0 行
    view = Path(summary["ledger_view"])
    assert view.name == dryrun.LEDGER_VIEW
    assert not view.exists()
    # 罐头网关确实被调过（否则「0 付费」是空转出来的）
    assert summary["trials"][0]["canned_calls"] >= 1


# --------------------------------------------------------------------------
# AC-02 逐件打标
# --------------------------------------------------------------------------

def test_ac_p281_6_02_all_marked(tmp_path):
    """干跑树里**每一个** ``*.json`` 顶层 ``simulated is True``。"""
    summary = _dryrun(tmp_path)
    root = Path(summary["root"])
    marked = sorted(root.rglob("*.json"))
    assert marked, "干跑没有落任何 JSON 件"
    unmarked = [p for p in marked
                if json.loads(p.read_text(encoding="utf-8")).get("simulated") is not True]
    assert unmarked == []

    index = json.loads((root / "simulated_index.json").read_text(encoding="utf-8"))
    assert index["schema"] == "simpraxis.phase281.dryrun_simulated_index.v1"
    assert index["simulated"] is True
    assert index["simulated_reason"] == dryrun.SIMULATED_REASON
    # 索引件逐件记了打标前后的 sha256；索引自己不在表里（它最后写）
    listed = {entry["path"] for entry in index["files"]}
    assert "simulated_index.json" not in listed
    assert listed == {p.relative_to(root).as_posix() for p in marked
                      if p.name != "simulated_index.json"}
    # 打标**确实改了字节**：至少一件 before != after（干跑件里本来就有不带
    # simulated 的，例如 trial.json / run_manifest / 交付件）。已经带标记的件
    # （``dryrun_summary.json`` 自己）重序列化后逐字节不变，属于幂等——不能
    # 反过来要求「每件都变」，那是把幂等当成失败。
    changed = 0
    for entry in index["files"]:
        assert len(entry["post_stamp_sha256"]) == 64
        current = hashlib.sha256(
            (root / entry["path"]).read_bytes()).hexdigest()
        assert entry["post_stamp_sha256"] == current, entry["path"]
        changed += entry["pre_stamp_sha256"] != entry["post_stamp_sha256"]
    assert changed >= 1, "打标一个字节都没改：标记没有真的写进树里"


def test_stamp_tree_refuses_non_object(tmp_path):
    """顶层不是对象的 JSON 打不上标 ⇒ ``StampError``（不能静默放过）。"""
    (tmp_path / "list.json").write_text("[1, 2, 3]\n", encoding="utf-8")
    with pytest.raises(dryrun.StampError):
        dryrun.stamp_tree(tmp_path)
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(dryrun.StampError):
        dryrun.stamp_tree(tmp_path)


# --------------------------------------------------------------------------
# AC-03 分析硬拒
# --------------------------------------------------------------------------

def test_ac_p281_6_03_analysis_refuses_simulated(tmp_path):
    """读干跑件即 ``AnalysisRefused``；真件照读；两道闸各自独立。"""
    summary = _dryrun(tmp_path)
    root = Path(summary["root"])
    trial = root / "runs" / M1 / "legacy_v1" / "rep0" / trial_harness.TRIAL_JSON
    assert trial.is_file()
    with pytest.raises(dryrun.AnalysisRefused):
        dryrun.load_real(trial)
    with pytest.raises(dryrun.AnalysisRefused):
        dryrun.assert_real(root)
    with pytest.raises(dryrun.AnalysisRefused):
        dryrun.assert_real(trial.parent)

    # 真件（无 simulated / 无 simulated_reason）照读
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"schema": "x", "value": 1}), encoding="utf-8")
    assert dryrun.load_real(real)["value"] == 1
    assert dryrun.assert_real(tmp_path / "real.json") == real

    # ``simulated: false`` 是自相矛盾的写法，但**不带** reason 时内容闸放行；
    # 路径闸仍然拦得住 —— 两道闸互不依赖
    sneaky = root / "runs" / "sneaky.json"
    sneaky.write_text(json.dumps({"simulated": False}), encoding="utf-8")
    assert dryrun.load_real(sneaky) == {"simulated": False}
    with pytest.raises(dryrun.AnalysisRefused):
        dryrun.assert_real(sneaky)

    # 只写 reason 也拒（合取的另一半）
    reason_only = tmp_path / "reason_only.json"
    reason_only.write_text(json.dumps({"simulated_reason": "canned"}),
                           encoding="utf-8")
    with pytest.raises(dryrun.AnalysisRefused):
        dryrun.load_real(reason_only)


# --------------------------------------------------------------------------
# AC-04 同一段代码路径
# --------------------------------------------------------------------------

def _run_trial_call_sites(path: Path) -> "list[ast.Call]":
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "run_trial":
            hits.append(node)
    return hits


def _dry_run_flag(call: "ast.Call") -> "bool | str | None":
    """``run_trial(...)`` 的 ``dry_run`` 实参。

    缺省 ``None``；常量给常量值；非常量（如 CLI 的 ``dry_run=dry``）给
    ``"expr"``——断言方按「不是 True」处理，只有字面 ``True`` 才算干跑。
    """
    for keyword in call.keywords:
        if keyword.arg == "dry_run":
            try:
                return ast.literal_eval(keyword.value)
            except ValueError:
                return "expr"
    return None


def test_ac_p281_6_04_same_code_path():
    """干跑与正式路径**同一个** ``run_trial``：没有第二条干跑流水线。

    机判口径（计划 §10.3）：「``dry_run=True`` 时**只**替换 ``gateway``」。这条
    断言把「只换网关」钉成结构事实——全仓 ``run_trial`` 的调用点一共三处，其中
    **恰好一处**传 ``dry_run=True``（本模块），另两处都是真流水线：

    * ``scripts/benchmark/phase281_run_trial.py``：正式 CLI，``dry_run`` 跟着
      命令行开关走；
    * ``orchestrator/benchmark/phase281/candidate_controls.py``：候选级负例
      驱动器，**不传** ``dry_run``（即 False），跑的是真流水线。

    三处调的是**同一个函数**，谁都没有另写一条「干跑专用」的流水线——这才是
    AC-04 要的实质，而不是「调用点只有两个」这个会随新调用方失效的计数。
    """
    sources = [p for p in (REPO_ROOT / "orchestrator").rglob("*.py")]
    sources += [p for p in (REPO_ROOT / "scripts").rglob("*.py")]
    callers = {}
    for path in sources:
        try:
            hits = _run_trial_call_sites(path)
        except SyntaxError:      # pragma: no cover - 仓里不会有语法错
            continue
        if hits:
            callers[path.relative_to(REPO_ROOT).as_posix()] = hits
    assert set(callers) == {
        "orchestrator/benchmark/phase281/candidate_controls.py",
        "orchestrator/benchmark/phase281/dryrun.py",
        "scripts/benchmark/phase281_run_trial.py",
    }, callers

    dry_calls = callers["orchestrator/benchmark/phase281/dryrun.py"]
    assert len(dry_calls) == 1
    kwargs = {kw.arg for kw in dry_calls[0].keywords if kw.arg}
    assert {"spec", "arm_id", "rep", "root", "gateway", "ledger",
            "dry_run"} <= kwargs
    assert _dry_run_flag(dry_calls[0]) is True

    prod_calls = callers["scripts/benchmark/phase281_run_trial.py"]
    assert len(prod_calls) == 1
    assert "dry_run" in {kw.arg for kw in prod_calls[0].keywords if kw.arg}

    # 候选级负例驱动器跑的是**真**流水线：不许偷偷用 dry_run 换掉网关
    for call in callers["orchestrator/benchmark/phase281/candidate_controls.py"]:
        assert _dry_run_flag(call) in (None, False)

    # 全仓只有上面这一处 dry_run=True
    flagged = [name for name, calls in callers.items()
               for call in calls if _dry_run_flag(call) is True]
    assert flagged == ["orchestrator/benchmark/phase281/dryrun.py"], flagged

    # 被调函数本体：``dry_run`` 只用于「换网关」（与目录重置），不是另一条流水线
    body = ast.parse(Path(trial_harness.__file__).read_text(encoding="utf-8"))
    run_trial = next(node for node in ast.walk(body)
                     if isinstance(node, ast.FunctionDef)
                     and node.name == "run_trial")
    uses = [node for node in ast.walk(run_trial)
            if isinstance(node, ast.Name) and node.id == "dry_run"]
    assert uses, "run_trial 没有使用 dry_run"


def test_canned_gateway_identity():
    """只有**自证身份**的罐头网关才被采信；形状不符的一律换成空补丁网关。"""
    gateway = dryrun.CannedGateway({"coding_debug": ["x"]})
    assert gateway.canned is True
    assert gateway.guard_of == "phase281._CannedGateway"
    assert trial_harness._is_canned_gateway(gateway) is True

    class Impostor:
        canned = True
        guard_of = "phase281._CannedGateway"

    assert trial_harness._is_canned_gateway(Impostor()) is True
    Impostor.canned = False
    assert trial_harness._is_canned_gateway(Impostor()) is False
    assert trial_harness._is_canned_gateway(object()) is False


def test_canned_gateway_exhausts_instead_of_repeating():
    """罐头用完即抛：缺条数就是缺，不许循环复用糊过去。"""
    gateway = dryrun.CannedGateway({"coding_debug": ["only"]})
    text, usage = gateway.call("coding_debug", [])
    assert text == "only" and usage == (0, 0)
    with pytest.raises(dryrun.CannedExhausted):
        gateway.call("coding_debug", [])
    with pytest.raises(dryrun.CannedExhausted):
        gateway.call("macro_ideation", [])
    with pytest.raises(dryrun.CannedExhausted):
        gateway.call_visual("whatever")


# --------------------------------------------------------------------------
# AC-09 干跑与 runs/ 分离
# --------------------------------------------------------------------------

def test_ac_p281_6_09_dryrun_separated(tmp_path):
    """干跑落在 ``<root>/dryrun/<stamp>/``，真 ``<root>/runs/`` 一个字节没动。"""
    summary = _dryrun(tmp_path)
    root = Path(summary["root"])
    assert root.parent == tmp_path / dryrun.DRYRUN_SUBDIR
    assert root.name == "20260101T000000Z"
    assert not (tmp_path / "runs").exists()
    assert (root / "runs" / M1 / "legacy_v1" / "rep0").is_dir()


# --------------------------------------------------------------------------
# 罐头候选本身：结构确实过得去（否则干跑等于什么都没跑）
# --------------------------------------------------------------------------

@pytest.mark.parametrize("task_id", list(constants.TASK_IDS))
def test_canned_candidates_are_well_formed(task_id):
    """三份罐头都写得出、编得过，且入口暴露了出生判定要的两个协议点。"""
    files = dryrun.files_for(task_id)
    assert files, task_id
    for rel, text in files.items():
        assert text.strip(), rel
        if rel.endswith(".py"):
            compile(text, rel, "exec")

    if task_id == "mujoco_contact_push_v1":
        scope = {}
        exec(compile(files["solution.py"], "solution.py", "exec"), scope)
        scene = scope["build_scene"]()
        assert isinstance(scene.action_dim, int) and scene.action_dim > 0
        result = scene.step([0.0] * scene.action_dim)
        numbers = [float(x) for x in _flatten(result)]
        assert numbers and all(_finite(n) for n in numbers)
        assert any(n != 0.0 for n in numbers)
    elif task_id == "libero_env_adapter_v1":
        scope = {}
        exec(compile(files["adapter.py"], "adapter.py", "exec"), scope)
        env = scope["build_env"]()
        assert isinstance(env.action_dim, int) and env.action_dim > 0
        assert env.observation_keys and all(isinstance(k, str)
                                            for k in env.observation_keys)
        env.reset(seed=0)
        assert env.step([0.0] * env.action_dim)


def _flatten(value):
    import math
    if isinstance(value, dict):
        for item in value.values():
            yield from _flatten(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _flatten(item)
    elif isinstance(value, bool):
        return
    elif isinstance(value, (int, float)):
        assert math.isfinite(float(value))
        yield value


def _finite(value: float) -> bool:
    import math
    return math.isfinite(value)


def test_legacy_and_agentic_replies_are_shaped_right():
    """旧引擎收补丁 JSON；agentic 收到一个 markdown python 块且自带 ``finish``。"""
    legacy = json.loads(dryrun.legacy_reply(M1))
    assert list(legacy) == ["patches"]
    assert [p["path"] for p in legacy["patches"]] == ["solution.py"]
    assert legacy["patches"][0]["old"] == ""

    reply = dryrun.agentic_reply(M1, seed="7")
    assert reply.startswith("```python")
    body = reply.split("```python", 1)[1].rsplit("```", 1)[0]
    compile(body, "step", "exec")
    assert "write_file(" in body and "run_cmd(" in body
    assert 'finish(answer=' in body


def test_agentic_reply_r1_writes_documents_without_running():
    """R1 是文档任务：只写四件、不自跑。"""
    body = dryrun.agentic_reply("icy_ground_dr_v1", seed="0")
    assert "write_file(path='experiment_plan.yaml'" in body
    assert "run_cmd(" not in body
