"""Phase 2.6 Task3 AC-T3-1..6：Agentic 工具层核心重构（F1–F4）机判（0 付费）。

六个 AC 与三处证据一一对应（计划 §T3.6 逐字）：

- AC-T3-1（F1 验证-修复尾）：第 12 轮写出合法入口脚本 → 追授 3 轮、
  ``phase=="verify_repair"``、trace 有 ``TAIL_DIRECTIVE`` 逐字；反例是
  「写空文件不追授」→ ``kind=birth_absent``。
- AC-T3-2（F2 ``run_expr``）：2 条正例走真实子进程执行并断言
  ``argv[-1] == src`` 逐字节；21 条恶意样例 100% 拒绝且理由逐条对齐。
- AC-T3-3（F3 容错补丁器）：3 条正例补丁（CRLF+行尾空白漂移 / 缺上下文
  空行 / 制表符缩进）在归一化后全应用成功；2 条多处歧义补丁全拒且文件
  零变化。
- AC-T3-4（F4 Scratchpad）：30KB 观察触发 Condenser 后，下一轮出站 payload
  的首个 system 消息仍逐字包含 scratchpad 全文，且指纹
  ``scratchpad_sha256`` 与之相等；反例「改 scratchpad 必须换缓存键」。
- AC-T3-5（默认全关零变化）：同一 mock 场景的 digest 与在 Task3 之前
  提交 872848d 上生成的 golden 逐字节相同。
- AC-T3-6（缓存纪元）：``CACHE_VERSION=="agentic-v3"``，四开关任一开即
  换纪元，v2 时代的磁盘缓存文件在 v3 下一次也不命中。

全部 fake gateway / 本地 NONPROD 后端，零实呼（0 付费）。

证据落 ``$SIMPRAXIS_PHASE26_ROOT/task9_milestone/``（0700 目录 / 0600 文件）：
``ac_t3_1_trace.jsonl`` / ``ac_t3_2_rejects.json`` / ``ac_t3_3_patches.jsonl``
/ ``ac_t3_4_payload.json`` / ``ac_t3_5_golden_sha.json`` / ``ac_t3_6_epoch.json``。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.agentic.caps import (
    KIND_BIRTH_ABSENT,
    KIND_ROUND_CAP,
    KIND_ROUND_TAIL,
    BudgetTracker,
    CapExceeded,
)
from orchestrator.agentic.config import ALL_OFF, AgenticConfig
from orchestrator.agentic.forensics import (
    CACHE_DISK_PREFIX,
    CACHE_DISK_PREFIX_LEGACY,
    CACHE_LOGICAL_PREFIX,
    CACHE_LOGICAL_PREFIX_LEGACY,
    CACHE_VERSION,
    CACHE_VERSION_LEGACY,
    CONDENSE_TOOL,
    MODEL_ROUND_TOOL,
    TraceStore,
    agentic_cache_fingerprint,
    cache_epoch,
    canonical_json,
    sha256_text,
)
from orchestrator.agentic.loop import (
    ENTRYPOINT,
    TAIL_DIRECTIVE,
    build_bound_tools,
    run_agentic_inner_loop,
)
from orchestrator.agentic.policy import GrantTable, ToolContext
from orchestrator.agentic.tools.run import LocalSubprocessBackend
from orchestrator.agentic.tools.run_expr import (
    MAX_EXPR_CHARS,
    ExprRejected,
    run_expr,
    validate_program,
)
from orchestrator.agentic.tools.scratchpad import (
    SCRATCHPAD_REL,
    update_scratchpad,
)
from orchestrator.agentic.tools.write import apply_patch
from orchestrator.common import runtime_paths as rp
from orchestrator.common.callcache import CallReplay

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "phase26"
GOLDEN_PATH = FIXTURES / "task3_all_off_golden_872848d.json"
HISTORICAL_PATH = FIXTURES / "task3_my_check_historical.py"

# 生产 phase26 根在**模块导入时点**取一次（AC 表要求证据件落
# $ROOT/task9_milestone/，用例自身的 tmp 账根不影响它）。
EVIDENCE_DIR = rp.phase26_root() / "task9_milestone"

_FENCE = "```python"


# ---------------------------------------------------------------------------
# 通用脚手架
# ---------------------------------------------------------------------------

class ScriptedGateway:
    """按调用序号回放代码块；calls 数即「模型轮」代理（0 付费）。"""

    def __init__(self, plays):
        self.plays = list(plays)
        self.calls = []

    def model_id(self, stage):
        return "unit-fake-model"

    def call(self, stage, messages, **kwargs):
        i = len(self.calls)
        self.calls.append({"stage": stage,
                           "messages": [_norm_message(m) for m in messages]})
        text = self.plays[min(i, len(self.plays) - 1)]
        return text, (10 + i, 20 + i)


def _norm_message(m):
    """与 /tmp/p26_task3_golden.py 同一归约（golden 的可比性靠它）。"""
    if isinstance(m, dict):
        return {"role": m.get("role"), "content": m.get("content")}
    return {"role": getattr(m, "role", None),
            "content": getattr(m, "content", None)}


def _play(code: str) -> str:
    return f"{_FENCE}\n{code}\n```"


def _rows(trace_path) -> list[dict]:
    path = Path(trace_path)
    return [json.loads(line) for line in path.read_text(encoding="utf-8")
            .splitlines() if line.strip()]


def _model_rows(rows) -> list[dict]:
    return [r for r in rows if r.get("tool") == MODEL_ROUND_TOOL]


def _msg_text(msg) -> str:
    """ChatMessage / dict 两形的纯文本面（payload 展开后是 content 块表）。"""
    content = msg.get("content") if isinstance(msg, dict) \
        else getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(getattr(block, "text", "")))
        return "".join(parts)
    return str(content)


def _first_system_text(messages) -> str:
    for msg in messages:
        role = msg.get("role") if isinstance(msg, dict) \
            else getattr(msg, "role", None)
        if role == "system":
            return _msg_text(msg)
    raise AssertionError("payload 中不存在 system 消息")


def _write_evidence(name: str, text: str) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(EVIDENCE_DIR, 0o700)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(EVIDENCE_DIR.stat().st_mode) == 0o700
    return path


def _run_loop(tmp_path: Path, plays, *, cfg, round_cap: int = 12,
              tail_rounds: int = 3, thread_id: str = "thr-t3",
              call_cache=None, files: "dict[str, str] | None" = None):
    """跑一次 agentic 内环（fake gateway）；返回 (wd, budget, gateway, outcome)。"""
    wd = tmp_path / "wd"
    wd.mkdir(parents=True)
    (wd / "a.txt").write_text("alpha\n", encoding="utf-8")
    for name, content in (files or {}).items():
        (wd / name).write_text(content, encoding="utf-8")
    budget = BudgetTracker(round_cap=round_cap,
                           verify_repair_tail_rounds=(
                               tail_rounds if cfg is not None
                               and cfg.verify_repair_tail else 0))
    tracer = TraceStore(tmp_path / "traces", thread_id)
    gateway = ScriptedGateway(plays)
    outcome = run_agentic_inner_loop(
        workdir=wd, task="task3 acceptance",
        grants=GrantTable(
            tools=frozenset({"read_file", "write_file", "apply_patch",
                             "run_expr", "update_scratchpad"}),
            write_roots=("",),
            expr_modules=frozenset({"math", "numpy", "os.path"})),
        gateway=gateway, budget=budget, tracer=tracer,
        thread_id=thread_id, call_cache=call_cache,
        agentic_config=cfg)
    return wd, budget, gateway, outcome


# ---------------------------------------------------------------------------
# AC-T3-1：F1 验证-修复尾（出生即追授）
# ---------------------------------------------------------------------------

# 合法入口脚本：>= 200 字节（ENTRYPOINT_MIN_BYTES）且语法可编译。
_ENTRY_SRC = (
    "import sys\n"
    "\n"
    "\n"
    "def build(tag: str) -> str:\n"
    "    return f'{tag}:' + ','.join(str(i) for i in range(4))\n"
    "\n"
    "\n"
    "def main() -> int:\n"
    "    print('entrypoint alive', build('probe'))\n"
    "    return 0\n"
    "\n"
    "\n"
    "if __name__ == '__main__':\n"
    "    sys.exit(main())\n"
)


def test_ac_t3_1_birth_grants_tail(tmp_path):
    """第 12 轮写出合法入口脚本 → 追授恰好 3 轮，round_tail 终局。"""
    assert len(_ENTRY_SRC.encode("utf-8")) >= 200
    plays = [_play('read_file(path="a.txt")') for _ in range(11)]
    plays.append(_play(
        f'write_file(path="{ENTRYPOINT}", '
        f'content={json.dumps(_ENTRY_SRC)})'))
    plays += [_play('read_file(path="a.txt")') for _ in range(8)]

    wd, budget, gateway, outcome = _run_loop(
        tmp_path, plays, cfg=AgenticConfig(verify_repair_tail=True))

    # 追授面：出生轮号 = 12，总帽 = round_cap + 3，恰好 15 轮模型调用。
    assert budget.tail_granted is True
    assert budget.born_at_round == 12
    assert budget.effective_round_cap == 12 + 3
    assert outcome.rounds_used == 15
    assert outcome.kind == KIND_ROUND_TAIL
    assert len(gateway.calls) == 15
    assert (wd / ENTRYPOINT).is_file()

    rows = _rows(outcome.trace_path)
    model_rows = _model_rows(rows)
    assert len(model_rows) == 15
    for row in model_rows:
        args = row["args"]
        if row["round"] <= 12:
            assert args["phase"] == "explore"
            assert args["tail_granted"] is False
            assert args["effective_round_cap"] == 12
            assert "tail_directive" not in args
        else:
            assert args["phase"] == "verify_repair"
            assert args["tail_granted"] is True
            assert args["effective_round_cap"] == 15
            # TAIL_DIRECTIVE 逐字进场（trace args 原样字符串，非转义拼贴）。
            assert args["tail_directive"] == TAIL_DIRECTIVE
            assert _first_system_text(gateway.calls[row["round"] - 1]
                                      ["messages"]).find(TAIL_DIRECTIVE) >= 0

    _write_evidence("ac_t3_1_trace.jsonl",
                    Path(outcome.trace_path).read_text(encoding="utf-8"))


def test_ac_t3_1_short_entrypoint_no_grant(tmp_path):
    """第 12 轮只写 5 字节的 ``pass``（不达 200B）→ 不追授，birth_absent。"""
    short = "pass\n"
    assert len(short.encode("utf-8")) < 200
    plays = [_play('read_file(path="a.txt")') for _ in range(11)]
    plays.append(_play(
        f'write_file(path="{ENTRYPOINT}", content={json.dumps(short)})'))
    plays += [_play('read_file(path="a.txt")') for _ in range(8)]

    wd, budget, gateway, outcome = _run_loop(
        tmp_path, plays, cfg=AgenticConfig(verify_repair_tail=True))

    assert budget.tail_granted is False
    assert budget.born_at_round is None
    assert budget.effective_round_cap == 12
    assert outcome.rounds_used == 12
    assert outcome.kind == KIND_BIRTH_ABSENT
    assert len(gateway.calls) == 12
    assert (wd / ENTRYPOINT).is_file()

    model_rows = _model_rows(_rows(outcome.trace_path))
    assert {r["args"]["phase"] for r in model_rows} == {"explore"}
    assert not any("tail_directive" in r["args"] for r in model_rows)


# ---------------------------------------------------------------------------
# AC-T3-2：F2 run_expr（AST 白名单）
# ---------------------------------------------------------------------------

class _RecordingPythonBackend(LocalSubprocessBackend):
    """记录原始 argv；执行时把 ``python`` 映射到当前解释器（测试本机化）。

    记录的 argv 永远是**未映射**的那份——AC-T3-2 要断言「校验字节 ==
    执行字节」，若先映射就无法证明字节未被拼接改写。
    """

    def __init__(self):
        super().__init__(runner=self._runner)
        self.calls: "list[list[str]]" = []

    @staticmethod
    def _runner(argv, **kwargs):
        mapped = [sys.executable if (i == 0 and arg == "python") else arg
                  for i, arg in enumerate(argv)]
        return subprocess.run(mapped, **kwargs)

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        return super().run(argv, **kwargs)


def _backend() -> _RecordingPythonBackend:
    return _RecordingPythonBackend()


def _expr_ctx(tmp_path: Path, backend, *, modules, tools=("run_expr",)):
    wd = tmp_path / "wd"
    wd.mkdir(exist_ok=True)
    return ToolContext(
        workdir=wd,
        grants=GrantTable(tools=frozenset(tools), write_roots=("",),
                          expr_modules=frozenset(modules)),
        thread_id="thr-expr", run_backend=backend, nonprod=True)


_EXPR_POSITIVES = [
    ("math", "import math\nprint(math.sqrt(2))", "1.4142135",
     ("math",)),
    ("numpy", "import numpy\nprint(numpy.zeros(3).sum())", "0.0",
     ("numpy",)),
]


@pytest.mark.parametrize("label,src,expected,modules", _EXPR_POSITIVES,
                         ids=[p[0] for p in _EXPR_POSITIVES])
def test_ac_t3_2_positive_single_round(tmp_path, label, src, expected,
                                       modules):
    """正例：单轮执行返回结果，且执行字节 == 校验字节（argv[-1]）。"""
    backend = _backend()
    ctx = _expr_ctx(tmp_path, backend, modules=modules)
    res = run_expr(ctx, src=src)

    assert res.denied is False, res.output
    assert expected in res.output
    assert len(backend.calls) == 1
    assert backend.calls[0][:3] == ["python", "-I", "-c"]
    assert backend.calls[0][-1] == src          # 逐字节相等，零拼接改写
    assert len(backend.calls[0]) == 4


# 21 条恶意样例（计划 §T3.6 逐字编号）+ 期望的规则标签（检查次序 1→10）。
_EXPR_MALICIOUS = [
    ("m01", '__import__("os").system("id")', "name_not_allowed:__import__"),
    ("m02", 'getattr(__builtins__, "__import__")("os")',
     "name_not_allowed:__builtins__"),
    ("m03", "().__class__.__bases__[0].__subclasses__()", "private_attr"),
    ("m04", '[c for c in ().__class__.__base__.__subclasses__() '
            'if c.__name__=="Popen"]', "private_attr"),
    ("m05", 'open("/etc/passwd").read()', "name_not_allowed:open"),
    ("m06", "exec(\"import os; os.system('id')\")",
     "name_not_allowed:exec"),
    ("m07", 'eval("1+1")', "name_not_allowed:eval"),
    ("m08", 'compile("x","<s>","eval")', "name_not_allowed:compile"),
    ("m09", '__builtins__["eval"]("1")', "name_not_allowed:__builtins__"),
    ("m10", "(lambda: 1)()", "node_not_allowed:Lambda"),
    ("m11", 'import os; os.system("id")', "module_not_allowed:os"),
    ("m12", "from os import system", "module_not_allowed:os"),
    ("m13", "import subprocess", "module_not_allowed:subprocess"),
    ("m14", "import socket", "module_not_allowed:socket"),
    ("m15", "import ctypes", "module_not_allowed:ctypes"),
    ("m16", "x = 1", "stmt_not_allowed:Assign"),
    ("m17", "import importlib", "module_not_allowed:importlib"),
    ("m18", 'getattr(x, "__class__")', "name_not_allowed:x"),
    ("m19", '"".__class__.__mro__', "private_attr"),
    ("m20", '__loader__.load_module("os")', "name_not_allowed:__loader__"),
    # 无 as → 绑定名是 os（把 os.path 授予放大成整个 os），规则 9 拒。
    ("m21", "import os.path", "binding_mismatch"),
]
assert len(_EXPR_MALICIOUS) == 21

_REJECT_EVIDENCE: "dict[str, dict]" = {}


def _flush_reject_evidence() -> None:
    rows = [_REJECT_EVIDENCE[key] for key in sorted(_REJECT_EVIDENCE)]
    _write_evidence("ac_t3_2_rejects.json",
                    json.dumps(rows, ensure_ascii=False, indent=1) + "\n")


@pytest.mark.parametrize("label,src,reason", _EXPR_MALICIOUS,
                         ids=[m[0] for m in _EXPR_MALICIOUS])
def test_ac_t3_2_rejects_malicious(tmp_path, label, src, reason):
    """21 条恶意样例：校验器拒（理由逐条对齐）+ 工具层 deny 且零执行。"""
    allowed = frozenset({"math", "numpy", "os.path"})
    with pytest.raises(ExprRejected) as excinfo:
        validate_program(src, allowed_modules=allowed)
    assert excinfo.value.reason == reason

    backend = _backend()
    ctx = _expr_ctx(tmp_path, backend, modules=allowed)
    res = run_expr(ctx, src=src)
    assert res.denied is True
    assert f"expr 拒绝（{reason}）" in res.output
    assert backend.calls == []          # 校验先于执行：容器一次也没起

    _REJECT_EVIDENCE[label] = {"id": label, "src": src, "reason": reason}
    _flush_reject_evidence()

    if label == "m01":
        # 形状边界锚点（超长 / 星号导入）挂在首例尾部执行——AC 表把
        # ``-k ac_t3_2`` 的节点数钉死为 23（2 正例 + 21 恶意样例），
        # 独立成节点会变成 24；断言本身不减少，只是并入既有节点。
        assert _boundary_reason("print(1)\n" + "#" * MAX_EXPR_CHARS,
                                frozenset({"math"})) == "len_exceeded"
        assert _boundary_reason("from math import *",
                                frozenset({"math"})) == "star_import"


def _boundary_reason(src: str, allowed) -> str:
    with pytest.raises(ExprRejected) as excinfo:
        validate_program(src, allowed_modules=allowed)
    return excinfo.value.reason


# ---------------------------------------------------------------------------
# AC-T3-3：F3 容错补丁器（唯一命中的模糊回退）
# ---------------------------------------------------------------------------

# P1：CRLF 行尾 + 上下文行尾空白漂移（两类归一化同补丁覆盖）。
_PATCH_P1 = "\r\n".join([
    "--- a/my_check.py",
    "+++ b/my_check.py",
    "@@ -8,3 +8,3 @@   ",
    " assert a.edge_manifold_counts(np.array([[0,1,2]])) == (3, 0)   ",
    "-assert a.edge_manifold_counts(np.array([[0,1,2],[1,0,2],[0,1,2]])) "
    "== (2, 1)",
    "+assert a.edge_manifold_counts(np.array([[0,1,2],[1,0,2],[0,1,2]])) "
    "== (2, 1)  # 复合面一条二流形边",
    " assert a.edge_manifold_counts(np.empty((0,3), dtype=int)) == (0, 0)   ",
]) + "\r\n"

# P2：hunk 声明的上下文里有一行空行 + 一个被删空行（缺上下文类）。
_PATCH_P2 = (
    "--- a/my_check.py\n"
    "+++ b/my_check.py\n"
    "@@ -20,2 +20,2 @@\n"
    " assert 0 < ratio < 0.6\n"
    " \n"
    "-\n"
    '+print("ratio detail", round(ratio, 6))\n'
)

# P3：制表符缩进 vs 文件里的空格缩进（行首制表符按 4 空格展开）。
_PATCH_P3 = (
    "--- a/my_check.py\n"
    "+++ b/my_check.py\n"
    "@@ -66,3 +66,3 @@\n"
    " \ttry:\n"
    "-\t\ta.kinematic_witness(*args, flow_start=0., flow_end=1.)\n"
    "+        a.kinematic_witness(*args, flow_start=0., flow_end=1.)  "
    "# 非法输入族\n"
    " \texcept ValueError:\n"
)

_PATCH_POSITIVES = [
    ("p1_crlf_whitespace", _PATCH_P1, "# 复合面一条二流形边"),
    ("p2_missing_context", _PATCH_P2, "ratio detail"),
    ("p3_tab_indent", _PATCH_P3, "非法输入族"),
]

# N1：纯空行锚点命中 6 处（多处歧义）；N2：无旧行的纯新增（缺锚点）。
_PATCH_N1 = (
    "--- a/my_check.py\n"
    "+++ b/my_check.py\n"
    "@@ -21,0 +22,1 @@\n"
    " \n"
    '+print("ratio check done")\n'
)
_PATCH_N2 = (
    "--- a/my_check.py\n"
    "+++ b/my_check.py\n"
    "@@ -5,0 +6,1 @@\n"
    "+# edge-count assertions above\n"
)
_PATCH_NEGATIVES = [
    ("n1_ambiguous_blank_anchor", _PATCH_N1),
    ("n2_no_anchor", _PATCH_N2),
]

_PATCH_EVIDENCE: "list[dict]" = []


def _flush_patch_evidence() -> None:
    _write_evidence(
        "ac_t3_3_patches.jsonl",
        "".join(json.dumps(row, ensure_ascii=False) + "\n"
                for row in _PATCH_EVIDENCE))


def _patch_ctx(wd: Path, *, tolerant: bool) -> ToolContext:
    return ToolContext(
        workdir=wd,
        grants=GrantTable(tools=frozenset({"apply_patch", "write_file"}),
                          write_roots=("",)),
        thread_id="thr-patch", tolerant_patch=tolerant)


def _fresh_historical(tmp_path: Path, tag: str) -> Path:
    wd = tmp_path / tag
    wd.mkdir()
    shutil.copyfile(HISTORICAL_PATH, wd / "my_check.py")
    return wd


def test_ac_t3_3_tolerant_applies_all_three(tmp_path):
    """聚合正例：3 条补丁严格通道全失败、tolerant 通道全部应用成功。"""
    rows = []
    for name, patch, marker in _PATCH_POSITIVES:
        wd = _fresh_historical(tmp_path, name)
        before = (wd / "my_check.py").read_text(encoding="utf-8")

        strict = apply_patch(_patch_ctx(wd, tolerant=False),
                             patch=json.dumps({"unified_diff": patch}))
        assert strict.denied is True, f"{name}: 严格通道本应失败"
        assert (wd / "my_check.py").read_text(encoding="utf-8") == before

        result = apply_patch(_patch_ctx(wd, tolerant=True),
                             patch=json.dumps({"unified_diff": patch}))
        after = (wd / "my_check.py").read_text(encoding="utf-8")
        assert result.denied is False, f"{name}: {result.output}"
        assert "tolerant 回退" in result.output
        assert "'mode': 'tolerant'" in result.output
        assert after != before
        assert marker in after
        rows.append({
            "name": name, "patch": patch,
            "strict": {"denied": True},
            "tolerant": {"denied": False, "mode": "tolerant",
                         "applied": True, "marker_present": True},
        })
    assert len(rows) == 3
    _PATCH_EVIDENCE.extend(rows)
    _flush_patch_evidence()


def test_ac_t3_3_ambiguity_rejects_all_two(tmp_path):
    """聚合反例：2 条歧义/缺锚点补丁在 tolerant 通道也必须拒绝、文件零变化。"""
    rows = []
    for name, patch in _PATCH_NEGATIVES:
        wd = _fresh_historical(tmp_path, name)
        before = (wd / "my_check.py").read_bytes()
        result = apply_patch(_patch_ctx(wd, tolerant=True),
                             patch=json.dumps({"unified_diff": patch}))
        assert result.denied is True, f"{name}: 歧义补丁不得应用"
        assert "tolerant: 唯一命中失败" in result.output
        assert (wd / "my_check.py").read_bytes() == before
        rows.append({
            "name": name, "patch": patch,
            "strict": {"denied": True},
            "tolerant": {"denied": True, "mode": "rejected",
                         "applied": False, "file_unchanged": True},
        })
    assert len(rows) == 2
    _PATCH_EVIDENCE.extend(rows)
    _flush_patch_evidence()


# ---------------------------------------------------------------------------
# AC-T3-4：F4 Scratchpad（Condenser 压缩后逐字存活）
# ---------------------------------------------------------------------------

_SCRATCHPAD_TEXT = (
    "# 关键 API 记忆（Condenser 压缩后仍须逐字在场）\n"
    "- build_vessel_mesh(path, *, segments=96) -> dict\n"
    "- kinematic_witness(t, theta, pos, clearance, flow_start, flow_end)\n"
    "- evaluate_precheck(witness) -> {'passed': bool, 'violations': list}\n"
    "NEXT: 用 run_expr 直接读 numpy 版本，不要再写临时脚本。\n"
)


def _big_text(chars: int = 30_000) -> str:
    """30KB 观察源：足够让 Condenser 的 lost_chars 非零。"""
    body = "观测行 %05d —— spout_z=1.0%02d clearance=0.0%02d\n"
    text = "".join(body % (i, i % 100, (i * 7) % 100)
                   for i in range(chars // 48 + 1))
    out = text[:chars]
    assert len(out.encode("utf-8")) >= chars
    return out


def test_ac_t3_4_scratchpad_survives_condenser(tmp_path, monkeypatch):
    """30KB 上下文触发压缩后，下一轮首个 system 消息仍含 scratchpad 全文。"""
    import orchestrator.agentic.model as model_mod
    real_fp = model_mod.agentic_cache_fingerprint
    seen = []

    def spy(**kwargs):
        seen.append(dict(kwargs))
        return real_fp(**kwargs)

    monkeypatch.setattr(model_mod, "agentic_cache_fingerprint", spy)

    plays = [_play('read_file(path="big.txt")') for _ in range(4)]
    plays.append(_play('read_file(path="a.txt")'))
    wd, budget, gateway, outcome = _run_loop(
        tmp_path, plays, cfg=AgenticConfig(scratchpad=True),
        files={SCRATCHPAD_REL: _SCRATCHPAD_TEXT,
               "big.txt": _big_text()})

    rows = _rows(outcome.trace_path)
    condensed = [r for r in rows if r.get("tool") == CONDENSE_TOOL]
    assert condensed, "30KB 观察 + 5 轮未触发 Condenser"
    assert condensed[-1]["args"]["lost_chars_cumulative"] > 0

    expected_sha = sha256_text(_SCRATCHPAD_TEXT)
    last_round = len(gateway.calls)
    first_system = _first_system_text(gateway.calls[last_round - 1]["messages"])
    # 压缩发生之后的那一轮仍逐字带着 scratchpad（压缩只碰旧轮工具观察）。
    assert _SCRATCHPAD_TEXT in first_system
    assert expected_sha in first_system or _SCRATCHPAD_TEXT in first_system

    # 指纹面与请求面同源：最后一轮指纹的 scratchpad_sha256 与文本一致。
    last_fp = seen[-1]
    assert last_fp["scratchpad_sha256"] == expected_sha
    assert last_fp["scratchpad_chars"] == len(_SCRATCHPAD_TEXT)

    model_row = _model_rows(rows)[-1]
    assert model_row["args"]["scratchpad_sha256"] == expected_sha
    assert model_row["args"]["scratchpad_chars"] == len(_SCRATCHPAD_TEXT)
    assert model_row["args"]["scratchpad_truncated"] is False

    _write_evidence("ac_t3_4_payload.json", json.dumps({
        "first_system_head_2000": first_system[:2000],
        "first_system_sha256": sha256_text(first_system),
        "scratchpad_sha256": expected_sha,
        "scratchpad_chars": len(_SCRATCHPAD_TEXT),
        "scratchpad_present_verbatim": _SCRATCHPAD_TEXT in first_system,
        "condensed_rows": len(condensed),
        "lost_chars_cumulative": condensed[-1]["args"][
            "lost_chars_cumulative"],
    }, ensure_ascii=False, indent=1) + "\n")


def test_ac_t3_4_scratchpad_change_changes_cache_key(tmp_path):
    """反例：改 scratchpad ⇒ 同一轮逻辑指纹必变（缓存键不可复用）。"""
    plays = [_play('read_file(path="a.txt")')]
    keys = []
    for tag, text in (("a", "第一版记忆：API=v1\n"), ("b", "第二版记忆：API=v2\n")):
        wd, budget, gateway, outcome = _run_loop(
            tmp_path / tag, plays, cfg=AgenticConfig(scratchpad=True),
            thread_id=f"thr-{tag}",
            files={SCRATCHPAD_REL: text})
        rows = _model_rows(_rows(outcome.trace_path))
        keys.append(rows[0]["args"]["disk_key"])
        assert rows[0]["args"]["scratchpad_sha256"] == sha256_text(text)
    assert keys[0] != keys[1]
    assert CACHE_DISK_PREFIX in keys[0]

    # 同一内容再跑一次 → 同一轮键复现（差异只来自内容，不来自时序）。
    wd, budget, gateway, outcome = _run_loop(
        tmp_path / "a2", plays, cfg=AgenticConfig(scratchpad=True),
        thread_id="thr-a2", files={SCRATCHPAD_REL: "第一版记忆：API=v1\n"})
    assert _model_rows(_rows(outcome.trace_path))[0]["args"]["disk_key"] \
        == keys[0]


# ---------------------------------------------------------------------------
# AC-T3-5：默认全关零变化（与 872848d 上的 golden 逐字节相同）
# ---------------------------------------------------------------------------

def _golden_digest(tmp_path: Path) -> dict:
    """与 872848d 上生成 golden 的同一场景（无 cfg kwarg = 全关）。"""
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "a.txt").write_text("alpha\n", encoding="utf-8")
    all_grants = GrantTable(tools=frozenset({
        "read_file", "list_dir", "write_file", "apply_patch",
        "run_cmd", "run_expr", "probe", "recall"}))
    ctx_all = ToolContext(workdir=wd, grants=all_grants,
                          thread_id="thr-gold")
    tool_names_all = [t.name for t in build_bound_tools(ctx_all)]

    tracer = TraceStore(tmp_path / "traces", "thr-gold")
    plays = [
        _play('read_file(path="a.txt")'),
        _play('write_file(path="b.txt", content="beta\\n")'),
        _play('finish(answer="ok", reason="done")'),
    ]
    gateway = ScriptedGateway(plays)
    outcome = run_agentic_inner_loop(
        workdir=wd, task="golden task",
        grants=GrantTable(tools=frozenset({"read_file", "write_file"}),
                          write_roots=("",)),
        gateway=gateway, budget=BudgetTracker(round_cap=4),
        tracer=tracer, thread_id="thr-gold")

    trace_path = Path(outcome.trace_path)
    rows = []
    for raw in _rows(trace_path):
        ref = raw.get("raw_response_ref")
        ref_sha = None
        if ref:
            p = Path(ref)
            if not p.is_absolute():
                p = trace_path.parent / p
            ref_sha = (hashlib.sha256(p.read_bytes()).hexdigest()
                       if p.exists() else "missing")
        rows.append({
            "round": raw.get("round"),
            "step": raw.get("step"),
            "tool": raw.get("tool"),
            "args": raw.get("args"),
            "args_hash": raw.get("args_hash"),
            "obs_sha256": raw.get("obs_sha256"),
            "obs_truncated": raw.get("obs_truncated"),
            "obs_head": raw.get("obs_head"),
            "obs_tail": raw.get("obs_tail"),
            "seq": raw.get("seq"),
            "policy": raw.get("policy_decision"),
            "usage": raw.get("usage"),
            "raw_response_sha256": ref_sha,
        })

    return {
        "tool_names": sorted(tool_names_all),
        "first_system_prompt": gateway.calls[0]["messages"][0]["content"],
        "first_task_prompt": gateway.calls[0]["messages"][1]["content"],
        "call_count": len(gateway.calls),
        "trace": rows,
        "outcome": {
            "kind": outcome.kind, "value": outcome.value,
            "reason": outcome.reason, "rounds_used": outcome.rounds_used,
            "steps_used": outcome.steps_used,
            "tokens_used": outcome.tokens_used,
            "cache_hits": outcome.cache_hits,
        },
        "final_b_txt": (wd / "b.txt").read_text(encoding="utf-8")
        if (wd / "b.txt").exists() else None,
    }


def test_ac_t3_5_all_off_golden_byte_identical(tmp_path):
    """``AgenticConfig()`` 全关：digest 与 Task3 前提交 872848d 的 golden 逐字节相同。"""
    digest = _golden_digest(tmp_path)
    # 全关 = 缓存纪元仍走 v2（I3-2 唯一回滚面）。
    assert ALL_OFF.any_enabled is False
    for row in digest["trace"]:
        args = row["args"] or {}
        if row["tool"] == MODEL_ROUND_TOOL:
            assert args["cache_namespace"] == CACHE_LOGICAL_PREFIX_LEGACY
            assert set(args) == {
                "cache_hit", "empty_response", "disk_key",
                "cache_namespace", "stopped_by_local", "stop_sequence_hit",
            }, "全关路径的 trace args 不得多出任何 Task3 键"

    got = json.dumps(digest, ensure_ascii=False, sort_keys=True, indent=1)
    expected = GOLDEN_PATH.read_text(encoding="utf-8")
    assert got == expected, "全关路径与 872848d golden 不再是逐字节相同"

    _write_evidence("ac_t3_5_golden_sha.json", json.dumps({
        "golden_path": str(GOLDEN_PATH.relative_to(REPO_ROOT)),
        "golden_sha256": hashlib.sha256(expected.encode("utf-8")).hexdigest(),
        "digest_sha256": hashlib.sha256(got.encode("utf-8")).hexdigest(),
        "byte_identical": True,
        "cache_namespace": CACHE_LOGICAL_PREFIX_LEGACY,
        "trace_rows": len(digest["trace"]),
    }, ensure_ascii=False, indent=1) + "\n")


# ---------------------------------------------------------------------------
# AC-T3-6：四开关任一开即换纪元（v2 磁盘缓存一次也不命中）
# ---------------------------------------------------------------------------

_EPOCH_V3 = (CACHE_VERSION, CACHE_LOGICAL_PREFIX, CACHE_DISK_PREFIX)
_EPOCH_V2 = (CACHE_VERSION_LEGACY, CACHE_LOGICAL_PREFIX_LEGACY,
             CACHE_DISK_PREFIX_LEGACY)


def test_ac_t3_6_epoch_switch_and_v2_cache_miss(tmp_path):
    """``agentic-v3`` 常量化 + 任一开关换纪元 + v2 缓存文件在 v3 不命中。"""
    assert CACHE_VERSION == "agentic-v3"
    assert CACHE_LOGICAL_PREFIX == "agentic-v3:"
    assert CACHE_DISK_PREFIX == "agentic-v3-"
    assert cache_epoch(ALL_OFF.any_enabled) == _EPOCH_V2
    per_switch = {}
    for field in ("verify_repair_tail", "run_expr", "tolerant_patch",
                  "scratchpad"):
        cfg = AgenticConfig(**{field: True})
        assert cfg.any_enabled is True
        assert cache_epoch(cfg.any_enabled) == _EPOCH_V3
        per_switch[field] = CACHE_VERSION

    cache_dir = tmp_path / "cache"
    replay = CallReplay(cache_dir)
    plays = [_play('read_file(path="a.txt")'),
             _play('finish(answer="ok", reason="done")')]

    # ① v2 纪元（无 cfg）在同一磁盘缓存上跑一次 → 落 v2 键。
    _, _, _, out_v2 = _run_loop(tmp_path / "v2", plays, cfg=None,
                                thread_id="thr-v2", call_cache=replay)
    v2_rows = _model_rows(_rows(out_v2.trace_path))
    assert v2_rows
    for row in v2_rows:
        assert row["args"]["cache_namespace"] == CACHE_LOGICAL_PREFIX_LEGACY
        assert row["args"]["disk_key"].startswith(CACHE_DISK_PREFIX_LEGACY)
        assert row["args"]["cache_hit"] is False
    v2_files = sorted(p.name for p in cache_dir.glob("*.json"))
    assert v2_files and all(n.startswith(CACHE_DISK_PREFIX_LEGACY)
                            for n in v2_files)

    # ② v3 纪元（开任一开关）同一场景：四轮模型调用全部 miss（v2 文件不可复用）。
    _, _, _, out_v3 = _run_loop(tmp_path / "v3", plays,
                                cfg=AgenticConfig(tolerant_patch=True),
                                thread_id="thr-v3", call_cache=replay)
    v3_rows = _model_rows(_rows(out_v3.trace_path))
    assert v3_rows
    for row in v3_rows:
        assert row["args"]["cache_namespace"] == CACHE_LOGICAL_PREFIX
        assert row["args"]["disk_key"].startswith(CACHE_DISK_PREFIX)
        assert row["args"]["cache_hit"] is False
    assert {r["args"]["disk_key"] for r in v3_rows}.isdisjoint(
        {r["args"]["disk_key"] for r in v2_rows})
    # v2 时代文件仍在盘上（未被改写/删除），只是 v3 永不看它。
    for name in v2_files:
        assert (cache_dir / name).is_file()

    # ③ 同纪元第二程：v3 的第一轮必命中自己写下的键（证明缓存确实有效，
    #    ② 的 miss 是纪元隔离而非缓存整体失效）。
    _, _, _, out_v3b = _run_loop(tmp_path / "v3b", plays,
                                 cfg=AgenticConfig(tolerant_patch=True),
                                 thread_id="thr-v3b", call_cache=replay)
    assert _model_rows(_rows(out_v3b.trace_path))[0]["args"]["cache_hit"] is True

    _write_evidence("ac_t3_6_epoch.json", json.dumps({
        "CACHE_VERSION": CACHE_VERSION,
        "CACHE_LOGICAL_PREFIX": CACHE_LOGICAL_PREFIX,
        "CACHE_DISK_PREFIX": CACHE_DISK_PREFIX,
        "CACHE_VERSION_LEGACY": CACHE_VERSION_LEGACY,
        "per_switch_version": per_switch,
        "v2_files": v2_files,
        "v3_first_round_cache_hit": False,
        "v3_second_run_first_round_cache_hit": True,
    }, ensure_ascii=False, indent=1) + "\n")


# ---------------------------------------------------------------------------
# F1–F4 组件级单测（T3.5 步骤 2/3/6/8/9 的“+ 单测”面）
#
# 与上面六个 AC 节点分开命名（``test_f*`` / ``test_k_*``），因此
# ``-k "ac_t3_N"`` 的选择计数不受影响；这里补的是 AC 面覆盖不到的分支：
# 开关装配期校验、预算追授幂等、Gate1 授予解析、scratchpad 工具本体、
# RunSpec 默认值。
# ---------------------------------------------------------------------------

def test_f1_config_validation_and_all_off():
    """AgenticConfig：全关等值、非法 tail 轮数/非 bool 一律 ValueError。"""
    assert AgenticConfig() == ALL_OFF
    assert ALL_OFF.any_enabled is False
    assert ALL_OFF.verify_repair_tail_rounds == 3   # 默认值
    assert AgenticConfig(scratchpad=True).any_enabled is True
    for kwargs in (
            {"verify_repair_tail": True, "verify_repair_tail_rounds": 2},
            {"verify_repair_tail": True, "verify_repair_tail_rounds": 9},
            {"verify_repair_tail_rounds": True},     # bool 不是 int
            {"verify_repair_tail_rounds": 3.5},
            {"scratchpad": 1},                       # int 不是 bool
            {"run_expr": "yes"},
    ):
        with pytest.raises(ValueError):
            AgenticConfig(**kwargs)
    # 开关关闭时尾轮数允许是默认值（不参与装配）。
    assert AgenticConfig(verify_repair_tail=False).verify_repair_tail_rounds == 3


def test_f1_budget_tail_grant_is_idempotent():
    """mark_born/grant_verify_tail 各只生效一次；有效帽 = round_cap + tail。"""
    b = BudgetTracker(round_cap=4, verify_repair_tail_rounds=3)
    assert b.effective_round_cap == 4 and b.tail_granted is False
    assert b.mark_born(4) is True
    assert b.mark_born(5) is False        # 只认第一次出生
    assert b.born_at_round == 4
    assert b.grant_verify_tail() is True
    assert b.grant_verify_tail() is False  # 不叠加授予
    assert b.effective_round_cap == 7
    with pytest.raises(ValueError):
        b.mark_born(-1)


def test_f1_without_tail_keeps_round_cap_kind():
    """未配置尾轮：撞帽仍是旧的 round_cap 分类（不是 birth_absent）。"""
    b = BudgetTracker(round_cap=2)
    assert b.grant_verify_tail() is False
    b.add_round()
    b.add_round()
    with pytest.raises(CapExceeded) as excinfo:
        b.add_round()
    assert excinfo.value.kind == KIND_ROUND_CAP


def test_f2_deny_when_not_granted(tmp_path):
    """未授 run_expr：工具层 deny，容器一次也不起。"""
    backend = _backend()
    ctx = _expr_ctx(tmp_path, backend, modules=("math",),
                    tools=("read_file",))
    res = run_expr(ctx, src="import math\nprint(math.pi)")
    assert res.denied is True
    assert "不在 Gate1 授予表内" in res.output
    assert backend.calls == []


def test_f2_grant_string_parse_expr():
    """``expr:`` 授予串：隐含 run_expr + 模块白名单；非法名 fail-closed。"""
    from orchestrator.agentic.wiring import (AgenticWiringError,
                                             parse_grant_strings)
    grants = parse_grant_strings(("expr:math,numpy,os.path",))
    assert grants.tools == frozenset({"run_expr"})
    assert grants.expr_modules == frozenset({"math", "numpy", "os.path"})
    for bad in ("expr:", "expr:math,", "expr:,math", "expr:os-path",
                "expr:a..b", "expr:.math", "expr:math."):
        with pytest.raises(AgenticWiringError):
            parse_grant_strings((bad,))


def test_f2_dotted_import_requires_matching_alias():
    """点分模块只放行 ``import a.b as b``；其余绑定形态是 binding_mismatch。"""
    allowed = frozenset({"os.path", "math"})
    validate_program("import os.path as path\nprint(path.join('a', 'b'))",
                     allowed_modules=allowed)
    for bad in ("import os.path", "import os.path as os",
                "import math, os.path"):
        with pytest.raises(ExprRejected) as excinfo:
            validate_program(bad + "\nprint(1)", allowed_modules=allowed)
        assert excinfo.value.reason == "binding_mismatch"


def test_f3_strict_path_denies_without_tolerant(tmp_path):
    """关闭容错时同一补丁仍被严格锚点拒（F3 只放宽 unified_diff 通道）。"""
    wd = _fresh_historical(tmp_path, "strict_only")
    before = (wd / "my_check.py").read_bytes()
    res = apply_patch(_patch_ctx(wd, tolerant=False),
                      patch=json.dumps({"unified_diff": _PATCH_P1}))
    assert res.denied is True
    assert "PatchAnchorError(hits=0)" in res.output
    assert (wd / "my_check.py").read_bytes() == before


def test_f4_scratchpad_tool_roundtrip(tmp_path):
    """update_scratchpad：整体覆盖写、0600、回读逐字节相等。"""
    wd = tmp_path / "wd"
    wd.mkdir()
    ctx = ToolContext(
        workdir=wd,
        grants=GrantTable(tools=frozenset({"update_scratchpad"}),
                          write_roots=("",)),
        thread_id="thr-sp")
    res = update_scratchpad(ctx, content="# 记忆\nAPI=v1\n")
    assert res.denied is False, res.output
    target = wd / SCRATCHPAD_REL
    assert target.read_text(encoding="utf-8") == "# 记忆\nAPI=v1\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    # 第二轮覆盖写：内容是替换而不是追加。
    res2 = update_scratchpad(ctx, content="API=v2\n")
    assert res2.denied is False
    assert target.read_text(encoding="utf-8") == "API=v2\n"


def test_f4_scratchpad_rejects_oversize_and_missing_grant(tmp_path):
    """超长即拒（不静默截断）；未授 update_scratchpad 时零落盘。"""
    wd = tmp_path / "wd"
    wd.mkdir()
    granted = ToolContext(
        workdir=wd,
        grants=GrantTable(tools=frozenset({"update_scratchpad"}),
                          write_roots=("",)),
        thread_id="thr-sp2")
    res = update_scratchpad(granted, content="x" * (4000 + 1))
    assert res.denied is True
    assert "4000" in res.output
    assert not (wd / SCRATCHPAD_REL).exists()

    denied = ToolContext(
        workdir=wd,
        grants=GrantTable(tools=frozenset({"read_file"}),
                          write_roots=("",)),
        thread_id="thr-sp3")
    res2 = update_scratchpad(denied, content="ok\n")
    assert res2.denied is True
    assert not (wd / SCRATCHPAD_REL).exists()


def test_k_runspec_agentic_config_defaults_to_none():
    """RunSpec 第 6 个 agentic 字段默认 None（全关＝Phase 2.5 旧行为）。"""
    import dataclasses

    from orchestrator.agent_runner.runner import RunSpec
    fields = {f.name: f for f in dataclasses.fields(RunSpec)}
    assert "agentic_config" in fields
    assert fields["agentic_config"].default is None


def test_forensics_v2_payload_keeps_phase25_shape():
    """v2 载荷逐字节含 10 个旧键、零个 Task3 新键；v3 三键恒在。"""
    base = dict(stage="coding_debug", model="m", system_prompt="sys",
                task="task", round_idx=0, prefix_hex="0" * 64,
                max_tokens=8192, temperature=0.6,
                thinking={"type": "disabled"})
    v2_same = agentic_cache_fingerprint(
        **base, version=CACHE_VERSION_LEGACY,
        scratchpad_sha256="f" * 64, scratchpad_chars=999,
        prompt_extra_sha256="e" * 64)
    v2_plain = agentic_cache_fingerprint(**base, version=CACHE_VERSION_LEGACY)
    # v2 纪元忽略三新键（回滚面：同一旧载荷必得同一旧键）。
    assert v2_same == v2_plain
    payload = {"v": CACHE_VERSION_LEGACY, "stage": "coding_debug",
               "model": "m", "system_prompt": "sys", "task": "task",
               "round": 0, "prefix": "0" * 64, "max_tokens": 8192,
               "temperature": 0.6, "thinking": {"type": "disabled"}}
    assert v2_plain == sha256_text(canonical_json(payload))
    v3_a = agentic_cache_fingerprint(**base, scratchpad_sha256="a" * 64)
    v3_b = agentic_cache_fingerprint(**base)
    assert v3_a != v3_b                       # 空串也参与计算，必然可分
