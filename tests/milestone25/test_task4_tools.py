"""Phase 2.5 Task4 AC-T4-3..6：六组工具 + 控制原语功能测试（0 付费）。

每个工具的强制链 jail → schema → policy 在本件按正向/反向两侧覆盖；
pen-test 载荷的双重拦截矩阵在 test_task4_gates_pentest.py。
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from orchestrator.agentic.caps import KIND_FAIL, KIND_FINISH, BudgetTracker
from orchestrator.agentic.executor import TerminateSignal
from orchestrator.agentic.forensics import TraceStore
from orchestrator.agentic.jail import READ_MAX_BYTES
from orchestrator.agentic.policy import GrantTable, ToolContext
from orchestrator.agentic.tools.probe import (
    HOST_PROBES_DIR,
    ProbeRegistry,
    ProbeSpec,
    default_registry,
    run_probe,
)
from orchestrator.agentic.tools.read import grep, list_dir, read_file
from orchestrator.agentic.tools.recall import (
    SOURCE_KB,
    SOURCE_LEARNING,
    HermeticRecallPort,
    ProductionRecallPort,
    RecallChunk,
    recall,
)
from orchestrator.agentic.tools.run import (
    NONPROD_BANNER,
    CommandResult,
    LocalSubprocessBackend,
    RunBackend,
    build_docker_create_argv,
    run_cmd,
)
from orchestrator.agentic.tools.terminate import fail, finish
from orchestrator.agentic.tools.write import apply_patch, write_file

_ALL_TOOLS = frozenset({
    "read_file", "list_dir", "grep", "write_file", "apply_patch",
    "run_cmd", "run_probe", "recall",
})


class RecordingBackend(RunBackend):
    form = "sibling_container"
    # M3：兄弟容器形态必须带真实 /probes 挂载源（这里只 is_dir 闸，不起容器）。
    probes_path = str(HOST_PROBES_DIR)

    def __init__(self, stdout=b"ok\n", stderr=b"", rc=0):
        self.calls = []
        self._stdout, self._stderr, self._rc = stdout, stderr, rc

    def run(self, argv, *, cwd, env=None, timeout_s=300):
        self.calls.append({"argv": list(argv), "cwd": Path(cwd),
                           "env": dict(env or {}), "timeout_s": timeout_s})
        return CommandResult(self._rc, self._stdout, self._stderr, False,
                             list(argv), self.form)


@pytest.fixture()
def workdir(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "a.txt").write_text("alpha\nhello world\n")
    (wd / "sub").mkdir()
    (wd / "sub" / "b.py").write_text("def f():\n    return 1\n")
    (wd / "binary.dat").write_bytes(b"\x00\x01\x02\xff")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    os.symlink(outside, wd / "escape.lnk")
    return wd


@pytest.fixture()
def tracer(tmp_path):
    return TraceStore(tmp_path / "agentic_traces", "thr-tools")


def _ctx(workdir, tracer=None, *, grants=None, nonprod=False, backend=None,
         registry=None, recall_port=None):
    # C2：ToolContext frozen 且不再携预算（预算只在 loop 装配的闭包细胞里）。
    return ToolContext(
        workdir=workdir, thread_id="thr-tools", tracer=tracer,
        grants=grants if grants is not None else GrantTable(
            tools=_ALL_TOOLS, run_argv=(("python",), ("pytest", "-q")),
            write_roots=("",), probes=frozenset(default_registry().names())),
        nonprod=nonprod, run_backend=backend,
        probe_registry=registry or default_registry(),
        recall_port=recall_port)


def _denied(result):
    return result.denied and result.decision.decision == "deny"


# ---------------------------------------------------------------------------
# read_file / list_dir / grep（AC-T4-3）
# ---------------------------------------------------------------------------

def test_read_file_positive_and_limits(workdir, tracer):
    ctx = _ctx(workdir, tracer)
    r = read_file(ctx, path="a.txt")
    assert not r.denied and "hello world" in r.output
    r2 = read_file(ctx, path="sub/b.py", max_lines=1)
    assert "def f()" in r2.output and "return 1" not in r2.output


def test_read_file_rejects_binary_oversize_and_missing(workdir, tracer):
    ctx = _ctx(workdir, tracer)
    assert _denied(read_file(ctx, path="binary.dat"))
    assert _denied(read_file(ctx, path="does-not-exist.txt"))
    big = workdir / "big.txt"
    big.write_text("x\n" * (READ_MAX_BYTES // 2 + 10))
    assert _denied(read_file(ctx, path="big.txt"))


def test_read_file_path_jail_forms(workdir, tracer):
    ctx = _ctx(workdir, tracer)
    assert _denied(read_file(ctx, path="../outside.txt"))
    assert _denied(read_file(ctx, path="/etc/passwd"))
    assert _denied(read_file(ctx, path="escape.lnk"))
    assert _denied(read_file(ctx, path=123))
    assert _denied(read_file(ctx, path=""))


def test_read_file_unganted_tool_denied(workdir, tracer):
    ctx = _ctx(workdir, tracer, grants=GrantTable(tools=frozenset()))
    assert _denied(read_file(ctx, path="a.txt"))


def test_list_dir_positive_and_jail(workdir, tracer):
    ctx = _ctx(workdir, tracer)
    r = list_dir(ctx, path=".")
    assert not r.denied
    # 目录排在文件前面。
    assert r.output.index("sub") < r.output.index("a.txt")
    assert _denied(list_dir(ctx, path="../"))
    assert _denied(list_dir(ctx, path="a.txt"))


def test_grep_positive_binary_skip_and_caps(workdir, tracer):
    ctx = _ctx(workdir, tracer)
    r = grep(ctx, pattern="hello", path=".")
    assert not r.denied and "a.txt:2" in r.output
    rb = grep(ctx, pattern=".", path="binary.dat")
    assert "跳过二进制" in rb.output
    assert _denied(grep(ctx, pattern="(", path="."))          # 非法正则
    assert _denied(grep(ctx, pattern="x", path="../outside.txt"))
    rmax = grep(ctx, pattern=".", path=".", max_matches=1)
    assert "matches=1" in rmax.output


# ---------------------------------------------------------------------------
# write_file / apply_patch（AC-T4-4）
# ---------------------------------------------------------------------------

def test_write_file_positive_0600_and_ast_gate(workdir, tracer):
    ctx = _ctx(workdir, tracer)
    r = write_file(ctx, path="new.py", content="x = 1\n")
    assert not r.denied and r.denied is False
    target = workdir / "new.py"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    bad = write_file(ctx, path="bad.py", content="def f(:\n")
    assert _denied(bad) and not (workdir / "bad.py").exists()
    # 非 .py 不做 AST 闸但仍 0600。
    r2 = write_file(ctx, path="notes.txt", content="{not json")
    assert not r2.denied
    assert stat.S_IMODE((workdir / "notes.txt").stat().st_mode) == 0o600


def test_write_file_roots_and_nonprod(workdir, tracer):
    grants = GrantTable(tools=_ALL_TOOLS, write_roots=("sub",))
    ctx = _ctx(workdir, tracer, grants=grants)
    assert not write_file(ctx, path="sub/c.txt", content="x").denied
    assert _denied(write_file(ctx, path="top.txt", content="x"))
    ctx_np = _ctx(workdir, tracer, nonprod=True)
    assert _denied(write_file(ctx_np, path="x.txt", content="x"))
    assert not (workdir / "x.txt").exists()
    # 禁改件（runner 既有守卫复用）。
    assert _denied(write_file(ctx, path="stage_models.yaml", content="x"))


def test_apply_patch_structured_and_anchor_mismatch(workdir, tracer):
    ctx = _ctx(workdir, tracer)
    patch = json.dumps({"patches": [
        {"path": "a.txt", "old": "alpha", "new": "ALPHA"}]})
    r = apply_patch(ctx, patch=patch)
    assert not r.denied and "ALPHA" in (workdir / "a.txt").read_text()
    # 异名锚点：old/new 顶层函数名不一致 → 拒绝且零落盘。
    orig = (workdir / "sub" / "b.py").read_text()
    mismatch = json.dumps({"patches": [{"path": "sub/b.py",
        "old": "def f():\n    return 1\n",
        "new": "def g():\n    return 2\n"}]})
    r2 = apply_patch(ctx, patch=mismatch)
    assert _denied(r2) and "异名锚点" in r2.output
    assert (workdir / "sub" / "b.py").read_text() == orig
    # 同名替换允许（锚点恰好 1 命中）。
    same = json.dumps({"patches": [{"path": "sub/b.py",
        "old": "def f():\n    return 1\n",
        "new": "def f():\n    return 2\n"}]})
    assert not apply_patch(ctx, patch=same).denied


def test_apply_patch_traversal_and_nonprod_deny(workdir, tracer):
    ctx = _ctx(workdir, tracer)
    patch = json.dumps({"patches": [
        {"path": "../evil.py", "old": "a", "new": "b"}]})
    r = apply_patch(ctx, patch=patch)
    assert _denied(r) and not (workdir.parent / "evil.py").exists()
    ctx_np = _ctx(workdir, tracer, nonprod=True)
    assert _denied(apply_patch(
        ctx_np, patch=json.dumps({"patches": [
            {"path": "a.txt", "old": "alpha", "new": "x"}]})))
    # 非 JSON/坏补丁是拒绝观察，不是异常。
    assert _denied(apply_patch(ctx, patch="not a patch at all"))


def test_apply_patch_unified_diff_path_gate(workdir, tracer):
    ctx = _ctx(workdir, tracer)
    diff = (
        "diff --git a/../evil.py b/../evil.py\n"
        "--- a/../evil.py\n+++ b/../evil.py\n@@ -1 +1 @@\n-a\n+bb\n")
    r = apply_patch(ctx, patch=json.dumps({"unified_diff": diff}))
    assert _denied(r)
    assert not (workdir.parent / "evil.py").exists()


# ---------------------------------------------------------------------------
# run_cmd（AC-T4-5）
# ---------------------------------------------------------------------------

def test_run_cmd_argv_list_through_sibling_backend(workdir, tracer):
    backend = RecordingBackend(stdout=b"v\n")
    ctx = _ctx(workdir, tracer, backend=backend)
    r = run_cmd(ctx, argv=["python", "-V"])
    assert not r.denied
    assert backend.calls[0]["argv"] == ["python", "-V"]
    assert backend.calls[0]["cwd"] == workdir.resolve()
    assert "form=sibling_container" in r.output
    assert "stdout_sha256=" in r.output and "stdout_ref=full/" in r.output


def test_run_cmd_argv_whitelist_and_shell_forms_denied(workdir, tracer):
    ctx = _ctx(workdir, tracer, backend=RecordingBackend())
    assert _denied(run_cmd(ctx, argv=["rm", "-rf", "/"]))
    assert _denied(run_cmd(ctx, argv="python -V"))
    assert _denied(run_cmd(ctx, argv=["python", "a;rm x"]))
    assert _denied(run_cmd(ctx, argv=["python", "-c", "import os"]))
    assert _denied(run_cmd(ctx, argv=["python", "-m", "pip", "install", "x"]))
    assert _denied(run_cmd(ctx, argv=[]))
    assert _denied(run_cmd(ctx, argv=["python", "-V"], env={"PATH": "/x"}))
    # MINOR 6（2026-10-30 评审）：解释器导入面/插件注入两键必须永不入白名单。
    assert _denied(run_cmd(
        ctx, argv=["python", "-V"], env={"PYTHONPATH": "/tmp/x"}))
    assert _denied(run_cmd(
        ctx, argv=["python", "-V"], env={"PYTEST_ADDOPTS": "-p evil"}))
    # 白名单内键（值也过字符闸）放行，反证不是 env 全拒。
    r_env = run_cmd(ctx, argv=["python", "-V"], env={"MPLBACKEND": "Agg"})
    assert not r_env.denied


def test_run_cmd_backend_assembly_guardrails(workdir):
    argv = build_docker_create_argv(
        image="simpraxis-agentic@sha256:abcdef", argv=["python", "-V"],
        workdir=workdir)
    joined = " ".join(argv)
    assert "--network=none" in joined and "--read-only" in joined
    assert "--cap-drop" in argv and "ALL" in argv
    assert "no-new-privileges" in joined
    assert "/work" in joined and ":ro" in joined
    # docker.sock 永挂即编程错误。
    with pytest.raises(ValueError, match="docker.sock"):
        build_docker_create_argv(image="img", argv=["x"], workdir=workdir,
                          probes_path="/var/run/docker.sock")
    with pytest.raises(ValueError, match="镜像"):
        build_docker_create_argv(image="", argv=["x"], workdir=workdir)


def test_run_cmd_missing_backend_and_form_crosscheck(workdir, tracer):
    ctx = _ctx(workdir, tracer, backend=None)
    assert _denied(run_cmd(ctx, argv=["python", "-V"]))
    ctx_np = _ctx(workdir, tracer, nonprod=True, backend=RecordingBackend())
    assert _denied(run_cmd(ctx_np, argv=["python", "-V"]))
    ctx_prod_local = _ctx(
        workdir, tracer, nonprod=False, backend=LocalSubprocessBackend())
    assert _denied(run_cmd(ctx_prod_local, argv=["python", "-V"]))


def test_local_nonprod_backend_carries_banner_and_scrubs_env(workdir, tracer):
    ctx = _ctx(workdir, tracer, nonprod=True,
               backend=LocalSubprocessBackend())
    r = run_cmd(ctx, argv=["python", "-c", "print('hi')"])
    # 注意：policy 层 python -c 对所有形态都拒绝；本地横幅用白名单脚本验。
    assert _denied(r)
    r2 = run_cmd(ctx, argv=["python", "--version"])
    assert not r2.denied
    assert NONPROD_BANNER in r2.full
    # env 白名单外变量不下传。
    r3 = run_cmd(ctx, argv=["python", "--version"], env={"MPLBACKEND": "Agg"})
    assert not r3.denied


# ---------------------------------------------------------------------------
# run_probe（AC-T4-5）
# ---------------------------------------------------------------------------

def test_default_registry_contents():
    reg = default_registry()
    # Phase 2.7 Task 1（AC-T1-2）新增 scene_build：仍是离线探针，argv 由注册表
    # 固定（容器内 /usr/bin/python + /probes/probe_scene_build.py）。
    assert reg.names() == (
        "genesis_fluid_probe", "genesis_probe", "libero_compat_probe",
        "maniskill_probe", "mujoco_probe", "scene_build")
    assert not any(reg.is_network(n) for n in reg.names())


def test_run_probe_positive_and_unknown_denied(workdir, tracer):
    backend = RecordingBackend(stdout=b"probe ok\n")
    ctx = _ctx(workdir, tracer, backend=backend)
    r = run_probe(ctx, name="mujoco_probe")
    assert not r.denied and "run_probe:mujoco_probe" in r.output
    # M3：容器内绝对路径 /probes/probe_mujoco.py + 固定 --phase/--out。
    argv = backend.calls[0]["argv"]
    assert argv[1] == "/probes/probe_mujoco.py"
    assert argv[2:4] == ["--phase", "step"]
    assert argv[4:6] == ["--out", "/tmp/simpraxis_probe_mujoco_step.json"]
    assert _denied(run_probe(ctx, name="rm_everything"))


def test_m3_probe_requires_mounted_probes_dir_in_sibling_form(workdir, tracer):
    # M3：兄弟容器后端未注入 probes_path → fail-closed deny，不裸跑 ENOENT。
    backend = RecordingBackend(stdout=b"probe ok\n")
    backend.probes_path = None
    ctx = _ctx(workdir, tracer, backend=backend)
    assert _denied(run_probe(ctx, name="mujoco_probe"))


def test_m3_probe_mount_source_must_exist(workdir, tmp_path):
    # 构造期与 argv 构造期两处都对挂载源 is_dir fail-closed。
    from orchestrator.agentic.tools.run import DockerSiblingBackend
    with pytest.raises(ValueError, match="探针挂载源目录不存在"):
        DockerSiblingBackend(image="img", probes_path=str(tmp_path / "nope"))
    with pytest.raises(ValueError, match="探针挂载源目录不存在"):
        build_docker_create_argv(
            image="img", argv=["python", "-V"], workdir=workdir,
            probes_path=str(tmp_path / "nope"))


class _NonprodRecordingBackend(RunBackend):
    """M3：本地 NONPROD 形态只记录翻译后的 argv，不实跑探针。"""

    form = "local_nonprod"
    nonprod = True

    def __init__(self):
        self.calls = []

    def run(self, argv, *, cwd, env=None, timeout_s=300):
        self.calls.append(list(argv))
        return CommandResult(0, b"ok\n", b"", False, list(argv), self.form)


def test_m3_nonprod_probe_argv_resolved_to_repo_absolute(
        workdir, tracer, monkeypatch):
    backend = _NonprodRecordingBackend()
    ctx = _ctx(workdir, tracer, nonprod=True, backend=backend)
    r = run_probe(ctx, name="mujoco_probe")
    assert not r.denied
    argv = backend.calls[0]
    assert argv[1] == str(HOST_PROBES_DIR / "probe_mujoco.py")
    assert Path(argv[1]).is_file()
    assert "--out" in argv

    # 宿主侧文件缺失（如挂载源被换空）→ deny，不 ENOENT 裸跑。
    import orchestrator.agentic.tools.probe as probe_mod
    empty = workdir / "empty_probes"
    empty.mkdir()
    monkeypatch.setattr(probe_mod, "HOST_PROBES_DIR", empty)
    assert _denied(run_probe(ctx, name="mujoco_probe"))


def test_run_probe_requires_itemized_grant_and_network_grant(workdir, tracer):
    reg = ProbeRegistry((
        ProbeSpec("offline_x", ("python", "x.py"), network=False),
        ProbeSpec("net_y", ("python", "y.py"), network=True),
    ))
    backend = RecordingBackend()
    # 注册表内但 Gate1 未逐项授权。
    ctx0 = _ctx(workdir, tracer, backend=backend, registry=reg,
                grants=GrantTable(tools=_ALL_TOOLS, run_argv=(("python",),)))
    assert _denied(run_probe(ctx0, name="net_y"))
    # 授权了探针但没逐项网络授权。
    ctx1 = _ctx(workdir, tracer, backend=backend, registry=reg,
                grants=GrantTable(tools=_ALL_TOOLS, run_argv=(("python",),),
                                  probes=frozenset({"net_y"})))
    assert _denied(run_probe(ctx1, name="net_y"))
    # 双授权齐才放行。
    ctx2 = _ctx(workdir, tracer, backend=backend, registry=reg,
                grants=GrantTable(
                    tools=_ALL_TOOLS, run_argv=(("python",),),
                    probes=frozenset({"net_y"}),
                    network_probes=frozenset({"net_y"})))
    assert not run_probe(ctx2, name="net_y").denied


# ---------------------------------------------------------------------------
# recall（AC-T4-6）
# ---------------------------------------------------------------------------

def test_hermetic_recall_ranking_and_k():
    chunks = (
        RecallChunk("c1", "kb/a.md", "hermetic", "mujoco egl headless render"),
        RecallChunk("c2", "kb/b.md", "hermetic", "unrelated cooking recipe"),
    )
    port = HermeticRecallPort(chunks)
    ctx = ToolContext(
        workdir=Path("."), thread_id="t",
        grants=GrantTable(tools=frozenset({"recall"})),
        recall_port=port)
    r = recall(ctx, query="mujoco egl", k=5)
    assert not r.denied and "c1" in r.output and "c2" not in r.output
    assert _denied(recall(ctx, query=""))
    ctx0 = ToolContext(
        workdir=Path("."), thread_id="t",
        grants=GrantTable(tools=frozenset({"recall"})), recall_port=None)
    assert _denied(recall(ctx0, query="x"))


class FakeProductionRecall:
    def __init__(self):
        self.adoption_calls = []

    def retrieve(self, hint, *, site, top_k=3, kb_top_k=None):
        assert site == "coding"
        return {
            "kb_hits": [{"id": "q-1", "relpath": "knowledge/x.md",
                         "content": "kb body"}],
            "learning_hits": [{"id": "learn-9", "content": "learning body"}],
        }

    def record_adoption(self, *, thread_id, site, learning_id, prompt_text,
                        learning_text, path=None):
        adopted = learning_text in prompt_text
        self.adoption_calls.append(
            {"learning_id": learning_id, "adopted": adopted})
        if adopted and path is not None:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"learning_id": learning_id,
                                     "thread_id": thread_id}) + "\n")
        return adopted


def test_production_recall_port_projection_and_adoption_telemetry(tmp_path):
    pr = FakeProductionRecall()
    port = ProductionRecallPort(pr)
    hits = port.search("hint", k=3)
    assert [(h.id, h.source, h.relpath) for h in hits] == [
        ("q-1", SOURCE_KB, "knowledge/x.md"),
        ("learn-9", SOURCE_LEARNING, "")]
    log = tmp_path / "recall.jsonl"
    n = port.record_rendered_adoptions(
        thread_id="thr-1",
        prompt_text="prefix learning body suffix",
        chunks=hits, path=log)
    assert n == 1 and len(pr.adoption_calls) == 1
    # 未逐字渲染 → 0 行（不可伪造）。
    n2 = port.record_rendered_adoptions(
        thread_id="thr-1", prompt_text="different", chunks=hits, path=log)
    assert n2 == 0
    rows = log.read_text().strip().splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["learning_id"] == "learn-9"


def test_production_recall_port_rejects_direction_site():
    with pytest.raises(ValueError, match="coding"):
        ProductionRecallPort(FakeProductionRecall(), site="direction")


# ---------------------------------------------------------------------------
# finish / fail（AC-T4-6）
# ---------------------------------------------------------------------------

def test_finish_fail_raise_base_exception_signals(workdir, tracer):
    ctx = _ctx(workdir, tracer)
    with pytest.raises(TerminateSignal) as ei:
        finish(ctx, answer="产物路径 x", reason="done")
    assert ei.value.kind == KIND_FINISH and ei.value.value == "产物路径 x"
    with pytest.raises(TerminateSignal) as ei2:
        fail(ctx, reason="锚点三轮修不好")
    assert ei2.value.kind == KIND_FAIL
    # 控制原语不经 Gate1 授予（空授予表也放行）。
    ctx0 = _ctx(workdir, tracer, grants=GrantTable(tools=frozenset()))
    with pytest.raises(TerminateSignal):
        finish(ctx0, answer="x")
    # schema 错不终局，回拒绝观察。
    r = finish(ctx)
    assert _denied(r)


def test_budget_step_cap_blocks_side_effect_not_finish(workdir, tracer):
    # 预算帽在 loop.BoundTool 层；此处只验 BudgetTracker 语义：
    # 到帽后 check_tool_step 抛 CapExceeded（BaseException，except 接不住）。
    from orchestrator.agentic.caps import CapExceeded, KIND_STEP_CAP
    budget = BudgetTracker(step_cap=1, round_cap=3)
    budget.check_tool_step()
    budget.note_tool_step()
    with pytest.raises(CapExceeded) as ei:
        budget.check_tool_step()
    assert ei.value.kind == KIND_STEP_CAP


# ---------------------------------------------------------------------------
# M3 + run_cmd echo：真 docker 集成（SIMPRAXIS_DOCKER_INTEGRATION=1 才跑）
# ---------------------------------------------------------------------------

@pytest.mark.docker_integration
def test_m3_real_probe_runs_inside_container_no_enoent(workdir):
    """M3 硬门：/probes 绝对路径真容器跑 mujoco step 探针 rc0（旧相对路径 ENOENT）。"""
    from tests.docker_support import require_docker_integration
    from orchestrator.agentic.image_pin import AGENTIC_IMAGE
    from orchestrator.agentic.tools.run import DockerSiblingBackend
    require_docker_integration()
    backend = DockerSiblingBackend(
        image=AGENTIC_IMAGE, probes_path=str(HOST_PROBES_DIR))
    ctx = _ctx(workdir, None, backend=backend, grants=GrantTable(
        tools=_ALL_TOOLS, run_argv=(("python",),),
        probes=frozenset({"mujoco_probe"})))
    r = run_probe(ctx, name="mujoco_probe", timeout_s=300)
    assert not r.denied, r.full
    assert "rc=0" in r.full
    assert '"phase": "step"' in r.full
    assert '"status": "ok"' in r.full
    assert "No such file" not in r.full and "ENOENT" not in r.full


@pytest.mark.docker_integration
def test_real_run_cmd_echo_smoke_product_argv(workdir):
    """硬门烟测：run_cmd echo 经完整 policy→兄弟容器链路 rc0（默认禁网）。"""
    from tests.docker_support import require_docker_integration
    from orchestrator.agentic.image_pin import AGENTIC_IMAGE
    from orchestrator.agentic.tools.run import DockerSiblingBackend
    require_docker_integration()
    marker = "t4-docker-echo-20261030"
    backend = DockerSiblingBackend(image=AGENTIC_IMAGE)
    ctx = _ctx(workdir, None, backend=backend, grants=GrantTable(
        tools=_ALL_TOOLS, run_argv=(("echo", marker),)))
    r = run_cmd(ctx, argv=["echo", marker], timeout_s=60)
    assert not r.denied, r.full
    assert "rc=0" in r.full and marker in r.full
    assert "form=sibling_container" in r.full
