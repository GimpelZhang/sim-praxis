"""Phase 2.8.0 Task 3 机判：AC-T3-1..8（Functional Birth Validator）。

纪律（硬规则 7 / T3.1 不变量）：

- **不 mock 子进程**：每个用例都真起 ``python -I -c`` 子进程，分类是子进程真实
  行为的结果（``os._exit(0)`` 真自杀、``while True`` 真被墙钟击杀、``bytearray(8G)``
  真撞 ``RLIMIT_AS``）；
- **不用真实物理引擎**：全部反例/正例都是合成 Python 假对象（Task 6 才跑真引擎）；
  支撑断言用 AST 守住这条边界；
- **不修改** ``orchestrator/agentic/loop.py``（AC-T3-8 用提交史 + AST 源码 sha 双扣）；
- ``Birth ≠ Task Success``：任何结果里 ``task_success`` 恒为 ``None``。
"""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.benchmark import birth, contracts, intake, registry

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIX = Path(__file__).resolve().parents[1] / "fixtures" / "benchmark" / "birth"

_DEFAULT_BC = {"import_required": True, "construction_required": True,
               "contract_required": True, "minimal_step_required": True}

#: AC-T3-2/3 的十类失败映射（fixture 目录 → 期望的 (层, reason)）
_TEN = {
    "missing": ("missing", "adapter.py", "B0", "missing"),
    "syntax": ("syntax_error", "adapter.py", "B0", "ast_error"),
    "import": ("import_error", "adapter.py", "B1", "import_error"),
    "construction": ("construction_error", "adapter.py", "B2", "construction_error"),
    "contract": ("contract_error", "adapter.py", "B3", "contract_error"),
    "reset": ("reset_error", "adapter.py", "B4", "step_error"),
    "step": ("step_error", "adapter.py", "B4", "step_error"),
    "nan": ("nan_step", "adapter.py", "B4", "non_finite"),
    "hang": ("hang", "adapter.py", "B4", "timeout"),
    "escape": ("escape", "adapter.py", "B0", "path_escape"),
}


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------

def _workdir(base: Path, fixture: str, entry: str = "adapter.py") -> Path:
    """把 fixture 复制成一次性 workdir；``*.py.txt`` 还原成 ``*.py``。"""
    wd = base / "wd"
    if wd.exists():
        shutil.rmtree(wd)
    shutil.copytree(_FIX / fixture, wd)
    renamed = wd / f"{entry}.txt"
    if renamed.exists():
        renamed.rename(wd / entry)
    return wd


def _spec(task_id: str = "libero_env_adapter_v1", *, contract: str = "libero_env_adapter",
          kind: str = "python_script", entry: str = "adapter.py",
          bc: "dict | None" = None, outputs: "tuple[str, ...]" = ()) -> birth.BirthSpec:
    return birth.BirthSpec(
        task_id=task_id, entry_kind=kind, entry_path=entry,
        birth_contract=dict(_DEFAULT_BC if bc is None else bc),
        contract=contract, outputs=tuple(outputs))


def _validate(wd: Path, out: Path, *, spec: "birth.BirthSpec | None" = None,
              limits: "birth.Limits | None" = None,
              network_mode: str = "audited_only"):
    validator = birth.BirthValidator(
        spec or _spec(), workdir=wd,
        limits=limits or birth.Limits(timeout_s=20.0),
        network_mode=network_mode)
    result = validator.validate(out)
    written = birth.write_birth(out, result)
    return result, written


@pytest.fixture(scope="module")
def good_run(tmp_path_factory):
    base = tmp_path_factory.mktemp("birth_good")
    wd = _workdir(base, "good")
    return _validate(wd, base / "out")


@pytest.fixture(scope="module")
def ten_runs(tmp_path_factory):
    """十类失败各跑一次（每类 5 层 = 5 个子进程），模块内复用结果。"""
    out = {}
    for name, (fixture, entry, _level, _reason) in _TEN.items():
        base = tmp_path_factory.mktemp(f"birth_{name}")
        if name == "escape":
            # 合法样本的入口换成指向 /etc/passwd 的软链接（逃出 workdir）
            wd = _workdir(base, "good", entry=entry)
            (wd / entry).unlink()
            (wd / entry).symlink_to("/etc/passwd")
        else:
            wd = _workdir(base, fixture, entry=entry)
        if name == "hang":
            limits = birth.Limits(timeout_s=3.0)
        elif name == "nan":
            limits = birth.Limits(timeout_s=20.0)
        else:
            limits = birth.Limits(timeout_s=20.0)
        result, _written = _validate(wd, base / "out", limits=limits)
        out[name] = result
    return out


