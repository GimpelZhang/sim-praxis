"""Phase 2 Task5：``phase2_learnings`` 独立演化表（计划 L509-527，0 付费）。

隔离铁律（沿用 phase1 learning_store 的同口径，任何一条不许放宽）：
- 同库第三张独立表：与 kb_chunks / solution_chunks / phase1_learnings 同
  uri，只打开/创建 ``phase2_learnings``；老两表（kb_chunks/phase1_learnings）
  对本模块恒只读，不调 VectorEngine 写方法。
- schema **逐字同构 + 3 列**：phase1 的恰好 12 列
  （id/text/vector/namespace/layer/run_id/track/rung/sim_engine/
  gate_source/evidence_relpath/created_at）一列不改，追加
  evolution_round:int64（>=1）、parent_run_id:string（必填非空，V2 必有
  V1 父运行）、pareto_gain_ratio:float64（可空；给值必须 finite，
  NaN/Inf/bool 一律拒——与 video.trim S-3 同口径 math.isfinite）。
- 幂等建表但**不自愈**：表已存在则校验列集合、列类型、vector 维度；维度/
  结构不符（含其他嵌入/版本写出的 v0 文件、损坏 manifest）抛专属
  ``EvolutionSchemaError``，消息带人工处置文案（备份后 drop
  phase2_learnings 重建，老两表不受影响），禁止 ALTER/混维度写入/裸崩。
- 嵌入复用 mpnet 768（fastembed 本地缓存），不二次下载；离线 env 可跑。
- 证据 relpath 双根校验**直接复用** LearningStore 的既有实现（继承，不复制
  一份）：证据根在 Phase 2 为 $SIMPRAXIS_PHASE2_ROOT（演化志落
  evolutions/），runs 根为 $SIMPRAXIS_PHASE2_ROOT/runs，标记仍 "@runs/"。

text 列承载 ``orchestrator.phase2.chronicle`` 渲染的五段式演化志全文；
本任务只做单行承载，不新增第四张向量表（计划 L517）。演化志可能超过嵌入
模型上下文，嵌入锚定首部（动机/V1 局限在最前）：切块常量 TARGET_CHARS/
HARD_CHARS 从 ingest_kb 单一源导入，禁止复制魔数。
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

import pyarrow as pa

from memory_flywheel.phase1.learning_store import (
    GATE_ACCEPTED,
    GATE_ACCEPTED_SUPPLEMENTARY,
    GATE_SOURCES,
    MAX_RUNG,
    TRACKS,
    LearningStore,
)
from memory_flywheel.scripts.ingest_kb import HARD_CHARS, TARGET_CHARS
from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.runtime_paths import phase2_root, phase2_runs_root

TABLE_NAME = "phase2_learnings"
NAMESPACE = "phase2_learnings"
LAYER = "phase2_evolution_note"

# ascent accept 才允许写本表；failure 结案只进 phase1 表（human_failure 语义
# 不在本任务改写），failure 类 gate_source 在此显式拒。
_ACCEPTED_GATE_SOURCES = frozenset(
    {GATE_ACCEPTED, GATE_ACCEPTED_SUPPLEMENTARY})

# 恰好 15 列：phase1 12 列逐字同构（同序同型）+ 3 新列。
COLUMNS: tuple[str, ...] = (
    "id", "text", "vector", "namespace", "layer", "run_id", "track", "rung",
    "sim_engine", "gate_source", "evidence_relpath", "created_at",
    "evolution_round", "parent_run_id", "pareto_gain_ratio",
)

# LOW-3：异 schema/异维度/v0 损坏文件一律给处置文案，不自愈、不裸崩。
_DISPOSITION = (
    f"{TABLE_NAME} 表由其他嵌入模型/其他版本创建，或属于 schema 不符的 v0 "
    "文件：禁止自愈 ALTER、禁止混维度写入。处置：人工备份该表（lancedb 目录 "
    f"{TABLE_NAME}.lance）后 drop {TABLE_NAME} 重建（仅删本新表即可）；"
    "重建不影响 kb_chunks 与 phase1_learnings 老两表（solution_chunks 同样"
    "不受影响）。")


class EvolutionSchemaError(RuntimeError):
    """phase2_learnings 维度/列结构与当前嵌入及 12+3 契约不一致（LOW-3）。

    消息必须携带人工处置文案（备份 → drop phase2_learnings → 重建，老两表
    不受影响）；调用方不得把它降级成默认建表或静默跳过。
    """


def embedding_input(text: str) -> str:
    """五段演化志的嵌入承载文本：不超 HARD_CHARS 全文嵌入；超长锚定首部。

    五段 md 的动机/V1 局限位于最前，是后续轮次召回最该命中的语义；全文仍
    逐字存 text 列，本函数只影响 vector。TARGET_CHARS/HARD_CHARS 必须来自
    ingest_kb 常量单一源（计划 L517，禁止复制 1200/2000 魔数）。
    """
    if len(text) <= HARD_CHARS:
        return text
    return text[:TARGET_CHARS]


class EvolutionStore(LearningStore):
    """phase2_learnings 的唯一合法读写者（同库独立表，768 维 mpnet）。

    继承 LearningStore 复用：嵌入懒加载/embed、证据双根换算
    （relativize_evidence/_resolve_evidence）、确定性先删后写主键口径；
    只覆盖表名/schema/建表校验与写入 API。
    """

    def __init__(self, uri: "str | None" = None,
                 evidence_root: "str | Path | None" = None,
                 runs_evidence_root: "str | Path | None" = None):
        # 证据根默认在 Phase 2（演化志 evolutions/ 落 phase2_root；V2 真实
        # rollout 产物在 phase2_root/runs，仍用 "@runs/" 标记）。
        super().__init__(
            uri=uri,
            evidence_root=Path(evidence_root) if evidence_root
            else phase2_root(),
            runs_evidence_root=Path(runs_evidence_root)
            if runs_evidence_root else phase2_runs_root())
        # 父类 __init__ 只守卫 phase1_learnings；本表的维度/列结构在此守卫。
        self._assert_evolution_schema()

    # ---------- LOW-3：维度 + 15 列结构守卫（不自愈，给处置文案） ----------

    def _assert_evolution_schema(self) -> None:
        if TABLE_NAME not in self.db.table_names():
            return
        try:
            existing = self.db.open_table(TABLE_NAME).schema
        except Exception as exc:  # 损坏 manifest / 不可识别的 v0 存储文件
            raise EvolutionSchemaError(
                f"打开已有 {TABLE_NAME} 失败（{type(exc).__name__}: {exc}）。"
                + _DISPOSITION) from exc
        names = set(existing.names)
        if "vector" not in names:
            raise EvolutionSchemaError(
                f"已有 {TABLE_NAME} 缺 vector 列（异 schema v0 文件）。"
                + _DISPOSITION)
        existing_dim = getattr(existing.field("vector").type, "list_size", None)
        if existing_dim is None or int(existing_dim) != self.dim:
            raise EvolutionSchemaError(
                f"已有 {TABLE_NAME} 向量维度 {existing_dim} 与当前嵌入配置 "
                f"{self.dim} 不一致（疑似换过嵌入模型的 v0/异维度表）。"
                + _DISPOSITION)
        expected_names = set(COLUMNS)
        if names != expected_names:
            missing = sorted(expected_names - names)
            extra = sorted(names - expected_names)
            raise EvolutionSchemaError(
                f"已有 {TABLE_NAME} 列集合不符 12+3 契约（缺 {missing}，"
                f"多余 {extra}）。" + _DISPOSITION)
        expected = self._schema()
        mismatched = [
            name for name in COLUMNS
            if existing.field(name).type != expected.field(name).type
        ]
        if mismatched:
            raise EvolutionSchemaError(
                f"已有 {TABLE_NAME} 列类型不符契约：{mismatched}。"
                + _DISPOSITION)

    # ---------- schema / table ----------

    def _schema(self) -> pa.Schema:
        # 前 12 个 field 与 LearningStore._schema() 逐字同构（含 rung int32、
        # vector float32 固定长 768）；同构对质见 tests/milestone2/test_task5。
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
            pa.field("evolution_round", pa.int64()),
            pa.field("parent_run_id", pa.string()),
            pa.field("pareto_gain_ratio", pa.float64()),
        ])

    def table_exists(self) -> bool:
        return TABLE_NAME in self.db.table_names()

    def table(self, create: bool = False):
        if not self.table_exists():
            if not create:
                raise RuntimeError(
                    f"表 {TABLE_NAME} 不存在；演化回写由 ascent 结案接缝首次创建")
            self.db.create_table(TABLE_NAME, schema=self._schema(),
                                 mode="create")
        return self.db.open_table(TABLE_NAME)

    # ---------- 写入 ----------

    @staticmethod
    def _id_for(run_id: str, parent_run_id: str, gate_source: str, rung: int,
                evolution_round: int, text: str, evidence_relpath: str) -> str:
        # 确定性主键：kill-9 resume 同参结案重放先删后写，不产生第二行。
        # 比 phase1 多 parent/round，避免同一 V2 run 不同轮演化互相覆盖；
        # created_at 不入 hash（重放视为同一次结案）。
        h = hashlib.sha256("|".join([
            run_id, parent_run_id, gate_source, str(rung),
            str(evolution_round), text, evidence_relpath,
        ]).encode("utf-8")).hexdigest()
        return h[:32]

    def add_evolution(self, *, run_id: str, track: str, rung: int,
                      sim_engine: str, gate_source: str,
                      evidence_relpath: str, evolution_round: int,
                      parent_run_id: str, text: str,
                      pareto_gain_ratio: "float | None" = None) -> dict[str, Any]:
        """写一条 V1→V2 升阶演化行。校验失败直接抛，绝不静默丢弃。

        与 add_learning 的差异：只接 accepted 类 gate_source（failure 不写本
        表）；rung 必须 >=1；evolution_round >=1 的 int64；parent_run_id
        必填非空且不等于本 run；pareto_gain_ratio 可空，给值必须 finite
        （NaN/Inf/bool 拒，S-3 同口径）；text 为五段演化志全文。
        """
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id 必填（V2 图 thread_id）")
        if track not in TRACKS:
            raise ValueError(f"track 必须是 {TRACKS}，收到 {track!r}")
        if isinstance(rung, bool) or not isinstance(rung, int) \
                or not (1 <= rung <= MAX_RUNG):
            raise ValueError(
                f"演化行 rung 必须是 1..{MAX_RUNG} 的整数（失败结案不写本表），"
                f"收到 {rung!r}")
        if not isinstance(sim_engine, str):
            raise ValueError("sim_engine 必须是字符串")
        if gate_source not in GATE_SOURCES:
            raise ValueError(f"gate_source 非法：{gate_source!r}；白名单 "
                             f"{sorted(GATE_SOURCES)}")
        if gate_source not in _ACCEPTED_GATE_SOURCES:
            raise ValueError(
                f"phase2_learnings 只记 accepted 升阶结案，收到 failure 类 "
                f"gate_source={gate_source!r}：失败学习只进 phase1_learnings")
        # evolution_round：bool 是 int 子类，先挡；0/负数/缺省拒（int64 >=1）。
        if evolution_round is None:
            raise ValueError("evolution_round 必填且为 >=1 的整数")
        if isinstance(evolution_round, bool) \
                or not isinstance(evolution_round, int) \
                or evolution_round < 1:
            raise ValueError(
                "evolution_round 必须是 >=1 的整数（int64），0/负数/bool 拒，"
                f"收到 {evolution_round!r}")
        if not isinstance(parent_run_id, str) or not parent_run_id.strip():
            raise ValueError(
                "parent_run_id 必填非空：V2 演化必须锚定 V1 父运行 thread_id")
        parent_run_id = parent_run_id.strip()
        if parent_run_id == run_id.strip():
            raise ValueError(
                "parent_run_id 不得等于本 run_id（V2 必须有独立的 V1 父运行）")
        if pareto_gain_ratio is not None:
            # bool 显式拒（True 被当 1.0 会伪造增益）；NaN/Inf 与 S-3 trim
            # validate_trim 同口径 math.isfinite 拒。
            if isinstance(pareto_gain_ratio, bool) \
                    or not isinstance(pareto_gain_ratio, (int, float)):
                raise ValueError(
                    "pareto_gain_ratio 必须是实数或 None，收到 "
                    f"{type(pareto_gain_ratio).__name__}")
            if not math.isfinite(float(pareto_gain_ratio)):
                raise ValueError(
                    "pareto_gain_ratio 必须 finite（NaN/Inf 拒），收到 "
                    f"{pareto_gain_ratio!r}")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text 必填：五段式演化志全文（chronicle 渲染件）")

        rel = self._resolve_evidence(evidence_relpath)
        rid = self._id_for(run_id.strip(), parent_run_id, gate_source, rung,
                           evolution_round, text, rel)
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
            "created_at": utc_now_iso_z(),
            "evolution_round": int(evolution_round),
            "parent_run_id": parent_run_id,
            "pareto_gain_ratio": (None if pareto_gain_ratio is None
                                  else float(pareto_gain_ratio)),
        }
        row["vector"] = self.embed([embedding_input(text)])[0]
        tbl = self.table(create=True)
        tbl.delete(f"id = '{rid}'")     # resume 重放幂等
        tbl.add([row])
        return {"id": rid, "table": TABLE_NAME, "evidence_relpath": rel,
                "gate_source": gate_source, "rung": rung,
                "evolution_round": int(evolution_round),
                "parent_run_id": parent_run_id}

    # ---------- 演化志证据件落盘 ----------

    def persist_chronicle(self, filename: str, markdown: str) -> str:
        """把五段式演化志原子落到 phase2 证据根 evolutions/，返回库内 relpath。

        文件名只接受 bare ``*.md``（无分隔符/无越界；文件名规则由
        orchestrator.phase2.chronicle.evolution_filename 产生，本层不反向
        import 编排面）；目录 0700、文件 0600；复用继承的双根 relpath 换算，
        phase2 根下返回裸 relpath（``evolutions/<name>.md``）。
        """
        if not isinstance(filename, str) or not filename.endswith(".md") \
                or filename in (".md",) or "/" in filename or "\\" in filename \
                or filename in (".", "..") or filename.startswith("."):
            raise ValueError(
                "演化志文件名必须是 bare *.md（无目录分隔符/无点前缀），收到 "
                f"{filename!r}")
        if not isinstance(markdown, str) or not markdown.strip():
            raise ValueError("演化志内容为空：禁止落空证据件")
        from orchestrator.common.fsutil import (
            atomic_write_text,
            ensure_private_dir,
        )
        evolutions_dir = self.evidence_root / "evolutions"
        ensure_private_dir(evolutions_dir, 0o700)
        target = evolutions_dir / filename
        atomic_write_text(target, markdown, mode=0o600)
        return self.relativize_evidence(target)

    # ---------- read ----------

    def snapshot(self) -> dict[str, Any]:
        """取证用：表名/行数/维度（Task5 隔离证据）。"""
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
