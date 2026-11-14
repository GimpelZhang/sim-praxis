"""P281-2 出生隔离 v2 测试（AC-P281-2-01..11、15）。

三条主线：

1. **地址空间隔离**（01/03/15）：子进程的输入里没有挑战值、也没有结果路径；
   候选主动去 ``__main__`` 拿会直接导入失败；``birth_child.py`` 的源码里连这三个
   词根都不出现（扫描面**非空真**：同一批词根在 ``birth_protocol.py`` 里确实存在，
   用例逐字断言这一点，否则「扫不到」只说明扫错了文件）。
2. **判定归父进程**（02/04/05/09）：伪造 stdout 标记、自写 ``birth.json``、
   静默早退、截断事件流——四种「候选想替父进程下结论」的形态，父进程一律不采信，
   结论仍按内核可观测量给出。
3. **进程清理与资源帽**（06/07/08）：超时、fork 逃逸、写盘洪水三种失控形态，
   父进程必须整组击杀并把实际字节记进 ``workdir_facts``。

parity（AC-10）分两车道：默认车道用鸭子类型候选（不 import mujoco）覆盖 B0–B4，
``kb_integration`` 车道在 Phase 2.8.0 的三个**参考运行**上复验（活数据层）。

诚实边界（与计划 §6.1 同口径，报告里不得写强）：本设计**不提供沙箱**。子进程仍可
fork、可写自己的文件、可伪造事件流；一个**刻意构造一致事件流**的恶意子进程仍可能
骗过判定——那是进程模型的固有上限（Phase 3 backlog），不是本 Task 遗漏。
AC-02/04/05/09 封堵的是「偷懒的伪造」，不是「能证明任何伪造都不可能」。
"""
from __future__ import annotations

import hashlib
import json
import os
import stat as statmod
from pathlib import Path

import pytest

from orchestrator.benchmark import birth as v1
from orchestrator.benchmark.phase281 import birth_protocol as P
from orchestrator.benchmark.phase281 import candidate_birth as cb
from tests.benchmark import phase281_birth_fixtures as fx

REPO_ROOT = Path(__file__).resolve().parents[2]
CHILD_SRC = REPO_ROOT / "orchestrator" / "benchmark" / "phase281" / "birth_child.py"
PROTOCOL_SRC = (REPO_ROOT / "orchestrator" / "benchmark" / "phase281"
                / "birth_protocol.py")
BIRTH_CLI = REPO_ROOT / "scripts" / "benchmark" / "phase281_birth.py"


@pytest.fixture(autouse=True)
def _no_benchmark_root_env(monkeypatch):
    """入口断言（P281-0 已锁）要求本阶段进程里 ``SIMPRAXIS_BENCHMARK_ROOT`` 未设置。"""
    monkeypatch.delenv("SIMPRAXIS_BENCHMARK_ROOT", raising=False)


def _trial(tmp_path: Path, name: str = "trial") -> Path:
    directory = tmp_path / name
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    return directory


def _work(tmp_path: Path, source: str, name: str = "work") -> Path:
    return fx.write_workdir(tmp_path / name, {"solution.py": source})


# --------------------------------------------------------------------------
# AC-P281-2-01：cfg 里没有挑战值/结果路径
# --------------------------------------------------------------------------

