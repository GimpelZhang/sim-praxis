#!/usr/bin/env python3
"""内容层钉点（Task0b 三层分离后替代旧 HEAD==aac53313 单点断言）。

旧纪律（Phase 0/1）：KB 仓永远只读、HEAD 必须逐字等于
aac53313c620efa6217fd51a55834b006d78e689。Task0b 起 KB 仓首次写入
（solutions/ 公开内容层），HEAD 允许前进，纪律改为两条分层不变量：

1. knowledge/ 只增不改：32 个 md 的 sha256 必须逐字等于本模块
   KNOWLEDGE_BASELINE_FILES（aac53313 基线集）；未来新增 md 必须经用户
   签收并显式登记进 KNOWLEDGE_ADDITIONS，改动既有文件即失败。
2. 当前 HEAD 必须是迁移提交 c5712e9f（solutions/
   首次写入）的后代——HEAD 可以前进，但不允许落在与该历史分叉的位置。

三处旧断言（scripts/01_preflight.sh、tests/milestone0/test_milestone0_e2e.py
test_05、memory_flywheel/scripts/ingest_kb.py）统一改调本模块；能力矩阵
校验器同此口径。单真值在此，禁止第四处字面量。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
from pathlib import Path

DEFAULT_KB_ROOT = Path(
    os.environ.get("KB_ROOT", "/home/vipuser/embodied-ai-sim-knowledge-base"))

# knowledge/ 全量只读基线提交（Phase 0/1 钉点；Task0b 后仍是 knowledge 树状态锚）
KNOWLEDGE_BASELINE_SHA = "aac53313c620efa6217fd51a55834b006d78e689"
# Task0b 回填：KB 仓 solutions/ 首次写入提交（引擎仓测试可指向的真实提交）。
# 注：首版 2bac483 在 push 前彩排中发现 .gitignore「data/」不忽略软链本身
# （git status 报 ?? data），因首次 push 是人工审阅门、提交尚未推送，已就地
# amend 修正为 /data 规则，SHA 变为本值；amend 后 knowledge/ 32-md 集不变。
MIGRATION_KB_SHA = "c5712e9f73fc17cd2649cae359aad216e8f3bd07"

# aac53313 时 knowledge/ 32 个 md 的逐文件 sha256（blob 内容口径，已用
# `git cat-file blob aac53313:<rel>` 32/32 校验）。relpath 以 knowledge/ 开头。
KNOWLEDGE_BASELINE_FILES: dict[str, str] = {
    "knowledge/00-index.md": "cc5597ea17c954c99edeff40fda685fc6791f8f9cde677a94d57563d0a7d93e1",
    "knowledge/global/00-index.md": "9f1d4b7a5a98e0b2ac1163fdf564ba39c8b9a7a1b12542dbebbde9b3ad2b82b9",
    "knowledge/global/best_practices.md": "fe8677acca462b96da45aa8ce39a9030746ad8cb9553876a3af21b466b074440",
    "knowledge/global/common_issues_solutions.md": "d6b05a9438d4050fbd20b1681153f953720de5c853e973a0e0dcdedbbc404caa",
    "knowledge/global/common_reproduction_guide.md": "27660e29fcc7deea1db9fb37bb9a127e5721451891d0eabb15d5e8ef5037d231",
    "knowledge/global/terminology_mapping.md": "acdc31fc63bfc43e4fbd7e97a74c894295ee6bc5ea93be4c10a794c06600237c",
    "knowledge/global/toolchain_comparison.md": "3444fb70e21b0777ebcb358de6ed9470dbc1c9c0bc453520ad03578df648675a",
    "knowledge/projects/00-index.md": "4cfcb7976ce6662360db70f76564b93f550002707f609bf6d6c1fd8e771c1fe1",
    "knowledge/projects/ge_sim_v2/00-index.md": "eac306815eedca50b36e2e5ddfead8f7dd8c2835a60f8da7d461ab5cb2f1cf21",
    "knowledge/projects/ge_sim_v2/ai_knowledge.md": "4c29043c283cc829fc43ef1c7449522e18cad75e299234e571f491fae31e0d2b",
    "knowledge/projects/ge_sim_v2/background_knowledge.md": "45eb5918dc3188e576e84977ed5fd07bf8815aa6ba6dfa062b6c4ad530d21de4",
    "knowledge/projects/ge_sim_v2/code_knowledge.md": "92598b27a9713a1fbe4cb5c3aa88962a60194ed76c97ba981b6c3028d3d3482e",
    "knowledge/projects/ge_sim_v2/quickstart.md": "7fa8a51ff5cf36105a9083bcdfade5275359e59b4aa0a17d2f04f2ad95b2c10d",
    "knowledge/projects/ge_sim_v2/troubleshooting.md": "38fef824d06fed82f4bfb1ba7450fa0f91b834480dbc93e5eca904a1c0f2a125",
    "knowledge/projects/genesis_world/00-index.md": "7a43531dee04d3c133cf48303d8f1466b26c30428a9b5a50fe449dbd31c2c234",
    "knowledge/projects/genesis_world/ai_knowledge.md": "1f448a006156bc195afdf3ff85905d83f46978540ec2e320044b2e86a339459e",
    "knowledge/projects/genesis_world/background_knowledge.md": "245b9effc0abe88d4da34b1450e44eb2db711a6b8861a13c731961f799ce5cdd",
    "knowledge/projects/genesis_world/code_knowledge.md": "d6521ef857ab4a2015e0ba90e82afd8be14ba4d4d86e24d80aed5fb44cbb1539",
    "knowledge/projects/genesis_world/quickstart.md": "9ea2937d240becb16e90e53ffe68221006be44e094f6b3275fb11d2218e5e0c8",
    "knowledge/projects/genesis_world/troubleshooting.md": "d580e916d5b8f7447a97fe12b1b1f74c4f3412b530b9687cd981f930d13ceeb1",
    "knowledge/projects/genie_sim_v3/00-index.md": "5fcb4f080d3a1a7dd57ffe5f29c8022cf66e090569f313fcc7c41031998c0b9c",
    "knowledge/projects/genie_sim_v3/ai_knowledge.md": "9cb5fb38b7343c3a21d6f360adce53852ef2665ab6371541b4e6461b93f1069c",
    "knowledge/projects/genie_sim_v3/background_knowledge.md": "dabfc51768acfd728565dfeeb3193fc49db4e420b298f406cf13064ba2a40911",
    "knowledge/projects/genie_sim_v3/code_knowledge.md": "96ddd1553b43697613fc7494e6b6efeb969174ceff7a4a28c5588fa18ec06004",
    "knowledge/projects/genie_sim_v3/quickstart.md": "e52651661a0b5d5e3fa83bd37201bed2eacb6e8f03f0eefe3de4ad6374fe21a0",
    "knowledge/projects/genie_sim_v3/troubleshooting.md": "599420069eacf8f44f05f524bc442a59d1c41287f8c60b71c7f6250698116e86",
    "knowledge/projects/lw_benchhub/00-index.md": "fdf8f0dc8710b5de61d39304ecaf38e75cf9def133c8391096dcd5e727473b26",
    "knowledge/projects/lw_benchhub/ai_knowledge.md": "6daf1d231176b6f9734d096cc9a7f98ce4f1390e307d18dbf9b4c0504abf8173",
    "knowledge/projects/lw_benchhub/background_knowledge.md": "9221e7cd0adef67b8ed9a41b693aa65121696e90721a6569f3225ae0a94dbcb2",
    "knowledge/projects/lw_benchhub/code_knowledge.md": "25f3a2eca33ba8c0b51cb223c0428ad3e3a088b0bb623aed55b8c97e2a23c61b",
    "knowledge/projects/lw_benchhub/quickstart.md": "dd3438c2fa219eea8dd7ba66fbe7114212b1765395cf3c01bd5d2e099d454368",
    "knowledge/projects/lw_benchhub/troubleshooting.md": "4d4cc95bfd5ddb35ceb32ec4af245be1131678e7fcf120d57d8a4592beaa0859",
}

# 只增不改登记表：Task0b 时为空。未来新增 knowledge md 必须经用户书面签收，
# 并把 "knowledge/...md": "<sha256>" 显式加到此表；未登记的额外 md 一律判失败。
KNOWLEDGE_ADDITIONS: dict[str, str] = {}

# Task0b QA（M2）：solution_chunks 规范方案全集（4 方案；单真值）。
# scripts/kb/check_solutions.py 树校验与 ingest_solutions strict 模式共同引用，
# 缺/多方案或表内孤儿 solution_id 一律 fail-closed。
EXPECTED_SOLUTION_IDS: tuple[str, ...] = (
    "track1_tools/01_maniskill_pushcube",
    "track1_tools/02_mujoco_contact_push",
    "track2_questions/01_ice_floor_humanoid_dr",
    "track2_questions/02_lidar_weather",
)


class DataLayerError(RuntimeError):
    """内容仓 data/ 软链缺失/悬空/实体目录 split-brain：拒绝在公开内容仓实建 data/。

    三个 LanceDB 构造入口（VectorEngine/LearningStore/SolutionIngester）在任何
    mkdir/connect 之前先过 ensure_content_data_link 闸；缺链新克隆不得靠
    ``Path(uri).mkdir`` 在公开内容仓里实建实体数据目录。
    """


def expected_knowledge_files() -> dict[str, str]:
    """基线集 + 已签收追加集（后者 Task0b 时为空）。"""
    return {**KNOWLEDGE_BASELINE_FILES, **KNOWLEDGE_ADDITIONS}


def _kb_root_path(kb_root: "str | Path | None") -> Path:
    if kb_root is None:
        kb_root = os.environ.get("KB_ROOT", str(DEFAULT_KB_ROOT))
    return Path(kb_root)


def default_lancedb_uri() -> str:
    """默认 LanceDB 库 URI 单真值：$SIMPRAXIS_LANCEDB_URI > $KB_ROOT/data/lancedb。

    Task0b QA（M1）：原先同一表达式散在 vector_engine/learning_store/
    ingest_solutions 三处，收口到此。
    """
    return os.environ.get(
        "SIMPRAXIS_LANCEDB_URI",
        str(_kb_root_path(None) / "data" / "lancedb"))


def is_content_data_uri(uri: "str | Path", *,
                        kb_root: "str | Path | None" = None) -> bool:
    """uri 解析后是否位于内容仓 data/ 之下（等于 data/lancedb 或更深）。

    用 resolve(strict=False) 跟随既有软链：活路径
    $KB_ROOT/data/lancedb 与其实体目标 /mnt/.../data/lancedb 判定一致。
    """
    data_dir = (_kb_root_path(kb_root) / "data").resolve(strict=False)
    target = Path(uri).resolve(strict=False)
    return target == data_dir or target.is_relative_to(data_dir)


def ensure_content_data_link(uri: "str | Path", *,
                             kb_root: "str | Path | None" = None) -> None:
    """内容层数据 URI 软链闸（先于任何 mkdir/connect 调用）。

    - 非内容层 URI（tmp 彩排库、不经内容仓 data/ 的显式路径）直接放行；
    - 内容层路径要求 data/ 必须是指向存在目标的 symlink；悬空/实体目录/缺失
      三种状态全部 DataLayerError，杜绝缺链克隆在公开内容仓实建 data/。
    """
    if not is_content_data_uri(uri, kb_root=kb_root):
        return
    root = _kb_root_path(kb_root)
    data = root / "data"
    fix = "python scripts/kb/migrate_data_layer.py --apply"
    if data.is_symlink():
        if not data.exists():
            raise DataLayerError(
                f"数据层软链悬空：{data} -> {os.readlink(data)}（目标不存在）。"
                f"先恢复实体目标或重跑 {fix}；拒绝在公开内容仓实建 data/")
        return
    if data.exists():
        raise DataLayerError(
            f"{data} 是实体目录而非软链（split-brain）：内容仓 data/ 必须软链到"
            "闭源数据层（/mnt/artifacts/embodied-kb/data）。处置："
            f"{fix} 或人工迁移后建链；拒绝在公开内容仓实建 data/")
    raise DataLayerError(
        f"内容仓缺数据层软链：{data} 不存在。先跑 {fix} 建链（实体落 /mnt，"
        "data 仅为软链）；拒绝在公开内容仓实建 data/")


def _git(kb_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(kb_root), *args],
                          capture_output=True, text=True)


def head(kb_root: "str | Path" = DEFAULT_KB_ROOT) -> str:
    p = _git(Path(kb_root), "rev-parse", "HEAD")
    if p.returncode != 0:
        raise RuntimeError(f"无法读取 KB HEAD：{p.stderr.strip()}")
    return p.stdout.strip()


def is_descendant(commit: str, kb_root: "str | Path" = DEFAULT_KB_ROOT,
                  *, ref: str = "HEAD") -> bool:
    """commit 是否为 ref 的祖先（merge-base --is-ancestor）。"""
    return _git(Path(kb_root), "merge-base", "--is-ancestor",
                commit, ref).returncode == 0


def tracked_knowledge_md(kb_root: "str | Path" = DEFAULT_KB_ROOT) -> list[str]:
    """git 跟踪的 knowledge/**/*.md（未跟踪文件不计入哈希集）。"""
    p = _git(Path(kb_root), "ls-files", "knowledge")
    if p.returncode != 0:
        raise RuntimeError(f"git ls-files knowledge 失败：{p.stderr.strip()}")
    return sorted(rel for rel in p.stdout.splitlines() if rel.endswith(".md"))


def check_knowledge_hash_set(kb_root: "str | Path" = DEFAULT_KB_ROOT) -> list[str]:
    """knowledge/ 32-md 哈希集校验，返回错误列表（空 = 通过）。

    既有文件 sha 不符=被修改（错误）；缺基线文件=错误；出现未登记的额外
    md=错误（新增必须显式登记 KNOWLEDGE_ADDITIONS）。

    Task0b QA（L5）：git ls-files 看不见未跟踪文件，故另 rglob 工作树
    knowledge/**/*.md，任何未被 git 跟踪的 md 单独报错（堵 docstring 承诺洞）。
    """
    kb_root = Path(kb_root)
    expected = expected_knowledge_files()
    errors: list[str] = []
    actual: dict[str, str] = {}
    tracked = set(tracked_knowledge_md(kb_root))
    for rel in tracked:
        p = kb_root / rel
        if not p.is_file():
            errors.append(f"knowledge 文件缺失：{rel}")
            continue
        actual[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    know_dir = kb_root / "knowledge"
    if know_dir.is_dir():
        for p in sorted(know_dir.rglob("*.md")):
            if not p.is_file():
                continue
            rel = p.relative_to(kb_root).as_posix()
            if rel not in tracked:
                errors.append(
                    f"knowledge/ 工作树存在未被 git 跟踪的 md（须签收登记并提交）：{rel}")
    for rel, want in sorted(expected.items()):
        got = actual.get(rel)
        if got is None:
            errors.append(f"knowledge 基线文件丢失：{rel}")
        elif got != want:
            errors.append(f"knowledge 基线文件被修改（只增不改）：{rel} "
                          f"want={want[:12]} got={got[:12]}")
    for rel in sorted(set(actual) - set(expected)):
        errors.append(f"出现未登记的 knowledge 新文件（须显式签收登记）：{rel}")
    return errors


def check_layered_pins(kb_root: "str | Path" = DEFAULT_KB_ROOT) -> list[str]:
    """Task0b 分层不变量全集：迁移提交后代 + knowledge 哈希集。"""
    kb_root = Path(kb_root)
    errors: list[str] = []
    if not (kb_root / ".git").exists():
        return [f"KB 路径不是 git 仓：{kb_root}"]
    cur = head(kb_root)
    if not is_descendant(MIGRATION_KB_SHA, kb_root):
        errors.append(
            f"KB HEAD {cur[:12]} 不是迁移提交 {MIGRATION_KB_SHA[:12]} 的后代"
            "（HEAD 与 solutions/ 历史分叉？）")
    errors.extend(check_knowledge_hash_set(kb_root))
    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description="Task0b 内容层分层不变量校验")
    ap.add_argument("--kb-root", default=str(DEFAULT_KB_ROOT))
    args = ap.parse_args()
    errors = check_layered_pins(args.kb_root)
    n_base = len(KNOWLEDGE_BASELINE_FILES)
    n_add = len(KNOWLEDGE_ADDITIONS)
    print(f"[kb_pins] knowledge 基线 {n_base} md + 登记追加 {n_add}；"
          f"HEAD={head(args.kb_root)[:12]}；迁移提交 {MIGRATION_KB_SHA[:12]}")
    for e in errors:
        print(f"[kb_pins FAIL] {e}")
    if not errors:
        print("[kb_pins OK] 分层不变量通过（后代关系 + knowledge 哈希集）")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
