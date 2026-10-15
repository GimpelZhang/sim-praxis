"""Phase 1 学习回写：独立 LanceDB 表 ``phase1_learnings``（计划 D7 / Task6 L527-538）。

隔离铁律（任何一条都不许放宽）：
- 同库不同表：``lancedb.connect`` 同一 ``SIMPRAXIS_LANCEDB_URI``，只打开/创建
  ``phase1_learnings``，本模块任何路径都不引用 ``kb_chunks``、不调用
  ``VectorEngine`` 的写方法、不改 DOC_TYPES/NAMESPACES。
- 同一套嵌入：``select_embedding()`` 读 ``embedding_config.json`` 钉死的 mpnet
  768（ONNX 已缓存 /mnt/caches/fastembed），懒加载同一个 ``TextEmbedding``
  封装，不复制模型文件、不触发下载、不 rebuild。
- 无证据不写：每条学习必须带可解析的 evidence_relpath，写入时该文件必须真实
  存在（越界/不存在直接拒写）。两个证据根（锁内适配，Task7 计划 §1.8 两路
  区分）：宿主侧证据相对 $SIMPRAXIS_PHASE1_ROOT 用裸 relpath（Task2-6 既有
  行格式不变）；真实 rollout 产物在 $SIMPRAXIS_RUN_DIR 下，用带标记前缀
  "@runs/" 的 relpath；标记集中在 RUNS_ROOT_TAG 一处，禁止裸 ../ 跨根。
- 过程级信号（RSI survey §5.5）：记录「失败现象/修复动作/人工意见/生效与否」
  的失败-修复过程结构，不是单轮结果数字。

独立 schema（计划 L205，恰好 12 列，不沿用 kb_chunks 的 14 列）：
  id, text, vector, namespace, layer, run_id, track, rung, sim_engine,
  gate_source, evidence_relpath, created_at

本模块**刻意不提供** ``__main__`` 写动作与删表脚本：回写只能经图节点
（orchestrator.graph.learning.make_learning_seam）发生；新表删除需单独
书面授权（删除守卫，计划 L538）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import pyarrow as pa

import lancedb

from memory_flywheel.embedding_selector import select_embedding

TABLE_NAME = "phase1_learnings"
NAMESPACE = "phase1_learnings"          # 固定值（不进 vector_engine.NAMESPACES）
LAYER = "phase1_run_note"               # 固定值（不进 kb_chunks 的 layer 枚举）

TRACKS = ("track1", "track2")
MAX_RUNG = 4

# gate_source 白名单：Gate2 验收通过 / human_failure 结案（阶段来自图节点分类）。
GATE_ACCEPTED = "gate2_accepted"
GATE_FAILURE_AGENT = "human_failure_agent_run"
GATE_FAILURE_GATE1 = "human_failure_gate1"
GATE_FAILURE_GATE2 = "human_failure_gate2"
GATE_SOURCES = frozenset({
    GATE_ACCEPTED, GATE_FAILURE_AGENT, GATE_FAILURE_GATE1, GATE_FAILURE_GATE2,
})
_FAILURE_RE = re.compile(r"^human_failure_(agent_run|gate1|gate2)$")


# evidence_relpath 根标记：裸 relpath = phase1 证据根（Task2-6 既有行）；
# 该前缀 = run 产物根 $SIMPRAXIS_RUN_DIR（Task7 真实 rollout，计划 §1.8 两路）。
RUNS_ROOT_TAG = "@runs/"


def phase1_root() -> Path:
    return Path(os.environ.get("SIMPRAXIS_PHASE1_ROOT",
                               "/mnt/artifacts/simpraxis/phase1"))


def runs_root() -> Path:
    return Path(os.environ.get("SIMPRAXIS_RUN_DIR",
                               "/mnt/artifacts/simpraxis/runs"))


def compose_text(*, gate_source: str, run_id: str, sim_engine: str, rung: int,
                 failure_symptom: str, fix_action: str, human_opinion: str,
                 effective: "bool | None") -> str:
    """把结构化摘要拼成单条检索正文（向量只嵌这一段；字段另存列）。"""
    eff = {"True": "是（Gate2 验收通过）", "False": "否（结案转人工）"}.get(
        str(effective), "未知")
    lines = [
        f"[phase1_learnings] gate_source={gate_source} run_id={run_id} "
        f"sim_engine={sim_engine or '未知'} rung={rung}",
        f"失败现象：{failure_symptom.strip() or '（无：本轮一次通过）'}",
        f"修复动作：{fix_action.strip() or '（无）'}",
        f"人工意见：{human_opinion.strip() or '（无）'}",
        f"生效与否：{eff}",
    ]
    return "\n".join(lines)


class LearningStore:
    """phase1_learnings 表的唯一合法读写者（同 uri 独立连接，768 维 mpnet）。"""

    def __init__(self, uri: "str | None" = None,
                 evidence_root: "str | Path | None" = None,
                 runs_evidence_root: "str | Path | None" = None):
        self.uri = uri or os.environ.get(
            "SIMPRAXIS_LANCEDB_URI", "/mnt/artifacts/simpraxis/lancedb")
        Path(self.uri).mkdir(parents=True, exist_ok=True)
        self.config = select_embedding()
        self.dim = int(self.config["dim"])
        self.evidence_root = Path(evidence_root) if evidence_root \
            else phase1_root()
        # Task7（§1.8 双证据根）：真实 rollout 产物根；单测可注入临时根。
        self.runs_evidence_root = Path(runs_evidence_root) if runs_evidence_root \
            else runs_root()
        self.db = lancedb.connect(self.uri)
        self._assert_own_vector_dim()
        self._embedder = None

    # ---------- 维度守卫：只查 phase1_learnings 自身（不碰 kb_chunks） ----------
    def _assert_own_vector_dim(self) -> None:
        if TABLE_NAME not in self.db.table_names():
            return
        vtype = self.db.open_table(TABLE_NAME).schema.field("vector").type
        existing_dim = getattr(vtype, "list_size", None)
        if existing_dim is not None and int(existing_dim) != self.dim:
            raise RuntimeError(
                f"已有 {TABLE_NAME} 向量维度 {existing_dim} 与当前嵌入配置 "
                f"{self.dim} 不一致（疑似换过嵌入模型）。新表为 Phase1 独立表，"
                f"禁止混维度写入；删除该表需单独书面授权（删除守卫）")

    # ---------- embedding：与 VectorEngine._model 完全同款封装 ----------
    def _model(self):
        if self._embedder is None:
            from fastembed import TextEmbedding
            self._embedder = TextEmbedding(
                model_name=self.config["model_name"],
                cache_dir=self.config["cache_dir"])
        return self._embedder

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._model().embed(texts)]

    # ---------- schema / table ----------
    def _schema(self) -> pa.Schema:
        return pa.schema([
            pa.field("id", pa.string()),
            pa.field("text", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), self.dim)),
            pa.field("namespace", pa.string()),
            pa.field("layer", pa.string()),
            pa.field("run_id", pa.string()),
            pa.field("track", pa.string()),
            pa.field("rung", pa.int32()),
            pa.field("sim_engine", pa.string()),
            pa.field("gate_source", pa.string()),
            pa.field("evidence_relpath", pa.string()),
            pa.field("created_at", pa.string()),
        ])

    def table_exists(self) -> bool:
        return TABLE_NAME in self.db.table_names()

    def table(self, create: bool = False):
        if not self.table_exists():
            if not create:
                raise RuntimeError(
                    f"表 {TABLE_NAME} 不存在；学习回写由图节点在首次结案时创建")
            self.db.create_table(TABLE_NAME, schema=self._schema(),
                                 mode="create")
        return self.db.open_table(TABLE_NAME)

    # ---------- 证据校验：无证据不写（phase1 / runs 双根，Task7 §1.8） ----------
    def _evidence_base(self, rel: str) -> "tuple[Path, str, bool]":
        """按根标记选证据根，返回 (root, 去标记 payload, 是否带 runs 标记)。"""
        if rel.startswith(RUNS_ROOT_TAG):
            return self.runs_evidence_root.resolve(), rel[len(RUNS_ROOT_TAG):], True
        return self.evidence_root.resolve(), rel, False

    def relativize_evidence(self, abs_path: "str | Path") -> str:
        """绝对证据路径 → 落库 relpath（先 phase1 根，再 runs 根）；文件必须存在。

        phase1 根下返回裸 relpath（Task2-6 既有格式不变）；runs 根下返回
        "@runs/<rel>"。两根都不命中或文件不存在即拒（无证据不写）。
        """
        target = Path(abs_path).resolve()
        if not target.is_file():
            raise FileNotFoundError(f"证据文件不存在，拒写学习：{target}（无证据不写）")
        phase = self.evidence_root.resolve()
        try:
            return target.relative_to(phase).as_posix()
        except ValueError:
            pass
        runs = self.runs_evidence_root.resolve()
        try:
            return RUNS_ROOT_TAG + target.relative_to(runs).as_posix()
        except ValueError as exc:
            raise ValueError(
                f"证据 {target} 既不在 phase1 根 {phase} 也不在 runs 根 "
                f"{runs} 之下（无证据不写）") from exc

    def _resolve_evidence(self, evidence_relpath: str) -> str:
        if not isinstance(evidence_relpath, str) or not evidence_relpath.strip():
            raise ValueError("evidence_relpath 必填：无证据不写学习条目")
        rel = evidence_relpath.strip().replace("\\", "/")
        if rel.startswith("/"):
            raise ValueError(
                "evidence_relpath 必须是相对证据根的路径（runs 根用 "
                f"{RUNS_ROOT_TAG!r} 前缀标记），收到绝对路径：{evidence_relpath}")
        root, payload, tagged = self._evidence_base(rel)
        if not payload or payload.startswith("/"):
            raise ValueError(
                f"evidence_relpath 标记 {RUNS_ROOT_TAG!r} 后必须接根内相对路径："
                f"{evidence_relpath}")
        target = (root / payload).resolve()
        # 越界守卫：禁 ../ 逃出对应证据根（tagged 路径同样按 runs 根校验）
        if not str(target).startswith(str(root) + os.sep) and target != root:
            kind = "$SIMPRAXIS_RUN_DIR" if tagged else "$SIMPRAXIS_PHASE1_ROOT"
            raise ValueError(
                f"evidence_relpath 越界（{evidence_relpath} → {target} 不在 "
                f"{kind} {root} 之下）")
        if not target.is_file():
            raise FileNotFoundError(
                f"证据文件不存在，拒写学习：{target}（无证据不写）")
        return target.relative_to(root).as_posix() if not tagged else \
            RUNS_ROOT_TAG + target.relative_to(root).as_posix()

    @staticmethod
    def _id_for(run_id: str, gate_source: str, rung: int, text: str,
                evidence_relpath: str) -> str:
        # 确定性主键：kill -9 resume 重放同一结案幂等（先删后写，不产生重复行）。
        # created_at 不入 hash——同一次结案的重放视为同一学习。
        h = hashlib.sha256("|".join(
            [run_id, gate_source, str(rung), text, evidence_relpath]
        ).encode("utf-8")).hexdigest()
        return h[:32]

    def add_learning(self, *, run_id: str, track: str, rung: int,
                     sim_engine: str, gate_source: str,
                     evidence_relpath: str, failure_symptom: str = "",
                     fix_action: str = "", human_opinion: str = "",
                     effective: "bool | None" = None) -> dict[str, Any]:
        """写入一条过程级学习。校验失败直接抛异常，绝不静默丢弃。"""
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id 必填（图 thread_id）")
        if track not in TRACKS:
            raise ValueError(f"track 必须是 {TRACKS}，收到 {track!r}")
        if isinstance(rung, bool) or not isinstance(rung, int) \
                or not (0 <= rung <= MAX_RUNG):
            raise ValueError(f"rung 必须是 0..{MAX_RUNG} 的整数（0=失败结案），"
                             f"收到 {rung!r}")
        if gate_source not in GATE_SOURCES:
            raise ValueError(f"gate_source 非法：{gate_source!r}；白名单 "
                             f"{sorted(GATE_SOURCES)}")
        is_failure = bool(_FAILURE_RE.match(gate_source))
        if is_failure and rung != 0:
            raise ValueError(f"{gate_source} 的 rung 必须为 0（失败也是学习），"
                             f"收到 {rung}")
        if not is_failure and rung == 0:
            raise ValueError("gate2_accepted 的 rung 必须 >=1")
        if not isinstance(sim_engine, str):
            raise ValueError("sim_engine 必须是字符串（Gate1 前失败可传 ''）")
        # 锁内适配（Task7 首条真实 Gate2 一次通过 run 取证，2026-10-15
        # t7-mujoco-20261015b）：计划 L534 要求「Gate2 验收通过…写一条」，
        # 而无打回/无补丁/无意见的干净 run 三项摘要必然全空——rung/engine/
        # effective/证据原件本身即该条学习的内容（compose_text 显式渲染「无」）。
        # 故空洞守卫只保留给失败结案：human_failure_* 却没有任何现象/动作/意见
        # 才是无内容行；accepted 的干净 run 允许写入。
        if is_failure and not any(str(x).strip() for x in
                                  (failure_symptom, fix_action, human_opinion)):
            raise ValueError(
                "失败结案结构化摘要三项（失败现象/修复动作/人工意见）全空："
                "拒绝写入无内容学习（gate2_accepted 干净 run 不在此限）")
        if effective is not None and not isinstance(effective, bool):
            raise ValueError("effective 必须是 bool 或 None")
        if is_failure and effective is True:
            raise ValueError("失败结案 effective 不能为 True")

        rel = self._resolve_evidence(evidence_relpath)
        text = compose_text(
            gate_source=gate_source, run_id=run_id, sim_engine=sim_engine,
            rung=rung, failure_symptom=failure_symptom, fix_action=fix_action,
            human_opinion=human_opinion, effective=effective)
        rid = self._id_for(run_id, gate_source, rung, text, rel)
        row = {
            "id": rid,
            "text": text,
            "namespace": NAMESPACE,
            "layer": LAYER,
            "run_id": run_id.strip(),
            "track": track,
            "rung": int(rung),
            "sim_engine": sim_engine,
            "gate_source": gate_source,
            "evidence_relpath": rel,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        row["vector"] = self.embed([text])[0]
        tbl = self.table(create=True)
        tbl.delete(f"id = '{rid}'")     # resume 重放幂等
        tbl.add([row])
        return {"id": rid, "table": TABLE_NAME, "evidence_relpath": rel,
                "gate_source": gate_source, "rung": rung}

    # ---------- read ----------
    def count(self) -> int:
        return self.table().count_rows() if self.table_exists() else 0

    def query(self, text: str, top_k: int = 5) -> list[dict[str, Any]]:
        """语义检索学习条目，仅服务 Phase 1 runner（与 query_kb 显式分通道）。

        返回行已 pop vector；调用方须把结果与基线知识分开标注来源（L208）。
        """
        if not isinstance(text, str) or not text.strip():
            raise ValueError("query 文本不能为空")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
            raise ValueError(f"top_k 必须是正整数，收到 {top_k!r}")
        qv = self.embed([text])[0]
        rows = self.table().search(qv).limit(top_k).to_list()
        for row in rows:
            row.pop("vector", None)
        return rows

    def snapshot(self) -> dict[str, Any]:
        """取证用：表名/行数/维度（Task6 隔离证据）。"""
        exists = self.table_exists()
        dim = None
        n = 0
        if exists:
            tbl = self.table()
            n = tbl.count_rows()
            dim = tbl.schema.field("vector").type.list_size
        return {"table": TABLE_NAME, "exists": exists, "rows": n,
                "vector_dim": dim, "uri": self.uri,
                "embedding": self.config["model_name"]}


def baseline_snapshot_json() -> str:
    """便于 -m 取证脚本序列化（不在这里做任何写动作）。"""
    return json.dumps(LearningStore().snapshot(), ensure_ascii=False, indent=2)