# --------------------------------------------------------------------------
# AC-T3-1 合法样本五层全 PASS
# --------------------------------------------------------------------------

def test_ac_p0_3_1_born(good_run):
    result, written = good_run
    assert result.birth_status == "BORN"
    assert result.to_json()["task_success"] is None
    for level in birth.LEVELS:
        assert result.levels[level].status == "PASS", (
            level, result.levels[level].reason, result.levels[level].detail)
        assert result.levels[level].reason is None
    assert result.problems == []
    # 五层各一个独立子进程（D3-2）
    pids = [result.levels[level].pid for level in birth.LEVELS]
    assert all(isinstance(pid, int) and pid > 0 for pid in pids), pids
    assert len(set(pids)) == len(birth.LEVELS), pids
    # 落盘件可回读，sha 与文件逐字节相符
    doc = birth.load_birth(written["birth_path"])
    assert doc["birth_status"] == "BORN"
    assert doc["schema"] == birth.BIRTH_SCHEMA
    sidecar = Path(written["sha256_path"]).read_text(encoding="utf-8").split()
    assert sidecar == [written["birth_sha256"], birth.BIRTH_NAME]
    import hashlib
    assert hashlib.sha256(Path(written["birth_path"]).read_bytes()).hexdigest() == \
        written["birth_sha256"]
    # 五层证据三件齐全（T3.7）
    out_dir = Path(written["birth_path"]).parent
    for level in birth.LEVELS:
        for name in ("stdout.txt", "stderr.txt", "meta.json"):
            path = out_dir / "levels" / level / name
            assert path.is_file(), path


# --------------------------------------------------------------------------
# AC-T3-2 十类失败分类全部命中
# --------------------------------------------------------------------------

def test_ac_p0_3_2_ten_failures(ten_runs):
    seen = set()
    for name, (_fixture, _entry, _level, _reason) in _TEN.items():
        result = ten_runs[name]
        assert result.birth_status == "NOT_BORN", name
        for level in birth.LEVELS:
            item = result.levels[level]
            if item.status == "FAIL":
                assert item.reason in birth.FAILURE_REASONS, (name, item.reason)
                seen.add(item.reason)
    expected = {reason for (_f, _e, _l, reason) in _TEN.values()}
    assert expected <= seen, sorted(expected - seen)
    # 十个失败类别 → 九个固定 reason（reset/step 同归 step_error，靠 detail 区分）
    assert len(_TEN) == 10
    assert expected == {"missing", "ast_error", "import_error",
                        "construction_error", "contract_error", "step_error",
                        "non_finite", "timeout", "path_escape"}


# --------------------------------------------------------------------------
# AC-T3-3 reason/level 映射表逐项
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(_TEN))
def test_ac_p0_3_3_reason_mapping(ten_runs, name):
    _fixture, _entry, level, reason = _TEN[name]
    result = ten_runs[name]
    item = result.levels[level]
    assert (item.status, item.reason) == ("FAIL", reason), (
        name, level, item.status, item.reason, item.detail)
    assert item.detail, f"{name}/{level} 失败必须给人读的 detail"


# --------------------------------------------------------------------------
# AC-T3-4 NOT_APPLICABLE 不计入 PASS
# --------------------------------------------------------------------------

