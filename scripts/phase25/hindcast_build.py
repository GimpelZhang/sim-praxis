#!/usr/bin/env python3
"""Phase 2.5 Task 1 — hindcast 数据集构建器（0 付费、只读封存账）。

输入（全部只读）：
  - $SIMPRAXIS_PHASE1_ROOT/quota/ledger.jsonl        （Phase 1 封存账，29 行）
  - $SIMPRAXIS_PHASE2_ROOT/quota/ledger.jsonl        （Phase 2 封存账，436+19 行）
  - $SIMPRAXIS_PHASE2_ROOT/closed/evidence-run5*     （run_5 三战 39 份不可变 req）
  - $SIMPRAXIS_PHASE1_ROOT/task4_runner/t7-*         （task7 存活 req，最后写入者字节）
  - $SIMPRAXIS_PHASE1_ROOT/.call_cache/              （仅文件计数；无命中日志）

输出（0600，目录 0700，幂等：同输入同字节）：
  - $SIMPRAXIS_PHASE25_ROOT/estimates/hindcast_dataset.json
  - $SIMPRAXIS_PHASE25_ROOT/walls/wall_b/SEQ_SPLIT.json

切分纪律（AC-T1-2；REVISE 2026-10-29 后两墙同取证纪律）：批次名 + req
关键词双条件；墙关键词只在当轮 ``## traceback / 日志尾部`` 段（driving
failure section）内匹配，缺段即 fail-closed；KB/避坑段剔除保留为
defense-in-depth。

墙 A 三级证据契约（M2）：
  byte_confirmed          存活 req 且 driving 段命中 G-9 四族（规则 RULE-G9）
  byte_unclassified       存活 req（sole-ref/末写者）但 driving 段不属 G-9，
                          按冻结规则码 RULE-DEP/LAUNCH/BENCH/BDDL/PATCH/
                          ENV-ADJ 逐件归因，带 tail 摘录
  seq_level_overwritten   path→seqs 映射机械派生的真·非末写者（字节不可复原）
线程 a/c 结构性排除（excluded_per_seq，规则码与窗内同 tail 对称）。
sA=60 是呼量计费构造＝窗内（线程 b/d/e）coding_debug 呼总数，与族归属无关：
2 confirmed + 13 unclassified + 45 overwritten。禁止用全盘数冒充墙呼量。

本脚本不触网、不打开任何写账入口、不写 phase1/phase2 根下一字节。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir
from orchestrator.common import runtime_paths as rp

SCHEMA = "simpraxis.phase25.hindcast.v1"

# ---- 活账锚点（AC-T1-1；Complete H.1 与合同一致，脚本对活账强断言） ----
ANCHORS = {
    "phase1_rows": 29,
    "phase2_calls": 436,
    "phase2_blocked": 19,
    "by_task": {
        # task: [calls, ok, fail, seq_lo, seq_hi]
        "4": [24, 1, 23, 1, 24],
        "6": [210, 195, 15, 25, 234],
        "7": [74, 51, 23, 235, 308],
        "8": [128, 123, 5, 309, 436],
    },
    "by_stage": {"macro_ideation": 14, "coding_debug": 398, "visual_rollout": 24},
    "blocked_by_task": {"6": 10, "7": 7, "8": 2},
    "phase2_stage_by_task": {
        "6": {"macro_ideation": 3, "coding_debug": 207},
        "7": {"macro_ideation": 8, "coding_debug": 66},
        "8": {"macro_ideation": 3, "coding_debug": 125},
        "4": {"visual_rollout": 24},
    },
}

# run_5 三次机判战役（结案报告 T8-C9/C10/C11；seq↔批次/轮次由 ledger evidence
#  basename 机械对应：birth=coding_round1.chunk0..5，repair=coding_round2..8）。
RUN5_BATCHES = [
    ("T8-C9-stage1", 398, "evidence-run5-cap-watertight-20261028T0821Z",
     ("watertight",)),
    ("T8-C10-stage1", 411, "evidence-run5c6-cap-clearance-20261028T0912Z",
     ("边界边", "boundary edge")),
    ("T8-C11-stage1", 424, "evidence-run5c7-cap-clearance-20261028T0944Z",
     ("哨兵", "数值导数")),
]

# B-eng：仅匹配当轮 driving failure（## traceback / 日志尾部 段）中的工程信号。
# 锁内适配（Phase 2.5）：完整合同文本每呼注入，整 req grep 会令全部呼同时命中
# eng/cap；且见证量字典里的 "nonmanifold_edges": 0 / is_watertight: True 是
# 拓扑已过的证据，故 B-eng 只认真失败短语（空/不水密、two-manifold 判负、
# API 漂移异常），泛词 nonmanifold/watertight 不作 eng 信号。
ENG_PATTERNS = [
    (r"empty or not watertight", "precheck: 源壶为空或不水密"),
    (r"not a two-manifold watertight solid", "合同 5/8：非二流形水密实体判负"),
    (r"AttributeError", "trimesh API 漂移（remove_duplicate_faces）"),
]
# B-cap：运动学见证量违约（speed≤0.20、clearance≥0.012、spout rho/z/drift、
# 铰链耦合角速）。B-eng 优先匹配（工程不合格先于能力调参归因）。
CAP_PATTERNS = [
    (r"contract 2 spout", "合同 2 spout rho/z 运动学违约"),
    (r"contract 3", "合同 3 pitcher/cup clearance 运动学违约"),
    (r"contract 6", "合同 6 spout speed/drift/angular speed 运动学违约"),
]

# 墙 A 关键词族（Complete G-9），只在非 KB/避坑注入段匹配。
WALL_A_FAMILIES = [
    ("binding_utils", re.compile(r"binding_utils"),
     "robosuite binding_utils 断言（jnt_type 为 7 维末位）"),
    ("qM", re.compile(r"\bqM\b"),
     "base_controller d.qM（mujoco 3.15 MjData 已改名 M）"),
    ("env_api", re.compile(r"KeyError|\.reset\(|\.step\("),
     "env API 三坑（obs 键名/reset/step）"),
    ("anchor", re.compile(r"锚 0 命中"),
     "replace 围栏 func 锚点 0 命中"),
]

EXCLUDED_SECTION_HINTS = ("知识库", "避坑")

# 窗内/窗外的机械判据（M2）：存活 req 的 driving 段出现「运行已越过依赖层
# 进入 benchmark/episode 执行」或直接命中 G-9 族的线程才属战役窗口（b/d/e）。
# 注意不能用 OffScreenRenderEnv 导入词：线程 a 的 239–241 与 b 的 252/255 有
# 逐字同形的 LIBERO_tour envs 导入栈（easydict/gym），该词不区分战役归属。
# a 止于启动器+依赖失败（无 benchmark/episode/G-9 信号）、c 仅启动器失败，
# 故结构性排除。判据只认 driving 段字节，不认线程名字母。
WINDOW_REACH_PATTERNS = [
    re.compile(r"get_task_names|get_task_bddl_file_names|Benchmark\."),
    re.compile(r"cannot read action bounds"),              # env 对象已建、读动作界
    re.compile(r"\brun_episode\b"),                         # episode 执行层帧
] + [pat for _, pat, _ in WALL_A_FAMILIES]                  # G-9 族命中自证在墙内

# driving 段规则码（冻结集，禁止新增 G-9 坑）。顺序即优先级：G-9 族最先
# （锚点 0 命中/env API 三坑/binding_utils/qM 是本墙定义本身），其后按
# 启动器→依赖→benchmark 契约→补丁围栏→BDDL→env 邻近失效归因。
RULE_G9 = "RULE-G9"
RULE_DEP = "RULE-DEP"
RULE_LAUNCH = "RULE-LAUNCH"
RULE_BENCH = "RULE-BENCH"
RULE_BDDL = "RULE-BDDL"
RULE_PATCH = "RULE-PATCH"
RULE_ENV_ADJ = "RULE-ENV-ADJ"

RULE_TABLE = [
    (RULE_G9, None, "driving 段命中 G-9 四族（env API 三坑/binding_utils/qM/锚点）"),
    (RULE_LAUNCH, re.compile(r"can't open file"),
     "启动器失败：stage1_minimal.py 不存在（出生/非修复轮，呼仍真金白银计窗内）"),
    (RULE_DEP, re.compile(
        r"No module named|ModuleNotFoundError|"
        r"cannot import OffScreenRenderEnv|"
        r"module 'gym' has no attribute|module \"gym\" has no attribute"),
     "模块/导入失败（easydict/gym/libero.envs/gym.Space/OffScreenRenderEnv）"),
    (RULE_BENCH, re.compile(
        r"get_task_names|get_task_bddl_file_names"),
     "benchmark 契约误用（缺 self/方法缺失）"),
    (RULE_PATCH, re.compile(r"补丁已被整体拒绝"),
     "补丁协议/围栏机器拒收（old 块非唯一/非恰好 1 次）；响应非 JSON 的"
     "「未产出可应用补丁」不计此规则"),
    (RULE_BDDL, re.compile(r"cannot resolve receptacle body"),
     "receptacle/BDDL body 解析失败"),
    (RULE_ENV_ADJ, re.compile(r"cannot read action bounds"),
     "最接近 env API 但无法证明到达 reset/step 之后（读动作界失败）"),
]

RULE_DESCRIPTIONS = {code: desc for code, _, desc in RULE_TABLE}


def classify_wall_a_driving(section_text: str) -> tuple[str, list[str], str]:
    """driving 段 → (rule, g9_families, rule_reason)。

    g9_families 仅记 driving 段实际命中的 G-9 族名（如 seq308 只记 env_api，
    不得再带 binding_utils/qM）；非 RULE-G9 时为空表。无法归类 fail-closed。
    """
    families = [name for name, pat, _ in WALL_A_FAMILIES
                if pat.search(section_text)]
    if families:
        return RULE_G9, families, RULE_DESCRIPTIONS[RULE_G9]
    for code, pat, why in RULE_TABLE[1:]:
        if pat.search(section_text):
            return code, [], why
    raise ValueError(
        "墙 A driving 段无法按冻结规则码归类（fail-closed）："
        f"{section_text[:160]!r}")


def _tail_error_line(section_text: str, limit: int = 200, *,
                     seq: int | None = None,
                     path: str | Path | None = None) -> str:
    """末条 Error 行摘录（倒序找 traceback 错误行；≤200 字符）。

    补丁围栏的机器注记（「机器核验…JSONDecodeError」）虽含 Error 字样但不是
    运行错误行，显式跳过；找不到标记行时回退末非空行，但回退行若本身仍是
    机器注释行（tail 候选全为注释）则 fail-closed——绝不静默拿注释贴标签
    （MINOR-2，与缺段/规则码不可归类同级）。
    """
    mark = re.compile(r"Error|Exception|No module named|can't open file|"
                      r"has no attribute")
    note = re.compile(r"机器核验|机器判定原因")
    lines = [ln for ln in (x.strip() for x in section_text.splitlines()) if ln]
    for ln in reversed(lines):
        if note.search(ln):
            continue
        if mark.search(ln):
            return ln[-limit:]
    fallback = lines[-1][-limit:] if lines else ""
    if fallback and note.search(fallback):
        raise ValueError(
            f"墙 A driving 段 tail 仅含机器注释行，无法机械归类（fail-closed；"
            f"seq={seq} path={path}）：{fallback[:160]!r}")
    return fallback


# ---------------------------------------------------------------- 读账

def read_ledger(path: str | Path) -> list[dict]:
    rows = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def split_calls_blocked(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    calls = [r for r in rows if isinstance(r.get("seq"), int)]
    blocked = [r for r in rows if r.get("seq") is None]
    return calls, blocked


def round_chunk(evidence: str | None):
    """ledger evidence basename → (round, chunk, label)。

    label: birth_chunk（拆块出生分片）/ birth_simple（简单出生单呼）/
    repair（修复轮）/ macro / unknown（供应探针或无 evidence 行）。
    """
    if not evidence:
        return None, None, "unknown"
    base = evidence.rsplit("/", 1)[-1]
    if base.startswith("macro_"):
        return None, None, "macro"
    m = re.search(r"coding_round(\d+)(?:\.chunk(\d+))?\.req\.json$", base)
    if not m:
        return None, None, "unknown"
    rnd, chk = int(m.group(1)), m.group(2)
    if rnd >= 2:
        return rnd, (int(chk) if chk is not None else None), "repair"
    return rnd, (int(chk) if chk is not None else None), (
        "birth_chunk" if chk is not None else "birth_simple")


# ---------------------------------------------------------------- req 解析

def req_user_text(req: dict) -> str:
    out = []
    for msg in req.get("messages", []):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    out.append(block.get("text", ""))
    return "\n".join(out)


def req_sections(text: str) -> dict[str, str]:
    """按 ``## 标题`` 切段（标题不含层级文本时并入 head）。"""
    sections: dict[str, str] = {}
    cur = "(head)"
    buf: list[str] = []
    for line in ("\n" + text).splitlines():
        m = re.match(r"^(#{2,})\s+(.+)$", line)
        if m:
            sections.setdefault(cur, "\n".join(buf))
            cur, buf = m.group(2).strip(), []
        else:
            buf.append(line)
    sections.setdefault(cur, "\n".join(buf))
    return sections


