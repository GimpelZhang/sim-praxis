#!/usr/bin/env python3
"""Task 6 Stage B（r4）：窗口工程证据机产器（只读、0 付费、确定性）。

把预注册 ``scripts/phase25/task6_r4_prereg.md`` §8 条件 4/5 所需的**可观测**
事实一次性采集，供 ``task6_r4_verdict.py --evidence`` 机判。凡是本脚本能自己
量的都自己量（四表身份、Q1/Q2/Q3 有序 id 集、活库写入面、ledger 行数、
容器、显存）。四项门禁**全部绑定到实跑工件**（修订 A11，收口 A10-M1 的措辞不实）：

- ``hermetic`` / ``kbint`` / ``gate_static`` ← ``--hermetic-log`` /
  ``--kbint-log`` / ``--gate-static-log`` 指向的**原始输出文件**：脚本自己
  解析（pytest 摘要行 / ``[STATIC GATE OK]`` 标记），记录解析出的 rc/ok 与
  日志 sha256；文件读不到、或日志里找不到摘要行、或摘要行多于一条（拼接
  日志），一律 ``EvidenceError``——"没跑过"不得写成"跑过了"。每份日志还须
  带**唯一的哨兵行** ``# T6R4-GATE: <gate> cmd=<实际命令>``，命令里必须含
  该门的判别串（kbint 须真选 ``-m kb_integration``，hermetic 须是排除
  kbint 的那条）——否则两排除全套的绿日志可以被冒充成 kbint 门绿。
- ``provenance_restore``（PROC-7 溯源终复）仍是**执行方声明**：它是执行方
  对活库做的一次动作，没有可解析的第三方工件；此项作为登记残余留在
  ``declared_by_executor`` 里，由门 2 人工复核兜底
  （预注册 §13.6 末尾已登记，修订 A10-M1 维持；A11 起它是**唯一**残余）。

活库写入面（§8.4 唯一例外的判别）：对比窗口前/后的
``gen_prewindow_snapshots.py`` 快照逐行 ``meta_sha256`` 与剔除
``ingested_at``/``source_commit`` 的 ``meta_noprov_sha256``：
- 行集/行数/文本/非戳记元数据有任何差异 → 记入 ``unexpected``（判负面）；
- 仅戳记差异（kbint 幂等重灌重戳 ``solution_chunks.ingested_at``）→ 记入
  ``gate_restamp``，并单列 ``ingested_at_only`` 行数；
- ``live_writes_except_gate_restamp`` = ``len(unexpected)``（0 才算干净）。

零联网零付费（离线三开关 setdefault）；只读，不建表不写库。输出 json
sort_keys + indent=2 + 单一尾换行，无时间戳（窗口时点由调用方在路径上标）。

用法：
  python scripts/phase25/gen_task6_r4_window_evidence.py \
      --prewindow <pre.json> --postwindow <post.json> \
      --pre-vector <pre_vector_identity.json> \
      --hermetic-log <hermetic.log> --kbint-log <kbint.log> \
      --gate-static-log <gate.log> \
      --provenance-restore [--out PATH]
退出码：0=采集成功（是否满足保留条件是判分器的事）；2=输入/装配失败。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

for _var in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
    os.environ.setdefault(_var, "1")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TABLES = ("kb_chunks", "solution_chunks", "phase1_learnings",
          "phase2_learnings")
SCHEMA = "simpraxis.phase25.task6_r4_window_evidence.v1"
# 窗口前向量身份基线件本身的 sha256（预注册 §5 / §11 登记值）：基线一旦重出
# （哪怕内容"看起来对"）就不许再产出证据（GATE1 规格 lane 第二轮 #2）。
PRE_VECTOR_BASELINE_SHA256 = (
    "ea896f6474ffeaeb68098461aa0448a213d8171e581915f2bff85036f9e07d6e")
# 容器名分隔符归一（`sim_praxis_sandbox` → `simpraxissandbox`），见
# `_simpraxis_container_names` 的放水说明。
_SEP_RE = re.compile(r"[-_.]")


# pytest `-q` 终局摘要行：`1 failed, 1996 passed, 7 skipped ... in 343.58s`。
# 必须同时有结果词与耗时，避免把测试名/告警里含 "passed" 的行误当摘要。
_PYTEST_TAIL_RE = re.compile(
    r"\b(?:passed|failed|error|errors|no tests ran)\b.*"
    r"\bin \d+(?:\.\d+)?s(?: \([^)]*\))?$", re.IGNORECASE)


class EvidenceError(RuntimeError):
    """输入缺失/形状不符（exit 2，不作判据）。"""


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _live_tables(uri: str) -> dict:
    import lancedb
    db = lancedb.connect(uri)
    present = set(db.table_names())
    rows: dict[str, int] = {}
    dims: dict[str, int | None] = {}
    id_sha8: dict[str, str] = {}
    for name in TABLES:
        if name not in present:
            raise EvidenceError(f"活库缺表 {name}")
        tbl = db.open_table(name)
        data = (tbl.search(None).select(["id"]).limit(10 ** 6).to_list())
        ids = sorted(str(r["id"]) for r in data)
        rows[name] = len(ids)
        dims[name] = getattr(tbl.schema.field("vector").type, "list_size", None)
        id_sha8[name] = _sha8("\n".join(ids))
    return {"rows": rows, "dims": dims, "id_sha8": id_sha8}


def _q1q2q3(uri: str, prewindow: dict) -> dict:
    from scripts.kb.gen_prewindow_snapshots import recall_snapshot
    want = prewindow.get("q1q2q3_mpnet_recall")
    if not isinstance(want, dict) or not want:
        raise EvidenceError("窗口前快照缺 q1q2q3_mpnet_recall")
    try:
        got = recall_snapshot(uri)
    except EvidenceError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 统一成输入层错误，见下
        # 召回快照内部抛的任何异常（KeyError/缺表/嵌入维度…）在旧实现里是
        # rc=1 的 traceback，调用方分不清"证据不合格"与"脚本有 bug"
        # （GATE1-LANE-A A10-L4）。这里收敛成 EvidenceError（exit 2）。
        raise EvidenceError(
            f"Q1/Q2/Q3 活库召回失败：{type(exc).__name__}: {exc}") from exc
    if not isinstance(got, dict) or not got:
        raise EvidenceError("Q1/Q2/Q3 活库召回返回空（观测缺口）")
    mismatches = []
    for name, node in want.items():
        expected = list(node.get("ids") or [])
        # 空集恒等不是证据：窗口前快照若把召回存成 []，`[] == []` 会让这条
        # 身份断言永远成立（GATE1-LANE-A L8）。三个查询在活库上必有命中。
        if not expected:
            raise EvidenceError(
                f"窗口前快照 {name} 的召回 id 集为空（无法作身份断言）")
        cur = got.get(name) or {}
        if expected != list(cur.get("ids") or []):
            mismatches.append({"query": name,
                               "expected": node.get("ids"),
                               "got": cur.get("ids")})
    extra = sorted(set(got) - set(want))
    return {"ok": not mismatches and not extra, "n_queries": len(want),
            "mismatches": mismatches, "unexpected_queries": extra}


def _is_authorized_gate_restamp(table: str, x: dict, y: dict) -> bool:
    """§7.3 唯一例外：**solution_chunks** 逐行**仅** `ingested_at` 变化。

    判据必须分列（快照 per_row 的 `ingested_at_sha256` / `source_commit_sha256`）：
    `meta_sha256` 变而 `meta_noprov_sha256` 不变，只说明"变的是某个戳记列"，
    区分不出是 ingested_at 还是 source_commit。任一列的分列哈希缺失、或
    source_commit 也变了、或表不是 solution_chunks → 都不是例外（按写入算）。
    """
    if table != "solution_chunks":
        return False
    keys = ("ingested_at_sha256", "source_commit_sha256")
    if any(k not in x or k not in y for k in keys):
        return False
    return (x["ingested_at_sha256"] != y["ingested_at_sha256"]
            and x["source_commit_sha256"] == y["source_commit_sha256"])


def _per_row_index(per_row, table: str, side: str) -> dict:
    """per_row → {str(id): row}；行不是 dict 或缺 id 即 ``EvidenceError``。

    旧实现直取 ``r["id"]``：缺键抛 ``KeyError``（rc=1 的 traceback，而不是
    ``EvidenceError``/exit 2），调用方分不清"证据不合格"与"脚本有 bug"
    （GATE1-LANE-A A10-L4）。这里统一成输入层错误。
    """
    idx: dict = {}
    for i, row in enumerate(per_row or []):
        if not isinstance(row, dict) or row.get("id") is None:
            raise EvidenceError(
                f"快照 {side} 侧 {table} per_row[{i}] 不是带 id 的对象"
                "（观测缺口不得记作零写入）")
        idx[str(row["id"])] = row
    return idx


def _write_surface(pre: dict, post: dict) -> dict:
    """窗口前/后逐行归约哈希比对（见模块头）。

    两侧任一侧缺表（快照里 ``missing=True`` 或没有 ``rows``/``id_set_sha256``/
    ``per_row`` 三键）即 ``EvidenceError``——**不能**因为"两边都缺"就判干净：
    那不是"没写入"，是"没观测到"，把观测缺口记成零写入正是放水
    （GATE1-LANE-A M6）。四表在活库里都存在，快照缺表本身即异常。
    """
    pre_t, post_t = pre.get("tables") or {}, post.get("tables") or {}
    unexpected: list[dict] = []
    gate_restamp: list[dict] = []
    unauthorized: list[dict] = []
    for name in TABLES:
        a, b = pre_t.get(name) or {}, post_t.get(name) or {}
        for side, node in (("pre", a), ("post", b)):
            # 「缺表」不只是键不在：生成器缺表时写 {rows: None, id_set_sha256:
            # None, missing: True}，而残缺节点也可能带着 None 值。键在而值为
            # None 的节点会让 `None == None` / `set() == set()` 恒成立，等于把
            # 观测缺口判成零写入（GATE1-LANE-A M6/D10）。这里按**类型与内部
            # 一致性**判，只认完整观测。
            if node.get("missing") is True:
                raise EvidenceError(
                    f"快照 {side} 侧缺表 {name}（观测缺口不得记作零写入）")
            rows, ids_sha, per_row = (node.get("rows"),
                                      node.get("id_set_sha256"),
                                      node.get("per_row"))
            if not isinstance(rows, int) or rows < 0 \
                    or not isinstance(ids_sha, str) or not ids_sha \
                    or not isinstance(per_row, list):
                raise EvidenceError(
                    f"快照 {side} 侧节点残缺 {name}（rows/id_set_sha256/"
                    f"per_row 缺失或为 None；观测缺口不得记作零写入）")
        if a.get("rows") != b.get("rows") or \
                a.get("id_set_sha256") != b.get("id_set_sha256"):
            unexpected.append({"table": name, "reason": "行数或 id 集变化",
                               "pre_rows": a.get("rows"),
                               "post_rows": b.get("rows")})
            continue
        pa = _per_row_index(a.get("per_row"), name, "pre")
        pb = _per_row_index(b.get("per_row"), name, "post")
        if set(pa) != set(pb):
            unexpected.append({"table": name, "reason": "per_row id 集不齐"})
            continue
        for rid in sorted(pa):
            x, y = pa[rid], pb[rid]
            if x.get("meta_noprov_sha256") != y.get("meta_noprov_sha256") or \
                    x.get("text_sha256") != y.get("text_sha256"):
                unexpected.append({"table": name, "id": rid,
                                   "reason": "非戳记元数据或文本变化"})
                continue
            if x.get("meta_sha256") == y.get("meta_sha256"):
                continue
            # 到这里：文本与非戳记元数据全等，仅戳记列有变。§7.3 只授权
            # **solution_chunks 的 ingested_at 重戳**这一种；其余一切（别的表、
            # 或者 source_commit 也变了）都是写入。没有分列哈希就无法区分，
            # 此时一律按写入处理（fail-closed，不猜）。
            if _is_authorized_gate_restamp(name, x, y):
                gate_restamp.append({"table": name, "id": rid})
            else:
                unauthorized.append({"table": name, "id": rid,
                                     "reason": "非授权戳记变化（仅 "
                                               "solution_chunks.ingested_at "
                                               "可为例外）"})
    unexpected.extend(unauthorized)
    return {"unexpected": unexpected,
            "gate_restamp_rows": len(gate_restamp),
            "gate_restamp_ingested_at_only_rows": len(gate_restamp),
            "gate_restamp_other_rows": len(unauthorized)}


def _ledger_rows() -> int:
    """phase25 ledger 行数 + **seq 连续性**（GATE1-LANE-A A10-L3）。

    只数非空行不够：追加一行再删一行仍是 56，"零付费"这条证据可空真。
    seq 是记账序（1..N 连续），删改任何一行都会破坏连续性或末位，故按
    「长度 == 末 seq 且 seq 恰为 1..N」判——**双口径同时成立**才认。
    """
    from orchestrator.common.runtime_paths import phase25_ledger_path
    path = phase25_ledger_path()
    if not path.is_file():
        raise EvidenceError(f"phase25 ledger 不存在：{path}")
    seqs: list[int] = []
    for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise EvidenceError(f"ledger 第 {lineno} 行不是 JSON：{exc}") from exc
        seq = row.get("seq") if isinstance(row, dict) else None
        if not isinstance(seq, int):
            raise EvidenceError(f"ledger 第 {lineno} 行缺 seq 整数（无法判连续）")
        seqs.append(seq)
    if not seqs:
        raise EvidenceError("phase25 ledger 为空（零实呼也得有历史行）")
    if seqs != list(range(1, len(seqs) + 1)):
        raise EvidenceError(
            f"ledger seq 非 1..{len(seqs)} 连续：{seqs[:5]}…{seqs[-3:]}"
            "（存在删改行，行数不可作零付费证据）")
    return len(seqs)


def _simpraxis_container_names(raw: str) -> "list[str]":
    """从 `docker ps --format {{.Names}}` 输出里挑 simpraxis 自有容器。

    必须先把分隔符归一：compose 服务名是 ``sim-sandbox``，而
    ``sandbox/docker-compose.yml`` 明写 ``container_name: sim_praxis_sandbox``
    （下划线）。旧实现直接 ``"simpraxis" in name.lower()``，对
    ``sim_praxis_sandbox`` 恒为 False——即便容器正在跑，证据字段也恒为 0，
    即"断言放水"（硬规则 7；GATE1-LANE-A C2）。归一后
    ``sim_praxis_sandbox``/``simpraxis-sandbox``/``SIM.PRAXIS.sandbox``
    都算自有机，``alpasim_agent`` 之类不误伤（不含 "simpraxis" 字母序）。
    """
    hits: "list[str]" = []
    for line in raw.splitlines():
        name = line.strip()
        if not name:
            continue
        if "simpraxis" in _SEP_RE.sub("", name).lower():
            hits.append(name)
    return hits


def _containers_present() -> int:
    """sim-praxis 自有容器**存量**计数（含已退出/created，`docker ps -a`）。

    必须是 ``-a``：硬规则 5 的收尾口径是「含已退出容器不得残留」，裸
    ``docker ps`` 只看运行中，退出后停在 ``Exited (0)`` 的容器会被漏检，
    于是「零容器」这条证据又可空真（GATE1-LANE-A C2 附加缺陷）。改名
    ``present`` 是为了让证据字段名与口径一致——它数的不是「在跑」。
    """
    try:
        out = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise EvidenceError(f"docker ps -a 失败：{exc}") from exc
    return len(_simpraxis_container_names(out))


def _vram_mib() -> int:
    """全卡已用显存合计；**空输出即失败**，不得把"没读到"当 0 MiB。

    原实现 ``sum([]) == 0``：nvidia-smi 返回空 stdout（驱动异常、被
    mock、命令打错）时会安静地给出 0 MiB，而 0 ≤ 200 直接通过——这正是
    「断言放水」（GATE1-LANE-A L10）。至少要读到一行合法数值才算数。
    """
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used",
             "--format=csv,noheader,nounits"],
            check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise EvidenceError(f"nvidia-smi 读取失败：{exc}") from exc
    lines = [x.strip() for x in out.splitlines() if x.strip()]
    if not lines:
        raise EvidenceError("nvidia-smi 输出为空（读不到显存，不得记 0）")
    try:
        values = [int(x) for x in lines]
    except ValueError as exc:
        raise EvidenceError(f"nvidia-smi 输出非数值：{exc}") from exc
    # 负值不是合法读数（异常驱动/被包装的命令可能给 -1 之类），合计后
    # 轻易落在 ≤200 里 → 判据恒过。宁可按未测处理（GATE1-LANE-A L10）。
    if any(v < 0 for v in values):
        raise EvidenceError(f"nvidia-smi 输出含负值：{values}")
    return sum(values)


def _read_log(path: "str | None", what: str) -> tuple:
    """读实跑日志 → ``(text, sha256)``；缺路径/读不到即 ``EvidenceError``。"""
    if not path:
        raise EvidenceError(f"缺 {what} 日志路径（门禁必须绑定实跑工件）")
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise EvidenceError(f"读不了 {what} 日志 {path}：{exc}") from exc
    return text, _sha256_text(text)


# 门禁日志**哨兵行**：窗口 runner 在落日志时先写一行
#   ``# T6R4-GATE: <gate> cmd=<真正执行的命令>``
# 解析侧要求它恰好出现一次、名字与所传参数一致、命令里含该门的判别串——
# 否则两排除全套的绿日志可以被当成 kbint 门用（`-m kb_integration` 与
# `-m "not paid and not kb_integration"` 的摘要行在形状上无法区分，
# GATE1-LANE-A 第二轮 B2）。
_GATE_MARK = "# T6R4-GATE:"
_GATE_RULES = {
    # 两排除全套：须是 §8.5 逐字那两条排除（少一条就不是「两排除」）
    "hermetic": {"must": ("not test_04", "not paid", "not kb_integration"),
                 "must_not": ()},
    # kb_integration 门：命令里必须真的选了 kb_integration 标记
    "kbint": {"must": ("-m kb_integration",),
              "must_not": ("not kb_integration",)},
    # 六步静态门：标记即命令自证
    "gate_static": {"must": (), "must_not": ()},
}


def _read_gate_log(path: "str | None", what: str) -> tuple:
    """读门禁日志并校验哨兵行 → ``(text, sha256)``（修订 A11）。"""
    text, sha = _read_log(path, what)
    marks = [ln.strip() for ln in text.splitlines()
             if ln.strip().startswith(_GATE_MARK)]
    if len(marks) != 1:
        raise EvidenceError(
            f"{what} 日志应有且仅有一条哨兵行"
            f"（`{_GATE_MARK} {what} cmd=…`），实得 {len(marks)} 条")
    body = marks[0][len(_GATE_MARK):].strip()
    head, _, cmd = body.partition(" ")
    if head.strip() != what:
        raise EvidenceError(
            f"{what} 日志的哨兵行标的是 {head.strip()!r}，不是 {what}")
    rule = _GATE_RULES[what]
    for needle in rule["must"]:
        if needle not in cmd:
            raise EvidenceError(
                f"{what} 哨兵行的命令缺 {needle!r}：{cmd!r}")
    for needle in rule["must_not"]:
        if needle in cmd:
            raise EvidenceError(
                f"{what} 哨兵行的命令不得含 {needle!r}：{cmd!r}")
    return text, sha


def _pytest_summary_rc(text: str, what: str) -> dict:
    """从 pytest ``-q`` 日志里解析**摘要行**并推出 rc（GATE1-LANE-A A10-M1）。

    只认含 ``passed``/``failed``/``error`` 且以耗时结尾的行：那是 pytest 的
    终局摘要。找不到摘要 → ``EvidenceError``（"没跑过"不得写成"跑过且绿"）。
    摘要行**多于一条**同样报错：拼接日志里旧跑的 "N passed" 会把新跑的失败
    盖成绿（GATE1-LANE-A 第二轮 LOW-2），单次门禁只能有一份终局摘要。
    摘要里出现 ``failed``/``error`` → rc=1。
    """
    summaries = [line.strip() for line in text.splitlines()
                 if _PYTEST_TAIL_RE.search(line.strip())]
    if not summaries:
        raise EvidenceError(
            f"{what} 日志里找不到 pytest 摘要行（未跑/日志被截断，视为未观测）")
    if len(summaries) > 1:
        raise EvidenceError(
            f"{what} 日志里有 {len(summaries)} 条 pytest 摘要行"
            f"（疑似拼接多次运行）：{summaries}")
    summary = summaries[0]
    low = summary.lower()
    bad = any(w in low for w in ("failed", "error", "no tests ran"))
    rc = 1 if bad else 0
    return {"rc": rc, "ok": rc == 0, "summary": summary}


def _load_snapshot(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvidenceError(f"读不了快照 {path}：{exc}") from exc


def _vector_identity(pre_vector: "str | None", uri: str) -> dict:
    """窗口前向量基线 vs 取证时实算（GATE1-LANE-A C-1）。

    快照的逐行哈希只覆盖 text/元数据/戳记，**不覆盖 vector 列**——同维重嵌入
    在写入面比对里看起来是干净的。这里用 ``gen_task6_r4_vector_identity.py``
    在窗口前产出的基线做集合级比对，缺基线/缺表/口径不符一律
    ``EvidenceError``（没观测到 ≠ 没变化）。

    基线件本身还要**按 sha256 钉死**：只比"实算 == 传入的基线"证明不了基线
    没被动过——若基线是在嵌入改动之后重出的，比对照样通过（GATE1 规格 lane
    第二轮 #2）。绝对锚点由本常量与 verdict 的 §5 四表哈希两侧共同看住。
    """
    if not pre_vector:
        raise EvidenceError(
            "缺 --pre-vector：窗口前向量基线未提供，无法断言嵌入身份不变")
    try:
        base_sha = hashlib.sha256(Path(pre_vector).read_bytes()).hexdigest()
    except OSError as exc:
        raise EvidenceError(f"读不了向量基线 {pre_vector}：{exc}") from exc
    if base_sha != PRE_VECTOR_BASELINE_SHA256:
        raise EvidenceError(
            f"向量基线件 sha256 与冻结值不符：{base_sha}"
            f" != {PRE_VECTOR_BASELINE_SHA256}（基线被动过，或不是窗前那份）")
    base = _load_snapshot(pre_vector)
    from scripts.phase25.gen_task6_r4_vector_identity import (
        SCHEMA as VEC_SCHEMA, TABLES as VEC_TABLES, IdentityError as VecError,
        build as vec_build)
    if base.get("schema") != VEC_SCHEMA:
        raise EvidenceError(
            f"向量基线 schema 不符 {VEC_SCHEMA}：{base.get('schema')!r}")
    pre_tables = base.get("tables") or {}
    for name in VEC_TABLES:
        node = pre_tables.get(name) or {}
        if not isinstance(node.get("vector_set_sha256"), str) or \
                not node["vector_set_sha256"]:
            raise EvidenceError(f"向量基线缺 {name} 的集合哈希（观测缺口）")
    try:
        live = vec_build(uri)
    except VecError as exc:
        # 缺表/缺 id/缺 vector/混维都是**输入观测失败**，须与"脚本有 bug"
        # 区分开：统一成 EvidenceError → exit 2（GATE1-LANE-A 第二轮 LOW-3）。
        raise EvidenceError(f"活库向量身份取不出来：{exc}") from exc
    mismatches = []
    for name in VEC_TABLES:
        want = pre_tables[name]
        got = live["tables"][name]
        if (got["rows"] != want.get("rows")
                or got["dim"] != want.get("dim")
                or got["vector_set_sha256"] != want["vector_set_sha256"]):
            mismatches.append({"table": name,
                               "pre": {"rows": want.get("rows"),
                                       "dim": want.get("dim"),
                                       "vector_set_sha256":
                                           want["vector_set_sha256"]},
                               "post": got})
    return {"observed": True, "unchanged": not mismatches,
            "mismatches": mismatches,
            "tables": {n: live["tables"][n]["vector_set_sha256"]
                       for n in VEC_TABLES}}


def build(args) -> dict:
    pre = _load_snapshot(args.prewindow)
    post = _load_snapshot(args.postwindow)
    if pre.get("schema") != "simpraxis.phase25.embedswap_prewindow.v1" or \
            post.get("schema") != pre.get("schema"):
        raise EvidenceError("窗口前/后快照 schema 不符 embedswap_prewindow.v1")
    from memory_flywheel.kb_pins import default_lancedb_uri
    uri = str(default_lancedb_uri())
    live = _live_tables(uri)
    surface = _write_surface(pre, post)
    hermetic_text, hermetic_sha = _read_gate_log(args.hermetic_log, "hermetic")
    hermetic = {**_pytest_summary_rc(hermetic_text, "hermetic"),
                "log_sha256": hermetic_sha}
    kbint_text, kbint_sha = _read_gate_log(args.kbint_log, "kbint")
    kbint = {**_pytest_summary_rc(kbint_text, "kbint"),
             "log_sha256": kbint_sha}
    gate_text, gate_sha = _read_gate_log(args.gate_static_log, "gate_static")
    gate_static = {"ok": "[STATIC GATE OK]" in gate_text,
                   "marker": "[STATIC GATE OK]" in gate_text,
                   "log_sha256": gate_sha}
    return {
        "schema": SCHEMA,
        "uri": uri,
        "four_tables": live,
        "q1q2q3": _q1q2q3(uri, pre),
        "write_surface": surface,
        "vector_identity": _vector_identity(
            getattr(args, "pre_vector", None), uri),
        "live_writes_except_gate_restamp": len(surface["unexpected"]),
        "ledger_rows": _ledger_rows(),
        "containers_present": _containers_present(),
        "vram_mib": _vram_mib(),
        # 门禁三项全部绑定实跑工件（A10-M1 → A11）：三项都从原始日志解析 rc/
        # 标记并记 log_sha256，不接受任何调用方传入的 rc 或独立 ok 布尔。
        "hermetic": hermetic,
        "kbint": kbint,
        "gate_static": gate_static,
        "provenance_restore": bool(args.provenance_restore),
        "declared_by_executor": ["provenance_restore"],
    }


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prewindow", required=True)
    ap.add_argument("--postwindow", required=True)
    ap.add_argument("--pre-vector", default=None,
                    help="窗口前四表向量身份基线（gen_task6_r4_vector_identity.py）")
    ap.add_argument("--hermetic-log", required=True,
                    help="两排除 hermetic 全套的原始 pytest 输出（本脚本解析"
                         "摘要行得 rc，并记入 log_sha256）")
    ap.add_argument("--kbint-log", required=True,
                    help="kb_integration 门的原始 pytest 输出（本脚本解析摘要行）")
    ap.add_argument("--gate-static-log", required=True,
                    help="gate_static.sh 的原始输出（须含 [STATIC GATE OK]）")
    ap.add_argument("--provenance-restore", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    try:
        out = build(args)
    except EvidenceError as exc:
        print(f"[T6R4-EVIDENCE][STOP] {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    text = json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    print(f"live_writes_except_gate_restamp="
          f"{out['live_writes_except_gate_restamp']} "
          f"ledger_rows={out['ledger_rows']} "
          f"containers={out['containers_present']} "
          f"vram={out['vram_mib']}MiB "
          f"q1q2q3_ok={out['q1q2q3']['ok']} "
          f"kbint_rc={out['kbint']['rc']} "
          f"gate_static_ok={out['gate_static']['ok']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
