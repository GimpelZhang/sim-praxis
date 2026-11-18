"""ResearchMemory：闭源数据层的 append-only 教训库（JSON + 哈希链）。

- 落 `$SIMPRAXIS_S0_CLOSED_ROOT/research_memory/`（0700），**永不进公开仓**；
- 每条教训绑定证据引用与反例（M5：教训必须绑证据与反例）；
- append-only：覆写既有 id 直接拒绝；`journal.jsonl` 每行带 `prev_sha256`
  形成哈希链，读取时整链校验（改一字节即 fail）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir
from orchestrator.hk.sparse_memory import closed_root

__all__ = ["ResearchMemoryError", "ResearchMemory", "default_memory_root"]


class ResearchMemoryError(RuntimeError):
    """写入违规（重复 id、链损坏、缺证据/反例）。"""


def default_memory_root() -> Path:
    return closed_root() / "research_memory"


def _canonical(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


class ResearchMemory:
    """append-only 教训库；每个研究战役一个 journal。"""

    def __init__(self, root: Path | None = None, *, journal: str = "research_memory"):
        self.root = Path(root) if root is not None else default_memory_root()
        self.path = self.root / f"{journal}.jsonl"
        self.entries: list[dict] = []
        if self.path.is_file():
            self._load()

    def _load(self) -> None:
        prev = ""
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("prev_sha256") != prev:
                raise ResearchMemoryError(f"{self.path}:{number} 哈希链断裂")
            body = dict(entry)
            body.pop("sha256", None)
            if hashlib.sha256(_canonical(body)).hexdigest() != entry.get("sha256"):
                raise ResearchMemoryError(f"{self.path}:{number} 行哈希不符（被改写过）")
            prev = entry["sha256"]
            self.entries.append(entry)

    def append(self, *, lesson_id: str, lesson: str, evidence: list[str],
               counterexample: str) -> dict:
        if any(entry.get("lesson_id") == lesson_id for entry in self.entries):
            raise ResearchMemoryError(f"lesson_id {lesson_id!r} 已存在：append-only，禁覆写")
        if not evidence:
            raise ResearchMemoryError("教训必须绑证据（evidence 非空）")
        if not counterexample.strip():
            raise ResearchMemoryError("教训必须绑反例（counterexample 非空）")
        body = {
            "schema": "simpraxis.s0.research_memory.v1",
            "lesson_id": lesson_id,
            "lesson": lesson,
            "evidence": list(evidence),
            "counterexample": counterexample,
            "prev_sha256": self.entries[-1]["sha256"] if self.entries else "",
        }
        entry = dict(body, sha256=hashlib.sha256(_canonical(body)).hexdigest())
        ensure_private_dir(self.root, mode=0o700)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        self.entries.append(entry)
        return entry

    def rewrite_forbidden(self, lesson_id: str, lesson: str) -> None:
        """显式覆写入口：永远拒绝（占位 API 让负控有稳定触面）。"""
        raise ResearchMemoryError(f"ResearchMemory 只允许 append；拒绝覆写 {lesson_id!r}")

    def export(self, target: Path) -> Path:
        """整库快照（供 ProductBundle）；内容与 journal 逐行相等。"""
        ensure_private_dir(Path(target).parent, mode=0o700)
        text = "".join(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
                       for entry in self.entries)
        atomic_write_text(Path(target), text, mode=0o600)
        return Path(target)
