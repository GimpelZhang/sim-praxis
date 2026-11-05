#!/usr/bin/env python3
"""Phase 2.8.0 Task 7 证据包构建（0 付费、只读源、`--clean` 从零重建）。

落 ``<bench_root>/evidence_pack/``（可用 ``--pack`` 指定，但目标必须仍是
``<bench_root>/evidence_pack``，见 I11）。纪律（D7-1/D7-2/D7-3）：

- **三个逻辑根**：``@bench/``（数据根十个证据目录）、``@runs/``
  （``$SIMPRAXIS_RUN_DIR/benchmark``，可缺省）、``@repo/``（引擎仓的
  benchmark 相关子树）；包内路径一律相对，不写死绝对路径；
- **逐件 sha256 + 字节数**；目录 0700 / 文件 0600；符号链接 FATAL；
  单件 > 10 MiB 拒收；密钥正则零命中；
- **先排除解析后的 pack 目标目录**（否则 ``--clean`` 重建把上一版包吞回去，
  不动点必破），再按**族匹配**排除判决件
  （``**/closure_judge*.json``、``**/acceptance_matrix*.json``、
  ``**/signs/*.json``）；
- ``manifest.json`` 不登记自身；``pack.sha256`` 存 ``manifest.json`` 的
  sha256（判决件三签绑它）；
- ``private/`` 允许入包但逐条打 ``sensitivity:"private"``；证据包本体落
  ``/mnt`` 闭源，不入 git。

用法：
  python scripts/benchmark/build_evidence_pack.py --root R --pack DIR [--clean]
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from orchestrator.common import secrets as secret_scan  # noqa: E402

SCHEMA = "simpraxis.benchmark.evidence_pack.v1"

MAX_FILE_BYTES = 10 * 1024 * 1024

#: D7-2：``@bench/`` 的遍历范围（十个一级目录；``evidence_pack/`` 与
#: ``closeout/`` 刻意不在其中——前者是包自身，后者是判决件）。
BENCH_DIRS = ("intake", "specs", "fingerprints", "preregistration", "runs",
              "negative_controls", "evaluator_results", "reports",
              "references", "private")

#: ``@repo/`` 的范围：只打 benchmark 五层代码与规格，不打整仓。
REPO_PATHS = ("benchmarks", "orchestrator/benchmark", "scripts/benchmark",
              "tests/benchmark", "tests/fixtures/benchmark")

SKIP_DIR_NAMES = {"__pycache__", ".pytest_cache", ".omc", ".git",
                  ".DS_Store", "node_modules"}
SKIP_SUFFIXES = (".pyc", ".pyo")


class PackError(RuntimeError):
    """缺件 / 越界 / 超限 / 软链 / 密钥命中；CLI exit 2，绝不产出半包。"""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git(args: "list[str]") -> str:
    proc = subprocess.run(["git", *args], cwd=_REPO, capture_output=True,
                          text=True, timeout=120)
    if proc.returncode != 0:
        raise PackError(f"git {' '.join(args)} 失败：{proc.stderr.strip()}")
    return proc.stdout.strip()


def is_judgment_relpath(relpath: str) -> bool:
    """判决件族（D7-3 ②）：族匹配而非字面名。"""
    parts = Path(relpath).parts
    name = parts[-1] if parts else ""
    if fnmatch.fnmatch(name, "closure_judge*.json"):
        return True
    if fnmatch.fnmatch(name, "acceptance_matrix*.json"):
        return True
    if "signs" in parts[:-1] and name.endswith(".json"):
        return True
    return False


def _check_clean_target(pack: Path, root: Path) -> None:
    """``--clean`` 的唯一删除入口（I11）。

    规则同时收紧到「``<root>/evidence_pack`` 这一处」：只比较「在根下且叫
    evidence_pack」会放行 ``<root>/sibling/evidence_pack`` 这类兄弟目录。
    """
    rp = pack.resolve(strict=False)
    rr = root.resolve(strict=False)
    if rp == rr:
        raise PackError(f"--clean 拒绝删除数据根本身：{rp}")
    if rr not in rp.parents:
        raise PackError(f"--clean 目标越出数据根：{rp} 不在 {rr} 之下")
    if rp.name != "evidence_pack":
        raise PackError(f"--clean 只允许删名为 evidence_pack 的目录：{rp}")
    if rp.parent != rr:
        raise PackError(f"--clean 只允许删 <root>/evidence_pack，目标为：{rp}")


def _iter_files(base: Path, *, skip_dirs: "set[str]" = None,
                exclude: "set[Path]" = None) -> "list[Path]":
    """安全遍历：不跟随链接；软链 / 非普通文件 / 超限即 PackError。"""
    skip_dirs = skip_dirs or set()
    excluded = {p.resolve(strict=False) for p in (exclude or set())}
    out: "list[Path]" = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dpath = Path(dirpath)
        keep = []
        for name in sorted(dirnames):
            child = dpath / name
            if name in SKIP_DIR_NAMES or name in skip_dirs:
                continue
            if child.is_symlink():
                raise PackError(f"目录符号链接不入包：{child}")
            if child.resolve(strict=False) in excluded:
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            path = dpath / name
            if name in SKIP_DIR_NAMES or name.endswith(SKIP_SUFFIXES):
                continue
            if path.is_symlink():
                raise PackError(f"文件符号链接不入包（会跟随出界）：{path}")
            st = path.stat()
            if not stat.S_ISREG(st.st_mode):
                raise PackError(f"非普通文件不入包：{path}")
            if st.st_size > MAX_FILE_BYTES:
                raise PackError(f"单件 {st.st_size} B 超 10 MiB：{path}")
            out.append(path)
    return out


def _entry(relpath: str, source: Path, logical: str, root_tag: str,
           sensitivity: "str | None") -> dict:
    return {
        "relpath": relpath,
        "origin": "copy",
        "source": logical,
        "size": int(source.stat().st_size),
        "sha256": _sha256_file(source),
        "logical_root": root_tag,
        "sensitivity": sensitivity,
    }


_TOKEN_PREFIXES = ("hf_", "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "nvapi-",
                   "ark-", "sk-", "cli_", "xoxb-", "xoxp-", "xoxa-", "xoxr-",
                   "xoxs-")


def _is_synthetic_token(matched: str) -> bool:
    """合成哨兵判定：token 主体是**单一字符的整段重复**（如 aaaa…/bbbb…）。

    单测里 ``test_scan_detects_all_strict_forms[hf_token-hf_aaaa…]`` 这类
    pytest nodeid 会把假令牌原样打进命令日志；它们没有熵、不是任何真实
    凭据形态。判定只认「去掉已知前缀后整段同一字符且 ≥ 20 位」——真实
    令牌（含多字符混合）一律仍判 FATAL，口径不放宽。
    """
    body = str(matched)
    for prefix in _TOKEN_PREFIXES:
        if body.startswith(prefix):
            body = body[len(prefix):]
            break
    core = body.strip("\"'")
    return len(core) >= 20 and len(set(core)) == 1


#: 扫描器自测的 pytest nodeid 标记：这些参数**就是** 10 条严格正则的
#: 假样本，行为「造得像真密钥」；只有出现在该测试的参数括号里才算哨兵。
FIXTURE_SELFTEST = "test_scan_detects_all_strict_forms["


def _scan_secrets(pack: Path) -> "tuple[list[str], list[str]]":
    """返回 (真实命中, 合成哨兵豁免)。豁免逐条登记进 manifest，不隐去。

    豁免只认两种**可复核**形态：① 命中行是扫描器自测的 nodeid
    （``test_scan_detects_all_strict_forms[...]``）；② 令牌主体是单一字符
    整段重复。二者之外的命中一律 FATAL，口径不放宽。
    """
    hits: "list[str]" = []
    synthetic: "list[str]" = []
    for path in sorted(pack.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            continue
        rel = path.relative_to(pack).as_posix()
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            found = secret_scan.scan_text(line)
            if not found:
                continue
            if FIXTURE_SELFTEST in line:
                for name, _matched in found:
                    synthetic.append(f"{rel}::{name}::L{lineno}")
            else:
                for name, matched in found:
                    if _is_synthetic_token(matched):
                        synthetic.append(f"{rel}::{name}::L{lineno}")
                    else:
                        hits.append(f"{rel}::{name}::{str(matched)[:24]}")
    return hits, synthetic


def _normalize_modes(pack: Path) -> dict:
    dirs = files = 0
    for dirpath, _dirnames, filenames in os.walk(pack):
        base = Path(dirpath)
        os.chmod(base, 0o700)
        dirs += 1
        for name in filenames:
            path = base / name
            if path.is_symlink():
                raise PackError(f"包内出现符号链接：{path}")
            os.chmod(path, 0o600)
            files += 1
    return {"dirs_0700": dirs, "files_0600": files}


def build(*, root: Path, pack: Path, clean: bool = False,
          bench_dirs: "tuple[str, ...] | None" = None,
          repo_paths: "tuple[str, ...] | None" = None,
          include_runs_root: bool = True,
          repo: "Path | None" = None) -> dict:
    root = Path(root)
    pack = Path(pack)
    if pack.resolve(strict=False).parent != root.resolve(strict=False):
        raise PackError(f"pack 必须位于 <root>/evidence_pack：{pack}")
    if clean:
        _check_clean_target(pack, root)
        if pack.exists():
            shutil.rmtree(pack)
    pack.mkdir(parents=True, exist_ok=True)
    os.chmod(pack, 0o700)

    files: "list[dict]" = []
    roots: dict = {}

    def add(source: Path, relpath: str, logical: str, root_tag: str,
            sensitivity: "str | None" = None) -> None:
        if is_judgment_relpath(relpath):
            return
        dst = pack / relpath
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dst)
        os.chmod(dst, 0o600)
        entry = _entry(relpath, dst, logical, root_tag, sensitivity)
        entry["origin"] = "@bench/private" if sensitivity else "copy"
        files.append(entry)

    # 1) @bench/ 十个证据目录
    for name in (bench_dirs if bench_dirs is not None else BENCH_DIRS):
        base = root / name
        if not base.exists():
            roots[f"@bench/{name}"] = {"path": str(base), "present": False,
                                       "files": 0}
            continue
        if base.is_symlink():
            raise PackError(f"@bench/{name} 是符号链接：{base}")
        picked = _iter_files(base, exclude={pack})
        for source in picked:
            rel = source.relative_to(root).as_posix()
            add(source, rel, f"@bench/{rel}", f"@bench/{name}",
                "private" if name == "private" else None)
        roots[f"@bench/{name}"] = {"path": str(base), "present": True,
                                   "files": len(picked)}

    # 2) @runs/（SIMPRAXIS_RUN_DIR/benchmark；按计划当前可缺省 → 0 件入账）
    if include_runs_root:
        raw = os.environ.get("SIMPRAXIS_RUN_DIR")
        runs_base = Path(raw) / "benchmark" if raw else None
        if runs_base is not None and runs_base.exists():
            picked = _iter_files(runs_base)
            for source in picked:
                rel_in = source.relative_to(runs_base).as_posix()
                add(source, f"runs_benchmark/{rel_in}",
                    f"@runs/benchmark/{rel_in}", "@runs/benchmark")
            roots["@runs/benchmark"] = {"path": str(runs_base), "present": True,
                                        "files": len(picked)}
        else:
            roots["@runs/benchmark"] = {"path": str(runs_base) if runs_base else None,
                                        "present": False, "files": 0,
                                        "note": "SIMPRAXIS_RUN_DIR/benchmark 不存在，0 件"}

    # 3) @repo/（只打 benchmark 子树）
    repo_dir = Path(repo) if repo else _REPO
    for rel_base in (repo_paths if repo_paths is not None else REPO_PATHS):
        base = repo_dir / rel_base
        if not base.exists():
            roots[f"@repo/{rel_base}"] = {"path": str(base), "present": False,
                                          "files": 0}
            continue
        picked = _iter_files(base)
        for source in picked:
            rel_in = source.relative_to(repo_dir).as_posix()
            add(source, f"repo/{rel_in}", f"@repo/{rel_in}",
                f"@repo/{rel_base}")
        roots[f"@repo/{rel_base}"] = {"path": str(base), "present": True,
                                      "files": len(picked)}

    # 4) manifest（不登记自身）+ pack.sha256
    files.sort(key=lambda row: row["relpath"])
    manifest_doc = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_commit": _git(["rev-parse", "HEAD"]),
        "git_dirty": bool(_git(["status", "--porcelain"])),
        "bench_root": str(root),
        "pack": str(pack),
        "logical_roots": roots,
        "files": files,
        "n_files": len(files),
        "total_bytes": int(sum(row["size"] for row in files)),
        "policy": {
            "max_file_bytes": MAX_FILE_BYTES,
            "no_symlinks": True,
            "clean_rebuild": bool(clean),
            "modes": {"dirs": "0700", "files": "0600"},
            "excluded": ["<pack 目标目录自身>",
                         "**/closure_judge*.json",
                         "**/acceptance_matrix*.json",
                         "**/signs/*.json",
                         "closeout/（不在 @bench 遍历范围）"],
            "pack_sha256_meaning": "manifest.json 的 sha256（本文件不含自身）",
        },
    }
    manifest_path = pack / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_doc, ensure_ascii=False,
                                        indent=1, sort_keys=True) + "\n",
                             encoding="utf-8")
    os.chmod(manifest_path, 0o600)
    digest = _sha256_file(manifest_path)
    (pack / "pack.sha256").write_text(digest + "\n", encoding="utf-8")
    os.chmod(pack / "pack.sha256", 0o600)

    # 5) 四查：超限 / 软链 / 密钥 / 权限（失败即异常，不返回半包摘要）
    big = [row["relpath"] for row in files if row["size"] > MAX_FILE_BYTES]
    if big:
        raise PackError(f"包内出现 >10 MiB 件：{big}")
    links = [p.relative_to(pack).as_posix() for p in pack.rglob("*")
             if p.is_symlink()]
    if links:
        raise PackError(f"包内出现符号链接：{links}")
    hits, synthetic = _scan_secrets(pack)
    if hits:
        raise PackError(f"包内命中密钥正则 {len(hits)} 处：{hits[:3]}")
    modes = _normalize_modes(pack)
    manifest_doc["policy"]["secrets"] = {
        "strict_hits": 0,
        "synthetic_fixture_skipped": len(synthetic),
        "synthetic_entries": sorted(set(synthetic)),
        "rule": "仅两种豁免：命中行是扫描器自测 nodeid"
                "（test_scan_detects_all_strict_forms[...]），或令牌主体整段"
                "同一字符且 ≥20 位；其余一律 FATAL",
    }
    manifest_doc["policy"]["modes"].update(modes)
    manifest_path.write_text(json.dumps(manifest_doc, ensure_ascii=False,
                                        indent=1, sort_keys=True) + "\n",
                             encoding="utf-8")
    os.chmod(manifest_path, 0o600)
    digest = _sha256_file(manifest_path)
    (pack / "pack.sha256").write_text(digest + "\n", encoding="utf-8")
    os.chmod(pack / "pack.sha256", 0o600)
    return {"manifest": str(manifest_path), "pack_sha256": digest,
            "n_files": len(files),
            "total_bytes": int(sum(row["size"] for row in files)),
            "roots": roots}


def main(argv: "list[str] | None" = None) -> int:
    from orchestrator.benchmark import paths

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=None,
                        help="数据根（缺省走 orchestrator.benchmark.paths）")
    parser.add_argument("--pack", default=None,
                        help="证据包目录（缺省 <root>/evidence_pack）")
    parser.add_argument("--clean", action="store_true", help="先删后建（唯一删除入口）")
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else paths.bench_root()
    pack = Path(args.pack) if args.pack else root / "evidence_pack"
    try:
        result = build(root=root, pack=pack, clean=args.clean)
    except PackError as exc:
        print(f"PACK ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
