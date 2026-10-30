"""Phase 2.5 Task4 D-3 判决守卫：AC-T4-14..16 形式 N/A，AC-T4-17 本地部分适用。

Task0 落位判决预裁为**主 venv 形态**（branch=main_venv：smolagents 1.26.0
零 extra 装入主 venv，兄弟容器只是 relabel 基础镜像 + 只读挂载 /opt/venv）。
因此独立 venv/常驻侧车及其 IPC 线协议（sidecar.v1）、执行侧重算签名、
step 键双侧归属三条 AC 形式上不适用——本件把这个判决**锁成测试**：判决件
与活 venv 零漂移、产品树不存在侧车协议面、设计文档不存在；若未来改判
独立 venv，本件全红、必须先补《侧车设计文档》再开 AC-T4-14..16。

AC-T4-17 仍适用的两半：本地形态必须打 NONPROD 标且 policy 拒写；
「侧车死亡签名」在一次性兄弟容器形态下的等价边界是 rc/timeout 结构化
暴露（不吞异常），进程级死亡由 Task 2 双签名看门狗覆盖。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.agentic.image_pin import SMOLAGENTS_PIN
from orchestrator.agentic.policy import GrantTable, PreToolUsePolicy, ToolContext
from orchestrator.agentic.tools.run import (
    NONPROD_BANNER,
    DockerSiblingBackend,
    LocalSubprocessBackend,
    build_docker_create_argv,
    check_backend,
    execute_render,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKER_DIR = REPO_ROOT / "sandbox" / "docker"
VERDICT = Path(
    "/mnt/artifacts/simpraxis/phase25/estimates/t0_dependency_verdict.json")
SIX_LIGHTS = (
    "huggingface-hub", "requests", "rich", "jinja2", "pillow",
    "python-dotenv",
)


@pytest.fixture(scope="module")
def verdict():
    assert VERDICT.is_file(), (
        "D-3 判决件缺失——独立/主 venv 形态无预裁，Task4 不得自行选形态")
    return json.loads(VERDICT.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# D-3 预裁：主 venv 形态（AC-T4-14..16 的形式 N/A 以此件为唯一依据）
# ---------------------------------------------------------------------------

def test_d3_verdict_pins_main_venv_branch(verdict):
    assert verdict["schema"] == "simpraxis.phase25.t0_dependency_verdict.v1"
    assert verdict["decision"]["branch"] == "main_venv"
    assert verdict["decision"]["reason"].strip()
    target = verdict["target"]
    assert target["name"] == "smolagents"
    assert target["version"] == SMOLAGENTS_PIN == "1.26.0"
    assert target["install_mode"] == "zero-extras (--no-deps)"
    assert target["present_after"] is True
    # 主 venv 形态：判决时未执行任何安装（只有零 extra 预装事实被记录）。
    assert verdict["install"]["performed"] is False
    assert verdict["install"]["already_present_correct_version"] is True


def test_live_venv_has_zero_drift_against_verdict(verdict):
    from importlib.metadata import PackageNotFoundError, version
    # 活 venv 的 smolagents 与六灯版本必须逐字等于判决件——改判先改证据。
    assert version("smolagents") == verdict["target"]["version"]
    for name in SIX_LIGHTS:
        rec = verdict["six_lights"][name]
        assert rec["unchanged"] is True
        assert rec["in_full_dry_run_would_install"] is False
        try:
            live = version(name)
        except PackageNotFoundError:
            pytest.fail(f"活 venv 缺少六灯包 {name}（判决件记录 {rec['installed_after']}）")
        assert live == rec["installed_after"], (
            f"{name} 活版本 {live} 偏离 D-3 判决 {rec['installed_after']}")


def test_sidecar_wire_protocol_and_design_doc_do_not_exist():
    # AC-T4-14..16 的载体（sidecar.v1 / IPC / 常驻侧车模块 / 设计文档）
    # 在主 venv 形态下必须整体不存在——出现即形态被擅自切换。
    product = list((REPO_ROOT / "orchestrator").rglob("*.py"))
    assert product, "产品树缺失（路径假设失效）"
    for py in product:
        text = py.read_text(encoding="utf-8")
        assert "sidecar.v1" not in text, f"{py} 出现 sidecar.v1 协议串"
        assert "AF_UNIX" not in text and "AF_INET" not in text, (
            f"{py} 出现 socket 族（侧车 IPC 形态才允许）")
    agentic = REPO_ROOT / "orchestrator" / "agentic"
    for py in agentic.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        # run.py 用 subprocess 调 docker CLI（宿主控制面），但永不 import socket。
        assert "import socket" not in text and "socket." not in text, (
            f"{py} agentic 包不得建 socket（无侧车 IPC 面）")
    for py in agentic.rglob("*.py"):
        assert "sidecar" not in py.name.lower()
    assert not (REPO_ROOT / "docs" / "design" / "sidecar_design.md").exists()


def test_image_is_relabel_with_venv_mount_not_a_sidecar_image(verdict):
    # 镜像侧证据与 D-3 一致：零 wheel + 只读主 venv 挂载，无独立侧车环境。
    manifest = json.loads(
        (DOCKER_DIR / "agentic_wheel_manifest.json").read_text("utf-8"))
    assert manifest["wheels"] == []
    mp = manifest["mounted_python"]
    assert mp["host_path"] == "/mnt/venvs/simpraxis"
    assert mp["mount_mode"] == "ro"
    assert mp["smolagents_version"] == verdict["target"]["version"]


# ---------------------------------------------------------------------------
# AC-T4-17（本地形态适用的两半）：NONPROD 标记 + policy 拒写 + 形态互斥
# ---------------------------------------------------------------------------

def test_nonprod_banner_present_in_local_form_output():
    assert "NONPROD" in NONPROD_BANNER and "AC-T4-17" in NONPROD_BANNER
    assert LocalSubprocessBackend.form == "local_nonprod"
    assert LocalSubprocessBackend.nonprod is True
    assert DockerSiblingBackend.form == "sibling_container"
    assert DockerSiblingBackend.nonprod is False

    def fake_runner(cmd, **kwargs):
        assert kwargs["shell"] is False          # 本地形态同样永不经过 shell
        return SimpleNamespace(returncode=0, stdout=b"hello\n", stderr=b"e\n")

    result = LocalSubprocessBackend(runner=fake_runner).run(
        ["true"], cwd=Path("/tmp"), env={}, timeout_s=5)
    assert result.form == "local_nonprod"
    assert result.stderr.startswith(NONPROD_BANNER.encode("utf-8")), (
        "缺 NONPROD 横幅的本地 run 必须让测试变红（AC-T4-17）")
    assert result.stdout == b"hello\n"


def test_nonprod_policy_denies_all_write_tools(tmp_path):
    grants = GrantTable(
        tools=frozenset({"write_file", "apply_patch", "read_file"}),
        write_roots=("",))
    policy = PreToolUsePolicy(grants, workdir=tmp_path, nonprod=True)
    target = tmp_path / "x.py"
    for tool in ("write_file", "apply_patch"):
        d = policy.check(tool, {}, resolved_path=target)
        assert d.decision == "deny" and "NONPROD" in d.reason
    # 只读工具在 NONPROD 下不被连坐。
    assert policy.check("read_file", {}).decision == "allow"


def test_backend_form_and_context_cross_check(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    local, sibling = LocalSubprocessBackend(), DockerSiblingBackend(
        image="sim-praxis/agentic:x")
    ctx_local = ToolContext(workdir=wd, grants=GrantTable(),
                            thread_id="t", nonprod=True, run_backend=sibling)
    # nonprod 上下文不得借兄弟容器后端绕过拒写面。
    r = check_backend(ctx_local)
    assert r.denied and "LocalSubprocessBackend" in r.output
    ctx_prod = ToolContext(workdir=wd, grants=GrantTable(),
                           thread_id="t", nonprod=False, run_backend=local)
    r = check_backend(ctx_prod)
    assert r.denied and "NONPROD" in r.output


# ---------------------------------------------------------------------------
# 「侧车死亡签名」的一次性容器等价边界 + Task2 看门狗承接
# ---------------------------------------------------------------------------

class _TimeoutRunner:
    """M2：create 成功返容器 ID，start -a -i 超时；rm <id> + label 扫尾被记录。"""

    CONTAINER_ID = "deadbeefcafef00d"

    def __init__(self):
        self.cleanups = []

    def __call__(self, cmd, **kwargs):
        if cmd[:2] == ["docker", "create"]:
            return SimpleNamespace(
                returncode=0, stdout=(self.CONTAINER_ID + "\n").encode(),
                stderr=b"")
        if cmd[:2] == ["docker", "start"]:
            raise subprocess.TimeoutExpired(
                cmd=cmd, timeout=kwargs.get("timeout"),
                output=b"partial", stderr=b"late")
        # 清理路径（docker rm -f <id>；docker ps -q --filter label=...）。
        self.cleanups.append(cmd)
        if cmd[:2] == ["docker", "ps"]:
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


def test_sibling_container_is_one_shot_and_timeout_surfaces_rc124(tmp_path):
    runner = _TimeoutRunner()
    backend = DockerSiblingBackend(
        image="sim-praxis/agentic:x", runner=runner)
    result = backend.run(["python", "-V"], cwd=tmp_path, timeout_s=1)
    # 无常驻侧车：create --rm 一次性容器；超时不吞异常，rc=124 + timed_out。
    assert "--rm" in result.argv[:4]
    assert result.rc == 124 and result.timed_out is True
    assert result.form == "sibling_container"
    # M2：先按记录的容器 ID 精确 docker rm -f <id>（永无 --filter 死命令），
    # 再 docker ps -q --filter label=<run label> 兜底扫尾；全 argv 列表。
    assert runner.cleanups[0] == [
        "docker", "rm", "-f", _TimeoutRunner.CONTAINER_ID]
    assert runner.cleanups[1][:2] == ["docker", "ps"]
    assert "--filter" in runner.cleanups[1]
    assert any(a.startswith("label=simpraxis-agentic-run=")
               for a in runner.cleanups[1])
    # 旧死命令 docker rm -f --filter=... 不得再出现：rm 只吃位置 ID。
    rm_cmds = [c for c in runner.cleanups if c[:2] == ["docker", "rm"]]
    assert rm_cmds and all(
        not any(a.startswith("--filter") for a in c) for c in rm_cmds)


class _RecordingBackend:
    form = "sibling_container"
    nonprod = False

    def run(self, argv, *, cwd, env=None, timeout_s=300):
        from orchestrator.agentic.tools.run import CommandResult
        return CommandResult(7, b"bad\n", b"err\n", False, list(argv), self.form)


def test_nonzero_rc_rendered_not_denied(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    ctx = ToolContext(workdir=wd, grants=GrantTable(), thread_id="t",
                      run_backend=_RecordingBackend())
    r = execute_render(ctx, ["python", "x.py"], tag="run_cmd", timeout_s=5)
    # 进程死亡/非零退出必须原样到达模型观察（不得静默成 allow/deny 之外）。
    assert r.denied is False and r.decision.decision == "allow"
    assert "rc=7" in r.output and "form=sibling_container" in r.output
    assert "stdout_sha256=" in r.output


def test_process_level_death_owned_by_task2_watchdog():
    # 没有侧车心跳桥：四类死亡签名（消失/超时/非零/健康探针失败）的进程级
    # 覆盖由 Task 2 双签名看门狗承接，件必须在位。
    ops = REPO_ROOT / "scripts" / "ops"
    watched = ops / "run_watched.sh"
    loop_sh = ops / "watch_loop.sh"
    assert watched.is_file() and loop_sh.is_file()
    assert watched.read_text(encoding="utf-8").count("PDEATHSIG") >= 1, (
        "内核同死（PDEATHSIG）是进程死亡签名的承接件，缺失即 AC-T4-17 无主")


# ---------------------------------------------------------------------------
# M2 真 docker 集成（SIMPRAXIS_DOCKER_INTEGRATION=1 才跑；裸跑 skip）
# ---------------------------------------------------------------------------

@pytest.mark.docker_integration
def test_m2_real_timeout_container_is_reaped(tmp_path):
    """M2 硬门：sleep 60 + timeout_s=1 → rc124，且 run label 下零容器残留。"""
    from tests.docker_support import require_docker_integration
    from orchestrator.agentic.image_pin import AGENTIC_IMAGE
    require_docker_integration()
    work = tmp_path / "work"
    work.mkdir()
    backend = DockerSiblingBackend(image=AGENTIC_IMAGE)
    result = backend.run(["sleep", "60"], cwd=work, timeout_s=1)
    assert result.rc == 124 and result.timed_out is True
    # 产品 argv 旗标（create 形态；旧 docker run 形态已废弃）。
    argv = result.argv
    assert argv[:4] == ["docker", "create", "--rm", "-i"]
    for flag in ("--network=none", "--read-only", "--cap-drop", "--user",
                 "--security-opt", "--pids-limit", "--memory"):
        assert flag in argv, flag
    assert "ALL" in argv and "1000:1000" in argv
    assert "no-new-privileges" in argv
    assert not any(a == "--filter" for a in argv[:6])
    # rm -f 后给 daemon 一拍；run label 下任何状态容器都必须为零。
    import time
    time.sleep(1)
    ps = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "label=simpraxis-agentic-run"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
    assert ps.returncode == 0
    assert ps.stdout.decode().strip() == "", (
        f"M2 孤儿容器残留：{ps.stdout.decode()!r}")
