"""RSI-R0.1 campaign 上下文：不可变根、写守卫与只读 hash 清单（T0）。

计划 §3.1/§3.2 的硬性要求：

* campaign 根一经构造即不可变；所有写入函数接收**显式 context**，不在写时
  重新读 env（杜绝「动态 env 漂移」把写重定向到别的 campaign/旧根）；
* 拒绝新旧根相等、嵌套、符号链接别名以及**解析后重合**；
* 旧根（六份封存期根 + RSI-R0 家族根）只读：任何 R0.1 写路径落进去即拒写；
* 旧根保护声明为「本执行路径的写保护」，不宣称能阻止同 UID 的任意恶意程序。

本模块不触网、不调用模型、不写旧根；只做路径判定与私有目录/文件写入。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import ensure_private_dir
from orchestrator.common.runtime_paths import (
    RSI_R01_CAMPAIGN_SUBDIRS,
    _validate_campaign_id,
    protected_write_roots,
    rsi_r01_root,
    sealed_phase_roots,
)

#: R0.1 schema 前缀（计划 §3.1：``simpraxis.rsi_r01.*.v1``）。
SCHEMA_PREFIX = "simpraxis.rsi_r01"
SCHEMA_VERSION = 1
#: 账本命名空间（与 quota_ledger.LEDGER_RSI_R01 同值；此处只作记录字段）。
LEDGER_NAMESPACE = 2901
CONTEXT_FILENAME = "campaign_context.json"


class CampaignError(RuntimeError):
    """R0.1 campaign 基座错误（上下文缺失/漂移/结构不符）。"""


class CampaignRootRefused(CampaignError):
    """campaign 根与旧根相等/嵌套/符号链接别名，或根含不安全字符。"""


class CampaignWriteRefused(CampaignError):
    """写路径越界：落在 campaign 根外、旧根内、或经由符号链接。"""


def _utc_now() -> str:
    # 与既有账本同口径：严格 UTC Z。
    return utc_now_iso_z()


def sha256_file(path: "str | Path") -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolved(path: "str | Path") -> Path:
    return Path(path).resolve(strict=False)


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def assert_root_isolated(campaign_root: "str | Path",
                         protected: "Mapping[str, Path] | None" = None) -> Path:
    """campaign 根与全部旧根两两互异、互不嵌套（解析后），否则 fail-fast。

    与 runtime_paths._assert_roots_isolated 同一判定强度（相等 + 双向嵌套）；
    这里额外把「旧根之间也两两互查」保留下来（旧根清单自身若被 env 指重，
    同样拒绝），并在报错里给出可读的具名根。
    """
    roots = dict(protected_write_roots() if protected is None else protected)
    target = _resolved(campaign_root)
    candidates = [(name, _resolved(p)) for name, p in roots.items()]
    for name, p in candidates:
        if target == p:
            raise CampaignRootRefused(
                f"R0.1 campaign 根与旧根同名同体（{name}={p}）：旧实验根只读，"
                "新 campaign 绝不落进去")
        if _is_within(target, p):
            raise CampaignRootRefused(
                f"R0.1 campaign 根嵌套在旧根 {name}（{p}）内：拒绝（AC02/AC03）")
        if _is_within(p, target):
            raise CampaignRootRefused(
                f"旧根 {name}（{p}）嵌套在 R0.1 campaign 根内：拒绝（AC02/AC03）")
    for i, (name_a, a) in enumerate(candidates):
        for name_b, b in candidates[i + 1:]:
            if a == b or _is_within(a, b) or _is_within(b, a):
                raise CampaignRootRefused(
                    f"旧根本身互相重合/嵌套（{name_a} 与 {name_b}）："
                    "无法判定写保护边界，拒绝构造 R0.1 campaign")
    return target


def _assert_no_symlink_components(path: Path, *, stop: Path) -> None:
    """path（到 stop 为止的既有组件）不得含符号链接。

    符号链接别名是 AC02 的显式反例：``rsi_r01 -> ../../rsi_r0`` 会让
    resolve() 之后看起来「在根内」，但写入实际落进旧根。
    """
    cur = Path(path)
    chain: list[Path] = []
    while True:
        chain.append(cur)
        if cur == stop or cur.parent == cur:
            break
        cur = cur.parent
    for p in reversed(chain):
        try:
            st = p.lstat()
        except FileNotFoundError:
            continue
        import stat as _stat

        if _stat.S_ISLNK(st.st_mode):
            raise CampaignWriteRefused(
                f"写路径含符号链接组件（拒绝别名写入）：{p}")


@dataclass(frozen=True)
class CampaignContext:
    """不可变 campaign 上下文：路径、命名空间、批准绑定与旧根快照。

    构造只经 ``create_campaign`` / ``load_campaign``；字段全冻结，写时不再读
    env（写守卫只用本对象的 ``root`` 与 ``protected_roots`` 快照）。
    """

    campaign_id: str
    root: Path
    namespace: int
    created_at_utc: str
    plan_path: str
    plan_sha256: str
    code_commit: str
    consent_sha256: str
    protected_roots: "tuple[tuple[str, Path], ...]"
    schema_prefix: str = SCHEMA_PREFIX
    schema_version: int = SCHEMA_VERSION

    # ---- 序列化 ----

    def to_json(self) -> "dict[str, Any]":
        return {
            "schema": f"{self.schema_prefix}.campaign_context.v{self.schema_version}",
            "campaign_id": self.campaign_id,
            "root": str(self.root),
            "namespace": self.namespace,
            "created_at_utc": self.created_at_utc,
            "plan_path": self.plan_path,
            "plan_sha256": self.plan_sha256,
            "code_commit": self.code_commit,
            "consent_sha256": self.consent_sha256,
            "protected_roots": {name: str(p) for name, p in self.protected_roots},
        }

    @classmethod
    def from_json(cls, obj: "Mapping[str, Any]", *, expected_root: Path) -> "CampaignContext":
        required = ("campaign_id", "root", "namespace", "created_at_utc",
                    "plan_path", "plan_sha256", "code_commit", "consent_sha256",
                    "protected_roots")
        missing = [k for k in required if k not in obj]
        if missing:
            raise CampaignError(f"campaign_context 缺字段：{missing}")
        if Path(obj["root"]) != expected_root:
            raise CampaignError(
                f"campaign_context 的根与期望不一致（漂移拒绝）："
                f"{obj['root']} != {expected_root}")
        roots = tuple(sorted((str(k), Path(v)) for k, v in obj["protected_roots"].items()))
        return cls(
            campaign_id=str(obj["campaign_id"]),
            root=Path(obj["root"]),
            namespace=int(obj["namespace"]),
            created_at_utc=str(obj["created_at_utc"]),
            plan_path=str(obj["plan_path"]),
            plan_sha256=str(obj["plan_sha256"]),
            code_commit=str(obj["code_commit"]),
            consent_sha256=str(obj["consent_sha256"]),
            protected_roots=roots,
        )

    # ---- 路径 ----

    def subdir(self, name: str) -> Path:
        if name not in RSI_R01_CAMPAIGN_SUBDIRS:
            raise CampaignError(f"未知 R0.1 campaign 子目录：{name!r}")
        return self.root / name

    def relative(self, relpath: "str | Path") -> Path:
        """campaign 根内相对路径 → 绝对路径（仅做词法拼接；写前须过守卫）。"""
        rel = Path(relpath)
        if rel.is_absolute() or ".." in rel.parts:
            raise CampaignWriteRefused(f"只接受 campaign 根内相对路径：{rel}")
        return self.root / rel

    # ---- 写守卫 ----

    def assert_write_allowed(self, target: "str | Path") -> Path:
        """写前守卫：必须落在本 campaign 根内，且不落旧根、不经符号链接。"""
        if self.root.is_symlink():
            raise CampaignWriteRefused(f"campaign 根已变成符号链接，拒绝写入：{self.root}")
        if _resolved(self.root) != self.root:
            raise CampaignWriteRefused(
                f"campaign 根解析结果漂移（构造后被替换）：{self.root} -> "
                f"{_resolved(self.root)}；写时拒绝（AC02）")
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

    def write_json(self, relpath: "str | Path", obj: Any) -> Path:
        return write_json(self, relpath, obj)

    def write_text(self, relpath: "str | Path", text: str) -> Path:
        return write_text(self, relpath, text)


def _atomic_write(path: Path, text: str) -> Path:
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


def write_text(ctx: CampaignContext, relpath: "str | Path", text: str) -> Path:
    target = ctx.assert_write_allowed(ctx.relative(relpath))
    return _atomic_write(target, text)


def write_json(ctx: CampaignContext, relpath: "str | Path", obj: Any) -> Path:
    text = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return write_text(ctx, relpath, text)


def read_json(path: "str | Path") -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover - 环境故障
        raise CampaignError(f"无法解析代码提交（拒绝无绑定实施）：{exc}") from exc
    return out.stdout.strip()


def create_campaign(*, campaign_id: str, plan_path: "str | Path",
                    consent_path: "str | Path", repo_root: "str | Path",
                    root: "str | Path | None" = None,
                    now: "str | None" = None) -> CampaignContext:
    """创建（或幂等加载）一个新 campaign：目录 0700、上下文 0600。

    拒绝：
    * 非法 campaign id / 根与旧根重合或嵌套（AC02）；
    * plan 或 consent 文件缺失（AC01：无授权不实施）；
    * 根已存在且非空，但其中没有与本调用一致的上下文（拒绝覆盖别人的 campaign）。
    """
    cid = _validate_campaign_id(campaign_id,
                                error_cls=CampaignRootRefused, label="R0.1")
    base = Path(root) if root is not None else rsi_r01_root()
    campaign_root = _resolved(base / cid)
    assert_root_isolated(campaign_root)

    plan = Path(plan_path)
    consent = Path(consent_path)
    if not plan.is_file():
        raise CampaignError(f"计划文件不存在（G0 未满足）：{plan}")
    if not consent.is_file():
        raise CampaignError(f"授权（consent）文件不存在（G0 未满足）：{consent}")
    plan_sha = sha256_file(plan)
    consent_sha = sha256_file(consent)
    commit = _git_commit(Path(repo_root))

    ctx = CampaignContext(
        campaign_id=cid,
        root=campaign_root,
        namespace=LEDGER_NAMESPACE,
        created_at_utc=now or _utc_now(),
        plan_path=str(plan.resolve()),
        plan_sha256=plan_sha,
        code_commit=commit,
        consent_sha256=consent_sha,
        protected_roots=tuple(sorted((k, _resolved(v))
                                     for k, v in protected_write_roots().items())),
    )

    ctx_file = campaign_root / "contract" / CONTEXT_FILENAME
    if campaign_root.exists() and any(campaign_root.iterdir()):
        if not ctx_file.is_file():
            raise CampaignError(
                f"campaign 根已存在且非空但无上下文（拒绝覆盖）：{campaign_root}")
        existing = CampaignContext.from_json(read_json(ctx_file),
                                             expected_root=campaign_root)
        if (existing.plan_sha256, existing.consent_sha256,
                existing.code_commit) != (plan_sha, consent_sha, commit):
            raise CampaignError(
                "campaign 已存在且上下文与本调用不一致（计划/授权/代码提交漂移）；"
                "拒绝在既有 campaign 上改写上下文（最终 LOCK 前应新开 campaign）")
        return existing

    ensure_private_dir(campaign_root)
    for name in RSI_R01_CAMPAIGN_SUBDIRS:
        ensure_private_dir(campaign_root / name)
    # 上下文写入必须过守卫（构造点即验证一次判定链）。
    write_json(ctx, f"contract/{CONTEXT_FILENAME}", ctx.to_json())
    return ctx


def load_campaign(*, campaign_id: str, root: "str | Path | None" = None
                  ) -> CampaignContext:
    """按 id 加载既有 campaign 上下文（并重跑隔离判定；漂移即拒）。"""
    cid = _validate_campaign_id(campaign_id,
                                error_cls=CampaignRootRefused, label="R0.1")
    base = Path(root) if root is not None else rsi_r01_root()
    campaign_root = _resolved(base / cid)
    assert_root_isolated(campaign_root)
    ctx_file = campaign_root / "contract" / CONTEXT_FILENAME
    if not ctx_file.is_file():
        raise CampaignError(f"campaign 上下文缺失：{ctx_file}")
    return CampaignContext.from_json(read_json(ctx_file),
                                     expected_root=campaign_root)


# ---- 只读 hash 清单（AC03 前后对照；不写旧根） ----

def hash_tree(root: "str | Path") -> "dict[str, Any]":
    """逐件 sha256 清单：symlink 一律记录为违规，不跟随（H-11 口径）。"""
    base = Path(root)
    if not base.exists():
        return {"root": str(base), "exists": False, "n_files": 0,
                "total_bytes": 0, "manifest_sha256": None, "entries": [],
                "symlinks": []}
    entries: list[dict[str, Any]] = []
    symlinks: list[str] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            p = Path(dirpath) / name
            rel = str(p.relative_to(base))
            if p.is_symlink():
                symlinks.append(rel)
                continue
            st = p.stat()
            total += st.st_size
            entries.append({"path": rel, "sha256": sha256_file(p),
                            "size": st.st_size})
    manifest = json.dumps(entries, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
    return {
        "root": str(base.resolve(strict=False)),
        "exists": True,
        "n_files": len(entries),
        "total_bytes": total,
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "entries": entries,
        "symlinks": symlinks,
    }


def hash_files(paths: Iterable["str | Path"]) -> "dict[str, Any]":
    """少量受保护文件的 hash 清单（封存期 quota 族用；不遍历大树）。"""
    entries: list[dict[str, Any]] = []
    missing: list[str] = []
    for p in paths:
        path = Path(p)
        if not path.exists():
            missing.append(str(path))
            continue
        st = path.stat()
        entries.append({"path": str(path), "sha256": sha256_file(path),
                        "size": st.st_size})
    return {"entries": entries, "missing": missing,
            "manifest_sha256": hashlib.sha256(
                json.dumps(entries, sort_keys=True).encode("utf-8")).hexdigest()}


def compare_tree(root: "str | Path", manifest: "Mapping[str, Any]") -> "list[str]":
    """当前树 vs 已存清单：返回差异描述（空列表=逐件不变）。"""
    diff: list[str] = []
    base = Path(root)
    old = {e["path"]: e for e in manifest.get("entries", [])}
    now = {e["path"]: e for e in hash_tree(base)["entries"]}
    for rel in sorted(set(old) - set(now)):
        diff.append(f"missing: {rel}")
    for rel in sorted(set(now) - set(old)):
        diff.append(f"added: {rel}")
    for rel in sorted(set(old) & set(now)):
        if old[rel]["sha256"] != now[rel]["sha256"]:
            diff.append(f"changed: {rel}")
    if manifest.get("symlinks"):
        diff.append(f"symlinks recorded in manifest: {manifest['symlinks']}")
    return diff


def phase_protected_files() -> "list[Path]":
    """六份封存期根里 R0.1 必须保护的台账族文件（只读 hash 用）。"""
    names = ("ledger.jsonl", "counters.json", "attempts.jsonl")
    files: list[Path] = []
    for name, root in sealed_phase_roots().items():
        quota = Path(root) / "quota"
        for fn in names:
            files.append(quota / fn)
    return files


SEALED_SCHEMA = f"{SCHEMA_PREFIX}.sealed_roots.v1"


def snapshot_protected() -> "dict[str, Any]":
    """旧实验只读快照：R0 全树逐件 hash + 六份封存期台账族 hash（不写旧根）。"""
    from orchestrator.common.runtime_paths import rsi_r0_root

    r0_root = Path(rsi_r0_root())
    phase_files: "dict[str, Any]" = {}
    for name, root in sealed_phase_roots().items():
        phase_files[name] = hash_files(
            [Path(root) / "quota" / fn
             for fn in ("ledger.jsonl", "counters.json", "attempts.jsonl")])
    return {
        "schema": SEALED_SCHEMA,
        "recorded_at_utc": _utc_now(),
        "rsi_r0": {"root": str(r0_root.resolve(strict=False)),
                   "tree": hash_tree(r0_root)},
        "phase_ledgers": phase_files,
        "note": ("只读 hash 清单：R0 家族根整树 + 六份封存期台账族。"
                 "本保护是本执行路径的写守卫，不宣称能阻止同 UID 任意恶意程序。"),
    }


def verify_protected(snapshot: "Mapping[str, Any]") -> "list[str]":
    """重算并与快照比对；返回差异（空=逐件不变，AC03/AC36）。"""
    from orchestrator.common.runtime_paths import rsi_r0_root

    diff: list[str] = []
    r0 = snapshot.get("rsi_r0") or {}
    r0_root = Path(r0.get("root") or rsi_r0_root())
    tree = r0.get("tree") or {}
    if not r0_root.exists():
        diff.append(f"missing old root: {r0_root}")
    else:
        diff.extend(f"rsi_r0/{d}" for d in compare_tree(r0_root, tree))
    for name, rec in (snapshot.get("phase_ledgers") or {}).items():
        now = hash_files([Path(e["path"]) for e in rec.get("entries", [])])
        old = {e["path"]: e["sha256"] for e in rec.get("entries", [])}
        cur = {e["path"]: e["sha256"] for e in now["entries"]}
        for path in sorted(set(old) - set(cur)):
            diff.append(f"{name}: missing {path}")
        for path in sorted(set(cur) - set(old)):
            diff.append(f"{name}: added {path}")
        for path in sorted(set(old) & set(cur)):
            if old[path] != cur[path]:
                diff.append(f"{name}: changed {path}")
        for path in now["missing"]:
            diff.append(f"{name}: missing {path}")
    return diff