def test_ac_p0_3_4_not_applicable(tmp_path: Path):
    wd = _workdir(tmp_path, "good")
    spec = _spec(bc={"import_required": False, "construction_required": True,
                     "contract_required": True, "minimal_step_required": True})
    result, _written = _validate(wd, tmp_path / "out", spec=spec)
    b1 = result.levels["B1"]
    assert b1.status == "NOT_APPLICABLE"
    assert b1.status != "PASS"
    assert b1.reason is None
    assert result.birth_status == "BORN"
    # 非必需层同样记 NA（不起子进程），且不计入 PASS
    spec2 = _spec(bc={"import_required": True, "construction_required": True,
                      "contract_required": True, "minimal_step_required": False})
    result2, written2 = _validate(_workdir(tmp_path / "x", "good"),
                                  tmp_path / "out2", spec=spec2)
    assert result2.levels["B4"].status == "NOT_APPLICABLE"
    assert result2.birth_status == "BORN"
    # NA 层也保留证据三件（结构一致），且不起子进程
    for name in ("stdout.txt", "stderr.txt", "meta.json"):
        assert (Path(written2["birth_path"]).parent / "levels" / "B4" / name).is_file()
    assert result2.levels["B4"].pid is None
    assert result2.levels["B0"].pid is not None
    # 必需层被标 NA 则不能算出生（矛盾即结论）
    spec3 = _spec(bc={"import_required": True, "construction_required": True,
                      "contract_required": True, "minimal_step_required": True},
                  kind="document_bundle", entry="adapter.py")
    result3, _ = _validate(_workdir(tmp_path / "y", "good"), tmp_path / "out3",
                           spec=spec3)
    # document_bundle 的 B1 恒 NA，但 birth_contract 要求 import → 自相矛盾 → NOT_BORN
    assert result3.levels["B1"].status == "NOT_APPLICABLE"
    assert result3.birth_status == "NOT_BORN"
    assert any("NOT_APPLICABLE" in problem for problem in result3.problems)


# --------------------------------------------------------------------------
# AC-T3-5 子进程隔离
# --------------------------------------------------------------------------

def test_ac_p0_3_5_isolation(tmp_path: Path, good_run):
    import sys
    wd = _workdir(tmp_path, "exit0")
    result, _written = _validate(wd, tmp_path / "out")
    # 子进程自杀只影响它自己：主进程继续、并如实记为失败
    assert result.levels["B1"].status == "FAIL"
    assert result.levels["B1"].reason == "import_error"
    assert "未产出层结果" in result.levels["B1"].detail
    assert result.birth_status == "NOT_BORN"
    # sys.modules 污染不跨层：候选从未被主进程 import
    assert "simpraxis_candidate" not in sys.modules
    # 五层 pid 互不相同（每层独立解释器）
    good, _ = good_run
    pids = [good.levels[level].pid for level in birth.LEVELS]
    assert len(set(pids)) == 5


def test_w2_01_forged_marker_is_rejected(tmp_path: Path):
    """评审 W2-01 回归：候选打印伪造标记行 + ``os._exit(0)`` ⇒ 必须 FAIL。

    修复前该用例会得到 B1 PASS / birth_status=BORN（判据被交给被验证方自报）；
    修复后层结果只认 ``result_path`` 上带 nonce 的记录，stdout 任一标记行即判伪造。
    """
    wd = _workdir(tmp_path, "forge_marker")
    result, _written = _validate(wd, tmp_path / "out")
    item = result.levels["B1"]
    assert (item.status, item.reason) == ("FAIL", "contract_error"), (
        item.status, item.reason, item.detail)
    assert "伪造出生标记行" in item.detail
    assert result.birth_status == "NOT_BORN"
    # 伪造内容绝不可作为层数据进入结果件
    assert "forged" not in item.data
    # 五层全部不得 PASS（B0 只看静态，入口本身合法 → PASS 允许）
    assert all(result.levels[level].status != "PASS"
               for level in ("B1", "B2", "B3", "B4"))
    # 证据件如实保留候选的伪造输出，供审计
    stdout = (Path(_written["birth_path"]).parent / "levels" / "B1" /
              "stdout.txt").read_text(encoding="utf-8")
    assert birth.RUNNER_MARKER in stdout


def test_w2_01_nonce_channel_is_absent_from_payload(tmp_path: Path):
    """评审 W2-01 二次加固：``payload.json`` 里不得再有 ``nonce`` / ``result_path``。

    这两个值改由父进程经 stdin 一次性挑战行下发（运行器在 import 候选**之前**
    读走）。``payload.json`` 的路径既是候选 argv 的 ``sys.argv[1]`` 也是候选
    ``open()`` 得到的内容——只要它含 nonce，候选就能抢先写出一条合法记录。
    """
    wd = _workdir(tmp_path, "good")
    out = tmp_path / "out"
    result, _written = _validate(wd, out)
    assert result.birth_status == "BORN"
    payloads = sorted((out / "levels").glob("*/payload.json"))
    assert len(payloads) == len(birth.LEVELS) == 5
    for path in payloads:
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert "nonce" not in doc, path
        assert "result_path" not in doc, path
        #: 挑战通道之外的东西都还在（层/工作目录/入口等参数不受影响）
        assert doc["level"] == path.parent.name