def non_injected_text(req: dict) -> str:
    """剔除 troubleshooting/capabilities KB 注入段与 phase1_learnings 避坑段。

    三级证据契约中的 defense-in-depth 层：墙 A/B 的机判主路径只看
    ``driving_failure_section``（## traceback / 日志尾部 段，天然不可达
    Stage0/源码/KB 等段）；本函数保留给 chunk0 出生合同关键词等需要非注入
    全文的辅路径。这些段把历次战役的失败短语（含「锚 0 命中」「watertight」
    等）逐字带进每个 req，绝不能作为当轮 driving 信号（39 行召回证据同款
    污染问题）。
    """
    return "\n".join(
        txt for head, txt in req_sections(req_user_text(req)).items()
        if not any(h in head for h in EXCLUDED_SECTION_HINTS))


def driving_failure_section(req: dict) -> str:
    """抽取当轮 ``## traceback / 日志尾部`` 段（到下一个 ## 标题为止）。"""
    text = req_user_text(req)
    m = re.search(r"## traceback[^\n]*\n(.*?)\n## ", text, re.S)
    if not m:
        raise ValueError("req 缺少 ## traceback / 日志尾部 段，无法机判 driving failure")
    return m.group(1)


# ---------------------------------------------------------------- 批次/线程切分

