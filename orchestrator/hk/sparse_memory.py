"""稀疏记忆 m_t：Run 级状态锚点，轮末归档，**永不入 KB**（设计族 §5）。

- 白名单键族固定为 `api_paths / fingerprints / block_effects / defects`；
  其余键一律拒（防止运行期绝对值被当作"永久经验"沉淀）。
- `archive()` 落 `$SIMPRAXIS_S0_CLOSED_ROOT`（缺省
  `/mnt/artifacts/simpraxis/s0_closed/`，0700）下的 evidence 根，写
  `sparse_state.json` 后**内存清零**；归档件只随 ProductBundle 走闭源数据层，
  kb_chunks / phase1_learnings 等表行数恒不变（本模块不导入任何向量库）。
- 大对象（轨迹/视频）以 BlobRef 风格引用记录，不内联字节：`bytes` 值直接拒。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

__all__ = ["SparseMemory", "ALLOWED_KEYS", "closed_root", "SparseMemoryError"]

ALLOWED_KEYS = ("api_paths", "fingerprints", "block_effects", "defects")
DEFAULT_CLOSED_ROOT = Path("/mnt/artifacts/simpraxis/s0_closed")


class SparseMemoryError(ValueError):
    """m_t 纪律违规（白名单外键、不可序列化值、坏归档）。"""


def closed_root() -> Path:
    """闭源数据层根（0700）；env 可覆盖，仅供测试/迁移演练。"""
    root = Path(os.environ.get("SIMPRAXIS_S0_CLOSED_ROOT", str(DEFAULT_CLOSED_ROOT)))
    return root


def _jsonable(value, key: str):
    if isinstance(value, (bytes, bytearray)):
        raise SparseMemoryError(
            f"m_t[{key}] 不接受内联字节；大对象请存 blob 后记引用（设计族 §2 要点②）")
    try:
        json.dumps(value, ensure_ascii=False)
    except TypeError as exc:
        raise SparseMemoryError(f"m_t[{key}] 不可 JSON 序列化：{exc}") from None
    return value


class SparseMemory:
    """Run 级稀疏状态；`close()/archive()` 后内存清零。"""

    def __init__(self, run_dir: Path | None = None):
        self._data: dict = {}
        self._run_dir = Path(run_dir) if run_dir is not None else None
        self._archived_to: Path | None = None

    # —— 读写 ——
    def put(self, key: str, value) -> None:
        if key not in ALLOWED_KEYS:
            raise SparseMemoryError(
                f"m_t 键 {key!r} 不在白名单 {ALLOWED_KEYS}；运行期发现只留在 Run 级证据")
        self._data[key] = _jsonable(value, key)

    def get(self, key: str):
        if key not in ALLOWED_KEYS:
            raise SparseMemoryError(f"m_t 键 {key!r} 不在白名单 {ALLOWED_KEYS}")
        return self._data.get(key)

    def snapshot(self) -> dict:
        return json.loads(json.dumps(self._data, ensure_ascii=False))

    def __len__(self) -> int:
        return len(self._data)

    # —— 落盘/归档 ——
    def payload(self) -> dict:
        body = json.dumps(self._data, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
        return {
            "schema": "simpraxis.s0.sparse_state.v1",
            "keys": sorted(self._data),
            "sha256": hashlib.sha256(body).hexdigest(),
            "state": self._data,
        }

    def save(self, path: Path) -> Path:
        """原子写（0600）；路径仅允许 Run 目录或闭源数据层。"""
        path = Path(path)
        ensure_private_dir(path.parent, mode=0o700)
        atomic_write_text(path, json.dumps(self.payload(), ensure_ascii=False,
                                           indent=2, sort_keys=True) + "\n", mode=0o600)
        return path

    @classmethod
    def load(cls, path: Path) -> "SparseMemory":
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        if document.get("schema") != "simpraxis.s0.sparse_state.v1":
            raise SparseMemoryError(f"{path} 不是 sparse_state.v1 归档")
        body = json.dumps(document.get("state", {}), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
        if hashlib.sha256(body).hexdigest() != document.get("sha256"):
            raise SparseMemoryError(f"{path} 归档哈希不符（被改写过）")
        memory = cls()
        for key, value in (document.get("state") or {}).items():
            memory.put(key, value)
        return memory

    def archive(self, evidence_root: Path | None = None) -> Path:
        """归档到证据根（缺省闭源数据层 `m_t/<run_id>/`）并清零内存。"""
        if evidence_root is None:
            root = closed_root() / "m_t"
            ensure_private_dir(root, mode=0o700)
            stamp = self.payload()["sha256"][:12]
            evidence_root = root / f"run_{stamp}"
        evidence_root = Path(evidence_root)
        ensure_private_dir(evidence_root, mode=0o700)
        target = self.save(evidence_root / "sparse_state.json")
        self.clear()
        self._archived_to = target
        return target

    def clear(self) -> None:
        self._data = {}

    @property
    def archived_to(self) -> Path | None:
        return self._archived_to