def test_w2_01_runner_refuses_without_stdin_challenge(tmp_path: Path):
    """评审 W2-01 二次加固：拿不到 stdin 挑战行 ⇒ 运行器拒绝执行（rc=3、零结果件）。

    直接起生产同一份 ``birth._RUNNER_SRC``，stdin 置 ``/dev/null``：这证明 nonce
    不是「候选猜不到」而是**payload/argv 里根本没有**——没有挑战行的运行器不替
    任何人写结果件（fail-closed），候选自己伪造的 stdout 标记行另有用例覆盖。
    """
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps({"level": "B0"}), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-I", "-c", birth._RUNNER_SRC, str(payload_path)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 3, (proc.returncode, proc.stdout, proc.stderr)
    assert "挑战行" in proc.stderr
    assert not list(tmp_path.glob("runner_result*.json"))


def test_w2_02_directory_entry_is_rejected(tmp_path: Path):
    """评审 W2-02 回归：入口是目录 ⇒ B0 必须 FAIL，且不得因后缀缺省而 PASS。

    修复前：不带 .py/.yaml 后缀的目录 ``suffix == ""`` 跳过 AST 分支，
    ``st_size``（≈4096）≥ ``min_bytes`` ⇒ B0 PASS。带后缀的目录则
    ``read_text`` 抛 ``IsADirectoryError`` 穿透成 BLOCKED_INFRA。
    """
    wd = _workdir(tmp_path, "good")
    (wd / "adapter.py").unlink()
    (wd / "adapter.py").mkdir()
    (wd / "adapter.py" / "payload.txt").write_text("x", encoding="utf-8")
    result, _ = _validate(wd, tmp_path / "out")
    item = result.levels["B0"]
    assert (item.status, item.reason) == ("FAIL", "missing"), (
        item.status, item.reason, item.detail)
    assert "不是常规文件" in item.detail
    assert result.birth_status == "NOT_BORN"


@pytest.mark.parametrize("payload,expected", [
    (b"def build_env():\n    return None\n\x00\n", "ValueError"),
    (b"def build_env():\n    return None\n# \xff\xfe\n", "UnicodeDecodeError"),
])
def test_w2_02_dirty_source_is_ast_error(tmp_path: Path, payload: bytes,
                                         expected: str):
    """评审 W2-02 回归：null 字节/非 UTF-8 源码 ⇒ B0 ast_error，不是 BLOCKED_INFRA。

    修复前该层穿透到 runner 顶层 ``except BaseException`` 记 ``blocked_infra``，
    把候选自己的脏交付物算成基础设施阻塞（违背 T3.1 不变量 4）。
    """
    wd = _workdir(tmp_path, "good")
    (wd / "adapter.py").write_bytes(payload)
    result, _ = _validate(wd, tmp_path / "out")
    item = result.levels["B0"]
    assert (item.status, item.reason) == ("FAIL", "ast_error"), (
        item.status, item.reason, item.detail)
    assert expected in item.detail
    assert result.birth_status == "NOT_BORN"


# --------------------------------------------------------------------------
# AC-T3-6 资源限制
# --------------------------------------------------------------------------

def test_ac_p0_3_6_limits(tmp_path: Path, ten_runs):
    assert ten_runs["hang"].levels["B4"].reason == "timeout"
    assert "3.0s" in ten_runs["hang"].levels["B4"].detail
    wd = _workdir(tmp_path, "memory_bomb")
    result, _written = _validate(
        wd, tmp_path / "out",
        limits=birth.Limits(timeout_s=20.0, mem_bytes=512 * 1024 ** 2))
    assert result.levels["B4"].status == "FAIL"
    assert result.levels["B4"].reason == "memory"
    assert "RLIMIT_AS" in result.levels["B4"].detail
    assert result.birth_status == "NOT_BORN"


# --------------------------------------------------------------------------
# AC-T3-7 路径越界
# --------------------------------------------------------------------------