def _task_calls(calls: list[dict], task: int) -> list[dict]:
    return [r for r in calls if r.get("task") == task]


def batch_slices(calls: list[dict]) -> dict:
    batches: dict[str, dict] = {}
    for r in calls:
        b = batches.setdefault(r["batch_id"], {
            "batch_id": r["batch_id"], "task": r["task"],
            "seq_min": r["seq"], "seq_max": r["seq"],
            "seqs": [], "calls": 0, "ok": 0, "fail": 0,
            "by_stage": Counter(), "threads": Counter(),
            "birth_chunk_rows": 0, "birth_simple_rows": 0,
            "repair_rows": 0, "macro_rows": 0, "unknown_rows": 0,
            "round_hist": Counter(), "chunk_rows": 0,
            "repair_rounds": set(), "birth_chunks": set(),
        })
        b["seq_min"] = min(b["seq_min"], r["seq"])
        b["seq_max"] = max(b["seq_max"], r["seq"])
        b["seqs"].append(r["seq"])
        b["calls"] += 1
        b["ok"] += 1 if r.get("ok") else 0
        b["fail"] += 0 if r.get("ok") else 1
        b["by_stage"][r["stage"]] += 1
        if r.get("thread_id"):
            b["threads"][r["thread_id"]] += 1
        rnd, chk, label = round_chunk(r.get("evidence"))
        b[f"{label}_rows"] += 1
        if rnd is not None:
            b["round_hist"][f"round{rnd}"] += 1
        if chk is not None:
            b["chunk_rows"] += 1
        if label == "repair" and rnd is not None:
            b["repair_rounds"].add(rnd)
        if label == "birth_chunk" and chk is not None:
            b["birth_chunks"].add(chk)
    return dict(sorted(batches.items(), key=lambda kv: kv[1]["seq_min"]))


def _freeze_batch(b: dict) -> dict:
    return {
        "batch_id": b["batch_id"], "task": b["task"],
        "seq_min": b["seq_min"], "seq_max": b["seq_max"],
        "seq_range": [b["seq_min"], b["seq_max"]],
        "calls": b["calls"], "ok": b["ok"], "fail": b["fail"],
        "by_stage": dict(b["by_stage"]),
        "threads": dict(b["threads"]),
        "birth_chunk_rows": b["birth_chunk_rows"],
        "birth_simple_rows": b["birth_simple_rows"],
        "repair_rows": b["repair_rows"],
        "macro_rows": b["macro_rows"],
        "unknown_rows": b["unknown_rows"],
        "chunk_rows": b["chunk_rows"],
        "round_hist": dict(sorted(b["round_hist"].items())),
        "repair_rounds_seen": sorted(b["repair_rounds"]),
        "max_repair_round": (max(b["repair_rounds"]) if b["repair_rounds"] else 0),
        "distinct_birth_chunks": sorted(b["birth_chunks"]),
    }


# ---------------------------------------------------------------- run 分组（带证据等级）

# t6：批次名 runN 机械分组；run1 为 B1 窗口（探针/多次重出生，无 run 目录名）。
T6_RUN_RE = re.compile(r"T6-B\d+-(?:run(\d+)|.*)")

# t8：结案报告/closed 包可证 run3=C5、run4=C6/C7/C8、run5=C9/C10/C11；
# C1–C4 与 run1/run2 的边界无存活 req 可证——仅批次精确，run 归属
# provenance_limited，禁止猜。
T8_RUN_MAP = {
    "T8-C5-stage1": ("run3", "byte_confirmed", "closed/evidence-run3-c5 包"),
    "T8-C6-stage1": ("run4", "byte_confirmed", "closed/evidence-run4-c6 包"),
    "T8-C7-stage1": ("run4", "byte_confirmed", "closed/evidence-run4-c7 包"),
    "T8-C8-stage1": ("run4", "byte_confirmed", "closed/evidence-run4-c8 包"),
    "T8-C9-stage1": ("run5", "byte_confirmed", "closed/evidence-run5* 三包"),
    "T8-C10-stage1": ("run5", "byte_confirmed", "closed/evidence-run5* 三包"),
    "T8-C11-stage1": ("run5", "byte_confirmed", "closed/evidence-run5* 三包"),
}
T8_RUN12_BATCHES = {"T8-C1-stage1", "T8-C2-stage1", "T8-C3-stage1", "T8-C4-stage1"}