def test_ac_p281_2_01_no_nonce_in_child(tmp_path: Path, monkeypatch):
    """子进程输入是**闭集**；挑战值/结果路径不在其中，也不许出现。

    计划原文这条写的是 ``diagnostics.nonce_in_child == true``，而 §6.4 的字段表写
    ``"nonce_in_child": false``。两者互斥；本实现取 §6.4（负向事实：子进程侧**不存在**
    挑战值）并在 Complete 登记。真正的正向事实由 ``nonce_absent``（子进程自报）
    与 ``nonce_absent_verified``（父进程校验过格式）承担，两者在下面一并机判。
    """
    work = _work(tmp_path, fx.CANDIDATE_OK)
    trial = _trial(tmp_path)
    _result, doc, _validator = fx.run_birth(work, trial, level="B1")

    cfg_path = trial / "birth_levels" / "B1" / "cfg.json"
    assert cfg_path.is_file(), "cfg.json 必须留档（AC 的机判面就是它）"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert set(cfg) == set(cb.CFG_KEYS), sorted(cfg)
    # 扫之前抹掉 tmp 路径：``workdir``/``ctx.workdir`` 里必然带着本用例的目录名，
    # 而 pytest 的目录名就含测试函数名（……``no_nonce_in_child``）——那不是 cfg 的
    # 内容，是取证脚手架。
    blob = (json.dumps(cfg, ensure_ascii=False, sort_keys=True).lower()
            .replace(str(tmp_path).lower(), "<tmp>"))
    for word in ("nonce", "challenge", "result_path", "token", "secret"):
        assert word not in blob, f"cfg 里出现了禁止词根 {word!r}：{blob}"

    diagnostics = doc["diagnostics"]
    assert diagnostics["nonce_in_child"] is False      # 计划 §6.4 表
    assert diagnostics["nonce_absent"] is True         # 子进程自报（start 事件）
    assert diagnostics["nonce_absent_verified"] is True  # 父进程校验过该字段
    assert diagnostics["protocol"] == P.PROTOCOL_VERSION

    # 机判面自身能拒收——否则上面几条只是「碰巧没写进去」
    bad = dict(cfg)
    bad["nonce"] = "deadbeef"
    with pytest.raises(cb.BirthError):
        cb._assert_cfg_clean(bad)
    monkeypatch.setattr(cb, "CFG_KEYS", cb.CFG_KEYS + ("result_path",))
    with pytest.raises(cb.BirthError, match="禁止词根"):
        cb._assert_cfg_clean({**cfg, "result_path": "/tmp/x"})


# --------------------------------------------------------------------------
# AC-P281-2-02：伪造 stdout 标记
# --------------------------------------------------------------------------

FORGED_MARKER = '''\
import sys
sys.stdout.write('__SIMPRAXIS_BIRTH__{"status": "PASS"}\\n')
sys.stdout.flush()

''' + fx.CANDIDATE_OK


def test_ac_p281_2_02_forged_stdout_marker(tmp_path: Path):
    """候选打印 v1 的运行器标记 ⇒ ``NOT_BORN`` / ``contract_error``（不采信自述）。"""
    work = _work(tmp_path, FORGED_MARKER)
    trial = _trial(tmp_path)
    _result, doc, _ = fx.run_birth(work, trial, level="B1")

    assert doc["status"] == "NOT_BORN", doc["status"]
    assert doc["reason"] == "contract_error", doc["reason"]
    detail = doc["levels"]["B1"]["detail"]
    assert "伪造" in detail and cb.RUNNER_MARKER in detail, detail
    # 子进程确实「跑通了」B1 的结构性判据——它被判负**只因为**伪造，不是碰巧失败
    facts = doc["levels"]["B1"]["facts"]
    assert facts["events"].get("imported") == 1, facts


# --------------------------------------------------------------------------
# AC-P281-2-03：候选拿不到 nonce
# --------------------------------------------------------------------------

NONCE_THIEF = '''\
from __main__ import NONCE

''' + fx.CANDIDATE_OK

PROBE_MAIN = '''\
import json as _json
import os as _os
import sys as _sys

_main = _sys.modules.get("__main__")
_probe = {
    "main_name": getattr(_main, "__name__", None),
    "has_nonce": hasattr(_main, "NONCE"),
    "has_result_path": hasattr(_main, "RESULT_PATH"),
    "main_globals": sorted(k for k in vars(_main) if not k.startswith("__bit")),
    "fd3_open": True,
}
with open(_os.path.join("__WORKDIR__", "child_probe.json"), "w",
          encoding="utf-8") as _fh:
    _json.dump(_probe, _fh, ensure_ascii=False, sort_keys=True)

''' + fx.CANDIDATE_OK