def test_ac_p0_3_7_path_jail(tmp_path: Path, ten_runs):
    item = ten_runs["escape"].levels["B0"]
    assert (item.status, item.reason) == ("FAIL", "path_escape")
    assert "workdir" in item.detail
    # 反向：workdir 内部的软链接同样被拒（但不诬告成越界）
    wd = _workdir(tmp_path, "good")
    (wd / "real.py").write_text((wd / "adapter.py").read_text(encoding="utf-8"),
                                encoding="utf-8")
    (wd / "adapter.py").unlink()
    (wd / "adapter.py").symlink_to(wd / "real.py")
    result, _ = _validate(wd, tmp_path / "out")
    assert (result.levels["B0"].status, result.levels["B0"].reason) == ("FAIL", "symlink")
    # `..` 逃逸同样被 jail 拦住
    wd2 = _workdir(tmp_path / "up", "good")
    (wd2 / "adapter.py").unlink()
    (wd2 / "adapter.py").symlink_to(wd2 / ".." / "outside.py")
    (wd2.parent / "outside.py").write_text("def build_env():\n    return None\n",
                                           encoding="utf-8")
    result2, _ = _validate(wd2, tmp_path / "out2")
    assert (result2.levels["B0"].status, result2.levels["B0"].reason) == (
        "FAIL", "path_escape")


# --------------------------------------------------------------------------
# AC-T3-8 不修改历史函数
# --------------------------------------------------------------------------

def _bench() -> Path:
    from orchestrator.benchmark import paths
    root = paths.bench_root(read_only=True)
    assert root.is_dir(), f"数据根不在场：{root}"
    return root


#: AC-T3-8 的授权例外：允许改动 ``loop.py`` 的提交（逐条登记，集合外仍判红）。
#: 2026-11-14 代码评审 CR-17/CR-23（断点续跑接线、condenser 单次读取）经用户
#: 指示修复，属登记在案的授权改动；此前 loop.py 自 Task 0 基线起零改动。
LOOP_CHANGE_ALLOWLIST = frozenset({
    "f7c48bf960755dda5ccdfb418915ce3a740a157f",
})


def test_ac_p0_3_8_no_loop_change():
    inv = json.loads((_bench() / "intake" / "invariants_before.json")
                     .read_text(encoding="utf-8"))
    baseline = json.loads((_bench() / "intake" / "repository_state.json")
                          .read_text(encoding="utf-8"))["head"]
    # (i) 提交史口径：Task 0 基线以来 loop.py 的改动必须全部在授权例外集内
    #     （空集 = 零改动；集合外的任何提交仍判红，不放水）。
    log = subprocess.run(
        ["git", "log", "--format=%H", f"{baseline}..HEAD", "--",
         "orchestrator/agentic/loop.py"],
        cwd=str(_REPO_ROOT), capture_output=True, text=True)
    assert log.returncode == 0, log.stderr
    changed = [line.strip() for line in log.stdout.splitlines() if line.strip()]
    unregistered = [sha for sha in changed
                    if sha not in LOOP_CHANGE_ALLOWLIST]
    assert not unregistered, (
        "loop.py 出现了未登记的改动（AC-T3-8）：\n" + "\n".join(unregistered))
    # (ii) 源码口径：_entrypoint_ready 的 AST 源码段 sha256 与 Task 0 采集逐字相同
    source = intake.entrypoint_ready_source()
    assert intake._sha256_bytes(source.encode("utf-8")) == \
        inv["entrypoint_ready_source_sha256"]


# --------------------------------------------------------------------------
# 支撑断言
# --------------------------------------------------------------------------

def test_birth_result_shape_is_fixed(good_run):
    result, _written = good_run
    doc = result.to_json()
    assert set(doc) == {
        "schema", "task_id", "task_success", "birth_status", "entry", "contract",
        "required", "levels", "problems", "limits", "network_audit", "workdir",
        "ts_iso"}
    assert list(doc["levels"]) == list(birth.LEVELS)
    for level, item in doc["levels"].items():
        assert set(item) == {"status", "reason", "detail", "duration_s",
                             "exit_code", "pid", "data", "stdout_bytes",
                             "stderr_bytes", "evidence"}
        assert item["status"] in birth.LEVEL_STATUSES
    assert doc["network_audit"]["mode"] == "audited_only"