def run_label(task: int, batch_id: str, thread_id: str | None) -> dict:
    if task == 6:
        m = re.search(r"run(\d+)", batch_id)
        if m:
            return {"run": f"run{int(m.group(1))}", "provenance": "batch_name"}
        if re.match(r"T6-B[12]-", batch_id) or batch_id in (
                "T6-B5-birth32k-r3",):
            return {"run": "run1", "provenance": "batch_window"}
        return {"run": "probes", "provenance": "batch_name"}
    if task == 7:
        # 线程 a..e 即 RunSpec 出生序列；批次 Cn 是同线程内再出生。
        return {"run": (thread_id or "").rsplit("-", 1)[-1],
                "provenance": "thread_id"}
    if task == 8:
        if batch_id in T8_RUN_MAP:
            run, prov, why = T8_RUN_MAP[batch_id]
            return {"run": run, "provenance": prov, "evidence": why}
        if batch_id in T8_RUN12_BATCHES:
            return {"run": "run1_or_run2",
                    "provenance": "provenance_limited",
                    "evidence": "C1–C4 无存活 run 标识 req，仅批次级可切，不猜 run 归属"}
        if batch_id.startswith("T8-M"):
            return {"run": "macro", "provenance": "batch_name"}
    return {"run": None, "provenance": "unknown"}


# ---------------------------------------------------------------- 墙 B（run_5）

def classify_driving_failure(section_text: str) -> tuple[str, str]:
    """B-eng/B-cap 二分类（driving failure 段；eng 优先；无法归类即 fail-closed）。"""
    for pattern, why in ENG_PATTERNS:
        if re.search(pattern, section_text, re.IGNORECASE):
            return "B-eng", why
    for pattern, why in CAP_PATTERNS:
        if re.search(pattern, section_text, re.IGNORECASE):
            return "B-cap", why
    raise ValueError(f"driving failure 无法归类：{section_text[:160]!r}")


def run5_req_path(closed_root: Path, pkg_dir: str, rnd: int,
                  chunk: int | None) -> Path:
    name = (f"coding_round{rnd}.chunk{chunk}.req.json" if chunk is not None
            else f"coding_round{rnd}.req.json")
    return closed_root / pkg_dir / "runner_evidence" / name


def build_wall_b(closed_root: Path) -> dict:
    """逐 seq 切 run_5 三战 39 呼；必须 39/39 双条件成立，否则 fail-closed。"""
    entries = []
    counts = Counter()
    for batch, base_seq, pkg_dir, birth_kws in RUN5_BATCHES:
        pkg = closed_root / pkg_dir / "runner_evidence"
        # 出生 6 分片：批次名 + chunk0 合同增量关键词双条件。
        chunk0 = json.loads((pkg / "coding_round1.chunk0.req.json").read_text())
        text0 = non_injected_text(chunk0)
        hit = next((kw for kw in birth_kws if kw in text0), None)
        if hit is None:
            raise ValueError(
                f"{batch} chunk0 缺出生合同增量关键词 {birth_kws}，双条件不成立")
        for chunk in range(6):
            seq = base_seq + chunk
            req_path = run5_req_path(closed_root, pkg_dir, 1, chunk)
            if not req_path.is_file():
                raise ValueError(f"墙B req 缺失：{req_path}")
            entries.append({
                "seq": seq, "batch_id": batch, "phase": "birth",
                "round": 1, "chunk": chunk, "wall": "B-eng",
                "reason": f"出生分片 chunk{chunk}；批次 {batch} 且 chunk0 "
                          f"含工程合同增量关键词 {hit!r}",
                "evidence": str(req_path),
                "evidence_level": "byte_confirmed",
            })
            counts["B-eng"] += 1
        # 修复轮 r2..r8：逐份读 driving failure 段机判。
        for rnd in range(2, 9):
            seq = base_seq + 6 + (rnd - 2)
            req_path = run5_req_path(closed_root, pkg_dir, rnd, None)
            req = json.loads(req_path.read_text())
            wall, why = classify_driving_failure(driving_failure_section(req))
            entries.append({
                "seq": seq, "batch_id": batch, "phase": "repair",
                "round": rnd, "chunk": None, "wall": wall,
                "reason": f"{batch} round{rnd} driving failure：{why}",
                "evidence": str(req_path),
                "evidence_level": "byte_confirmed",
            })
            counts[wall] += 1
    seqs = [e["seq"] for e in entries]
    if seqs != list(range(398, 437)) or len(entries) != 39:
        raise ValueError("墙B 切片不覆盖 seq398–436 全集")
    return {
        "schema": "simpraxis.phase25.wall_b_seq_split.v1",
        "wall": "B（Task8 run_5 机判战役；M7 B-eng/B-cap 二分）",
        "frozen_scope": "首呼前冻结；Task 5 子帽公式分母只取 B-eng",
        "seq_range": [398, 436],
        "run5_clarification": (
            "合同措辞「run_5 seq309–436」以活账更正：run_5 机判战役＝"
            "T8-C9/C10/C11＝seq398–436（结案报告同）；seq309–397 属 "
            "run_1..4 四次真人 Gate2 reject 战役，不在墙 B 工程/能力二分内。"),
        "classifier": {
            "method": "仅对 ## traceback / 日志尾部 段做短语机判，"
                      "不对整 req grep（完整合同每呼注入，整 req 必双中）",
            "eng_patterns": [p for p, _ in ENG_PATTERNS],
            "cap_patterns": [p for p, _ in CAP_PATTERNS],
            "precedence": "B-eng 优先；见证量字典里 nonmanifold_edges=0/"
                          "is_watertight=true 为拓扑已过，不作 eng 信号",
            "birth_rule": "出生 6 分片全归 B-eng（双条件：批次名 + chunk0 "
                          "工程合同增量关键词）",
        },
        "counts": {"total": 39, "B_eng": counts["B-eng"],
                   "B_cap": counts["B-cap"],
                   "birth": 18, "repair": 21,
                   "by_batch": {
                       b: {"B_eng": sum(1 for e in entries
                                        if e["batch_id"] == b
                                        and e["wall"] == "B-eng"),
                           "B_cap": sum(1 for e in entries
                                        if e["batch_id"] == b
                                        and e["wall"] == "B-cap")}
                       for b, *_ in RUN5_BATCHES}},
        "sB_eng": counts["B-eng"],
        "sB_cap": counts["B-cap"],
        "entries": entries,
    }