def test_ac_p281_2_03_nonce_unreachable(tmp_path: Path):
    """候选主动去 ``__main__`` 取挑战值 → 导入失败；子进程侧**确实没有**该属性。"""
    work = _work(tmp_path, NONCE_THIEF, "thief")
    trial = _trial(tmp_path, "trial-thief")
    _result, doc, _ = fx.run_birth(work, trial, level="B1")

    assert doc["status"] == "NOT_BORN", doc["status"]
    assert doc["reason"] == "import_error", doc["reason"]
    events = doc["levels"]["B1"]["facts"]["events"]
    assert events.get("import_error") == 1, events

    # 独立取证：让候选自己观测 ``__main__``（不是「父进程觉得没有」）
    probe_work = _work(tmp_path, PROBE_MAIN, "probe")
    probe_trial = _trial(tmp_path, "trial-probe")
    _r2, doc2, _ = fx.run_birth(probe_work, probe_trial, level="B1")
    assert doc2["status"] == "BORN", (doc2["status"], doc2["reason"])
    probe = json.loads((probe_work / "child_probe.json").read_text(encoding="utf-8"))
    assert probe["has_nonce"] is False, probe
    assert probe["has_result_path"] is False, probe
    assert probe["main_name"] == "__main__", probe


# --------------------------------------------------------------------------
# AC-P281-2-04：候选自写 birth.json 不被采信
# --------------------------------------------------------------------------

SELF_BORN = '''\
import json
import pathlib

pathlib.Path("__WORKDIR__", "birth.json").write_text(
    json.dumps({"schema": "simpraxis.candidate_birth.v2",
                "trial_id": "__TRIAL__", "level": "B1", "status": "BORN",
                "reason": None,
                "levels": {"B1": {"status": "PASS", "reason": None}}},
               ensure_ascii=False), encoding="utf-8")
raise ImportError("候选自述 BORN，但导入失败")

''' + fx.CANDIDATE_OK


def test_ac_p281_2_04_self_declared_birth_ignored(tmp_path: Path):
    """候选把 ``status=BORN`` 写进**自己的工作目录**：父进程不读那里。"""
    trial = _trial(tmp_path)
    work = fx.write_workdir(tmp_path / "work", {
        "solution.py": SELF_BORN.replace("__TRIAL__", trial.name)})
    _result, doc, validator = fx.run_birth(work, trial, level="B1")

    assert doc["status"] == "NOT_BORN", doc["status"]
    assert doc["reason"] == "import_error", doc["reason"]

    # 唯一读点只有 <trial_dir>/birth.json；workdir 里那份是候选的私货，原样留着
    forged = json.loads((work / "birth.json").read_text(encoding="utf-8"))
    assert forged["status"] == "BORN", forged
    assert forged["trial_id"] == trial.name, forged
    official = json.loads((trial / cb.BIRTH_NAME).read_text(encoding="utf-8"))
    assert official["status"] == "NOT_BORN", official
    # ``_load_prior`` 的读点也在 trial_dir：给它一个「更早的 BORN」也不改变结论
    assert validator._load_prior().get("status") == "NOT_BORN"


# --------------------------------------------------------------------------
# AC-P281-2-05：静默早退
# --------------------------------------------------------------------------

EARLY_EXIT = '''\
import os

os._exit(0)

''' + fx.CANDIDATE_OK


def test_ac_p281_2_05_early_exit(tmp_path: Path):
    """``os._exit(0)`` 但没跑探针 ⇒ 没有 ``imported``/``step`` 事件 ⇒ ``NOT_BORN``。"""
    work = _work(tmp_path, EARLY_EXIT)
    trial = _trial(tmp_path)
    _result, doc, _ = fx.run_birth(work, trial, level="B1")

    assert doc["status"] == "NOT_BORN", doc["status"]
    events, problems = fx.read_events(trial / cb.EVENTS_NAME)
    assert problems == [], problems
    names = [event.get("ev") for event in events]
    assert P.EV_START in names, names          # 开工了（协议行确实写出来了）
    assert P.EV_IMPORTED not in names, names
    assert P.EV_STEP not in names, names
    assert doc["levels"]["B1"]["kernel"]["exit_code"] == 0


