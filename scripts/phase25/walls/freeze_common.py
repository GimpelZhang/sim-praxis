"""Task 5 冻结件公共库（0 付费）。

两面墙的冻结脚本共用：路径真相、闸件哈希清单、原子 0600/0700 落盘、
双引擎密钥扫描、快照登记（只登记不复制：tour/venv 是 /mnt 上数百 MB 的
既有 Phase 2 产物）、冻结清单合段。冻结清单 schema：
``simpraxis.phase25.t5_freeze_manifest.v1``。

首呼锁：run_wall.py 在首个付费调用前写 ``manifests/LOCK.json``；锁后任何
冻结脚本一律拒跑（首呼后不得改字）。
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from orchestrator.common import secrets

REPO_ROOT = Path(__file__).resolve().parents[3]
GATES_DIR = Path(__file__).resolve().parent / "gates"

# 闸件白名单（冻结清单只哈希这些；任何散落在 gates/ 下的其他文件一律不进
# 冻结面，也不进模型容器）。
GATE_FILES = {
    "common": ["VERDICT_CRITERIA.md"],
    "wall_a": ["TASK.md", "GATE.md", "wall_a_runner.py", "expected_vectors.json"],
    "wall_b": ["TASK.md", "GATE.md", "wall_b_checker.py", "SEQ_SPLIT.json"],
}

# GATE.md 内文留痕的哈希必须与实算一致（文档撒谎即 fail-closed）。
EMBEDDED_HASH_RE = __import__("re").compile(
    r"([A-Za-z0-9_./-]+\.(?:md|py|json))\s+sha256\s+`?([0-9a-f]{64})`?"
)

MANIFEST_SCHEMA = "simpraxis.phase25.t5_freeze_manifest.v1"
SECRET_SCAN_SCHEMA = "simpraxis.phase25.t5_secret_scan.v1"
SNAPSHOT_SCHEMA = "simpraxis.phase25.t5_snapshot.v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def freeze_root() -> Path:
    root = os.environ.get("SIMPRAXIS_T5_FREEZE_ROOT")
    if root:
        return Path(root)
    base = os.environ.get("SIMPRAXIS_PHASE25_ROOT")
    if not base:
        raise SystemExit(
            "SIMPRAXIS_PHASE25_ROOT 未设置（先 source scripts/00_env.sh）")
    return Path(base) / "walls" / "t5_freeze"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def gate_hashes() -> dict:
    out: dict = {"common": {}, "wall_a": {}, "wall_b": {}}
    for section, names in GATE_FILES.items():
        for name in names:
            p = GATES_DIR / (name if section == "common" else
                             f"{section}/{name}")
            if not p.is_file():
                raise SystemExit(f"闸件缺失：{p}")
            key = "VERDICT_CRITERIA.md" if section == "common" else name
            out[section if section != "common" else "common"][key] = (
                sha256_file(p))
    return out


def verify_embedded_hashes() -> list[dict]:
    """重算 GATE.md 内文逐字留痕的哈希，不一致即退出。"""
    current = gate_hashes()
    problems: list[str] = []
    rows = []
    for wall in ("wall_a", "wall_b"):
        text = (GATES_DIR / wall / "GATE.md").read_text(encoding="utf-8")
        for fname, embedded in EMBEDDED_HASH_RE.findall(text):
            actual = current[wall].get(fname) or current["common"].get(fname)
            rows.append({"gate": f"{wall}/GATE.md", "file": fname,
                         "embedded": embedded, "actual": actual,
                         "match": actual == embedded})
            if actual != embedded:
                problems.append(f"{wall}/GATE.md 内 {fname} 哈希 "
                                f"{embedded[:12]}… 与实算 {actual[:12]}… 不符")
    if problems:
        raise SystemExit("GATE.md 留痕哈希与闸件不符（fail-closed）：\n  - "
                         + "\n  - ".join(problems))
    return rows


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    ensure_dir(path.parent)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    os.chmod(path, mode)


def write_json(path: Path, obj: dict) -> None:
    atomic_write_bytes(
        path,
        (json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=False)
         + "\n").encode("utf-8"))


def copy_readonly(src: Path, dst: Path) -> dict:
    data = src.read_bytes()
    atomic_write_bytes(dst, data, 0o600)
    return {"path": str(dst), "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data)}


def make_symlink(link_path: Path, target: str) -> None:
    ensure_dir(link_path.parent)
    if link_path.is_symlink() and os.readlink(link_path) == target:
        return
    if link_path.exists() or link_path.is_symlink():
        raise SystemExit(f"目标位置已存在非预期文件：{link_path}")
    os.symlink(target, link_path)


def lock_path(root: Path) -> Path:
    return root / "manifests" / "LOCK.json"


def assert_unlocked(root: Path) -> None:
    if lock_path(root).exists():
        raise SystemExit(
            f"首呼锁已存在：{lock_path(root)}；首呼后冻结件不得改字，拒跑。")


def load_manifest(root: Path) -> dict | None:
    p = root / "manifests" / "freeze_manifest.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def save_manifest(root: Path, manifest: dict) -> None:
    write_json(root / "manifests" / "freeze_manifest.json", manifest)


def merge_section(root: Path, section: str, payload: dict) -> dict:
    """幂等合段：created 保留首次时间，updated 刷新，两段哈希必须一致。"""
    assert_unlocked(root)
    gh = gate_hashes()
    verify_embedded_hashes()
    now = utc_now()
    manifest = load_manifest(root)
    if manifest is None:
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "created_iso": now,
            "updated_iso": now,
            "locked": False,
            "image": {
                "name": "sim-praxis/agentic:phase25-t4",
                "id_prefix": "abf96496a6d7",
                "repo_digest": (
                    "sha256:abf96496a6d72cf4f61c37a3cfe7d657013c1a567f"
                    "cc88b04ac2b616d012fed7"),
            },
            "gate_hashes": gh,
            "sections": {},
        }
    else:
        if manifest.get("gate_hashes") != gh:
            # 另一段先冻结后闸件又被动过：拒绝合段。
            raise SystemExit("闸件哈希与既有冻结清单不符，拒写（重冻须先查"
                             "明改动，首呼前两脚本须重跑）。")
        manifest["updated_iso"] = now
    manifest["sections"][section] = {"frozen_iso": now, **payload}
    save_manifest(root, manifest)
    return manifest


# ---------------------------------------------------------------- 密钥扫描

def _is_probably_text(data: bytes) -> bool:
    return b"\x00" not in data[:4096]


def python_scan_paths(paths: list[Path]) -> dict:
    """引擎二（orchestrator.common.secrets，严口径反向引用版）。

    只记录模式名与计数，**不回写命中串**（命中串本身即疑似密钥）。
    """
    files = []
    hit_total = 0
    for p in paths:
        if not p.is_file() or p.is_symlink():
            continue
        data = p.read_bytes()
        if not _is_probably_text(data):
            files.append({"path": str(p), "scanned": False, "binary": True,
                          "hits": []})
            continue
        text = data.decode("utf-8", "replace")
        hits = [name for name, _match in secrets.scan_text(text)]
        hit_total += len(hits)
        files.append({"path": str(p), "scanned": True, "binary": False,
                      "hit_count": len(hits),
                      "hits": [{"pattern": n} for n in hits]})
    return {"engine": "orchestrator.common.secrets.scan_text",
            "file_count": len(files), "hit_total": hit_total, "files": files}


def shell_scan() -> dict:
    """引擎一（scan_secrets.sh 10 条 ERE，覆盖仓内已跟踪+未跟踪文件）。"""
    proc = subprocess.run(
        ["bash", "scripts/verification/scan_secrets.sh"], cwd=REPO_ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
    out = proc.stdout.decode("utf-8", "replace")
    return {"engine": "scripts/verification/scan_secrets.sh",
            "rc": proc.returncode,
            "hit_lines": [ln for ln in out.splitlines()
                          if "[secret-scan HIT]" in ln]}


def write_secret_report(root: Path, *, scanned_paths: list[Path],
                        scope: str) -> dict:
    shell = shell_scan()
    py = python_scan_paths(scanned_paths)
    report = {
        "schema": SECRET_SCAN_SCHEMA,
        "ts_iso": utc_now(),
        "scope": scope,
        "shell": shell,
        "python": {"engine": py["engine"], "file_count": py["file_count"],
                   "hit_total": py["hit_total"], "files": py["files"]},
    }
    report["clean"] = (shell["rc"] == 0 and not shell["hit_lines"]
                       and py["hit_total"] == 0)
    write_json(root / "snapshots" / "secret_scan.json", report)
    if not report["clean"]:
        raise SystemExit("双引擎密钥扫描命中，冻结中止：\n"
                         + json.dumps({"shell_hits": shell["hit_lines"],
                                       "py_hits": py["hit_total"]},
                                      ensure_ascii=False, indent=1))
    return report


# ---------------------------------------------------------------- 快照登记

def register_tree_hashes(root_dir: Path, subdir: str,
                         *, limit: int | None = None) -> dict:
    base = root_dir / subdir
    entries = []
    total_bytes = 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames.sort()
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if p.is_symlink():
                continue
            rel = str(p.relative_to(root_dir))
            size = p.stat().st_size
            total_bytes += size
            entries.append({"path": rel, "size": size,
                            "sha256": sha256_file(p)})
    if limit is not None and len(entries) > limit:
        raise SystemExit(f"快照登记文件数 {len(entries)} 超上限 {limit}")
    return {"root": str(root_dir), "subdir": subdir, "file_count": len(entries),
            "total_bytes": total_bytes, "entries": entries}


def register_git_worktree(path: Path) -> dict:
    def git(*args: str) -> str:
        proc = subprocess.run(["git", "-C", str(path), *args],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=120)
        return proc.stdout.decode("utf-8", "replace").strip()
    head = git("rev-parse", "HEAD")
    porcelain = git("status", "--porcelain")
    tracked = git("ls-files")
    return {"root": str(path), "head": head,
            "porcelain": porcelain.splitlines() if porcelain else [],
            "tracked_count": len(tracked.splitlines()) if tracked else 0}


def register_venv(path: Path) -> dict:
    binpy = path / "bin" / "python"
    file_count = 0
    total_bytes = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for fn in filenames:
            p = Path(dirpath) / fn
            if not p.is_symlink():
                file_count += 1
                try:
                    total_bytes += p.stat().st_size
                except OSError:
                    pass
    versions = {}
    sitepkgs = list((path / "lib").glob("python3*/site-packages"))
    wanted = {"numpy", "mujoco", "robosuite", "trimesh", "gym", "numba"}
    for sp in sitepkgs:
        for d in sp.glob("*.dist-info"):
            name = d.name.split("-")[0].replace("_", "-").lower()
            if name in wanted:
                versions[name] = d.name.rsplit("-", 1)[-1].replace(
                    ".dist-info", "")
    return {"root": str(path),
            "python_symlink": os.readlink(binpy) if binpy.is_symlink()
            else None,
            "python_realpath": str(Path(os.path.realpath(binpy))),
            "file_count": file_count, "total_bytes": total_bytes,
            "package_versions": versions}


def write_snapshot(root: Path, name: str, payload: dict) -> Path:
    payload = {"schema": SNAPSHOT_SCHEMA, "ts_iso": utc_now(), **payload}
    p = root / "snapshots" / f"{name}.json"
    write_json(p, payload)
    return p