# ---------------------------------------------------------------- 墙 A（task7）

def _compute_wall_a_tiers(t7: list[dict], runner_root: Path) -> dict:
    """墙 A 三级机械切分（无活账锚点断言；hermetic 合成测试直接调本函数）。

    纯机械：path→seqs 末写者取证 + driving 段规则码 + env-reach 窗口派生。
    任何 req 缺件/driving 段缺失/规则码无法归类均 fail-closed。
    """
    rows_by_seq = {r["seq"]: r for r in t7}
    # evidence 路径（相对 artifacts 根）→ 引用它的全部 seq；文件被同轮重试覆盖，
    # 存活字节＝最后写入者＝引用 seq 的最大值（仅由 ledger 机械推出）。
    path_seqs: dict[str, list[int]] = defaultdict(list)
    for r in t7:
        ev = r.get("evidence")
        if ev and "task4_runner/t7-" in ev:
            path_seqs[ev].append(r["seq"])

    # ---- 逐件读存活 req：driving 段规则码 + 窗内/窗外机械判据 ----
    byte_confirmed: list[dict] = []
    byte_unclassified: list[dict] = []
    excluded_per_seq: list[dict] = []
    reach_by_thread: dict[str, list[str]] = defaultdict(list)
    surviving: list[tuple[int, str, Path, list[int], str, str, str, list[str]]] = []
    for ev, seqs in sorted(path_seqs.items(), key=lambda kv: max(kv[1])):
        seq = max(seqs)
        row = rows_by_seq[seq]
        req_path = runner_root / ev.split("task4_runner/", 1)[1]
        if not req_path.is_file():
            raise ValueError(f"墙 A 存活 req 缺件（fail-closed）：{req_path}")
        req = json.loads(req_path.read_text())
        section = driving_failure_section(req)  # 缺段即 raise（与墙 B 同纪律）
        rule, families, rule_reason = classify_wall_a_driving(section)
        tail = _tail_error_line(section, seq=seq, path=req_path)
        reach = [p.pattern for p in WINDOW_REACH_PATTERNS
                 if p.search(section)]
        if reach:
            reach_by_thread[row["thread_id"]].extend(reach)
        surviving.append((seq, row["thread_id"], req_path, sorted(seqs),
                          rule, rule_reason, tail, families))

    # 窗口成员由 driving 字节机械派生（不认线程名字母）：至少一条存活 req
    # 证明该线程运行已进入 env 构造/benchmark/episode 层或直接命中 G-9。
    window_threads = set(reach_by_thread)

    for seq, thread, req_path, refs, rule, rule_reason, tail, families \
            in surviving:
        rnd, _, round_label = round_chunk(req_path.name)
        base = {
            "seq": seq, "thread_id": thread,
            "evidence": str(req_path),
            "evidence_round": rnd, "round_label": round_label,
            "is_repair_round": round_label == "repair",
            "rule": rule, "rule_reason": rule_reason,
            "tail_excerpt": tail,
            "refs_to_same_path": refs,
            "survivor_is_last_writer": refs[-1] == seq and len(refs) >= 1,
        }
        if thread not in window_threads:
            excluded_per_seq.append({
                **base,
                "structural_reason": (
                    f"线程 {thread} 全部存活 driving 段无 env-reach 信号，"
                    "结构性排除出 sA 战役窗口；规则码与窗内同 tail 对称"
                    "（同 tail 必同 rule，桶只由窗口成员身份决定）"),
            })
            continue
        if rule == RULE_G9:
            byte_confirmed.append({
                **base, "g9_families": families,
                "reason": f"存活 req 末写者（同路径被 refs={refs} "
                          f"{len(refs)} 个 seq 引用，存活字节属 seq {seq}）；"
                          f"driving 段命中 G-9 族 {families}",
                "overwritten_seqs": [s for s in refs if s != seq],
            })
        else:
            byte_unclassified.append({
                **base,
                "reason": f"存活字节在位（refs={refs} {len(refs)} 个 seq "
                          f"引用同一证据路径，存活字节属 seq {seq}），但 "
                          f"driving 段不属 G-9 四族，按冻结规则码归 {rule}；"
                          "呼仍计 sA（真金白银花掉，T5 可能再花）",
            })

    # ---- seq 级：path_seqs 机械派生的真·非末写者（仅窗内）----
    seq_level = []
    for ev, seqs in path_seqs.items():
        survivor = max(seqs)
        if rows_by_seq[survivor]["thread_id"] not in window_threads:
            continue
        for s in seqs:
            if s == survivor:
                continue
            row = rows_by_seq[s]
            seq_level.append({
                "seq": s, "thread_id": row["thread_id"],
                "evidence_path": ev,
                "survivor_seq": survivor,
                "refs": sorted(seqs),
                "rule": "UNRECOVERABLE",
                "reason": f"同路径被 refs={sorted(seqs)} {len(seqs)} 个 seq "
                          f"引用，存活字节属 seq {survivor}，本呼不可复原"
                          "（机械派生，非硬写；仅 seq 级可切，不猜内容）",
                "evidence_level": "provenance_limited",
            })
    seq_level.sort(key=lambda e: e["seq"])

    window_path_seqs = {
        s for ev, seqs in path_seqs.items()
        if rows_by_seq[max(seqs)]["thread_id"] in window_threads
        for s in seqs}
    window_rows = [r for r in t7 if r["thread_id"] in window_threads]
    unmapped = sorted(r["seq"] for r in window_rows
                      if r["seq"] not in window_path_seqs)
    return {
        "byte_confirmed": byte_confirmed,
        "byte_unclassified": byte_unclassified,
        "seq_level_overwritten": seq_level,
        "excluded_per_seq": excluded_per_seq,
        "window_threads": sorted(window_threads),
        "sA": len(window_rows),
        "unmapped_window_seqs": unmapped,
    }