# --------------------------------------------------------------------------
# AC-P281-2-06 / 07：超时与 fork 逃逸的进程组清理
# --------------------------------------------------------------------------

HANG = '''\
import os
import pathlib

pathlib.Path("__WORKDIR__", "child.pid").write_text(
    str(os.getpid()), encoding="utf-8")
while True:
    pass
'''

FORKER = '''\
import os
import pathlib
import time

_gc = os.fork()
if _gc == 0:
    pathlib.Path("__WORKDIR__", "grandchild.pid").write_text(
        str(os.getpid()), encoding="utf-8")
    while True:
        time.sleep(0.05)
os._exit(0)
'''


def _pid_file(path: Path, timeout: float = 5.0) -> int:
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return int(text)
        _time.sleep(0.05)
    raise AssertionError(f"{path} 未出现（候选没跑到写 pid 那一步）")


def test_ac_p281_2_06_timeout_kills_group(tmp_path: Path):
    """无限循环 ⇒ ``reason=timeout``，且进程组被整体击杀（无残留 pid）。"""
    work = _work(tmp_path, HANG)
    trial = _trial(tmp_path)
    limits = cb.LimitsV2(wall_s=2.0, cpu_s=180)
    _result, doc, _ = fx.run_birth(work, trial, level="B1", limits=limits)

    assert doc["reason"] == "timeout", (doc["status"], doc["reason"])
    assert doc["status"] == "NOT_BORN", doc["status"]
    kernel = doc["levels"]["B1"]["kernel"]
    assert kernel["timed_out"] is True, kernel
    assert kernel["reaped"] is True, kernel
    assert kernel["wall_s"] < limits.wall_s + 30.0, kernel

    pid = _pid_file(work / "child.pid")
    assert fx.wait_gone(pid), f"pid {pid} 超时后仍活着（进程组未清理）"


def test_ac_p281_2_07_fork_escape_cleaned(tmp_path: Path):
    """候选 fork 后父退出、孙进程继续 ⇒ 父进程仍要清掉整个进程组。"""
    work = _work(tmp_path, FORKER)
    trial = _trial(tmp_path)
    limits = cb.LimitsV2(wall_s=10.0)
    _result, doc, _ = fx.run_birth(work, trial, level="B1", limits=limits)

    assert doc["status"] == "NOT_BORN", doc["status"]
    grandchild = _pid_file(work / "grandchild.pid")
    assert fx.wait_gone(grandchild), (
        f"孙进程 {grandchild} 在子进程退出后仍活着（fork 逃逸未被清理）")


# --------------------------------------------------------------------------
# AC-P281-2-08：资源帽
# --------------------------------------------------------------------------

FLOOD = '''\
try:
    with open("__WORKDIR__/big.bin", "wb") as fh:
        fh.write(b"x" * (64 * 1024 * 1024))
except OSError:
    pass
# 再吃内存：``bytearray`` 会真的落页（RSS 可观测），不是只占虚拟地址
_hog = bytearray(512 * 1024 * 1024)
for _i in range(0, len(_hog), 4096):
    _hog[_i] = 1
'''


