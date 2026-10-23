#!/usr/bin/env python3
"""Task6 run7：host-shim（sitecustomize Tensor 主机迁移托底）hermetic 测试。

权威契约：.omc/handoffs/task6-run7-host-shim-design.md v1.1a（§2/§3/§6）。
零 GPU/零联网/零付费：shim 单测主形态＝真子进程 + PYTHONPATH 前拼 prelude
+ 临时伪 torch 包（真 site 机制、真链式加载）；Gate2 机验走合成 timeline；
G3 探针走 tmp 合成 checkpoint 副本（只读 sqlite/msgpack，不碰活库）。

正则与 /tmp/g3_shim_evidence_probe.py v1.1a 逐字同一对（另有机验防漂移 pin
断言 task6 模块内编译式 pattern 串与本件一致）。
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import msgpack
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PRELUDE = REPO_ROOT / "orchestrator" / "common" / "host_shim"
BOOTSTRAP = PRELUDE / "sitecustomize.py"
CORE_PATH = PRELUDE / "shim_core.py"
# H-1（提交前 LEAD 冷读，终态六装饰器：G3×2/rehang×3/冻结规格 handoff×1）：
# 下列三件均为**不入库**操作件——干净检出/重启清 /tmp（或 .omc 操作件缺失）
# 后缺失即诚实 skip，绝不依赖本地脏状态才绿（硬规则 7）。
# G3/rehang 见冻结规格 §9「rehang 脚本与 G3 探针不入库」（§7 标题重申）。
G3_PROBE = Path("/tmp/g3_shim_evidence_probe.py")
REHANG_SCRIPT = Path("/tmp/rehang_task6_run7.py")
FROZEN_SPEC_HANDOFF = (
    REPO_ROOT / ".omc" / "handoffs" / "task6-run7-host-shim-design.md")

_G3_PROBE_PRESENT = G3_PROBE.is_file()
_REHANG_PRESENT = REHANG_SCRIPT.is_file()
_FROZEN_SPEC_PRESENT = FROZEN_SPEC_HANDOFF.is_file()

# skip reason 逐字引规格条款。
_SKIP_NO_G3 = pytest.mark.skipif(
    not _G3_PROBE_PRESENT,
    reason="run7 G3 证据探针不入库（冻结规格 §9：rehang 脚本与 G3 探针"
           "不入库），仅在操作件在位时干跑")
_SKIP_NO_REHANG = pytest.mark.skipif(
    not _REHANG_PRESENT,
    reason="run7 rehang 手术脚本不入库（冻结规格 §7「脚本已实现并干跑绿"
           "/tmp/rehang_task6_run7.py，不入库」、§9 重申），仅在操作件"
           "在位时干跑")
_SKIP_NO_FROZEN_SPEC = pytest.mark.skipif(
    not _FROZEN_SPEC_PRESENT,
    reason="run7 冻结规格 handoff 属 .omc 操作件（git-ignored，不入库；"
           "冻结规格 §9 提交文件清单不含），仅在操作件在位时对账票件 schema")

MARKER_ENV = "SIMPRAXIS_HOST_SHIM"

# 与 G3 探针 v1.1a 逐字同一对（禁裸 site=torch 子串：9 banner+9 atexit=18）。
BANNER_RE = re.compile(r"\[simpraxis-host-shim v1\] 平台托底激活")
ATEXIT_RE = re.compile(
    r"\[simpraxis-host-shim v1\] 平台托底（非模型代码）[^\n]*"
    r"site=torch[^\n]*?"
    r"array_migrated=(\d+) numpy_migrated=(\d+) cpu_grad_detached=(\d+)")
ATEXIT_IDLE_RE = re.compile(
    r"\[simpraxis-host-shim v1\] 平台托底（非模型代码）[^\n]*site=idle")
FFMPEG_LINE_RE = re.compile(r"(?mi)^.*ffmpeg.*$")

SIX_FORBIDDEN = ("SIMPRAXIS_HOST_SHIM", "simpraxis-host-shim", "host_shim",
                 "平台托底", "sitecustomize", "shim_core")

# 真 torch 2.4.1 cuda→numpy 报错逐字串（S1 对照用）。
TORCH_CUDA_NUMPY_TYPEERROR = (
    "TypeError: can't convert cuda:0 device type tensor to numpy. "
    "Use Tensor.cpu() to copy the tensor to host memory first.")


# ---------------------------------------------------------------------------
# 合成夹具与子进程工具
# ---------------------------------------------------------------------------

FAKE_TORCH_PY = '''
"""hermetic 伪 torch：cuda 张量 numpy() 抛与真 torch 逐字相同 TypeError。"""
__version__ = "2.4.1-fake"


class Tensor:
    def __init__(self, is_cuda=False, requires_grad=False, value=1.0):
        self.is_cuda = is_cuda
        self.requires_grad = requires_grad
        self._v = value
        self.cpu_called = 0
        self.detach_called = 0

    def detach(self):
        self.detach_called += 1
        return Tensor(False, False, self._v)

    def cpu(self):
        self.cpu_called += 1
        return Tensor(False, False, self._v)

    def numpy(self, *a, **kw):
        if self.is_cuda:
            raise TypeError(
                "can't convert cuda:0 device type tensor to numpy. "
                "Use Tensor.cpu() to copy the tensor to host memory first.")
        import numpy as np
        return np.array(self._v, dtype="float32")

    def __array__(self, dtype=None):
        arr = self.numpy()
        return arr.astype(dtype, copy=False) if dtype is not None else arr
'''

FAKE_GENESIS_PY = '''
"""hermetic 伪 genesis：包内 import torch（模拟 V2 惰性导入点）。"""
import torch  # noqa: F401
'''

_IMPOSTOR_TMPL = '''
"""冒名桩：被执行即落 sentinel 并打印标记（零副作用期望＝永不出现）。"""
import pathlib
import sys
pathlib.Path(__file__).parent.joinpath({sentinel!r}).write_text(
    "1", encoding="utf-8")
sys.stderr.write({marker!r})
'''


def _write_fake_torch(pkgdir: Path) -> Path:
    torch_dir = pkgdir / "torch"
    torch_dir.mkdir(parents=True, exist_ok=True)
    (torch_dir / "__init__.py").write_text(FAKE_TORCH_PY, encoding="utf-8")
    return pkgdir


def _write_fake_genesis(pkgdir: Path) -> Path:
    gdir = pkgdir / "genesis"
    gdir.mkdir(parents=True)
    (gdir / "__init__.py").write_text(FAKE_GENESIS_PY, encoding="utf-8")
    return pkgdir


def _write_impostor(path: Path, sentinel: str, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _IMPOSTOR_TMPL.format(sentinel=sentinel, marker=marker),
        encoding="utf-8")


def _run_hermetic(script: Path, *, cwd: Path | None = None,
                  prelude: Path | str | None = PRELUDE,
                  fakepkg: Path | None = None, marker=None,
                  extra_pp: list[str] | None = None,
                  env_updates: dict | None = None,
                  cli_args: list[str] | None = None,
                  inherit_pp: bool = False) -> subprocess.CompletedProcess:
    """真子进程：PYTHONPATH 前拼 prelude（可选），fakepkg 随尾。"""
    env = dict(os.environ)
    pp: list[str] = []
    if prelude is not None:
        pp.append(str(prelude))
    if extra_pp:
        pp.extend(str(p) for p in extra_pp)
    if fakepkg is not None:
        pp.append(str(fakepkg))
    if pp:
        env["PYTHONPATH"] = os.pathsep.join(pp)
    elif not inherit_pp:
        env.pop("PYTHONPATH", None)
    if marker is not None:
        env[MARKER_ENV] = marker
    else:
        env.pop(MARKER_ENV, None)
    if env_updates:
        env.update(env_updates)
    return subprocess.run(
        [sys.executable, str(script), *(cli_args or [])],
        cwd=str(cwd or script.parent), env=env,
        capture_output=True, text=True)


def _write_script(d: Path, name: str, body: str) -> Path:
    p = d / name
    p.write_text(body, encoding="utf-8")
    return p


def _torch_anchor(a: int = 0, b: int = 0, c: int = 0,
                  pid: int = 4242, label: str = "run_x") -> str:
    return (f"[simpraxis-host-shim v1] 平台托底（非模型代码） "
            f"site=torch pid={pid} label={label} "
            f"array_migrated={a} numpy_migrated={b} "
            f"cpu_grad_detached={c}\n")


def _idle_anchor(pid: int = 4243, label: str = "gpu_sampler.py") -> str:
    return (f"[simpraxis-host-shim v1] 平台托底（非模型代码） "
            f"site=idle pid={pid} label={label} "
            f"array_migrated=0 numpy_migrated=0 cpu_grad_detached=0\n")


def _banner(pid: int = 4242, label: str = "run_x") -> str:
    return (f"[simpraxis-host-shim v1] 平台托底激活（非模型代码） "
            f"site=torch pid={pid} label={label} torch=2.4.1-fake\n")


def _rc0_tail(*, n_torch: int = 9, n_idle: int = 2, n_banner: int = 9,
              counters: tuple[int, int, int] = (1, 0, 0),
              prefix_noise: str = "", suffix: str = "",
              crlf: bool = False) -> str:
    a, b, c = counters
    text = prefix_noise
    text += "".join(_banner(pid=1000 + i) for i in range(n_banner))
    text += "".join(_torch_anchor(a, b, c, pid=2000 + i)
                    for i in range(n_torch))
    text += "".join(_idle_anchor(pid=3000 + i) for i in range(n_idle))
    text += suffix
    if crlf:
        text = text.replace("\n", "\r\n")
    return text


def _entry(rc: int, tail: str, *, with_key: bool = True) -> dict:
    # 仿真引擎落盘口径：stderr_only_tail 已按字符切尾 8192（offset 按
    # UTF-8 字节计，CJK 字符/字节差异由 offset-STOP 用例专门构造）。
    e = {"rc": rc, "stdout_chars": 0, "stderr_chars": len(tail)}
    if with_key:
        e["stderr_only_tail"] = tail[-8192:]
    return e


def _state_from_entries(entries: list[dict]) -> tuple[dict, dict]:
    rec = {"timeline": entries}
    state = {"agent_runs": [rec]}
    return state, rec


# ---------------------------------------------------------------------------
# G3 探针合成 checkpoint 夹具（tmp sqlite，msgpack；不碰活库）
# ---------------------------------------------------------------------------

def _load_g3_probe():
    spec = importlib.util.spec_from_file_location("g3_shim_probe_under_test",
                                                  G3_PROBE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _synth_checkpoint_db(db_path: Path, entries: list[dict],
                         events: list | None = None) -> None:
    import msgpack
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE checkpoints (thread_id TEXT, checkpoint_ns TEXT, "
        "checkpoint_id TEXT, checkpoint BLOB)")
    blob = msgpack.packb(
        {"channel_values": {
            "agent_runs": [{"timeline": entries}],
            "events": events or [{"type": "gate2_blocking"}]}},
        use_bin_type=True)
    con.execute(
        "INSERT INTO checkpoints VALUES (?,?,?,?)",
        ("t-thread", "", "cid-synth", blob))
    con.commit()
    con.close()


def _run_g3_probe(tmp_path: Path, monkeypatch, entries: list[dict],
                  mode: str, *, expect_torch: int | None = None,
                  expect_idle: int | None = None, events: list | None = None):
    mod = _load_g3_probe()
    db = tmp_path / "cp.db"
    _synth_checkpoint_db(db, entries, events=events)
    wd = tmp_path / "work"
    wd.mkdir()
    monkeypatch.setattr(mod, "CP_DB", str(db))
    monkeypatch.setattr(mod, "THREAD", "t-thread")
    monkeypatch.setattr(mod, "WORKDIR", str(wd))
    monkeypatch.setattr(mod, "GRAPH_FAILURE",
                        str(tmp_path / "graph_FAILURE.json"))
    argv = ["g3_probe", "--mode", mode]
    if expect_torch is not None:
        argv += ["--expect-torch", str(expect_torch)]
    if expect_idle is not None:
        argv += ["--expect-idle", str(expect_idle)]
    monkeypatch.setattr(sys, "argv", argv)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod.main()
    report_json = buf.getvalue()
    return rc, report_json


# ===========================================================================
# 红测组 1：test_s0b —— marker 开三层冒名零执行 + 合成名 core + 子目录同名
# ===========================================================================

def test_s0b_marker_on_three_impostors_never_execute(tmp_path):
    """d1/d2：abs-script 激活形下 workdir 三件冒名零执行；core 走合成名；
    子目录同名件不触发；伪 torch 迁移正常（证明用的是 prelude core）。"""
    assert PRELUDE.is_dir(), f"prelude 目录缺失：{PRELUDE}"
    workdir = tmp_path / "wd"
    workdir.mkdir()
    pkgdir = tmp_path / "fakepkg"
    _write_fake_torch(pkgdir)

    # 三件顶层冒名 + 子目录同名件。
    _write_impostor(workdir / "sitecustomize.py",
                    "SENTINEL_SITECUSTOMIZE", "IMPOSTOR_sitecustomize\n")
    _write_impostor(workdir / "shim_core.py",
                    "SENTINEL_SHIMCORE", "IMPOSTOR_shim_core\n")
    _write_impostor(workdir / "usercustomize.py",
                    "SENTINEL_USERCUSTOMIZE", "IMPOSTOR_usercustomize\n")
    _write_impostor(workdir / "sub" / "sitecustomize.py",
                    "SENTINEL_SUB", "IMPOSTOR_sub\n")

    probe = _write_script(workdir, "probe_s0b.py", '''
import json
import sys
import numpy as np
import torch
t = torch.Tensor(is_cuda=True)
arr = np.asarray(t)
sys.stdout.write("JSONRESULT " + json.dumps({
    "ok": bool(float(arr) == 1.0),
    "bare": "shim_core" in sys.modules,
    "core": "simpraxis_host_shim_core_v1" in sys.modules,
    "dtype": str(arr.dtype),
}) + "\\n")
''')
    r = _run_hermetic(probe, cwd=workdir, fakepkg=pkgdir, marker="1")
    assert r.returncode == 0, f"rc={r.returncode}\nERR={r.stderr}"
    line = next((ln for ln in r.stdout.splitlines()
                 if ln.startswith("JSONRESULT ")), None)
    assert line is not None, f"探针无 JSONRESULT：{r.stdout!r}/{r.stderr!r}"
    payload = json.loads(line[len("JSONRESULT "):])
    assert payload["ok"] is True and payload["dtype"] == "float32"
    assert payload["core"] is True, "core 必须以合成名急切加载"
    assert payload["bare"] is False, "裸名 shim_core 永不入 sys.modules"
    # 四件冒名桩零执行（sentinel 缺席 + 输出无标记）。
    for sentinel in ("SENTINEL_SITECUSTOMIZE", "SENTINEL_SHIMCORE",
                     "SENTINEL_USERCUSTOMIZE", "SENTINEL_SUB"):
        assert not (workdir / sentinel).exists(), f"{sentinel} 被写出＝冒名件执行"
    assert "IMPOSTOR_" not in r.stdout + r.stderr
    # 真 prelude 路径：恰好 1 banner + 1 torch-atexit，array_migrated=1，
    # numpy_migrated=0（__array__ 委托不双计数）；无 idle（本进程触达 torch）。
    assert len(BANNER_RE.findall(r.stderr)) == 1
    torch_lines = list(ATEXIT_RE.finditer(r.stderr))
    idle_lines = ATEXIT_IDLE_RE.findall(r.stderr)
    assert len(torch_lines) == 1 and len(idle_lines) == 0
    a, b, c = (int(x) for x in torch_lines[0].groups())
    assert (a, b, c) == (1, 0, 0)


# ===========================================================================
# 红测组 2：bootstrap 急切加载（脚本首行探针 + 静态 grep）
# ===========================================================================

def test_bootstrap_eager_core_cached_before_script(tmp_path):
    """d1：脚本首行前合成名 core 已缓存、裸名不在；引导件静态面无裸绑定、
    无任何禁词字面量（禁词唯一规范源＝shim_core）。"""
    assert BOOTSTRAP.is_file(), f"bootstrap 缺失：{BOOTSTRAP}"
    probe = _write_script(tmp_path, "probe_eager.py", '''
import json
import sys
sys.stdout.write("FIRST " + json.dumps({
    "core": "simpraxis_host_shim_core_v1" in sys.modules,
    "bare": "shim_core" in sys.modules,
}) + "\\n")
''')
    r = _run_hermetic(probe, cwd=tmp_path, marker="1")
    assert r.returncode == 0, f"rc={r.returncode}\nERR={r.stderr}"
    line = next((ln for ln in r.stdout.splitlines()
                 if ln.startswith("FIRST ")), None)
    assert line is not None, r.stdout + r.stderr
    payload = json.loads(line[len("FIRST "):])
    assert payload["core"] is True, "脚本首行前 core 必须已缓存（急切加载）"
    assert payload["bare"] is False
    # marker 开但本进程不触达 torch → idle 行在场（atexit 无条件注册）。
    assert len(ATEXIT_IDLE_RE.findall(r.stderr)) == 1
    assert not BANNER_RE.search(r.stderr)

    src = BOOTSTRAP.read_text(encoding="utf-8")
    # d2 静态面：禁顶层裸绑定（允许合成名 spec_from_file_location）。
    assert not re.search(r"^\s*import\s+shim_core\b", src, re.M)
    assert not re.search(r"^\s*from\s+shim_core\b", src, re.M)
    assert not re.search(r"^\s*from\s+\.shim_core\b", src, re.M)
    assert "simpraxis_host_shim_core_v1" in src
    # 锁内适配（规格 §2.2/§2.3 明确要求引导件含 marker 名、合成名、
    # shim_core.py 路径与发行版 sitecustomize 绝对路径等字面量，故 d1/d2
    # 的静态面只禁裸绑定形式；人类机制文案唯一源仍在 core——引导件不得
    # 出现 banner/atexit 中文案），g1：引导件源码无 genesis 字样。
    assert "平台托底" not in src
    assert "genesis" not in src


def test_bootstrap_guard_direct_exec_no_selfstart(tmp_path):
    """§2.1(c)：引导件仅在被 site 机制以 ``sitecustomize`` 名导入时自启；
    直接当脚本执行（__name__=="__main__"）或以其他合成名 exec_module 时，
    即使 marker=1 也不链载发行版钩子、不装载 core、不注册 atexit。"""
    # 场景一：直接当脚本执行。必须摘掉 PYTHONPATH，否则 site 初始化会先以
    # sitecustomize 名导入本件（守卫通过），测不到直接执行路径。
    env_direct = dict(os.environ)
    env_direct.pop("PYTHONPATH", None)
    env_direct[MARKER_ENV] = "1"
    r = subprocess.run(
        [sys.executable, str(BOOTSTRAP)],
        cwd=str(tmp_path), env=env_direct,
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert len(ATEXIT_IDLE_RE.findall(r.stderr)) == 0
    assert not BANNER_RE.search(r.stderr)

    # 场景二：合成名 exec_module（名 != "sitecustomize"）同样不自启。
    probe = _write_script(tmp_path, "probe_synth_name.py", '''
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location(
    "_run7_not_sitecustomize", BOOTSTRAP)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
sys.stdout.write("SN " + json.dumps({
    "core": "simpraxis_host_shim_core_v1" in sys.modules,
    "chain": "_simpraxis_host_shim_distro_chain_v1" in sys.modules,
}) + "\\n")
'''.replace("BOOTSTRAP", repr(str(BOOTSTRAP))))
    r2 = _run_hermetic(probe, cwd=tmp_path, prelude=None, marker="1")
    assert r2.returncode == 0, r2.stderr
    payload = json.loads(next(
        ln[3:] for ln in r2.stdout.splitlines() if ln.startswith("SN ")))
    assert payload == {"core": False, "chain": False}, payload
    assert len(ATEXIT_IDLE_RE.findall(r2.stderr)) == 0


# ===========================================================================
# 红测组 3：PYTHONPATH 前拼保留外部值 + clobber fail-closed
# ===========================================================================

def test_pythonpath_prepend_and_clobber_failclosed(tmp_path):
    ext = tmp_path / "ext"
    ext.mkdir()
    probe = _write_script(tmp_path, "probe_pp.py", '''
import json
import sys
sys.stdout.write("PATHS " + json.dumps(sys.path) + "\\n")
''')
    r = _run_hermetic(probe, cwd=tmp_path, marker="1", extra_pp=[ext])
    assert r.returncode == 0, r.stderr
    line = next(ln for ln in r.stdout.splitlines() if ln.startswith("PATHS "))
    paths = json.loads(line[len("PATHS "):])
    assert str(PRELUDE) in paths and str(ext) in paths
    # 前拼：prelude 在外部 ext 之前（script dir 居 sys.path[0] 是解释器语义）。
    assert paths.index(str(PRELUDE)) < paths.index(str(ext))
    # marker 生效（idle 行）佐证前拼的 prelude 真被 site 机制加载。
    assert len(ATEXIT_IDLE_RE.findall(r.stderr)) == 1

    # clobber：空 prelude（sitecustomize 缺失）→ marker=1 也无任何 atexit 行；
    # Gate2 机验对无锚 rc0 时间线必须 GraphStateError，不得假放行（d3）。
    empty_prelude = tmp_path / "prelude_gone"
    empty_prelude.mkdir()
    r2 = _run_hermetic(probe, cwd=tmp_path, prelude=empty_prelude,
                       marker="1")
    assert "simpraxis-host-shim" not in r2.stderr

    from orchestrator.graph import task6 as task6_mod
    state, rec = _state_from_entries([_entry(0, "")])
    with pytest.raises(task6_mod.GraphStateError):
        task6_mod._require_shim_evidence(state, rec)


# ===========================================================================
# 红测组 4：W1 —— atexit 锚结构化落 stderr_only 新键（A3'，D4 双红）
# ===========================================================================

def _w1_build_io() -> tuple[str, str]:
    """按 /tmp/a3gen + W1 原型 2026-10-23 实测体量合成三层夹具 I/O。

    stdout ≥30721B（genesis 日志走 stdout）；stderr＝309B 警告 + 9 banner
    +600B teardown +9 torch-atexit+2 idle；末段含供脱敏红测的三行。
    返回 (stdout, stderr)，测试与探针脚本共用同一字符串构造。
    """
    stderr = ""
    stderr += "UserWarning: torch 警告桩 " + "w" * (309 - 28) + "\n"
    stderr += "".join(_banner(pid=2000 + i) for i in range(9))
    stderr += "x" * 600 + "\n"  # teardown 噪声（m10 同类，非 ffmpeg）
    stderr += "".join(_torch_anchor(1, 0, 0, pid=2100 + i) for i in range(9))
    stderr += "".join(_idle_anchor(pid=3100 + i) for i in range(2))

    child_line = "[rollout] " + "r" * (267 - len("[rollout] "))
    parent_tail = "".join(f"[batch] line {i} " + "b" * 40 + "\n"
                          for i in range(9))
    parent_tail = (parent_tail + "p" * max(
        0, 929 - len(parent_tail)))[:929]
    # 末段三行：两行含禁词（必被整行丢弃）+ 真 torch TypeError 行（必保留）。
    parent_tail += (
        f"debug env {MARKER_ENV}=1 prelude={PRELUDE}\n"
        "note: [simpraxis-host-shim v1] 平台托底 机制泄漏行\n"
        + TORCH_CUDA_NUMPY_TYPEERROR + "\n")
    genesis_log = ("G" * 3077 + "\n") * 10
    stdout = genesis_log + child_line * 9 + parent_tail
    assert len(stdout) >= 30721
    return stdout, stderr


def test_w1_atexit_anchor_structured_stderr_only(tmp_path):
    from orchestrator.agent_runner import runner as runner_mod
    from orchestrator.agent_runner.runner import AgentRunner, RunSpec

    # —— 常量存在（A3'）——
    assert runner_mod.STDERR_ONLY_TAIL_CHARS == 8192

    stdout, stderr = _w1_build_io()
    wd = tmp_path / "work"
    wd.mkdir(parents=True)
    script = wd / "w1_script.py"
    # 首次：按实测体量产出且 rc=1；补丁出生 fix.py 后二次：rc0 + 写 telemetry。
    script.write_text(
        "import json, sys\n"
        f"STDOUT = {stdout!r}\n"
        f"STDERR = {stderr!r}\n"
        "from pathlib import Path\n"
        "wd = Path(__file__).parent\n"
        "sys.stdout.write(STDOUT)\n"
        "sys.stderr.write(STDERR)\n"
        "if (wd / 'fix.py').exists():\n"
        "    (wd / 'summary.json').write_text(json.dumps({'ok': True}))\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n",
        encoding="utf-8")

    class W1Gateway:
        def __init__(self):
            self.calls: list[dict] = []

        def model_id(self, stage):
            return "fake-coding-w1"

        def call(self, stage, messages, **kw):
            self.calls.append({"stage": stage, "messages": messages, "kw": kw})
            return json.dumps({"patches": [{
                "path": "fix.py", "old": "", "new": "x = 1\n"}]}), (None, None)

    class W1Vector:
        def query_kb(self, query, *, namespace, top_k):
            return []

    gw = W1Gateway()
    runner = AgentRunner(gw, W1Vector(), evidence_root=tmp_path / "ev",
                         call_cache=None, recall=None)
    spec = RunSpec(
        thread_id="t-w1", workdir=wd,
        cmd=[sys.executable, str(script)],
        telemetry_relpath="summary.json", batch_id="T-W1",
        billing_task=6, run_idx=1, max_coding_rounds=1)
    rec = runner.run(spec)
    assert rec["status"] == "ok" and rec["executions"] == 2
    assert len(gw.calls) == 1

    e1 = rec["timeline"][0]
    # (3) 键契约：删键即红的三新键。
    assert "stderr_only_tail" in e1
    assert e1["stdout_chars"] == len(stdout)
    assert e1["stderr_chars"] == len(stderr)
    tail = e1["stderr_only_tail"]
    # (2) D4 双红：锚在纯 stderr 8192 窗存活；旧 merged 500 与假想 merged
    # 8192（D4 撤销案）都不含锚——stdout ≥30721，merged 窗在对无界 stdout 赌博。
    assert ATEXIT_RE.search(tail) and ATEXIT_IDLE_RE.search(tail)
    assert "site=torch" not in e1["stderr_tail"]
    merged = (stderr + "\n" + stdout)
    assert "site=torch" not in merged[-500:]
    assert "site=torch" not in merged[-8192:]
    # (4) 单条目内 9/2，裸子串恰 18（9 banner+9 atexit），机验不得误判。
    assert len(ATEXIT_RE.findall(tail)) == 9
    assert len(ATEXIT_IDLE_RE.findall(tail)) == 2
    assert tail.count("site=torch") == 18
    # (5) 末 torch 锚→字段尾 UTF-8 字节偏移与 W1 实测同量级（<1024；327B）。
    last = list(ATEXIT_RE.finditer(tail))[-1]
    off = len(tail[last.start():].encode("utf-8", "replace"))
    assert off < 1024, off
    # (1) stdout 体量 ≥30721（可复跑件口径，非理论值）。
    assert e1["stdout_chars"] >= 30721

    # (6) 模型面零增长/零泄漏 pin：两通道本路径仅普通修补一次调用。
    blob = json.dumps(gw.calls[0], ensure_ascii=False)
    for w in SIX_FORBIDDEN:
        assert w not in blob, f"模型 kwargs 命中禁词 {w!r}"
    assert str(PRELUDE) not in blob
    user_msg = gw.calls[0]["messages"][1]["content"]
    assert TORCH_CUDA_NUMPY_TYPEERROR in user_msg       # 真错误行保留
    # traceback 段相对 v1.0 基线零增长：恰为 redact(merged[-4096:])，≤4096。
    from orchestrator.common.host_shim.shim_core import redact_for_model
    expected_tail = redact_for_model(merged[-runner_mod.TB_TAIL_CHARS:])
    assert expected_tail in user_msg
    assert len(expected_tail) <= runner_mod.TB_TAIL_CHARS
    assert "debug env" not in user_msg
    # (7) G3 报告字段可读：stdout_chars/stderr_chars 已在上方逐值断言；
    # 人侧 timeline 保留 RAW（禁词原文在，模型面才脱敏）。
    assert "simpraxis-host-shim" in tail and "平台托底" in tail


# ===========================================================================
# 红测组 5：MAJOR-1 —— 计数/offset/求和闸只作用于最后一个 rc==0 exec 条目
# ===========================================================================

def _multi_exec_fixture():
    """exec1 rc=1（3 torch+2 idle 崩溃轮锚）；exec2 rc=0（9 banner+9 torch
    +2 idle，A=9/B=0/C=0）。旧拼全文必得 12/4（反证夹具）。"""
    t1 = "".join(_torch_anchor(1, 0, 0, pid=5001 + i) for i in range(3))
    t1 += "".join(_idle_anchor(pid=5101 + i) for i in range(2))
    e1 = _entry(1, t1)
    t2 = _rc0_tail()
    e2 = _entry(0, t2)
    return [e1, e2]


def test_f2_major1_scope_last_rc0_entry():
    from orchestrator.graph import task6 as task6_mod
    entries = _multi_exec_fixture()
    # 反证：旧拼全文实现必得 12/4（本夹具钉死作用域）。
    joined = "".join(e["stderr_only_tail"] for e in entries)
    assert len(ATEXIT_RE.findall(joined)) == 12
    assert len(ATEXIT_IDLE_RE.findall(joined)) == 4

    state, rec = _state_from_entries(entries)
    ev = task6_mod._require_shim_evidence(state, rec)
    assert set(ev) == {
        "shim_banner_seen", "gate_scope", "rc0_gate_entry_index",
        "array_migrated", "numpy_migrated", "cpu_grad_detached",
        "atexit_torch_lines", "idle_lines", "last_anchor_offset_bytes"}
    assert ev["gate_scope"] == "last_rc0_exec_entry"
    assert ev["rc0_gate_entry_index"] == 1
    assert ev["atexit_torch_lines"] == 9
    assert ev["idle_lines"] == 2
    assert (ev["array_migrated"], ev["numpy_migrated"],
            ev["cpu_grad_detached"]) == (9, 0, 0)
    assert ev["shim_banner_seen"] is True
    assert ev["last_anchor_offset_bytes"] < 8192

    # 落盘义务遍历全部条目：exec1 缺键同样 fail-closed。
    e1_missing = dict(entries[0])
    e1_missing.pop("stderr_only_tail")
    state2, rec2 = _state_from_entries([e1_missing, entries[1]])
    with pytest.raises(task6_mod.GraphStateError):
        task6_mod._require_shim_evidence(state2, rec2)


def test_f2_count_and_offset_and_zero_migration():
    from orchestrator.graph import task6 as task6_mod
    # 8 torch → GraphStateError；3 idle → GraphStateError。
    for n_torch, n_idle in ((8, 2), (9, 3)):
        state, rec = _state_from_entries(
            [_entry(0, _rc0_tail(n_torch=n_torch, n_idle=n_idle))])
        with pytest.raises(task6_mod.GraphStateError):
            task6_mod._require_shim_evidence(state, rec)
    # 末锚 offset ≥8192 → STOP（文案钉点），禁止静默加大常量。窗按字符切、
    # offset 按 UTF-8 字节计：末 torch 锚后放 3100 个 CJK 字符（9300B）。
    far = _rc0_tail(suffix="中" * 3100)
    state, rec = _state_from_entries([_entry(0, far)])
    with pytest.raises(task6_mod.GraphStateError,
                       match="禁止静默加大常量") as excinfo:
        task6_mod._require_shim_evidence(state, rec)
    assert "回退项另开评审" in str(excinfo.value)
    # A+B==0：9/2 不放宽，放行但 explanation 切零迁移固定句。
    zero = _rc0_tail(counters=(0, 0, 0))
    state, rec = _state_from_entries([_entry(0, zero)])
    ev = task6_mod._require_shim_evidence(state, rec)
    assert ev["atexit_torch_lines"] == 9 and ev["idle_lines"] == 2
    assert ev["array_migrated"] + ev["numpy_migrated"] == 0
    expl = task6_mod.build_gate2_explanation(state, rec, ev)
    joined = expl["problem"] + expl["task"] + expl["video"]
    assert "不得归因托底" in joined
    # A+B≥1 主变体固定句（9 rollout + 2 idle 数字在面）。
    state2, rec2 = _state_from_entries(
        [_entry(0, _rc0_tail(counters=(1, 0, 0)))])
    ev2 = task6_mod._require_shim_evidence(state2, rec2)
    expl2 = task6_mod.build_gate2_explanation(state2, rec2, ev2)
    j2 = expl2["problem"] + expl2["task"] + expl2["video"]
    assert "平台支持声明" in j2 and "9 个 rollout 子进程 + 2 个 idle" in j2


def test_f2_weak_gate_without_rc0_entry():
    from orchestrator.graph import task6 as task6_mod
    # 无 rc0 但失败 exec 有可解析 torch 锚 → 弱闸不做 9/2、不抛计数错。
    t1 = "".join(_torch_anchor(1, 0, 0) for _ in range(3))
    state, rec = _state_from_entries([_entry(1, t1)])
    ev = task6_mod._require_shim_evidence(state, rec)
    assert ev["rc0_gate_entry_index"] is None
    assert ev["atexit_torch_lines"] >= 1
    # 无 rc0 且全时间线零锚（含纯 rc124 无锚）→ GraphStateError，不假放行。
    rc124 = _entry(124, "\nTIMEOUT after 2400s\n")
    state2, rec2 = _state_from_entries([rc124])
    with pytest.raises(task6_mod.GraphStateError):
        task6_mod._require_shim_evidence(state2, rec2)
    # banner 仅尽力项：缺 banner 不硬败。
    state3, rec3 = _state_from_entries(
        [_entry(0, _rc0_tail(n_banner=0))])
    ev3 = task6_mod._require_shim_evidence(state3, rec3)
    assert ev3["shim_banner_seen"] is False
    # ffmpeg 行单列：绝不作锚、不抵 9/2。
    with_ff = _rc0_tail(suffix="ffmpeg teardown noise line 1\n"
                              "FFMPEG line 2 mp4\n")
    state4, rec4 = _state_from_entries([_entry(0, with_ff)])
    ev4 = task6_mod._require_shim_evidence(state4, rec4)
    assert ev4["atexit_torch_lines"] == 9 and ev4["idle_lines"] == 2


@_SKIP_NO_G3
def test_g3_postflight_offset_stop(tmp_path, monkeypatch):
    # 多 exec MAJOR-1：探针 success exit 0，JSON 指向 exec2（下标 1）。
    entries = _multi_exec_fixture()
    rc, out = _run_g3_probe(tmp_path / "c1", monkeypatch, entries, "success")
    assert rc == 0, out
    report = json.loads(out)
    assert report["rc0_gate_entry_index"] == 1
    assert report["gate_scope"] == "last_rc0_exec_entry"
    assert report["atexit_torch_lines"] == 9 and report["idle_lines"] == 2

    # 任一 exec 条目缺键 → exit 2（遍历全部）。
    bad = [dict(entries[0]), dict(entries[1])]
    bad[0].pop("stderr_only_tail")
    rc2, out2 = _run_g3_probe(tmp_path / "c2", monkeypatch, bad, "success")
    assert rc2 == 2, out2

    # torch≠9 / idle≠2 → exit 5。
    rc5a, _ = _run_g3_probe(
        tmp_path / "c3", monkeypatch,
        [_entry(0, _rc0_tail(n_torch=8))], "success")
    assert rc5a == 5
    rc5b, _ = _run_g3_probe(
        tmp_path / "c4", monkeypatch,
        [_entry(0, _rc0_tail(n_idle=3))], "success")
    assert rc5b == 5

    # 末锚 offset ≥8192 → exit 4（STOP 文案）：末 torch 锚后 3100 CJK
    # 字符（9300B，字符窗容得下、字节偏移超闸）。
    far = _rc0_tail(suffix="中" * 3100)
    rc4, out4 = _run_g3_probe(
        tmp_path / "c5", monkeypatch, [_entry(0, far)], "success")
    assert rc4 == 4, out4

    # A+B==0 success → exit 3。
    zero = _rc0_tail(counters=(0, 0, 0))
    rc3, _ = _run_g3_probe(
        tmp_path / "c6", monkeypatch, [_entry(0, zero)], "success")
    assert rc3 == 3

    # success 无 rc0 条目 → exit 2（含纯 rc124 时间线）。
    rc2b, _ = _run_g3_probe(
        tmp_path / "c7", monkeypatch,
        [_entry(124, "\nTIMEOUT after 2400s\n")], "success")
    assert rc2b == 2
    # 同时间线 failure 模式：仅弱诊断不硬败（rc=0）。
    rcf, _ = _run_g3_probe(
        tmp_path / "c8", monkeypatch,
        [_entry(124, "\nTIMEOUT after 2400s\n")], "failure")
    assert rcf == 0
    # failure 模式带锚时间线同样 rc=0。
    t1 = "".join(_torch_anchor(1, 0, 0) for _ in range(3))
    rcf2, _ = _run_g3_probe(
        tmp_path / "c9", monkeypatch, [_entry(1, t1)], "failure")
    assert rcf2 == 0

    # CRLF 帧计数一致（探针正则不依赖行尾）。
    crlf = _rc0_tail(crlf=True)
    rcc, outc = _run_g3_probe(
        tmp_path / "c10", monkeypatch, [_entry(0, crlf)], "success")
    assert rcc == 0, outc


# ===========================================================================
# S0/S1：marker 关 —— 冒名零执行、distro 链载仍发生、cuda TypeError 原样
# ===========================================================================

def test_s0_marker_off_impostors_silent_distro_chains(tmp_path):
    """marker 关：三冒名桩零执行；core 合成名缺席；发行版钩子链载仍发生
    （合成链名入 sys.modules）；无 banner/无 atexit。"""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    _write_impostor(workdir / "sitecustomize.py", "S0_SC", "S0_MARK_SC\n")
    _write_impostor(workdir / "shim_core.py", "S0_CORE", "S0_MARK_CORE\n")
    _write_impostor(workdir / "usercustomize.py", "S0_UC", "S0_MARK_UC\n")
    probe = _write_script(workdir, "probe_s0.py", '''
import json, sys
sys.stdout.write("S0 " + json.dumps({
    "chain": "_simpraxis_host_shim_distro_chain_v1" in sys.modules,
    "core": "simpraxis_host_shim_core_v1" in sys.modules,
    "bare": "shim_core" in sys.modules,
}) + "\\n")
''')
    r = _run_hermetic(probe, cwd=workdir, marker=None)
    assert r.returncode == 0, r.stderr
    payload = json.loads(next(
        ln[3:] for ln in r.stdout.splitlines() if ln.startswith("S0 ")))
    assert payload == {"chain": True, "core": False, "bare": False}
    assert "simpraxis-host-shim" not in r.stderr
    for sentinel, mark in (("S0_SC", "S0_MARK_SC"), ("S0_CORE", "S0_MARK_CORE"),
                           ("S0_UC", "S0_MARK_UC")):
        assert not (workdir / sentinel).exists()
        assert mark not in r.stdout + r.stderr


def test_s1_marker_off_cuda_typeerror_verbatim(tmp_path):
    """marker 关：伪 cuda 张量经 np.asarray 抛与真 torch 逐字相同 TypeError；
    shim 零痕迹（无 banner/无 atexit/无迁移）。"""
    pkgdir = tmp_path / "fakepkg"
    _write_fake_torch(pkgdir)
    probe = _write_script(tmp_path, "probe_s1.py", '''
import json, sys
import numpy as np
import torch
try:
    np.asarray(torch.Tensor(is_cuda=True))
except TypeError as exc:
    sys.stdout.write("S1 " + json.dumps({"etype": type(exc).__name__,
                                         "msg": str(exc)}) + "\\n")
else:
    sys.stdout.write("S1 " + json.dumps({"etype": "NO_ERROR"}) + "\\n")
''')
    r = _run_hermetic(probe, cwd=tmp_path, fakepkg=pkgdir, marker=None)
    assert r.returncode == 0, r.stderr
    payload = json.loads(next(
        ln[3:] for ln in r.stdout.splitlines() if ln.startswith("S1 ")))
    assert payload["etype"] == "TypeError"
    assert "TypeError: " + payload["msg"] == TORCH_CUDA_NUMPY_TYPEERROR
    assert "simpraxis-host-shim" not in r.stderr
    assert not BANNER_RE.search(r.stderr)
    assert not ATEXIT_RE.search(r.stderr)
    assert not ATEXIT_IDLE_RE.search(r.stderr)


# ===========================================================================
# S2/S3/S4/S5/S12：torch_wrap 包装语义系列（真子进程 + 伪 torch）
# ===========================================================================

def _torch_semantics_probe(tmp_path, body: str, *, marker="1"):
    """通用：伪 torch 包子进程跑 body（body 末尾给 result 赋 dict）。"""
    pkgdir = tmp_path / "fakepkg"
    _write_fake_torch(pkgdir)
    probe = _write_script(tmp_path, "probe_wrap.py", '''
import json, sys
import numpy as np
import torch
import simpraxis_host_shim_core_v1 as core
result = {}
''' + body + '''
sys.stdout.write("WRAP " + json.dumps(
    {"cnt": dict(core.cnt), "result": result}, ensure_ascii=False) + "\\n")
''')
    r = _run_hermetic(probe, cwd=tmp_path, fakepkg=pkgdir, marker=marker)
    assert r.returncode == 0, f"rc={r.returncode}\nERR={r.stderr}"
    payload = json.loads(next(
        ln[5:] for ln in r.stdout.splitlines() if ln.startswith("WRAP ")))
    return payload, r


def test_s2_marker_on_asarray_cuda_migrates(tmp_path):
    # detach().cpu()：detach 命中原张量（spy=1），cpu 落在 detach 返回的
    # 副本上（原张量 cpu spy 仍 0）。
    payload, r = _torch_semantics_probe(tmp_path, '''
t = torch.Tensor(is_cuda=True, value=2.5)
arr = np.asarray(t)
result["ok"] = bool(abs(float(arr) - 2.5) < 1e-6)
result["orig_called_cpu"] = t.cpu_called
result["orig_called_detach"] = t.detach_called
result["migrated_tensor_no_longer_cuda"] = True
''')
    assert payload["result"]["ok"] is True
    assert payload["result"]["orig_called_cpu"] == 0
    assert payload["result"]["orig_called_detach"] == 1
    assert payload["cnt"] == {"array_migrated": 1, "numpy_migrated": 0,
                              "cpu_grad_detached": 0}
    assert len(BANNER_RE.findall(r.stderr)) == 1
    torch_lines = list(ATEXIT_RE.finditer(r.stderr))
    assert len(torch_lines) == 1 and not ATEXIT_IDLE_RE.search(r.stderr)
    assert tuple(int(x) for x in torch_lines[0].groups()) == (1, 0, 0)


def test_s3_direct_numpy_cuda_migrates(tmp_path):
    payload, r = _torch_semantics_probe(tmp_path, '''
t = torch.Tensor(is_cuda=True, value=3.0)
arr = t.numpy()
result["ok"] = bool(abs(float(arr) - 3.0) < 1e-6)
''')
    assert payload["result"]["ok"] is True
    assert payload["cnt"] == {"array_migrated": 0, "numpy_migrated": 1,
                              "cpu_grad_detached": 0}


def test_s4_array_delegation_not_double_counted(tmp_path):
    """np.asarray 只计 array_migrated=1：原 __array__ 内部对已 cpu/no-grad
    张量调 wrapped numpy 必须直通不计数。"""
    payload, _ = _torch_semantics_probe(tmp_path, '''
arr = np.asarray(torch.Tensor(is_cuda=True))
result["arr_ok"] = bool(float(arr) == 1.0)
''')
    assert payload["result"]["arr_ok"] is True
    assert payload["cnt"]["array_migrated"] == 1
    assert payload["cnt"]["numpy_migrated"] == 0


def test_s5_cpu_grad_detach_only_plain_cpu_passthrough(tmp_path):
    payload, _ = _torch_semantics_probe(tmp_path, '''
g = torch.Tensor(is_cuda=False, requires_grad=True, value=4.0)
arr = np.asarray(g)
result["grad_val"] = bool(abs(float(arr) - 4.0) < 1e-6)
result["grad_cpu_called"] = g.cpu_called
result["grad_detach_called"] = g.detach_called
p = torch.Tensor(is_cuda=False, requires_grad=False, value=5.0)
np.asarray(p)
result["plain_cpu_called"] = p.cpu_called
result["plain_detach_called"] = p.detach_called
''')
    res = payload["result"]
    assert res["grad_val"] is True
    assert res["grad_cpu_called"] == 0 and res["grad_detach_called"] == 1
    assert res["plain_cpu_called"] == 0 and res["plain_detach_called"] == 0
    assert payload["cnt"] == {"array_migrated": 0, "numpy_migrated": 0,
                              "cpu_grad_detached": 1}


def test_s12_dtype_path_and_force_kwarg_passthrough(tmp_path):
    payload, _ = _torch_semantics_probe(tmp_path, '''
t1 = torch.Tensor(is_cuda=True)
arr = np.asarray(t1, dtype=np.float32)
result["dtype"] = str(arr.dtype)
t2 = torch.Tensor(is_cuda=True)
arr2 = t2.numpy(force=True)            # m8：kw 透传不特判
result["force_ok"] = bool(float(arr2) == 1.0)
''')
    assert payload["result"]["dtype"] == "float32"
    assert payload["result"]["force_ok"] is True
    assert payload["cnt"] == {"array_migrated": 1, "numpy_migrated": 1,
                              "cpu_grad_detached": 0}


# ===========================================================================
# S6/finder：幂等 + finder 一次性（不回插、meta_path 长度复原）
# ===========================================================================

def test_s6_double_activate_single_wrap_no_double_count(tmp_path):
    pkgdir = tmp_path / "fakepkg"
    _write_fake_torch(pkgdir)
    probe = _write_script(tmp_path, "probe_s6.py", '''
import json, sys
import numpy as np
import torch
import simpraxis_host_shim_core_v1 as core
core.activate()
core.activate()
result = {
    "sentinel": bool(getattr(torch.Tensor, "_simpraxis_host_shim_v1", False)),
    "array_name": torch.Tensor.__array__.__name__,
    "numpy_name": torch.Tensor.numpy.__name__,
    "patched": core._PATCHED,
    "finders_left": sum(type(f).__name__ == "_ShimFinder"
                        for f in sys.meta_path),
}
np.asarray(torch.Tensor(is_cuda=True))
np.asarray(torch.Tensor(is_cuda=True))
result["cnt"] = dict(core.cnt)
sys.stdout.write("S6 " + json.dumps(result) + "\\n")
''')
    r = _run_hermetic(probe, cwd=tmp_path, fakepkg=pkgdir, marker="1")
    assert r.returncode == 0, r.stderr
    payload = json.loads(next(
        ln[3:] for ln in r.stdout.splitlines() if ln.startswith("S6 ")))
    assert payload["sentinel"] is True and payload["patched"] is True
    assert payload["array_name"] == "_array" and payload["numpy_name"] == "_numpy"
    assert payload["finders_left"] == 0
    assert payload["cnt"] == {"array_migrated": 2, "numpy_migrated": 0,
                              "cpu_grad_detached": 0}
    assert len(BANNER_RE.findall(r.stderr)) == 1


def test_finder_one_shot_meta_path_restored(tmp_path):
    pkgdir = tmp_path / "fakepkg"
    _write_fake_torch(pkgdir)
    probe = _write_script(tmp_path, "probe_finder.py", '''
import json, sys
import simpraxis_host_shim_core_v1 as core
core.activate()
core.activate()
n_finder = sum(type(f).__name__ == "_ShimFinder" for f in sys.meta_path)
pre_import = len(sys.meta_path)             # 含 bootstrap 装的 1 只 finder
import torch  # noqa
after_import = len(sys.meta_path)
n_left = sum(type(f).__name__ == "_ShimFinder" for f in sys.meta_path)
second_len = len(sys.meta_path)
import torch  # 二次：sys.modules 缓存，无新补丁、meta_path 再不变
sys.stdout.write("F " + json.dumps({
    "n_finder": n_finder, "pre_import": pre_import,
    "after_import": after_import, "n_left": n_left,
    "second_len": second_len,
    "patched": core._PATCHED}) + "\\n")
''')
    r = _run_hermetic(probe, cwd=tmp_path, fakepkg=pkgdir, marker="1")
    assert r.returncode == 0, r.stderr
    payload = json.loads(next(
        ln[2:] for ln in r.stdout.splitlines() if ln.startswith("F ")))
    assert payload["n_finder"] == 1, "双 activate 不得重复装 finder"
    assert payload["n_left"] == 0, "补丁后 finder 必须自摘除、不回插"
    # finder 自摘除使 meta_path 恰好 -1；二次 import 长度再不变。
    assert payload["after_import"] == payload["pre_import"] - 1
    assert payload["second_len"] == payload["after_import"]
    assert payload["patched"] is True
    assert len(BANNER_RE.findall(r.stderr)) == 1


# ===========================================================================
# S7：torch 缺失 × find_spec 三异常（m2）—— 启动不炸、idle 行在场
# ===========================================================================

@pytest.mark.parametrize("exc_name", ["ModuleNotFoundError", "ValueError",
                                      "ImportError"])
def test_s7_find_spec_three_exceptions_swallowed(tmp_path, exc_name):
    """m2：破损安装让 importlib.util.find_spec('torch') 抛三异常之一时，
    shim finder 吞掉返 None（不炸启动），且自身先自摘除。进程内直测。"""
    probe = _write_script(tmp_path, f"probe_s7_{exc_name.lower()}.py", '''
import json, sys
import importlib.abc
import simpraxis_host_shim_core_v1 as core

class Raiser(importlib.abc.MetaPathFinder):
    def __init__(self):
        self.calls = 0
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "torch":
            return None
        self.calls += 1
        raise EXC_CLS("broken install")

raiser = Raiser()
sys.meta_path.insert(0, raiser)
finder = core._ShimFinder()
spec = finder.find_spec("torch")                 # 三异常必须被吞、返 None
sys.stdout.write("S7 " + json.dumps({
    "spec_none": spec is None, "calls": raiser.calls,
    "manual_left": finder in sys.meta_path,       # 执行者自摘除（本就未插）
    "bootstrap_left": sum(
        isinstance(f, core._ShimFinder) for f in sys.meta_path)}) + "\\n")
'''.replace("EXC_CLS", exc_name))
    r = _run_hermetic(probe, cwd=tmp_path, marker="1")
    assert r.returncode == 0, r.stderr
    payload = json.loads(next(
        ln[3:] for ln in r.stdout.splitlines() if ln.startswith("S7 ")))
    # 启动期 bootstrap 装的那只 finder 不受手动实例影响，仍留 1 只。
    assert payload == {"spec_none": True, "calls": 1,
                       "manual_left": False, "bootstrap_left": 1}


def test_s7_torch_missing_subprocess_idle_line(tmp_path):
    """m2 子进程形：sys.path 清空到空目录（venv 真 torch 不可见），marker 开
    时 import torch 等同缺失——启动不炸、无 banner、恰一条 site=idle atexit。"""
    empty = tmp_path / "empty_path"
    empty.mkdir()
    probe = _write_script(tmp_path, "probe_s7miss.py", '''
import json, sys
sys.path = [EMPTY]
try:
    import torch  # noqa
    outcome = "imported"
except ModuleNotFoundError:
    outcome = "missing"
import simpraxis_host_shim_core_v1 as core
sys.stdout.write("S7M " + json.dumps({
    "outcome": outcome, "patched": core._PATCHED,
    "finder_left": sum(isinstance(f, core._ShimFinder)
                       for f in sys.meta_path)}) + "\\n")
'''.replace("EMPTY", repr(str(empty))))
    r = _run_hermetic(probe, cwd=tmp_path, marker="1")
    assert r.returncode == 0, r.stderr
    payload = json.loads(next(
        ln[4:] for ln in r.stdout.splitlines() if ln.startswith("S7M ")))
    assert payload["outcome"] == "missing"
    assert payload["patched"] is False and payload["finder_left"] == 0
    assert len(ATEXIT_IDLE_RE.findall(r.stderr)) == 1
    assert not ATEXIT_RE.search(r.stderr) and not BANNER_RE.search(r.stderr)


# ===========================================================================
# S8：发行版 sitecustomize 链式加载（合成名、静默吞异常、具名默认参数）
# ===========================================================================

def test_s8_distro_chain_loads_and_swallows(tmp_path):
    good = tmp_path / "good_distro_sc.py"
    sentinel = tmp_path / "DISTRO_SENTINEL"
    good.write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('1')\n",
        encoding="utf-8")
    raising = tmp_path / "raising_distro_sc.py"
    raising.write_text("raise RuntimeError('distro hook broken')\n",
                       encoding="utf-8")
    driver = _write_script(tmp_path, "probe_s8.py", '''
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location(
    "_run7_bootstrap_probe_sc", BOOTSTRAP)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod._chain_load_distro(GOOD)                 # 伪发行版被执行
mod._chain_load_distro(RAISING)              # 抛异常静默吞、启动不炸
sys.stdout.write("S8 " + json.dumps({
    "default": mod._DISTRO_PATH,
    "default_is_file": os.path.isfile(mod._DISTRO_PATH),
    "chain_name": mod._CHAIN_NAME}) + "\\n")
'''.replace("BOOTSTRAP", repr(str(BOOTSTRAP)))
        .replace("GOOD", repr(str(good)))
        .replace("RAISING", repr(str(raising))))
    r = _run_hermetic(driver, cwd=tmp_path, prelude=None, marker=None)
    assert r.returncode == 0, f"rc={r.returncode}\n{r.stderr}"
    payload = json.loads(next(
        ln[3:] for ln in r.stdout.splitlines() if ln.startswith("S8 ")))
    assert sentinel.is_file(), "tmp 伪发行版 sitecustomize 必须被链载执行"
    assert payload["default"] == "/usr/lib/python3.10/sitecustomize.py"
    assert payload["default_is_file"] is True
    assert payload["chain_name"] == "_simpraxis_host_shim_distro_chain_v1"
    assert "RuntimeError" not in r.stderr


# ===========================================================================
# S9：伪 genesis（包内 import torch）端到端 + 运行期归因
# ===========================================================================

def test_s9_fake_genesis_import_torch_end_to_end(tmp_path):
    pkgdir = tmp_path / "fakepkg"
    _write_fake_torch(pkgdir)
    _write_fake_genesis(pkgdir)
    probe = _write_script(tmp_path, "probe_s9.py", '''
import json, sys
import numpy as np
import genesis  # noqa
import simpraxis_host_shim_core_v1 as core
arr = np.asarray(core and __import__("torch").Tensor(is_cuda=True))
sys.stdout.write("S9 " + json.dumps({
    "ok": bool(float(arr) == 1.0),
    "core_file": core.__file__,
    "genesis_attr_on_core": hasattr(core, "genesis"),
    "cnt": dict(core.cnt)}) + "\\n")
''')
    r = _run_hermetic(probe, cwd=tmp_path, fakepkg=pkgdir, marker="1")
    assert r.returncode == 0, r.stderr
    payload = json.loads(next(
        ln[3:] for ln in r.stdout.splitlines() if ln.startswith("S9 ")))
    assert payload["ok"] is True
    assert Path(payload["core_file"]).resolve() == (CORE_PATH).resolve()
    assert payload["genesis_attr_on_core"] is False
    assert payload["cnt"]["array_migrated"] == 1
    assert len(BANNER_RE.findall(r.stderr)) == 1
    # g1：shim 两件源码无 genesis 字样（伪 genesis 只活在测试夹具）。
    assert "genesis" not in CORE_PATH.read_text(encoding="utf-8")
    assert "genesis" not in BOOTSTRAP.read_text(encoding="utf-8")


# ===========================================================================
# S10：零写盘 —— tmp cwd 跑前后清单不变
# ===========================================================================

def test_s10_zero_writes_into_cwd(tmp_path):
    pkgdir = tmp_path / "fakepkg"
    _write_fake_torch(pkgdir)
    wd = tmp_path / "wd"
    wd.mkdir()
    probe = wd / "probe_s10.py"
    probe.write_text('''
import numpy as np
import torch
np.asarray(torch.Tensor(is_cuda=True))
''', encoding="utf-8")
    before = sorted(p.name for p in wd.iterdir())
    r = _run_hermetic(probe, cwd=wd, fakepkg=pkgdir, marker="1")
    assert r.returncode == 0, r.stderr
    after = sorted(p.name for p in wd.iterdir())
    assert before == after == ["probe_s10.py"]


# ===========================================================================
# S11/W1b：label 防注入 + 40 字符硬截断参数化（m5）
# ===========================================================================

_LABEL_RE = re.compile(r"label=(\S+)")


def _label_probe(tmp_path, cli_args):
    pkgdir = tmp_path / "fakepkg"
    _write_fake_torch(pkgdir)
    probe = _write_script(tmp_path, "probe_label.py", "import torch\n")
    r = _run_hermetic(probe, cwd=tmp_path, fakepkg=pkgdir, marker="1",
                      cli_args=cli_args)
    assert r.returncode == 0, r.stderr
    return _LABEL_RE.search(r.stderr).group(1)


@pytest.mark.parametrize("raw,expect", [
    ("x\ny", "x?y"),                                  # 换行注入净化
    ("a" * 40, "a" * 40),                             # 恰 40 保留
    ("a" * 41, "a" * 40),                             # 41 截断
    ("a" * 100, "a" * 40),                            # 100 截断
    ("bad name/../x", "bad?name?..?x"),               # 白名单外替 ?
])
def test_s11_label_sanitize_and_truncate(tmp_path, raw, expect):
    assert _label_probe(tmp_path, ["--label", raw]) == expect


def test_w1b_label_40char_hard_truncation_explicit(tmp_path):
    """W1b 独立显式钉（防 S11 稀释）：--label 40/41/100 三最坏值。"""
    for n in (40, 41, 100):
        got = _label_probe(tmp_path, ["--label", "z" * n])
        assert got == "z" * min(n, 40)
        assert len(got) <= 40


def test_s11_label_derived_from_argv(tmp_path):
    # 无 --label：basename(argv[0])-argv[1]（batch 父派生形）。
    got = _label_probe(tmp_path, ["batch"])
    assert got == "probe_label.py-batch"
    # argv[1] 以 - 开头（--sidecar）不拼接。
    sub = tmp_path / "sub"
    sub.mkdir()
    probe = _write_script(sub, "sidecar_probe.py", "import torch\n")
    r = _run_hermetic(probe, cwd=sub, fakepkg=tmp_path / "fakepkg",
                      marker="1", cli_args=["--sidecar", "x"])
    assert r.returncode == 0
    assert _LABEL_RE.search(r.stderr).group(1) == "sidecar_probe.py"
    # 派生形超长（out_dir 场景）同样 40 硬截断：basename-argv1 总长 41+。
    long_arg = "o" * 100
    got_long = _label_probe(tmp_path, [long_arg])
    assert got_long == ("probe_label.py-" + "o" * 100)[:40]
    assert len(got_long) == 40


# ===========================================================================
# marker 矩阵（m8）：只精确 "1" 开
# ===========================================================================

@pytest.mark.parametrize("value", ["0", "true", "1 ", "01", ""])
def test_marker_matrix_off_values(tmp_path, value):
    probe = _write_script(tmp_path, f"probe_m_{ord(value[0]) if value else 0}.py",
                          "import sys\nsys.stdout.write('ok\\n')\n")
    r = _run_hermetic(probe, cwd=tmp_path, marker=value)
    assert r.returncode == 0
    assert "simpraxis-host-shim" not in r.stderr
    assert not ATEXIT_IDLE_RE.search(r.stderr)


def test_marker_unset_off_and_exact_one_on(tmp_path):
    probe = _write_script(tmp_path, "probe_m_unset.py", "pass\n")
    r_off = _run_hermetic(probe, cwd=tmp_path, marker=None)
    assert "simpraxis-host-shim" not in r_off.stderr
    r_on = _run_hermetic(probe, cwd=tmp_path, marker="1")
    assert len(ATEXIT_IDLE_RE.findall(r_on.stderr)) == 1


# ===========================================================================
# CRLF + 正则漂移 pin：task6 四正则与 G3 探针逐字同一对
# ===========================================================================

def test_crlf_frames_count_same_as_lf():
    lf = _rc0_tail()
    crlf = _rc0_tail(crlf=True)
    for rx in (BANNER_RE, ATEXIT_RE, ATEXIT_IDLE_RE):
        assert len(rx.findall(crlf)) == len(rx.findall(lf))
    assert len(ATEXIT_RE.findall(crlf)) == 9
    assert len(ATEXIT_IDLE_RE.findall(crlf)) == 2
    # ffmpeg 行两种行尾都只计数。
    assert len(FFMPEG_LINE_RE.findall("x ffmpeg y\r\nz\r\n")) == 1


@_SKIP_NO_G3
def test_regex_quartet_identical_to_g3_probe():
    mod = _load_g3_probe()
    from orchestrator.graph import task6 as task6_mod
    for name in ("BANNER_RE", "ATEXIT_RE", "ATEXIT_IDLE_RE", "FFMPEG_LINE_RE"):
        assert getattr(task6_mod, name).pattern == getattr(mod, name).pattern, name
        assert getattr(task6_mod, name).flags == getattr(mod, name).flags, name


# ===========================================================================
# g1/g2 卫生：源码禁 genesis + GS_ENABLE_ZEROCOPY 等值透传
# ===========================================================================

def test_g2_env_zerocopy_passthrough(tmp_path):
    pkgdir = tmp_path / "fakepkg"
    _write_fake_torch(pkgdir)
    probe = _write_script(tmp_path, "probe_g2.py", '''
import json, os, sys
sys.stdout.write("G2 " + json.dumps({
    "v": os.environ.get("GS_ENABLE_ZEROCOPY"),
    "marker": os.environ.get("SIMPRAXIS_HOST_SHIM")}) + "\\n")
''')
    r = _run_hermetic(probe, cwd=tmp_path, fakepkg=pkgdir, marker="1",
                      env_updates={"GS_ENABLE_ZEROCOPY": "0"})
    assert r.returncode == 0, r.stderr
    payload = json.loads(next(
        ln[3:] for ln in r.stdout.splitlines() if ln.startswith("G2 ")))
    assert payload == {"v": "0", "marker": "1"}


# ===========================================================================
# P1：task6 工厂 env（marker/prelude 前拼/外部值保留/scenario 键）+ cmd 14 项
# ===========================================================================

def test_p1_task6_factory_env_prelude_cmd14(tmp_path, monkeypatch):
    from tests.milestone1.test_task8_seam import (
        FIXTURE_SOLUTIONS_ROOT, _calibration_v2)
    from orchestrator.graph import task8
    from orchestrator.graph import task6 as task6_mod

    monkeypatch.setenv("SIMPRAXIS_SOLUTIONS_ROOT",
                       str(FIXTURE_SOLUTIONS_ROOT))
    # stage 落 tmp，绝不写真实 /mnt phase2 根。
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(tmp_path / "p2root"))
    cal_src = tmp_path / "cal_src.json"
    cal_src.write_text(json.dumps(_calibration_v2()), encoding="utf-8")
    monkeypatch.setattr(task8, "CALIBRATION_SOURCE", cal_src)

    state = {
        "chosen": {"sim_engine": "genesis", "name": "P1 夹具"},
        "v1_archive": {"thread_id": "v1-p1",
                       "host_workdir": str(tmp_path / "v1arch")},
        "agent_runs": [], "events": [], "gate1_rejects": 0, "proposals": []}
    factory = task6_mod.make_spec_factory("t6-p1", "T6-P1")
    spec = factory(state)

    # marker + prelude 前拼 + 三文件在。
    assert spec.env_extra["SIMPRAXIS_HOST_SHIM"] == "1"
    pp = spec.env_extra["PYTHONPATH"].split(os.pathsep)
    assert pp[0] == str(task6_mod.REPO_ROOT / "orchestrator" / "common"
                        / "host_shim") == str(PRELUDE)
    for name in ("__init__.py", "shim_core.py", "sitecustomize.py"):
        assert (PRELUDE / name).is_file()
    # scenario 键不丢。
    assert spec.env_extra["PYOPENGL_PLATFORM"] == "egl"
    assert spec.env_extra["GS_ENABLE_ZEROCOPY"] == "0"
    # cmd 恰 14 项，`--` 在第 5 位、其后 9 项。
    assert len(spec.cmd) == 14
    assert spec.cmd[4] == "--" and len(spec.cmd[5:]) == 9

    # d3：scenario env 既有 PYTHONPATH（外部旧值）必须保留在 prelude 之后，
    # 前拼不替换。
    scen_env = task8.ENGINE_SCENARIOS["genesis"]["env"]
    assert "PYTHONPATH" not in scen_env          # 快照前提：基线无此键
    scen_env["PYTHONPATH"] = "/tmp/ext_run7"
    try:
        spec2 = factory(dict(state, agent_runs=[{"run_idx": 1}]))
    finally:
        scen_env.pop("PYTHONPATH", None)
    pp2 = spec2.env_extra["PYTHONPATH"].split(os.pathsep)
    assert pp2[0] == str(PRELUDE)
    assert pp2[1] == "/tmp/ext_run7" and len(pp2) == 2
    assert task8.ENGINE_SCENARIOS["genesis"]["env"] == {
        "GS_ENABLE_ZEROCOPY": "0", "PYOPENGL_PLATFORM": "egl"}


# ===========================================================================
# P2：Task7/8 env_extra 键集快照（无 marker、无 PYTHONPATH 注入）
# ===========================================================================

def test_p2_task7_task8_env_keyset_snapshot():
    import ast
    from orchestrator.graph import task8

    base_keys = {"HOME", "HF_HOME", "HF_ENDPOINT", "SIMPRAXIS_USE_PROXY"}
    for rel in ("orchestrator/graph/task7.py", "orchestrator/graph/task8.py"):
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        # run7 唯一注入点在 task6：task7/8 源码必须零 marker/零 prelude。
        assert "SIMPRAXIS_HOST_SHIM" not in src, rel
        assert "host_shim" not in src, rel
        assert "PYTHONPATH" not in src, rel
        tree = ast.parse(src, filename=rel)
        dict_literals = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys = []
                ok = True
                for k in node.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.append(k.value)
                    else:
                        ok = False
                if ok and "SIMPRAXIS_USE_PROXY" in keys:
                    dict_literals.append(set(keys))
        assert dict_literals, f"{rel} 未找到 env_extra 基字典"
        assert all(keys == base_keys for keys in dict_literals), (rel, dict_literals)
    # genesis scenario env 键值快照（task6 工厂 update 之源）。
    assert task8.ENGINE_SCENARIOS["genesis"]["env"] == {
        "GS_ENABLE_ZEROCOPY": "0", "PYOPENGL_PLATFORM": "egl"}


# ===========================================================================
# F1：模型面脱敏（redact 纯函数单测 + 拆块出生通道全 kwargs 零命中）
# ===========================================================================

def test_f1_redact_unit_drops_lines_idempotent_keeps_typeerror():
    from orchestrator.common.host_shim.shim_core import redact_for_model
    sample = (
        "ordinary torch warning line\n"
        f"debug marker {MARKER_ENV}=1\n"
        "prefix [simpraxis-host-shim v1] banner 平台托底 line\n"
        f"abs prelude path: {PRELUDE}/shim_core.py\n"
        "note sitecustomize shadow attempt\n"
        f"{TORCH_CUDA_NUMPY_TYPEERROR}\n"
        "tail normal frame File x.py line 3\n")
    out = redact_for_model(sample)
    # 六禁词四行 + prelude 路径行整行消失，零替身影子文本。
    for w in SIX_FORBIDDEN:
        assert w not in out
    assert str(PRELUDE) not in out
    assert TORCH_CUDA_NUMPY_TYPEERROR in out          # 真错误上下文保留
    assert "ordinary torch warning line" in out
    assert "tail normal frame" in out
    # 零影子：丢弃行不留空壳。
    assert "shadow attempt" not in out
    # 幂等 + 空输入。
    assert redact_for_model(out) == out
    assert redact_for_model("") == ""
    assert redact_for_model(None) == ""


def test_f1_normal_repair_channel_zero_leak(tmp_path):
    """F1 第一通道：普通修补轮 FakeGateway 全 kwargs 对六禁词+prelude 绝对路径
    零命中，真 TypeError 行保留；人侧 stderr_only_tail 仍 RAW。拆块出生通道的
    对称件为 test_f1_chunked_birth_channel_zero_leak。"""
    from orchestrator.agent_runner import AgentRunner, RunSpec

    wd = tmp_path / "work"
    wd.mkdir(parents=True)
    script = wd / "f1_normal_script.py"
    # 首 exec rc=1：stderr 四行覆盖六禁词 + prelude 绝对路径 + 真 TypeError；
    # fix.py 出生后二跑 rc0 并写约定产物（恰 1 次模型调用）。
    script.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "wd = Path(__file__).parent\n"
        "sys.stderr.write('[simpraxis-host-shim v1] 平台托底激活行\\n')\n"
        "sys.stderr.write('调试 SIMPRAXIS_HOST_SHIM host_shim "
        "sitecustomize shim_core 行\\n')\n"
        f"sys.stderr.write({('prelude=' + str(PRELUDE) + '/sitecustomize.py')!r})\n"
        "sys.stderr.write('\\n')\n"
        f"sys.stderr.write({TORCH_CUDA_NUMPY_TYPEERROR!r})\n"
        "sys.stderr.write('\\n')\n"
        "if (wd / 'fix.py').exists():\n"
        "    (wd / 'summary.json').write_text(json.dumps({'ok': True}))\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n",
        encoding="utf-8")

    class NormalGateway:
        def __init__(self):
            self.calls = []

        def model_id(self, stage):
            return "fake-coding-f1normal"

        def call(self, stage, messages, **kw):
            self.calls.append({"stage": stage, "messages": messages, "kw": kw})
            return json.dumps({"patches": [{
                "path": "fix.py", "old": "", "new": "x = 1\n"}]}), (None, None)

    class NoHits:
        def query_kb(self, query, *, namespace, top_k):
            return []

    gw = NormalGateway()
    runner = AgentRunner(gw, NoHits(), evidence_root=tmp_path / "ev",
                         call_cache=None, recall=None)
    spec = RunSpec(
        thread_id="t-f1normal", workdir=wd,
        cmd=[sys.executable, str(script)],
        telemetry_relpath="summary.json", batch_id="T-F1N",
        billing_task=6, run_idx=1, max_coding_rounds=1,
        env_extra={"SIMPRAXIS_HOST_SHIM": "1"})
    rec = runner.run(spec)
    assert rec["status"] == "ok" and rec["executions"] == 2
    assert len(gw.calls) == 1 and gw.calls[0]["stage"] == "coding_debug"
    # 全 kwargs（不止 messages 文本）序列化后六禁词 + prelude 路径零命中。
    blob = json.dumps(gw.calls[0], ensure_ascii=False)
    for w in SIX_FORBIDDEN:
        assert w not in blob, f"普通修补通道模型 kwargs 命中禁词 {w!r}"
    assert str(PRELUDE) not in blob
    user_content = gw.calls[0]["messages"][1]["content"]
    assert TORCH_CUDA_NUMPY_TYPEERROR in user_content
    # 人侧 RAW 不脱敏：纯 stderr 键保留禁词行与 prelude 绝对路径。
    raw = rec["timeline"][0]["stderr_only_tail"]
    assert "host_shim" in raw and "shim_core" in raw
    assert "sitecustomize" in raw and str(PRELUDE) in raw


def test_f1_chunked_birth_channel_zero_leak(tmp_path):
    """F1 第二通道：拆块出生轮 FakeGateway 全 kwargs 对六禁词+prelude 零命中，
    TypeError 行保留；普通修补通道的对称件为
    test_f1_normal_repair_channel_zero_leak。"""
    from orchestrator.agent_runner import AgentRunner, RunSpec
    from orchestrator.agent_runner.runner import ChunkSpec

    wd = tmp_path / "work"
    wd.mkdir(parents=True)
    exec_script = tmp_path / "emit_exec.py"
    # 首 exec rc=1：stderr 含 banner/atexit/prelude 路径各若干行 + TypeError。
    exec_script.write_text(
        "import sys\n"
        f"sys.stderr.write({('[' + 'simpraxis-host-shim v1] 平台托底激活行')!r})\n"
        "sys.stderr.write('\\n')\n"
        f"sys.stderr.write({('调试 SIMPRAXIS_HOST_SHIM sitecustomize shim_core 行')!r})\n"
        "sys.stderr.write('\\n')\n"
        f"sys.stderr.write({('prelude=' + str(PRELUDE) + '/sitecustomize.py')!r})\n"
        "sys.stderr.write('\\n')\n"
        f"sys.stderr.write({TORCH_CUDA_NUMPY_TYPEERROR!r})\n"
        "sys.stderr.write('\\n')\n"
        "sys.exit(1)\n",
        encoding="utf-8")

    # 缺 index 的坏围栏：块 0 即 ChunkBirthError 整轮短路（与 ascent 夹具
    # test_chunked_birth_local_cap_3_not_raised 同形），cap=1 恰 1 次调用。
    bad_response = ("<<<<<<< simpraxis.chunk.v1 path=born.py total=6\n"
                    "x = 1\n>>>>>>> simpraxis.chunk.v1.end")

    from orchestrator.common.callcache import CallReplay

    class ChunkGateway:
        def __init__(self):
            self.calls = []

        def model_id(self, stage):
            return "fake-coding-f1"

        def call(self, stage, messages, **kw):
            self.calls.append({"stage": stage, "messages": messages, "kw": kw})
            return bad_response, (None, None)

    class NoHits:
        def query_kb(self, query, *, namespace, top_k):
            return []

    gw = ChunkGateway()
    runner = AgentRunner(gw, NoHits(), evidence_root=tmp_path / "ev",
                         call_cache=CallReplay(tmp_path / "cache"))
    spec = RunSpec(
        thread_id="t-f1chunk", workdir=wd,
        cmd=[sys.executable, str(exec_script)],
        telemetry_relpath="summary.json", batch_id="T-F1",
        billing_task=6, run_idx=1, max_coding_rounds=1,
        coding_max_tokens=32768, coding_timeout_s=600,
        env_extra={"SIMPRAXIS_HOST_SHIM": "1"},
        chunked_birth=ChunkSpec(
            target_path="born.py", chunk_count=6,
            chunk_titles=(
                "头部/合成常量", "场景/合成注入", "步态/合成控制",
                "采集/合成判定", "batch/合成数据产物", "媒体/合成产物"),
            per_call_sections=(
                ("V2 升阶任务合同（F1 夹具）", "合同夹具"),
                ("已批准 macro 提案（F1 夹具）", '{"name": "fixture"}'),
                ("V1 结构骨架（F1 夹具）", "V1 骨架行")),
            chunk_boundaries=tuple(f"第 {i} 块边界（F1 夹具）。"
                                   for i in range(6))))
    rec = runner.run(spec)
    assert rec["status"] == "failed"
    assert len(gw.calls) == 1 and gw.calls[0]["stage"] == "coding_debug"
    blob = json.dumps(gw.calls[0], ensure_ascii=False)
    for w in SIX_FORBIDDEN:
        assert w not in blob, f"拆块通道模型 kwargs 命中禁词 {w!r}"
    assert str(PRELUDE) not in blob
    user_content = gw.calls[0]["messages"][1]["content"]
    assert TORCH_CUDA_NUMPY_TYPEERROR in user_content


# ===========================================================================
# F3：两句固定文案逐字快照（§3.5）
# ===========================================================================

def test_f3_two_fixed_sentences_verbatim():
    from orchestrator.graph import task6 as task6_mod
    assert task6_mod._SHIM_SUPPORT_SENTENCE == (
        "平台支持声明：本次 batch 经引擎侧主机迁移托底（sitecustomize Tensor "
        "托底，非模型代码；stderr 激活横幅与逐进程迁移计数 array/numpy/cpu_grad "
        "已机器核验入证据，rc0 核验 9 个 rollout 子进程 + 2 个 idle 进程）。"
        "若 V2 物理占优，结论口径为「去除运行时摩擦后物理改进成立，但代码经"
        "平台托底」，不得表述为模型独立修对 GPU→numpy 迁移；run6 的 3 轮独立"
        "修复失败结论原样保留。")
    assert task6_mod._SHIM_ZERO_MIGRATION_SENTENCE == (
        "托底已激活但本次迁移计数为 0（9+2 进程拓扑核验通过），本批结果未实际"
        "依赖托底，**不得归因托底**，转人工研判。")
    # 默认 None 保持旧行为：problem 不含两句任一。
    expl = task6_mod.build_gate2_explanation({"chosen": {"name": "x"}}, {}, None)
    assert "平台支持声明" not in expl["problem"]
    assert "不得归因托底" not in expl["problem"]


# ===========================================================================
# hook 预检四测（§3.6，MINOR 卫生项；非安全门）
# ===========================================================================

def _hook_runner(tmp_path, workdir, gw):
    from orchestrator.agent_runner import AgentRunner
    exec_script = tmp_path / "exec_outside.py"
    exec_script.write_text(
        "import sys\nfrom pathlib import Path\n"
        "wd = Path(sys.argv[1])\n"
        "(wd / 'EXEC_SENTINEL').write_text('1')\n"
        "(wd / 'summary.json').write_text('{\"ok\": true}')\n",
        encoding="utf-8")

    def _run(spec_env):
        from orchestrator.agent_runner import RunSpec
        runner = AgentRunner(gw, type("V", (), {
            "query_kb": lambda self, q, **k: []})(),
            evidence_root=tmp_path / "ev", call_cache=None, recall=None)
        spec = RunSpec(
            thread_id="t-hook", workdir=workdir,
            cmd=[sys.executable, str(exec_script), str(workdir)],
            telemetry_relpath="summary.json", batch_id="T-HOOK",
            billing_task=6, run_idx=1, max_coding_rounds=1,
            env_extra=dict(spec_env))
        return runner.run(spec), gw.calls

    return _run


class _HookGateway:
    def __init__(self):
        self.calls = []

    def model_id(self, stage):
        return "fake-hook"

    def call(self, stage, messages, **kw):
        self.calls.append({"stage": stage, "messages": messages})
        return json.dumps({"patches": [{"path": "x.py", "old": "",
                                        "new": "x=1\n"}]}), (None, None)


@pytest.mark.parametrize("hook_rel", [
    "sitecustomize.py", "shim_core.py", "usercustomize.py",
    "__pycache__/sitecustomize.cpython-310.pyc",
    "__pycache__/shim_core.cpython-310.pyc",
    "__pycache__/usercustomize.cpython-310.pyc",
])
def test_hook_precheck_marker_on_each_basename_blocks_exec(tmp_path, hook_rel):
    from orchestrator.common.host_shim.shim_core import redact_for_model
    wd = tmp_path / "wd"
    hook = wd / hook_rel
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("# hook impostor fixture\n", encoding="utf-8")
    gw = _HookGateway()
    run = _hook_runner(tmp_path, wd, gw)
    rec, calls = run({"SIMPRAXIS_HOST_SHIM": "1"})
    assert rec["status"] == "failed"
    assert calls == [] and gw.calls == []                 # 0 模型调用
    assert not (wd / "EXEC_SENTINEL").exists()           # 本轮零 exec
    entry = rec["timeline"][0]
    assert entry["rc"] is None and "stderr_only_tail" in entry
    assert hook.name in rec["reason"]                    # 真名只进人侧
    assert "卫生预检" in rec["reason"]
    # 模型面先过 redactor 兜底：六禁词含 sitecustomize/shim_core，命中两者
    # 的诊断逐物理行整丢；usercustomize 刻意不在六词元组（其诊断不含任何
    # 六禁词，净化后原文保留——而本轮 0 模型调用，它永远到不了模型面）。
    stem = hook.name.split(".")[0]
    redacted = redact_for_model(rec["reason"])
    if stem in ("sitecustomize", "shim_core"):
        assert redacted == ""
    else:
        assert stem == "usercustomize"
        assert "usercustomize" in redacted
        assert all(w not in redacted for w in (
            "SIMPRAXIS_HOST_SHIM", "simpraxis-host-shim", "host_shim",
            "平台托底", "sitecustomize", "shim_core"))


def test_hook_precheck_four_cases(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir(parents=True)
    # (1) marker 关：三件在场也 noop，照常 exec 成功。
    for name in ("sitecustomize.py", "shim_core.py", "usercustomize.py"):
        (wd / name).write_text("# fixture\n", encoding="utf-8")
    gw = _HookGateway()
    run = _hook_runner(tmp_path, wd, gw)
    rec, calls = run({})
    assert rec["status"] == "ok" and calls == []
    assert (wd / "EXEC_SENTINEL").is_file()
    (wd / "EXEC_SENTINEL").unlink()
    (wd / "summary.json").unlink()
    for name in ("sitecustomize.py", "shim_core.py", "usercustomize.py"):
        (wd / name).unlink()

    # (3) 真实 workdir 形态（V1/V2 + 校准件 + params/）不触发。
    for v in ("genesis_humanoid_dr.py", "genesis_humanoid_dr_v2.py"):
        (wd / v).write_text("# V fixture\n", encoding="utf-8")
    (wd / "push_window_calibration.json").write_text("{}", encoding="utf-8")
    (wd / "params").mkdir(exist_ok=True)
    gw2 = _HookGateway()
    run2 = _hook_runner(tmp_path, wd, gw2)
    rec2, calls2 = run2({"SIMPRAXIS_HOST_SHIM": "1"})
    assert rec2["status"] == "ok" and calls2 == []
    assert (wd / "EXEC_SENTINEL").is_file()
    (wd / "EXEC_SENTINEL").unlink()
    (wd / "summary.json").unlink()

    # (4) 子目录同名件不触发（非递归）。
    sub = wd / "subdir"
    sub.mkdir(exist_ok=True)
    (sub / "sitecustomize.py").write_text("# deep fixture\n",
                                          encoding="utf-8")
    gw3 = _HookGateway()
    run3 = _hook_runner(tmp_path, wd, gw3)
    rec3, calls3 = run3({"SIMPRAXIS_HOST_SHIM": "1"})
    assert rec3["status"] == "ok" and calls3 == []
    assert (wd / "EXEC_SENTINEL").is_file()


# ===========================================================================
# R1–R4：rehang 手术脚本在 tmp 合成树上干跑/施用（不碰活库，不写票）
# REHANG_SCRIPT 常量与在位守卫见文件头（H-1：操作脚本不入库）。
# ===========================================================================


THREAD = "t6-genesis-ascent-20261016a"
KEEP_CID = "1f1cc52e-f57a-6639-8003-a01b57b61cc0"
TERM_CIDS = ["1f1ce1f7-a452-6c94-8005-91a5e92175b8",   # human_failure 最新
             "1f1ce1f7-9480-6e11-8004-5baaf23ae936"]   # agent_run_failed
OTHER_NS_CID = "00000000-0000-6000-8000-0000000000aa"  # 非终态 cid（验 ns 谓词）


def _cp_blob(events, agent_runs):
    return msgpack.packb(
        {"channel_values": {
            "events": [{"type": e} for e in events],
            "agent_runs": list(range(agent_runs))}},
        use_bin_type=True)


def _exact_bytes(path: Path, size: int, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    if path.stat().st_size < size:
        with path.open("a", encoding="utf-8") as fh:
            fh.write("#" * (size - path.stat().st_size - 1) + "\n")
    assert path.stat().st_size == size, (path, path.stat().st_size)


def _build_rehang_tree(root: Path) -> dict:
    """按脚本 shape gate 期望构造完整合成树（全部在 tmp，零 /mnt）。"""
    root.mkdir(parents=True, exist_ok=True)
    state = root / "state"
    state.mkdir()
    db = state / "langgraph_checkpoints.db"
    gates = state / "pending_gates.db"
    workdir = root / "work"
    workdir.mkdir()
    evid = root / "evidence"
    evid.mkdir()
    (evid / "coding_round2.req.json").write_text("{}")
    graph_failure = root / "graph_FAILURE.json"
    graph_failure.write_text("{}")
    cache = root / "call_cache"
    cache.mkdir()

    con = sqlite3.connect(db)
    con.execute("CREATE TABLE checkpoints (thread_id TEXT, checkpoint_ns TEXT,"
                " checkpoint_id TEXT, checkpoint BLOB)")
    con.execute("CREATE TABLE writes (thread_id TEXT, checkpoint_ns TEXT,"
                " checkpoint_id TEXT)")
    # rowid 顺序：keep 最旧 → term2 → term1 最新（脚本按 rowid DESC 取三行）。
    con.execute("INSERT INTO checkpoints VALUES (?,?,?,?)", (
        THREAD, "", KEEP_CID,
        _cp_blob(["direction", "gate1_approved"], 0)))
    con.execute("INSERT INTO checkpoints VALUES (?,?,?,?)", (
        THREAD, "", TERM_CIDS[1],
        _cp_blob(["direction", "gate1_approved", "agent_run_failed"], 1)))
    con.execute("INSERT INTO checkpoints VALUES (?,?,?,?)", (
        THREAD, "", TERM_CIDS[0],
        _cp_blob(["direction", "gate1_approved", "agent_run_failed",
                  "human_failure"], 1)))
    con.execute("INSERT INTO writes VALUES (?,?,?)",
                (THREAD, "", KEEP_CID))   # gate1 的 1 行 __error__
    con.commit()
    con.close()
    gates.write_text("")                  # 脚本只验存在 + copy2 备份

    _exact_bytes(workdir / "genesis_humanoid_dr_v2.py", 90407,
                 'if __name__ == "__main__":\n    print("v2")\n')
    _exact_bytes(workdir / "genesis_humanoid_dr.py", 43324, "# v1 fixture\n")
    (workdir / "push_window_calibration.json").write_text("{}")
    (workdir / "params").mkdir()
    (workdir / "videos").mkdir()
    (workdir / "README.md").write_text("r")
    (workdir / "versions.txt").write_text("v")
    (workdir / "FAILURE.json").write_text("{}")
    (workdir / "vram_peak.json").write_text("{}")
    pycache = workdir / "__pycache__"
    pycache.mkdir()
    (pycache / "v2.cpython-310.pyc").write_text("x")

    for key in (f"coding_{THREAD}_r1_round2.json",
                f"coding_{THREAD}_r1_round3.json"):
        (cache / key).write_text("{}")
    chunk_name = f"coding_{THREAD}_r1_round1_chunk0.json"
    (cache / chunk_name).write_text("{}")

    return {"STATE_DIR": state, "DB": db, "GATES_DB": gates,
            "WORKDIR": workdir, "EVID": evid, "GRAPH_FAILURE": graph_failure,
            "CALLCACHE_DIR": cache,
            "DB_BACKUP_DIR": state / "checkpoint_backups",
            "ARCHIVE": root / "archive", "THREAD": THREAD,
            "KEEP": KEEP_CID, "TERM": TERM_CIDS,
            "CHUNK_NAME": chunk_name}


def _load_rehang(monkeypatch, paths, *, apply=False):
    spec = importlib.util.spec_from_file_location(
        f"rehang_under_test_{id(paths)}", REHANG_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for key in ("STATE_DIR", "DB", "GATES_DB", "WORKDIR", "EVID",
                "GRAPH_FAILURE", "CALLCACHE_DIR", "DB_BACKUP_DIR",
                "ARCHIVE"):
        monkeypatch.setattr(mod, key, str(paths[key]))
    monkeypatch.setattr(mod, "THREAD", paths["THREAD"])
    monkeypatch.setattr(sys, "argv",
                        ["rehang", *(["--apply"] if apply else [])])
    return mod


def _cp_count(db, thread, ns=""):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    n = con.execute(
        "SELECT COUNT(*) FROM checkpoints WHERE thread_id=? AND checkpoint_ns=?",
        (thread, ns)).fetchone()[0]
    con.close()
    return n


def _run_main(mod):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = mod.main()
    return rc, out.getvalue(), err.getvalue()


@_SKIP_NO_REHANG
def test_r1_dryrun_apply_reentrancy_bad_uuid(tmp_path, monkeypatch):
    # m4 静态钉：恰两条 DELETE，且各自谓词带 checkpoint_ns=''
    # （同串另出现在 rowid SELECT 与人侧注释，不属删除面，故按 DELETE 行钉）。
    src = REHANG_SCRIPT.read_text(encoding="utf-8")
    delete_lines = [ln for ln in src.splitlines() if "DELETE FROM" in ln]
    assert len(delete_lines) == 2
    assert all("checkpoint_ns=''" in ln for ln in delete_lines)

    paths = _build_rehang_tree(tmp_path / "tree")
    assert REHANG_SCRIPT.is_file()

    # —— dry-run：形状过、零改动、无备份、无归档 ——
    mod = _load_rehang(monkeypatch, paths, apply=False)
    rc, out, err = _run_main(mod)
    assert rc == 0, out + err
    assert "DRY-RUN OK" in out
    assert _cp_count(paths["DB"], THREAD) == 3
    assert not paths["ARCHIVE"].exists()
    assert not paths["DB_BACKUP_DIR"].exists()
    assert (paths["WORKDIR"] / "FAILURE.json").exists()
    assert (paths["CALLCACHE_DIR"]
            / f"coding_{THREAD}_r1_round2.json").is_file()

    # m4 动态钉：非终态 cid 的跨 namespace 行在 ns='' 删除后必须存活。
    con = sqlite3.connect(paths["DB"])
    con.execute("INSERT INTO checkpoints VALUES (?,?,?,?)",
                (THREAD, "subns", OTHER_NS_CID, b"x"))
    con.execute("INSERT INTO writes VALUES (?,?,?)",
                (THREAD, "subns", OTHER_NS_CID))
    con.commit()
    con.close()

    # —— apply：恰删 2 cp/0 writes，gate1 最新，跨 ns 行存活 ——
    rc, out, err = _run_main(_load_rehang(monkeypatch, paths, apply=True))
    assert rc == 0, out + err
    assert '"rehang": "ok"' in out and '"deleted_checkpoints": 2' in out
    assert _cp_count(paths["DB"], THREAD, ns="") == 1
    assert _cp_count(paths["DB"], THREAD, ns="subns") == 1
    con = sqlite3.connect(f"file:{paths['DB']}?mode=ro", uri=True)
    latest = con.execute(
        "SELECT checkpoint_id FROM checkpoints WHERE thread_id=? "
        "AND checkpoint_ns='' ORDER BY rowid DESC LIMIT 1",
        (THREAD,)).fetchone()[0]
    keep_writes = con.execute(
        "SELECT COUNT(*) FROM writes WHERE thread_id=? AND checkpoint_id=?",
        (THREAD, KEEP_CID)).fetchone()[0]
    other_writes = con.execute(
        "SELECT COUNT(*) FROM writes WHERE thread_id=? AND checkpoint_ns='subns'",
        (THREAD,)).fetchone()[0]
    term_left = con.execute(
        "SELECT COUNT(*) FROM checkpoints WHERE thread_id=? "
        "AND checkpoint_id IN (?,?)",
        (THREAD, *TERM_CIDS)).fetchone()[0]
    con.close()
    assert latest == KEEP_CID and keep_writes == 1 and other_writes == 1
    assert term_left == 0
    # 两库原件仍在（备份是 copy 不是替代）。
    assert paths["GATES_DB"].is_file() and paths["DB"].is_file()
    baks = sorted(paths["DB_BACKUP_DIR"].glob("*.bak"))
    assert len(baks) == 2

    # —— 可重入：resume 重建空证据卷/图 FAILURE 占位后再跑 → ALREADY REHUNG ——
    paths["EVID"].mkdir()
    paths["GRAPH_FAILURE"].write_text("{}")
    rc, out, err = _run_main(mod)
    assert rc == 0, out + err
    assert "ALREADY REHUNG" in out
    assert _cp_count(paths["DB"], THREAD, "") == 1

    # —— 错误 UUID 形（gate1 缺席，终态行还在）：拓扑门 FATAL、零删行零归档 ——
    bad = _build_rehang_tree(tmp_path / "bad")
    con = sqlite3.connect(bad["DB"])
    con.execute("DELETE FROM checkpoints WHERE checkpoint_id=?", (KEEP_CID,))
    con.execute("DELETE FROM writes WHERE checkpoint_id=?", (KEEP_CID,))
    con.commit()
    con.close()
    rc, out, err = _run_main(_load_rehang(monkeypatch, bad, apply=True))
    assert rc == 2 and "FATAL" in err
    assert _cp_count(bad["DB"], THREAD) == 2
    assert not bad["ARCHIVE"].exists() and not bad["DB_BACKUP_DIR"].exists()
    assert (bad["WORKDIR"] / "FAILURE.json").exists()


@_SKIP_NO_REHANG
def test_r2_shape_gate_five_aborts(tmp_path, monkeypatch):
    cases = []

    def fresh():
        d = tmp_path / f"case_{len(cases)}"
        return _build_rehang_tree(d)

    # 1) V2 != 90407
    p = fresh()
    (p["WORKDIR"] / "genesis_humanoid_dr_v2.py").write_text("short")
    cases.append(p)
    # 2) V1 != 43324
    p = fresh()
    (p["WORKDIR"] / "genesis_humanoid_dr.py").write_text("short")
    cases.append(p)
    # 3) py_compile 失败（字节数仍恰 90407，越过尺寸门到编译门）。
    p = fresh()
    v2 = p["WORKDIR"] / "genesis_humanoid_dr_v2.py"
    v2.write_text("def (:  # syntax error\n")
    v2.write_text(v2.read_text() + "#" * (90407 - v2.stat().st_size))
    assert v2.stat().st_size == 90407
    cases.append(p)
    # 4) summary.json 已存在
    p = fresh()
    (p["WORKDIR"] / "summary.json").write_text("{}")
    cases.append(p)
    # 5) 顶层白名单外第三件 .py
    p = fresh()
    (p["WORKDIR"] / "extra_run6_helper.py").write_text("# third py\n")
    cases.append(p)

    for i, p in enumerate(cases):
        mod = _load_rehang(monkeypatch, p, apply=True)
        rc, out, err = _run_main(mod)
        assert rc == 2, f"case {i} 应形状中止：\n{out}\n{err}"
        assert "FATAL" in err, f"case {i}\n{err}"
        # 零动作：无归档/无备份、cp 仍 3 行、workdir FAILURE 仍在原位。
        assert not p["ARCHIVE"].exists(), f"case {i} 不得产生归档"
        assert not p["DB_BACKUP_DIR"].exists(), f"case {i} 不得备份"
        assert _cp_count(p["DB"], THREAD) == 3, f"case {i} 不得删行"
        assert (p["WORKDIR"] / "FAILURE.json").exists()


@_SKIP_NO_REHANG
def test_r3_mv_not_rm_modes_m1_two_keys(tmp_path, monkeypatch):
    paths = _build_rehang_tree(tmp_path / "tree")
    wd = paths["WORKDIR"]
    inode_fail = (wd / "FAILURE.json").stat().st_ino
    inode_cache2 = (paths["CALLCACHE_DIR"]
                    / f"coding_{THREAD}_r1_round2.json").stat().st_ino

    mod = _load_rehang(monkeypatch, paths, apply=True)
    rc, out, err = _run_main(mod)
    assert rc == 0, out + err

    arch = paths["ARCHIVE"]
    assert oct(arch.stat().st_mode & 0o777) == "0o700"
    assert oct(paths["DB_BACKUP_DIR"].stat().st_mode & 0o777) == "0o700"
    cache_arch = arch / "call_cache"
    assert oct(cache_arch.stat().st_mode & 0o777) == "0o700"
    # 只 mv 不 rm：归档件 inode 与源一致，源位缺席。
    assert (arch / "workdir_FAILURE.json").stat().st_ino == inode_fail
    moved2 = cache_arch / f"coding_{THREAD}_r1_round2.json"
    assert moved2.stat().st_ino == inode_cache2
    assert oct(moved2.stat().st_mode & 0o777) == "0o600"
    for bak in paths["DB_BACKUP_DIR"].glob("*.bak"):
        assert oct(bak.stat().st_mode & 0o777) == "0o600"
    assert not (wd / "FAILURE.json").exists()
    assert not (paths["CALLCACHE_DIR"]
                / f"coding_{THREAD}_r1_round2.json").exists()
    # workdir 三归档件齐（FAILURE/vram_peak/__pycache__）。
    assert (arch / "workdir_vram_peak.json").is_file()
    assert (arch / "workdir___pycache__").is_dir()
    # 证据卷整卷 + 图级 FAILURE 改名归档。
    assert (arch / "task4_runner_evidence").is_dir()
    assert (arch / "graph_FAILURE_run6.json").is_file()
    # M1：恰两键归档；chunk 键留在原位不碰。
    left = sorted(p.name for p in paths["CALLCACHE_DIR"].iterdir())
    assert left == [paths["CHUNK_NAME"]]
    # 保留项术后仍在。
    for keep in ("genesis_humanoid_dr.py", "genesis_humanoid_dr_v2.py",
                 "push_window_calibration.json", "params", "videos",
                 "README.md", "versions.txt"):
        assert (wd / keep).exists(), keep

    # round4 键在场即 FATAL（零动作）。
    p2 = _build_rehang_tree(tmp_path / "case_forbidden")
    (p2["CALLCACHE_DIR"] / f"coding_{THREAD}_r1_round4.json").write_text("{}")
    rc2, out2, err2 = _run_main(_load_rehang(monkeypatch, p2, apply=True))
    assert rc2 == 2 and "FATAL" in err2
    assert not p2["ARCHIVE"].exists() and not p2["DB_BACKUP_DIR"].exists()
    assert _cp_count(p2["DB"], THREAD) == 3
    assert (p2["CALLCACHE_DIR"] / f"coding_{THREAD}_r1_round4.json").is_file()
    # chunk 键同样不被 M1 归档。
    assert (p2["CALLCACHE_DIR"] / p2["CHUNK_NAME"]).is_file()


@_SKIP_NO_FROZEN_SPEC
def test_r4_ticket_schema_fields_pinned_without_writing_ticket():
    """R4：T6-B11-run7 票件 schema/字段在测试里钉死；票本身本轮绝不写出。"""
    required_note_fields = (
        "用户 2026-10-23「2+3 一起做」裁决引用",
        "模型 ID 零变更声明",
        "付费信封 5+2+1（5 coding/2 节点重放/1 储备，approved=8）",
        "三门余量（批前 115/128、91/106；批后 123/128、99/106）",
        "Phase_2_Plan_detailed.md L193/L544 显式覆盖声明",
        "矛盾即结论承诺（5 轮再败不调参/不翻案）",
        "M1 callcache 两键归档声明（watcher REPLAY_DUP 暂停条款）",
        "三表基线 kb_chunks=1542 / solution_chunks=82 / "
        "phase1_learnings=11（规格 §8 票件口径；活表基线 5→11 对账已由先于本"
        "特性提交的独立债务提交落定、CLAUDE.md 同步 11；执行人不顺手写库，"
        "run7 任一结局再 11→12）",
    )
    assert len(required_note_fields) == 8
    assert all(required_note_fields)
    # 票件未写：仓内 quota/approvals 无 T6-B11-run7 任何形态。
    approvals = REPO_ROOT / "quota" / "approvals"
    if approvals.is_dir():
        assert not list(approvals.rglob("*T6-B11-run7*"))
    # schema 锚点与冻结规格头 561 行逐字对账（信封/余量/三表不凭空造）。
    spec_text = FROZEN_SPEC_HANDOFF.read_text(encoding="utf-8")
    spec_head = "".join(spec_text.splitlines(keepends=True)[:561])
    for token in ("T6-B11-run7", "L193/L544", "approved=8", "1542",
                  "REPLAY_DUP", "123/128", "99/106", "5+2+1"):
        assert token in spec_head, token
    # 信封行逐字钉（防口径漂移）。
    assert "envelope 8（5 coding+2 节点重放+1 储备）" in spec_head