def build_wall_a(calls: list[dict], runner_root: Path) -> dict:
    t7 = [r for r in _task_calls(calls, 7) if r["stage"] == "coding_debug"]
    tiers = _compute_wall_a_tiers(t7, runner_root)
    byte_confirmed = tiers["byte_confirmed"]
    byte_unclassified = tiers["byte_unclassified"]
    seq_level = tiers["seq_level_overwritten"]
    excluded_per_seq = tiers["excluded_per_seq"]
    window_threads = set(tiers["window_threads"])
    sA = tiers["sA"]

    # 三级加总＝窗内 coding 呼总数（生产账中每个窗内 seq 都有 task4_runner
    # 证据路径；出现未映射行＝取证模型失配，fail-closed，禁止悄悄漏计）。
    assert not tiers["unmapped_window_seqs"], tiers["unmapped_window_seqs"]
    assert sA == len(byte_confirmed) + len(byte_unclassified) \
        + len(seq_level), "sA 三级加总必须等于窗内 coding 呼总数"
    # 活账锚点（LEAD 锁死口径；与字节不一致即 fail-closed，禁止凑数）。
    confirmed_seqs = sorted(e["seq"] for e in byte_confirmed)
    unclass_seqs = sorted(e["seq"] for e in byte_unclassified)
    excluded_seqs = sorted(e["seq"] for e in excluded_per_seq)
    assert sA == 60, f"sA 活账锚点 60，实算 {sA}"
    assert confirmed_seqs == [254, 308], confirmed_seqs
    assert len(byte_unclassified) == 13, unclass_seqs
    assert len(seq_level) == 45
    assert excluded_seqs == [237, 238, 239, 240, 241, 259], excluded_seqs
    expected_rules = {
        252: RULE_DEP, 253: RULE_DEP, 255: RULE_DEP, 256: RULE_DEP,
        261: RULE_LAUNCH, 262: RULE_BENCH, 263: RULE_BENCH,
        264: RULE_BENCH, 265: RULE_ENV_ADJ,
        304: RULE_BDDL, 305: RULE_BDDL, 306: RULE_PATCH, 307: RULE_LAUNCH,
    }
    for e in byte_unclassified:
        assert e["rule"] == expected_rules[e["seq"]], (e["seq"], e["rule"])
    expected_excluded_rules = {
        237: RULE_LAUNCH, 238: RULE_DEP, 239: RULE_DEP, 240: RULE_DEP,
        241: RULE_DEP, 259: RULE_LAUNCH}
    for e in excluded_per_seq:
        assert e["rule"] == expected_excluded_rules[e["seq"]], \
            (e["seq"], e["rule"])
    # 规则码对称：同 tail 族在窗内/窗外必须同 rule（由同一分类器保证，
    # 此处机判 LAUNCH 四呼与 DEP 类集合）。
    assert {e["seq"] for e in excluded_per_seq if e["rule"] == RULE_LAUNCH} \
        == {237, 259}

    return {
        "sA": sA,
        "sA_definition": (
            "sA=战役窗口（线程 b/d/e，窗口成员由存活 driving 段 env-reach "
            "信号机械派生）内 coding_debug 呼总数；呼量计费构造，与 G-9 族"
            "归属无关"),
        "sA_breakdown": {
            "byte_confirmed_RULE_G9": len(byte_confirmed),
            "byte_unclassified": len(byte_unclassified),
            "seq_level_overwritten": len(seq_level),
            "total": sA,
            "legacy_remap": (
                "REVISE 前「字节 9 + seq 级 51」：9 中 2 留 confirmed、"
                "7 转 unclassified；51 中 6 为 sole-ref 存活转 unclassified、"
                "45 为真·覆盖转 seq_level_overwritten"),
        },
        "byte_confirmed_count": len(byte_confirmed),
        "byte_unclassified_count": len(byte_unclassified),
        "seq_level_overwritten_count": len(seq_level),
        "byte_confirmed": byte_confirmed,
        "byte_unclassified": byte_unclassified,
        "seq_level_overwritten": seq_level,
        "excluded_per_seq": excluded_per_seq,
        "excluded_summary": (
            f"{len(excluded_per_seq)} 呼（线程 a seq237–241、线程 c seq259）"
            "结构性排除出窗口；规则码与窗内同 tail 对称（237/259/261/307 同 "
            "can't-open-file tail 同 RULE-LAUNCH；238–241 与 252/255 同导入"
            "失败族同 RULE-DEP）；a/c 不计 sA"),
        "window_threads": sorted(window_threads),
        "window_derivation": (
            "窗口不认线程名字母：线程任一存活 req 的 driving 段命中"
            " WINDOW_REACH_PATTERNS（越过依赖层：benchmark 契约/action "
            "bounds/run_episode，或直接命中 G-9 族）即在窗内；a 止于启动器"
            "+依赖失败（OffScreenRenderEnv 导入栈与 b 同形但无越过信号，"
            "故该词不入判据）、c 仅启动器失败，零 reach 故排除"),
        "supply_orthogonality": (
            "t7 全部 23 个供应侧失败呼计入 C_supply 失败税，不重复计墙呼量"
            "（同一呼可既在墙窗口内又被失败税加总，两层正交）"),
        "method": (
            "批次（t7 战役批次/线程）+ driving 段（## traceback / 日志尾部）"
            "规则码双条件；缺段 fail-closed；KB/避坑/Stage0/源码段天然不可达，"
            "non_injected_text 剔除仅作 defense-in-depth"),
    }


# ---------------------------------------------------------------- 失败税/代际

def failure_tax_bins(calls: list[dict]) -> list[dict]:
    bins = []
    for task, label, note in [
            (6, "deepseek/glm/mimo 代际（t6）", "15 fail 全在 B1 窗口"),
            (7, "mimo 代际（t7）", "23 fail 集中 C2/C5–C8 供应侧"),
            (8, "glm-5.3 macro + kimi-k3.1 coding 代际（t8）",
             "2 macro timeout + 3 coding C1"),
            (4, "ark-code-latest 视觉代际（t4，独立箱，不外推）",
             "HTTP200 空答 23/24")]:
        rows = _task_calls(calls, task)
        ok = sum(1 for r in rows if r.get("ok"))
        fail = sum(1 for r in rows if not r.get("ok"))
        bins.append({
            "task": task, "generation": label, "calls": len(rows),
            "ok": ok, "fail": fail,
            "fail_rate": round(fail / len(rows), 4), "note": note,
        })
    return bins