def test_ac_p281_2_08_resource_limits(tmp_path: Path):
    """资源帽的两半各验一次：**磁盘**帽与**内存**帽。

    磁盘帽的实测形态值得记下来（计划 §6.3 只写了「触发上限」）：CPython 启动时把
    ``SIGXFSZ`` 设成 ``SIG_IGN``（本用例外的机制，不是本设计的选择），所以
    ``RLIMIT_FSIZE`` 越界**不会**变成信号击杀，而是 ``write`` 抛 ``OSError``
    (EFBIG) 并留下恰好等于上限的前缀文件。因此「``reason=memory``」只能由**内存**
    帽触发——父进程 ``_judge`` 在诊断事件之前先看 ``maxrss``，超帽即判 ``memory``。
    两半都要有：磁盘帽验的是「写不穿」，内存帽验的是「被判负的类别」。
    """
    # ① 磁盘帽：64 MiB 写不进去，文件被截在上限，父进程 bytes 照实记
    work = _work(tmp_path, FLOOD, "disk")
    trial = _trial(tmp_path, "trial-disk")
    limits = cb.LimitsV2(wall_s=120.0, fsize_bytes=1 << 20)
    _result, doc, _ = fx.run_birth(work, trial, level="B1", limits=limits)

    big = work / "big.bin"
    assert big.is_file(), "写盘被拦住了，但文件应当已经落了一部分"
    assert big.stat().st_size == limits.fsize_bytes, big.stat().st_size
    expected = sum(path.stat().st_size for path in work.rglob("*")
                   if path.is_file() and not path.is_symlink())
    facts = doc["workdir_facts"]
    assert facts["bytes"] == expected, (facts["bytes"], expected)
    assert facts["bytes"] >= limits.fsize_bytes, facts
    assert facts["over_disk_cap"] is False, facts

    # ② 内存帽：maxrss 超帽 ⇒ ``reason=memory``（不是 import_error/contract_error）
    work2 = _work(tmp_path, FLOOD, "rss")
    trial2 = _trial(tmp_path, "trial-rss")
    limits2 = cb.LimitsV2(wall_s=120.0, fsize_bytes=1 << 20, max_rss_mb=128)
    _r2, doc2, _ = fx.run_birth(work2, trial2, level="B1", limits=limits2)

    assert doc2["status"] == "NOT_BORN", doc2["status"]
    assert doc2["reason"] == "memory", (doc2["reason"],
                                        doc2["levels"]["B1"]["detail"])
    kernel = doc2["levels"]["B1"]["kernel"]
    assert kernel["maxrss_kb"] > limits2.max_rss_mb * 1024, kernel
    assert "maxrss" in doc2["levels"]["B1"]["detail"], doc2["levels"]["B1"]["detail"]


# --------------------------------------------------------------------------
# AC-P281-2-09：事件流被截断
# --------------------------------------------------------------------------

TRUNCATE = '''\
import os

os.write(3, b'{"ev": "step", "level": "B1"')
os._exit(0)
'''


def test_ac_p281_2_09_truncated_events(tmp_path: Path):
    """事件流半行 ⇒ ``NOT_BORN`` + ``diagnostics.event_conflicts`` 非空。"""
    work = _work(tmp_path, TRUNCATE)
    trial = _trial(tmp_path)
    _result, doc, _ = fx.run_birth(work, trial, level="B1")

    assert doc["status"] == "NOT_BORN", doc["status"]
    assert doc["reason"] == "event_conflict", doc["reason"]
    conflicts = doc["diagnostics"]["event_conflicts"]
    assert conflicts, conflicts
    assert any("事件流" in item for item in conflicts), conflicts
    assert doc["levels"]["B1"]["conflicts"], doc["levels"]["B1"]["conflicts"]


# --------------------------------------------------------------------------
# AC-P281-2-10：与 v1 BirthValidator 逐层 parity
# --------------------------------------------------------------------------

def _v1_spec(task_id: str = "synthetic",
             birth_contract: "dict[str, bool] | None" = None,
             contract: str = "mujoco_push") -> v1.BirthSpec:
    return v1.BirthSpec(task_id=task_id, entry_kind="python_script",
                        entry_path="solution.py",
                        birth_contract=dict(fx.BIRTH_ALL if birth_contract is None
                                            else birth_contract),
                        contract=contract, min_bytes=16)


def _level_statuses(doc: dict) -> "dict[str, str]":
    return {level: str(doc["levels"][level]["status"]) for level in cb.LEVELS}


