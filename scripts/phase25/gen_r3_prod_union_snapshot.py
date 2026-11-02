#!/usr/bin/env python3
"""Task6 r3/mpnet 生产距离合并并集机产快照（攢批128 收口补注册）。

背景（2026-11-01 Stage A 回滚验收发现，详见攢批128 commit body）：
tests/milestone25/test_task6_planner_recall.py 的 mpnet 活等式在攢批119
（9c894eb，r1/r2 规则时代）以常量 PROD_UNION_3=3/12 落盘；攢批121
（b5444be）r3 作用域保底配额（q=4，保底席仅 S-only）落地后，生产合并对
本件 12 条 LIBERO 探针查询入座 3/12→8/12（r2 三块零丢失、单调 +5）。
121 收口仅跑 hermetic（合成/fuzz），kb_integration 活门自 119 起未再
执行；攢批126 双轨改造在 qwen 活轨落盘，mpnet 分支直到 2026-11-01 回滚
mpnet/768 后首次活跑即暴露该常量陈旧。r3 时代的活值当时漏采、无既存机产
物证，本件在回滚保真的 mpnet+r3 活库上补做机产注册（活 gold 重算
38/0.8085 与 iter3 冻结报告 f1c0e7f4 逐位相同，佐证活库即窗前 r3 态）。

口径与测试
tests/milestone25/test_task6_planner_recall.py::
test_live_task6_3_libero_scoped_seam_12_prod_union_3 完全一致：
- 作用域缝：VectorEngine.query_solutions(q, top_k=8,
  solution_id="track1_tools/03_libero")，12 问并集须为 12 个 LIBERO 块；
- 生产距离合并：PlannerRecall.retrieve("track1", {"name": q, "urls": []},
  tier=TIER_PROBE, site="direction") 的 solution_hits[:8]。
测试把本件 source_queries 与其 LIBERO_VLA_QUERIES 逐字对账（防双事实源），
并对 per_query 全 32 位 id 序逐字重算（防漂移）。

输出确定性：json sort_keys、indent=2、ensure_ascii=False、单一尾换行；
无时间戳/wall-clock/主机名/PID。同一活库连续两次运行必须逐字节相同
（独立 lane 据 --out /tmp 重跑 cmp）。零联网零付费（离线三开关 setdefault；
retrieve 为本地嵌入检索，无方舟调用）。
"""
from __future__ import annotations

import hashlib
import json
import os

for _v in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
    os.environ.setdefault(_v, "1")

import lancedb  # noqa: E402

from memory_flywheel.embedding_selector import select_embedding  # noqa: E402
from memory_flywheel.kb_pins import default_lancedb_uri  # noqa: E402
from memory_flywheel.phase1.learning_store import LearningStore  # noqa: E402
from memory_flywheel.phase2.evolution_store import EvolutionStore  # noqa: E402
from memory_flywheel.recall import ProductionRecall  # noqa: E402
from memory_flywheel.vector_engine import VectorEngine  # noqa: E402
from orchestrator.direction.planner_recall import (  # noqa: E402
    R3_RULES_VERSION,
    TIER_PROBE,
    PlannerRecall,
)

SOLUTION_ID = "track1_tools/03_libero"
TOP_K = 8
TABLES = ("kb_chunks", "solution_chunks", "phase1_learnings",
          "phase2_learnings")

# 与测试 LIBERO_VLA_QUERIES 逐字一致（测试侧反向对账，禁止双事实源漂移）。
SOURCE_QUERIES = [
    "LIBERO stage1 setrlimit RLIMIT_NOFILE MUJOCO_GL egl OffScreenRenderEnv import",
    "LIBERO servo 七维动作 np.zeros(7) a[:3] 平移 a[5] yaw a[6] 夹爪 clip",
    "LIBERO 24 角度 clearance 扫描 pick_offset_angle run_episode 单控制环",
    "LIBERO bddl_file_name 大写谓词 resolve_target 区域 plate ramekin 均值",
    "LIBERO finger_joint pad tip geom_xpos finger_axis_err 指轴角",
    "LIBERO align_yaw 符号反转 restore set_init_state attempt_grasp 闭爪抬升",
    "LIBERO held rise 0.05 trail 拖尾 分轴运输 反应式下放 check_success",
    "LIBERO get_benchmark libero_spatial OffScreenRenderEnv TypeError horizon",
    "LIBERO results judge libero_official telemetry episode manifest",
    "LIBERO incomplete suite sys.exit cover imageio 收尾",
    "VLA LIBERO policy action 7-DOF delta EEF gripper 归一化 Stage 切分",
    "OpenVLA LIBERO 动作接口 夹爪 归一化 action space 闭环",
]


def _sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _table_id_shas(uri: str) -> dict[str, str]:
    db = lancedb.connect(uri)
    out = {}
    for name in TABLES:
        rows = (db.open_table(name).search(None)
                .select(["id"]).limit(1_000_000).to_list())
        ids = sorted(str(r["id"]) for r in rows)
        out[name] = _sha256_text("\n".join(ids))
    return out


def _table_rows(uri: str) -> dict[str, int]:
    db = lancedb.connect(uri)
    return {
        "kb_chunks": db.open_table("kb_chunks").count_rows(),
        "solution_chunks": db.open_table("solution_chunks").count_rows(),
        "phase1_learnings": LearningStore().count(),
        "phase2_learnings": EvolutionStore().count(),
    }


def build(uri: str | None = None) -> dict:
    uri = uri or str(default_lancedb_uri())
    cfg = select_embedding()
    ve = VectorEngine(uri=uri)
    planner = PlannerRecall(ProductionRecall(
        LearningStore(), ve, extra_learning_stores=(EvolutionStore(),)),
        rules_version="r3")   # 历史锚点钉子：本快照是 r3 语义，不随生产默认漂移

    scoped_prefixes: set[str] = set()
    per_query = []
    union_prefixes: set[str] = set()
    for q in SOURCE_QUERIES:
        for r in ve.query_solutions(q, top_k=TOP_K, solution_id=SOLUTION_ID):
            scoped_prefixes.add(str(r["id"])[:12])
        ret = planner.retrieve("track1", {"name": q, "urls": []},
                               tier=TIER_PROBE, site="direction")
        ids32 = [str(r["id"]) for r in ret["solution_hits"][:TOP_K]]
        per_query.append({"query": q, "solution_hit_ids": ids32})
        union_prefixes.update(x[:12] for x in ids32)

    per_query.sort(key=lambda x: x["query"])
    return {
        "schema": "simpraxis.phase25.task6_r3_prod_union.v1",
        "header": {
            "config_model": cfg.get("model_name"),
            "config_dim": cfg.get("dim"),
            "rules_version": R3_RULES_VERSION,
            "solution_id": SOLUTION_ID,
            "top_k": TOP_K,
            "uri": uri,
            "table_id_set_sha256": _table_id_shas(uri),
            "table_rows": _table_rows(uri),
        },
        "source_queries": list(SOURCE_QUERIES),
        "scoped_seam_prefixes": sorted(scoped_prefixes),
        "prod_union": {
            "per_query": per_query,
            "union_solution_prefixes_all": sorted(union_prefixes),
            "intersection_scoped12_prefixes":
                sorted(union_prefixes & scoped_prefixes),
        },
    }


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--uri", default=None)
    ap.add_argument("--out",
                    default="scripts/phase25/snapshots/"
                            "task6_r3_mpnet_prod_union_snapshot.json")
    args = ap.parse_args(argv)

    data = build(args.uri)
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2)
    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