# ---------------------------------------------------------------- 数据集

def build_dataset(phase1_root: Path, phase2_root: Path) -> tuple[dict, dict]:
    p1_rows = read_ledger(phase1_root / "quota" / "ledger.jsonl")
    p2_rows = read_ledger(phase2_root / "quota" / "ledger.jsonl")
    calls, blocked = split_calls_blocked(p2_rows)

    # 锚点强断言（对不上即拒，禁止凑数）。
    assert len(p1_rows) == ANCHORS["phase1_rows"], "phase1 行数与锚点不符"
    assert len(calls) == ANCHORS["phase2_calls"], "phase2 实呼数与锚点不符"
    assert len(blocked) == ANCHORS["phase2_blocked"], "phase2 blocked 数与锚点不符"
    by_stage = Counter(r["stage"] for r in calls)
    for stage, n in ANCHORS["by_stage"].items():
        assert by_stage[stage] == n, f"by_stage {stage}={by_stage[stage]}!={n}"
    for task, (tot, ok, fail, lo, hi) in ANCHORS["by_task"].items():
        rows = _task_calls(calls, int(task))
        got_ok = sum(1 for r in rows if r.get("ok"))
        assert (len(rows), got_ok, len(rows) - got_ok,
                min(r["seq"] for r in rows), max(r["seq"] for r in rows)) == (
            tot, ok, fail, lo, hi), f"task {task} 锚点不符"
        st = Counter(r["stage"] for r in rows)
        for stage, n in ANCHORS["phase2_stage_by_task"][task].items():
            assert st[stage] == n, f"task{task} {stage}={st[stage]}!={n}"
    blk = Counter(r["task"] for r in blocked)
    for task, n in ANCHORS["blocked_by_task"].items():
        assert blk[int(task)] == n, f"blocked task{task}={blk[int(task)]}!={n}"

    batches = batch_slices(calls)

    # ---- phase1 切分
    p1 = {
        "rows": len(p1_rows),
        "ok": sum(1 for r in p1_rows if r.get("ok")),
        "fail": sum(1 for r in p1_rows if not r.get("ok")),
        "by_task": {str(t): dict(Counter(r["stage"] for r in p1_rows
                                          if r.get("task") == t))
                    for t in sorted({r.get("task") for r in p1_rows})},
        "by_stage": dict(Counter(r["stage"] for r in p1_rows)),
        "fail_rate": round(
            sum(1 for r in p1_rows if not r.get("ok")) / len(p1_rows), 4),
        "sealed": "Phase 1 封存账只读；本脚本不以写方式打开",
    }

    # ---- 逐 task 批次/线程/run 切分
    threads = {}
    for r in calls:
        t = threads.setdefault((r["task"], r.get("thread_id")), {
            "task": r["task"], "thread_id": r.get("thread_id"),
            "calls": 0, "ok": 0, "fail": 0, "by_stage": Counter(),
            "seq_min": r["seq"], "seq_max": r["seq"], "batches": Counter()})
        t["calls"] += 1
        t["ok"] += 1 if r.get("ok") else 0
        t["fail"] += 0 if r.get("ok") else 1
        t["by_stage"][r["stage"]] += 1
        t["seq_min"] = min(t["seq_min"], r["seq"])
        t["seq_max"] = max(t["seq_max"], r["seq"])
        t["batches"][r["batch_id"]] += 1

    thread_out = []
    for (task, tid), t in sorted(threads.items(),
                                 key=lambda kv: kv[1]["seq_min"]):
        runs: dict[str, dict] = {}
        for r in calls:
            if r["task"] != task or r.get("thread_id") != tid:
                continue
            lab = run_label(task, r["batch_id"], tid)
            run = runs.setdefault(lab["run"] or "_", {
                "run": lab["run"], "provenance": lab["provenance"],
                "calls": 0, "batches": Counter()})
            if "evidence" in lab:
                run["evidence"] = lab["evidence"]
            run["calls"] += 1
            run["batches"][r["batch_id"]] += 1
        for run in runs.values():
            run["batches"] = dict(sorted(run["batches"].items()))
        thread_out.append({
            "task": task, "thread_id": tid,
            "seq_range": [t["seq_min"], t["seq_max"]],
            "calls": t["calls"], "ok": t["ok"], "fail": t["fail"],
            "by_stage": dict(t["by_stage"]),
            "runs": sorted(runs.values(), key=lambda x: str(x["run"])),
        })

    # ---- 修复帽演进实证：按批次 max_repair_round 归档
    cap_evidence = {"cap3": [], "cap5": [], "cap8": []}
    for b in batches.values():
        mx = max(b["repair_rounds"], default=0)
        rec = {"batch_id": b["batch_id"], "max_repair_round": mx}
        if mx >= 8:
            cap_evidence["cap8"].append(rec)
        elif mx >= 5:
            cap_evidence["cap5"].append(rec)
        elif mx >= 3:
            cap_evidence["cap3"].append(rec)

    # ---- 出生呼数经验样本（按批次；供应重试致 rows>文件数，两栏并陈）
    birth_samples = {"simple": [], "chunked": []}
    for b in batches.values():
        if b["birth_chunk_rows"]:
            birth_samples["chunked"].append({
                "batch_id": b["batch_id"],
                "distinct_birth_chunks": len(b["birth_chunks"]),
                "birth_chunk_rows": b["birth_chunk_rows"],
                "note": "distinct 分片数为出生宽度；rows 含同分片供应重试",
            })
        elif b["birth_simple_rows"]:
            birth_samples["simple"].append({
                "batch_id": b["batch_id"],
                "birth_simple_rows": b["birth_simple_rows"]})

    wall_a = build_wall_a(
        calls, phase2_root.parent / "phase1" / "task4_runner")
    wall_b = build_wall_b(phase2_root / "closed")

    cache_dir = phase2_root.parent / "phase1" / ".call_cache"
    cache_files = sum(1 for p in cache_dir.iterdir() if p.is_file()) \
        if cache_dir.is_dir() else None

    dataset = {
        "schema": SCHEMA,
        "generator": "scripts/phase25/hindcast_build.py（同输入同字节；无时间戳）",
        "anchors": ANCHORS,
        "phase1": p1,
        "phase2": {
            "calls": len(calls), "blocked": len(blocked),
            "ok": sum(1 for r in calls if r.get("ok")),
            "fail": sum(1 for r in calls if not r.get("ok")),
            "by_stage": dict(by_stage),
            "blocked_by_task": {str(k): v for k, v in sorted(blk.items())},
            "blocked_by_batch": {
                b: n for b, n in sorted(Counter(
                    r["batch_id"] for r in blocked).items())},
            "failure_tax_bins": failure_tax_bins(calls),
            "batches": [_freeze_batch(b) for b in batches.values()],
            "threads": thread_out,
            "repair_cap_evolution": {
                "cap3_batches": cap_evidence["cap3"],
                "cap5_batches": cap_evidence["cap5"],
                "cap8_batches": cap_evidence["cap8"],
                "note": "3→5→8：t6 早窗 3 轮、t6 run8 起/t7 RunSpec 5 轮、"
                        "t8 C3 起 8 轮（run_5 三战均 8 轮帽）",
            },
            "birth_call_samples": birth_samples,
            "wall_a": wall_a,
            "wall_b": {
                "seq_range": wall_b["seq_range"],
                "sB_eng": wall_b["sB_eng"], "sB_cap": wall_b["sB_cap"],
                "counts": wall_b["counts"],
                "split_file": "walls/wall_b/SEQ_SPLIT.json",
                "run5_clarification": wall_b["run5_clarification"],
            },
            "cache": {
                "legacy_m2_prior": {
                    "hits": 17, "calls": 120, "hit_rate": round(17 / 120, 4),
                    "scope": "Phase 2 M2 时点实测：17 条 cache 与 17 个 ok "
                             "seq 配对；仅适用旧路径冻结指纹空间",
                    "source": "Phase_2_Plan_Complete.md M2 台账（无独立 hit log）",
                },
                "current_cache_file_count": cache_files,
                "current_cache_note": (
                    "终态 .call_cache 文件数非命中数；历史命中无 hit log，"
                    "不可事后重算，禁止拿文件数冒充命中率"),
                "agentic_path_prior": 0,
                "t9_coding_prior": 0,
                "zero_prior_reason": "agentic-v2 指纹空间（规范化 obs+工具轨迹"
                                     "前缀）与 T9 VLA 新观察是全新指纹，禁止借用 17/120",
            },
        },
        "corrections": [
            {
                "id": "C-2",
                "about": "墙 A provenance 三级重构（2026-10-29 独立评审 REVISE M1/M2）",
                "finding": "旧实现两处缺陷：(M1) G-9 族对非注入全文匹配，"
                           "Stage0/源码/合同注入段中的族词可污染归属（如 seq308 "
                           "被多记 binding_utils/qM）；(M2) 无字节证据即静默 "
                           "continue，并把「同战役窗口」写成命中理由，致假覆盖、"
                           "sole-ref 存活 req 被丢弃、窗内/窗外边界矛盾。",
                "resolution": "两墙同取证纪律：族匹配只在 driving 段（## "
                              "traceback / 日志尾部），缺段 fail-closed；三级 "
                              "byte_confirmed/byte_unclassified/seq_level_"
                              "overwritten + excluded_per_seq；窗口由 driving "
                              "段 env-reach 信号机械派生；冻结规则码逐件归因，"
                              "同 tail 跨线程同 rule。sA=60 口径不变（计费构造）"
                              "＝2 confirmed + 13 unclassified + 45 "
                              "overwritten。",
            },
            {
                "id": "C-1",
                "about": "墙 B run_5 范围（合同/计划措辞 vs 活账）",
                "finding": "合同措辞「墙 B：run_5 seq309–436」与活账不符："
                           "run_5 机判战役为 T8-C9/C10/C11＝seq398–436；"
                           "seq309–397（M1/M2+C1–C8）属 run_1..4 四次真人 "
                           "Gate2 reject 战役。",
                "resolution": "SEQ_SPLIT 精确切 398–436；t8 总锚点 309–436 不变；"
                              "以活账+结案报告+closed 包三重证据为准，不凑数。",
            },
        ],
    }
    return dataset, wall_b