def test_ac_p281_2_10_parity_with_v1(tmp_path: Path):
    """同一份候选 → v1 与 v2 对 B0–B4 的判定**逐层相同**（默认车道）。

    候选是鸭子类型对象，不 import mujoco：``mujoco_push`` 契约只读
    ``action_dim``/``step``/``dt``，所以这条 parity 在默认车道就能跑，不必等活数据层。
    """
    work = _work(tmp_path, fx.CANDIDATE_OK)
    legacy = v1.BirthValidator(_v1_spec(), workdir=work).validate(tmp_path / "v1")
    _result, doc, _ = fx.run_birth(work, _trial(tmp_path), level="ALL")

    assert legacy.birth_status == doc["status"] == "BORN", (
        legacy.birth_status, doc["status"])
    assert _level_statuses(legacy.to_json()) == _level_statuses(doc)


@pytest.mark.kb_integration
def test_ac_p281_2_10b_parity_reference_runs(tmp_path: Path):
    """**活数据层**版 parity：Phase 2.8.0 的三个参考运行（真候选，含 skills 包）。

    workdir 取 ``pinned_run(...)/candidate`` 而不是 ``delivered/``：后者是评测用的
    扁平产物快照，**不含**运行器 staged 出来的 ``simpraxis_skills``，拿它跑出生会
    得到 ``No module named 'simpraxis_skills'``——那是搬运形态的差异，不是候选的
    能力差异（P281-1 已就 ``delivered/`` 的边界登记过）。入口文件名也**不统一**
    （``solution.py`` / ``adapter.py`` / ``build.py``），一律取冻结定义里的
    ``entry_path``，不在测试里硬编码。
    """
    from orchestrator.benchmark import paths as p0_paths

    for task_id in ("mujoco_contact_push_v1", "libero_env_adapter_v1",
                    "icy_ground_dr_v1"):
        run = p0_paths.pinned_run(None, task_id)
        work = run / "candidate"
        birth_spec = v1.BirthSpec.from_task_spec(cb.load_task(task_id))
        assert (work / birth_spec.entry_path).is_file(), (task_id, work)

        legacy = v1.BirthValidator(birth_spec, workdir=work).validate(
            tmp_path / f"{task_id}-v1")
        _result, doc, _ = fx.run_birth(
            work, _trial(tmp_path, f"{task_id}-trial"), level="ALL",
            entry=birth_spec.entry_path,
            birth_contract=dict(birth_spec.birth_contract),
            contract=birth_spec.contract, min_bytes=birth_spec.min_bytes)

        assert legacy.birth_status == doc["status"], (
            task_id, legacy.birth_status, doc["status"])
        assert _level_statuses(legacy.to_json()) == _level_statuses(doc), task_id


# --------------------------------------------------------------------------
# AC-P281-2-11：birth.json 卫生
# --------------------------------------------------------------------------