def test_registered_tasks_map_to_contracts():
    reg = registry.BenchmarkRegistry()
    expected = {
        "mujoco_contact_push_v1": ("mujoco_push", {"B0": True, "B1": False,
                                                   "B2": True, "B3": True,
                                                   "B4": True}),
        "libero_env_adapter_v1": ("libero_env_adapter", {"B0": True, "B1": True,
                                                         "B2": True, "B3": True,
                                                         "B4": True}),
        "icy_ground_dr_v1": ("research_doc", {"B0": True, "B1": False,
                                              "B2": False, "B3": True,
                                              "B4": False}),
    }
    assert set(expected) == set(reg.list_tasks())
    for task_id, (contract, required) in expected.items():
        bspec = birth.BirthSpec.from_task_spec(reg.get(task_id))
        assert bspec.contract == contract
        assert bspec.required_levels() == required
        assert contracts.contract_by_name(contract).name == contract
    with pytest.raises(KeyError):
        contracts.contract_by_name("no_such_contract")


def test_document_bundle_contract(tmp_path: Path):
    wd = _workdir(tmp_path, "good_doc", entry="experiment_plan.yaml")
    spec = _spec(task_id="icy_ground_dr_v1", contract="research_doc",
                 kind="document_bundle", entry="experiment_plan.yaml",
                 bc={"import_required": False, "construction_required": False,
                     "contract_required": True, "minimal_step_required": False})
    result, _written = _validate(wd, tmp_path / "out", spec=spec)
    assert result.birth_status == "BORN"
    assert result.levels["B3"].status == "PASS"
    for level in ("B1", "B2", "B4"):
        assert result.levels[level].status == "NOT_APPLICABLE"
    # 少一件产物 → 契约不符
    (wd / "parameter_space.json").unlink()
    result2, _ = _validate(wd, tmp_path / "out2", spec=spec)
    assert (result2.levels["B3"].status, result2.levels["B3"].reason) == (
        "FAIL", "contract_error")
    assert "parameter_space.json" in result2.levels["B3"].detail


def test_network_audit_is_honest(tmp_path: Path):
    wd = _workdir(tmp_path, "network")
    result, _written = _validate(wd, tmp_path / "out")
    audit = result.network_audit
    assert audit["mode"] == "audited_only"
    assert audit["network_mode"] == "audited_only"
    assert any(hit["module"].split(".")[0] == "socket"
               for hit in audit["static_import_hits"]), audit
    # 容器路径才记 enforced
    result2, _ = _validate(wd, tmp_path / "out2", network_mode="none")
    assert result2.network_audit["mode"] == "enforced"
    assert result2.birth_status == "BORN"


def test_fixtures_do_not_import_real_engines():
    """出生验证的合成样本不得把真实物理引擎变成 Task 3 的依赖（T3.5-7）。"""
    forbidden = ("mujoco", "robosuite", "libero", "sapien", "genesis", "torch")
    offenders = []
    for path in sorted(_FIX.rglob("*.py")) + sorted(_FIX.rglob("*.py.txt")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in forbidden:
                    offenders.append((path.name, name))
    assert offenders == [], offenders


def test_capture_truncation_is_64k():
    """D3-4：stdout/stderr 各截断 64 KiB（meta 仍记全量字节数）。"""
    big = "x" * (70 * 1024)
    out = birth._truncate_capture(big)
    assert "已截断" in out
    assert len(out.encode("utf-8")) < len(big.encode("utf-8"))
    assert len(out.encode("utf-8")) <= birth.MAX_CAPTURE_BYTES + 128
    assert birth._truncate_capture("small") == "small"


def test_birth_spec_min_bytes_and_entry_types():
    spec = _spec()
    assert spec.min_bytes >= 1
    doc = spec.to_json()
    assert doc["entry"] == {"kind": "python_script", "path": "adapter.py"}
    assert doc["required"] == {"B0": True, "B1": True, "B2": True,
                               "B3": True, "B4": True}
    with pytest.raises(birth.BirthError):
        birth.BirthValidator(spec, workdir="/definitely/not/here")
    with pytest.raises(birth.BirthError):
        birth.BirthValidator(spec, workdir=_FIX / "good", network_mode="on")