def write_outputs(out_root: Path, dataset: dict, seq_split: dict) -> dict:
    est = ensure_private_dir(out_root / "estimates")
    wall_b_dir = ensure_private_dir(out_root / "walls" / "wall_b")
    ds_path = est / "hindcast_dataset.json"
    split_path = wall_b_dir / "SEQ_SPLIT.json"
    atomic_write_text(ds_path, json.dumps(dataset, ensure_ascii=False, indent=2)
                      + "\n")
    atomic_write_text(split_path,
                      json.dumps(seq_split, ensure_ascii=False, indent=2) + "\n")
    return {"dataset": str(ds_path), "seq_split": str(split_path)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--phase1-root", default=None)
    ap.add_argument("--phase2-root", default=None)
    ap.add_argument("--out-root", default=None)
    args = ap.parse_args(argv)
    p1 = Path(args.phase1_root) if args.phase1_root else rp.phase1_root()
    p2 = Path(args.phase2_root) if args.phase2_root else rp.phase2_root()
    out = Path(args.out_root) if args.out_root else rp.phase25_root()
    dataset, seq_split = build_dataset(p1, p2)
    paths = write_outputs(out, dataset, seq_split)
    wa = dataset["phase2"]["wall_a"]
    print(json.dumps({
        "written": paths,
        "anchors_ok": True,
        "wall_a": {"sA": wa["sA"],
                   "confirmed": wa["byte_confirmed_count"],
                   "unclassified": wa["byte_unclassified_count"],
                   "overwritten": wa["seq_level_overwritten_count"]},
        "wall_b": {"sB_eng": seq_split["sB_eng"],
                   "sB_cap": seq_split["sB_cap"]},
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
