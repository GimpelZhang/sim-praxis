#!/usr/bin/env python3
"""Task 6 Stage B（r4）：窗口工程证据机产器（只读、0 付费、确定性）。

把预注册 ``scripts/phase25/task6_r4_prereg.md`` §8 条件 4/5 所需的**可观测**
事实一次性采集，供 ``task6_r4_verdict.py --evidence`` 机判。凡是本脚本能自己
量的都自己量（四表身份、Q1/Q2/Q3 有序 id 集、活库写入面、ledger 行数、
容器、显存）；只有**执行方才能观测**的三项（两排除 hermetic rc、kbint 门、
gate_static 门、PROC-7 溯源终复）由命令行显式声明——这正是「按实跑结论
写入」的口径，脚本不猜、不默认为真（缺参数即报错）。

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
      --hermetic-rc 0 --hermetic-ok --kbint-ok --gate-static-ok \
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
# 容器名分隔符归一（`sim_praxis_sandbox` → `simpraxissandbox`），见
# `_simpraxis_container_names` 的放水说明。
_SEP_RE = re.compile(r"[-_.]")


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
    got = recall_snapshot(uri)
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
    need = ("rows", "id_set_sha256", "per_row")
    for name in TABLES:
        a, b = pre_t.get(name) or {}, post_t.get(name) or {}
        for side, node in (("pre", a), ("post", b)):
            if node.get("missing") is True or \
                    any(k not in node for k in need):
                raise EvidenceError(
                    f"快照 {side} 侧缺表或残缺 {name}（观测缺口不得记作零写入）")
        if a.get("rows") != b.get("rows") or \
                a.get("id_set_sha256") != b.get("id_set_sha256"):
            unexpected.append({"table": name, "reason": "行数或 id 集变化",
                               "pre_rows": a.get("rows"),
                               "post_rows": b.get("rows")})
            continue
        pa = {r["id"]: r for r in a.get("per_row") or []}
        pb = {r["id"]: r for r in b.get("per_row") or []}
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
    from orchestrator.common.runtime_paths import phase25_ledger_path
    path = phase25_ledger_path()
    if not path.is_file():
        raise EvidenceError(f"phase25 ledger 不存在：{path}")
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines()
               if line.strip())


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
        return sum(int(x) for x in lines)
    except ValueError as exc:
        raise EvidenceError(f"nvidia-smi 输出非数值：{exc}") from exc


def _load_snapshot(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvidenceError(f"读不了快照 {path}：{exc}") from exc


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
    return {
        "schema": SCHEMA,
        "uri": uri,
        "four_tables": live,
        "q1q2q3": _q1q2q3(uri, pre),
        "write_surface": surface,
        "live_writes_except_gate_restamp": len(surface["unexpected"]),
        "ledger_rows": _ledger_rows(),
        "containers_present": _containers_present(),
        "vram_mib": _vram_mib(),
        "hermetic": {"rc": args.hermetic_rc, "ok": bool(args.hermetic_ok)},
        "kbint": {"ok": bool(args.kbint_ok)},
        "gate_static": {"ok": bool(args.gate_static_ok)},
        "provenance_restore": bool(args.provenance_restore),
        "declared_by_executor": [
            "hermetic", "kbint", "gate_static", "provenance_restore"],
    }


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prewindow", required=True)
    ap.add_argument("--postwindow", required=True)
    ap.add_argument("--hermetic-rc", type=int, required=True)
    ap.add_argument("--hermetic-ok", action="store_true")
    ap.add_argument("--kbint-ok", action="store_true")
    ap.add_argument("--gate-static-ok", action="store_true")
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
          f"q1q2q3_ok={out['q1q2q3']['ok']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