def test_ac_p281_2_11_birth_file_hygiene(tmp_path: Path):
    """``birth.json`` 0600、父目录 0700、不是符号链接（原子写不留过渡件）。"""
    work = _work(tmp_path, fx.CANDIDATE_OK)
    trial = _trial(tmp_path)
    result, _doc, validator = fx.run_birth(work, trial, level="B1")
    saved = validator.write(result)

    path = Path(saved["birth_path"])
    assert path == trial / cb.BIRTH_NAME
    assert not path.is_symlink()
    assert statmod.S_IMODE(path.stat().st_mode) == 0o600
    assert statmod.S_IMODE(trial.stat().st_mode) == 0o700
    assert saved["birth_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    # 目录里不留原子写的过渡件（``atomic_write_text`` 的 tmp 件必须是瞬时的）
    names = {entry.name for entry in trial.iterdir()}
    assert names <= {cb.BIRTH_NAME, cb.EVENTS_NAME, "birth_levels", ".birth_tmp"}, names
    assert cb.BIRTH_NAME in names, names


# --------------------------------------------------------------------------
# AC-P281-2-15：子进程源码干净
# --------------------------------------------------------------------------

_FORBIDDEN_WORDS = ("secrets", "token_hex", "nonce", "result_path")


def test_ac_p281_2_15_child_source_clean():
    """``birth_child.py`` 源码里不存在挑战值/结果路径的任何词根。

    非空真：同一批词根在 ``birth_protocol.py``（协议与探针所在）与
    ``candidate_birth.py``（父进程侧）里**确实存在**——用例逐字断言这一点，
    否则「扫不到」只说明扫错了文件。
    """
    child = CHILD_SRC.read_text(encoding="utf-8")
    lowered = child.lower()
    for word in _FORBIDDEN_WORDS:
        assert word not in lowered, f"birth_child.py 出现 {word!r}"
        assert word in PROTOCOL_SRC.read_text(encoding="utf-8").lower(), word
    # 父进程侧的行为证据：候选真的拿不到（AC-03 已机判），这里只钉源码面
    assert "NONCE" not in child


# --------------------------------------------------------------------------
# 计划 §6.10 的静态断言：v2 是**新件**，2.8.0 的出生/评测入口一字不动
# --------------------------------------------------------------------------

#: `82a1427` 时点的字节摘要（计划 §6.10 要求「`birth.py` 与 `evaluator.py` 的
#: sha256 与 82a1427 时点相同」）。v2 全部落在 ``phase281/`` 下，这两个文件是
#: 2.8.0 已结案阶段的能力入口——它们一改，2.8.0 的判决链就不再是那条被验证过的链。
V1_BIRTH_SHA256 = "44fbc6da9312273d6f493622aba9443294fec0f77344af894ad0da8895631b6f"
#: 2026-11-14 代码评审 CR-06（HG-EVAL-2 畸形审计件 fail-open）按用户批次的
#: 修复要求改了 ``evaluator.py::_hidden_access``；该门是 2.8.0 判决链的一部分，
#: 修完必须重登记本摘要（否则本用例会把**有意修复**误报成漂移）。行为由
#: ``test_task4_evaluator.py`` 的畸形审计件用例与 parity 用例覆盖。
V1_EVALUATOR_SHA256 = "9f7ef41aba88394180325298139d35d4db8ab8fa55cc24711eab9dd4e3be8061"


def test_phase281_2_v1_entrypoints_untouched():
    """2.8.0 的 ``birth.py`` / ``evaluator.py`` 与 82a1427 逐字节相同（§6.10）。"""
    for relpath, expected in (
            ("orchestrator/benchmark/birth.py", V1_BIRTH_SHA256),
            ("orchestrator/benchmark/evaluator.py", V1_EVALUATOR_SHA256)):
        data = (REPO_ROOT / relpath).read_bytes()
        assert hashlib.sha256(data).hexdigest() == expected, relpath


# ---------------------------------------------------------------------------
# 评审 CR-58：总量磁盘帽必须真的执行（RLIMIT_FSIZE 只管单文件）
# ---------------------------------------------------------------------------

DISK_FLOOD = '''\
for _i in range(3):
    with open(f"__WORKDIR__/part{_i}.bin", "wb") as fh:
        fh.write(b"x" * (1 << 20))
'''


def test_f5_cr58_disk_cap_is_enforced(tmp_path: Path):
    """写 3×1 MiB（单个文件都低于 fsize 帽）超过 2 MiB 总量帽 ⇒ 判负。

    缺陷形态：``over_disk_cap`` 只写进 ``workdir_facts`` 无人读；候选靠
    ``RLIMIT_FSIZE``（单文件 64 MiB）绕开总量帽仍 birth PASS。
    """
    work = _work(tmp_path, DISK_FLOOD, "diskcap")
    trial = _trial(tmp_path, "trial-diskcap")
    limits = cb.LimitsV2(fsize_bytes=4 << 20, disk_cap_bytes=2 << 20)
    _result, doc, _ = fx.run_birth(work, trial, level="B1", limits=limits)

    assert doc["status"] == "NOT_BORN", doc["status"]
    assert doc["reason"] == "memory", (doc["reason"],
                                       doc["levels"]["B1"]["detail"])
    assert "磁盘帽" in doc["levels"]["B1"]["detail"], doc["levels"]["B1"]["detail"]
    assert doc["workdir_facts"]["over_disk_cap"] is True, doc["workdir_facts"]
