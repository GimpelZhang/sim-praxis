"""RSI-R1 campaign 运行时上下文：不可变根、写守卫、与账本/授权的最小装配。

Plan §2.1 规定本模块负责「ledger/ModelGateway/准入/预算装配，无裸发网入口」；
T0 只落地**写守卫与上下文**部分（不装配 sender：付费入口在 T2 才成立）。

两条 T0 硬要求（Plan §9 T0「只读旧根守卫」；AC03）：

* campaign 根一经构造即不可变；写函数接收**显式 context**，不在写时重新读 env；
* 任何写路径必须落在本 campaign 根内，且不落旧根（六份封存期根 + R0/R0.1/R0.2
  三族家族根），不经符号链接；路径解析后漂移一律拒写。

本模块不触网、不调用模型、不跑仿真；只做路径判定与私有文件写入。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from orchestrator.common.fsutil import ensure_private_dir
from orchestrator.common.runtime_paths import (
    RSI_R1_CAMPAIGN_SUBDIRS,
    assert_rsi_r1_roots_isolated,
    rsi_r1_campaign_id,
    rsi_r1_campaign_root,
    rsi_r1_protected_write_roots,
    rsi_r1_root,
)


class CampaignError(RuntimeError):
    """R1 campaign 基座错误（上下文缺失/漂移/结构不符）。"""


class CampaignRootRefused(CampaignError):
    """campaign 根与旧根相等/嵌套/符号链接别名，或根含不安全字符。"""


class CampaignWriteRefused(CampaignError):
    """写路径越界：落在 campaign 根外、旧根内、或经由符号链接。"""


def _resolved(path: "str | Path") -> Path:
    return Path(path).resolve(strict=False)


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


#: 家族根下的封存登记（**不是** campaign 内的文件）：§8.1 换 root 时把旧
#: campaign 根登记为「已封存、只读」。结案工具（``adopt_context``）据此拒写，
#: 否则旧根虽不在写守卫保护集里，也会被 closeout 长出新产物（AC34 只读）。
ARCHIVE_REGISTRY_NAME = "archived_roots.json"
ARCHIVE_REGISTRY_SCHEMA = "simpraxis.rsi_r1.archived_roots.v1"


def archive_registry_path(campaign_root: "str | Path") -> Path:
    """登记件位置 = campaign 根的**家族根**（父目录），不是 campaign 内部。"""
    return Path(campaign_root).resolve(strict=False).parent / ARCHIVE_REGISTRY_NAME


def _read_archive_registry(family: Path) -> "dict[str, Any]":
    path = Path(family) / ARCHIVE_REGISTRY_NAME
    if not path.is_file():
        return {"schema": ARCHIVE_REGISTRY_SCHEMA, "schema_version": 1,
                "roots": []}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignError(f"{path} 不可读：{exc}") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("roots"), list):
        raise CampaignError(f"{path} 结构不符（roots 必须是列表）")
    return doc


def is_archived_root(root: "str | Path") -> "str | None":
    """若 root 已在家族登记里，返回登记理由；否则 None。"""
    resolved = Path(root).resolve(strict=False)
    for entry in _read_archive_registry(resolved.parent).get("roots") or []:
        target = entry.get("root") if isinstance(entry, dict) else entry
        if target and Path(str(target)).resolve(strict=False) == resolved:
            return str(entry.get("reason") or "") if isinstance(entry, dict) else ""
    return None


def register_archived_root(campaign_root: "str | Path", *, reason: str,
                           clock: str) -> None:
    """把旧 campaign 根登记进家族根（幂等；只追加不覆盖）。"""
    resolved = Path(campaign_root).resolve(strict=False)
    family = resolved.parent
    doc = _read_archive_registry(family)
    roots = doc.get("roots") or []
    if any((e.get("root") if isinstance(e, dict) else e)
           and Path(str(e.get("root") if isinstance(e, dict) else e)
                    ).resolve(strict=False) == resolved for e in roots):
        return
    roots.append({"root": str(resolved), "archived_at_utc": clock,
                  "reason": reason})
    doc.update({"schema": ARCHIVE_REGISTRY_SCHEMA, "schema_version": 1,
                "roots": roots, "updated_at_utc": clock})
    path = family / ARCHIVE_REGISTRY_NAME
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (json.dumps(doc, ensure_ascii=False, sort_keys=True,
                                 indent=2) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _assert_no_symlink_components(path: Path, *, stop: Path) -> None:
    """从 path 往上走到 stop，任何一级是符号链接即拒（防目录穿越/别名）。"""
    p = Path(path)
    stop_r = _resolved(stop)
    cur = p
    while True:
        if cur.exists() or cur.is_symlink():
            if cur.is_symlink():
                raise CampaignWriteRefused(f"写路径经符号链接组件：{cur}")
        if _resolved(cur) == stop_r or cur.parent == cur:
            break
        cur = cur.parent


@dataclass(frozen=True)
class CampaignContext:
    """不可变 campaign 上下文：根、envelope、旧根保护集。"""

    envelope_id: str
    root: Path
    protected_roots: "tuple[tuple[str, Path], ...]"

    # ---- 路径 ----

    def subdir(self, name: str) -> Path:
        if name not in RSI_R1_CAMPAIGN_SUBDIRS:
            raise CampaignError(f"未知 R1 campaign 子目录：{name!r}")
        return self.root / name

    def relative(self, relpath: "str | Path") -> Path:
        """campaign 根内相对路径 → 绝对路径（仅词法拼接；写前须过守卫）。"""
        rel = Path(relpath)
        if rel.is_absolute() or ".." in rel.parts:
            raise CampaignWriteRefused(f"只接受 campaign 根内相对路径：{rel}")
        return self.root / rel

    # ---- 写守卫 ----

    def assert_write_allowed(self, target: "str | Path") -> Path:
        if self.root.is_symlink():
            raise CampaignWriteRefused(f"campaign 根已变成符号链接，拒绝写入：{self.root}")
        if _resolved(self.root) != self.root:
            raise CampaignWriteRefused(
                f"campaign 根解析结果漂移（构造后被替换）：{self.root} -> "
                f"{_resolved(self.root)}；写时拒绝")
        p = Path(target)
        if not p.is_absolute():
            p = self.root / p
        _assert_no_symlink_components(p, stop=self.root)
        rp = _resolved(p)
        if not _is_within(rp, self.root):
            raise CampaignWriteRefused(
                f"写路径越出 campaign 根：{p}（resolve={rp}，root={self.root}）")
        for name, prot in self.protected_roots:
            if rp == prot or _is_within(rp, prot):
                raise CampaignWriteRefused(
                    f"写路径落进旧根 {name}（{prot}）：旧实验只读，拒绝写入")
        return p

    def write_text(self, relpath: "str | Path", text: str) -> Path:
        target = self.assert_write_allowed(self.relative(relpath))
        return _atomic_write_text(target, text)

    def write_json(self, relpath: "str | Path", obj: Any) -> Path:
        text = json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True,
                          allow_nan=False) + "\n"
        return self.write_text(relpath, text)

    def read_json(self, relpath: "str | Path") -> Any:
        p = self.relative(relpath)
        return json.loads(p.read_text(encoding="utf-8"))

    def exists(self, relpath: "str | Path") -> bool:
        return self.relative(relpath).is_file()


def _atomic_write_text(path: Path, text: str) -> Path:
    ensure_private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return path


def protected_roots_tuple() -> "tuple[tuple[str, Path], ...]":
    """旧根保护集的冻结快照（resolve 归一，构造 context 时取一次）。"""
    return tuple((name, _resolved(p))
                 for name, p in rsi_r1_protected_write_roots().items())


def create_context(envelope_id: str) -> CampaignContext:
    """按 env 构造新 campaign 上下文；先过根隔离，再建目录（0700）。"""
    assert_rsi_r1_roots_isolated()
    if rsi_r1_campaign_id() != envelope_id:
        raise CampaignRootRefused(
            f"envelope_id={envelope_id!r} 与 SIMPRAXIS_RSI_R1_CAMPAIGN="
            f"{rsi_r1_campaign_id()!r} 不一致（写路径必须显式对应）")
    root = _resolved(rsi_r1_campaign_root())
    ctx = CampaignContext(envelope_id=envelope_id, root=root,
                          protected_roots=protected_roots_tuple())
    for name in RSI_R1_CAMPAIGN_SUBDIRS:
        ensure_private_dir(root / name)
    return ctx


def load_context(envelope_id: str) -> CampaignContext:
    """加载已初始化的 campaign（不建目录，只读）。"""
    from orchestrator.common.runtime_paths import RsiR1CampaignUnsetError
    try:
        assert_rsi_r1_roots_isolated()
    except RsiR1CampaignUnsetError:
        raise
    root = _resolved(rsi_r1_campaign_root())
    ctx = CampaignContext(envelope_id=envelope_id, root=root,
                          protected_roots=protected_roots_tuple())
    if not (root / "contract" / "scope.json").is_file():
        raise CampaignError(f"campaign 未初始化：{root}/contract/scope.json 不存在")
    return ctx


def adopt_context(envelope_id: str, root: "str | Path") -> CampaignContext:
    """按**显式根路径**装配已初始化 campaign 的上下文（不读 env）。

    AC35「任意退出可离线结案」要求结案工具在 env 缺失/损坏时仍可运行：
    此处只用盘面自证——根必须存在、非符号链接、解析后不漂移、含
    ``contract/scope.json``，且 ``contract/envelope.json`` 若存在其
    ``envelope_id`` 必须与入参一致。写守卫（旧根保护集）与 load_context 同源。
    """
    path = Path(root)
    if not path.is_absolute():
        raise CampaignRootRefused(f"closeout 根必须是绝对路径：{root}")
    if path.is_symlink():
        raise CampaignRootRefused(f"closeout 根是符号链接，拒绝：{path}")
    if not path.is_dir():
        raise CampaignRootRefused(f"closeout 根不存在或不是目录：{path}")
    resolved = _resolved(path)
    if resolved != path:
        raise CampaignRootRefused(f"closeout 根解析后漂移：{path} -> {resolved}")
    if not (resolved / "contract" / "scope.json").is_file():
        raise CampaignError(f"campaign 未初始化：{resolved}/contract/scope.json 不存在")
    envelope_path = resolved / "contract" / "envelope.json"
    if envelope_path.is_file():
        try:
            envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CampaignError(f"contract/envelope.json 不可读：{exc}") from None
        declared = envelope.get("envelope_id") if isinstance(envelope, dict) else None
        if declared is not None and declared != envelope_id:
            raise CampaignRootRefused(
                f"根内 envelope_id={declared!r} 与入参 {envelope_id!r} 不一致")
    protected = protected_roots_tuple()
    family = rsi_r1_root().resolve(strict=False)
    if resolved == family:
        raise CampaignRootRefused(
            f"closeout 根是 R1 家族根本身（{family}），不是 campaign 根：拒绝")
    archived_reason = is_archived_root(resolved)
    if archived_reason is not None:
        raise CampaignRootRefused(
            f"closeout 根已登记为封存旧根（{archive_registry_path(resolved)}）："
            f"旧实验只读，拒绝结案写盘（{archived_reason or '见登记件'}）")
    for name, prot in protected:
        # 双向包含：根落在旧根内、或根**包含**旧根（祖先目录）都拒。
        if resolved == prot or _is_within(resolved, prot) \
                or _is_within(prot, resolved):
            raise CampaignRootRefused(
                f"closeout 根与旧根 {name}（{prot}）相等/嵌套：旧实验只读，拒绝")
    return CampaignContext(envelope_id=envelope_id, root=resolved,
                           protected_roots=protected)


def context_from_doc(doc: Mapping[str, Any]) -> CampaignContext:
    """从持久化的 envelope 文档恢复上下文（不读 env 的根）。"""
    envelope = doc["envelope_id"]
    return CampaignContext(
        envelope_id=envelope,
        root=Path(doc["campaign_root"]),
        protected_roots=protected_roots_tuple(),
    )
