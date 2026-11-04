#!/usr/bin/env python3
"""S0：只读核对候选 VLA（Phase 2.7 Task 6 / 计划 §T6.4-A，0 付费、不下载权重）。

做三件事，全部是**只读**动作（HF 镜像 API 元数据 + 本地缓存清单）：

1. 每个候选落 `repo_commit`（40 hex）、`license`、`params`、`weights_sha256`
   （64 hex，单文件权重时才可直接取；分片模型写 null + `unverified`）、
   `requires`、`libero_compat`；
2. 依据 `safetensors` 元数据算 `vram_fp16` / `vram_int4`（**只算权重字节**，
   渲染/上下文/碎片在 `vram_budget.md` 里另算）；
3. 写 `vram_budget.md`：按 A800-40GB 逐项列峰值预估并给判定（D6-1b：
   > 32 GB ⇒ 该候选默认 4-bit 或改用更小模型）。

纪律：
- **禁止编造**。拿不到的字段一律 `null` 并在 `unverified` 列表里逐项点名；
- 不下载权重：只读 `/api/models/{id}`（元数据）与 `/api/models/{id}/tree/{rev}`
  （文件名 + LFS sha256 + 字节数），两者都是 KB 级响应；
- 本地缓存命中时用本地文件复算 sha256，与远端 LFS oid **互证**（不一致即报错）；
- 0 付费：本脚本不碰网关、不写 ledger。

用法：
    source scripts/00_env.sh
    /mnt/venvs/simpraxis/bin/python scripts/phase27/s0_vla_candidates.py \
        --out-dir /mnt/artifacts/simpraxis/phase27/task6_vla/s0
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 选择顺序钉死（计划 §T6.5 步骤 2 / AC-T6-8）：轻量优先，不得开跑后改序。
STRATEGY_ORDER = ["octo-small", "openvla-7b-int4", "openvla-7b-fp16"]

# 计划 §T6.3 D6-1b：A800-40GB，留 8 GB 安全垫 ⇒ 峰值 > 32 GB 即判「默认 4-bit
# 或换更小模型」。
VRAM_LIMIT_GB = 32.0
GPU_TOTAL_GB = 40.0

# 非权重项的会计口径（逐项在 vram_budget.md 里写明来源）：
CUDA_CONTEXT_GB = 1.2      # CUDA context + cuDNN/cuBLAS 句柄（A800 实测 0.9–1.3）
RENDER_GB = 0.8            # MuJoCo EGL 离屏 1080p + 单个 scene（Phase 1 实测口径）
ACTIVATION_GB = 1.5        # 单步推理激活峰值（batch=1，VLA 视觉编码器）
FRAGMENT_FRACTION = 0.25   # PyTorch caching allocator 碎片余量（保守 25%）

CANDIDATES = [
    {
        "name": "octo-small",
        "model_id": "rail-berkeley/octo-small-1.5",
        "repo_url": "https://github.com/octo-models/octo",
        "github_repo": "octo-models/octo",
        "policy_form": "octo-small",
        "note": "主力轻量候选（≈27M 参数）；LIBERO 兼容性未在官方文档中声明",
    },
    {
        "name": "openvla-7b",
        "model_id": "openvla/openvla-7b",
        "repo_url": "https://github.com/openvla/openvla",
        "github_repo": "openvla/openvla",
        "policy_form": "openvla-7b",
        "note": "主策略候选；分片权重，fp16 ≈14 GB，4-bit ≈3.5 GB",
    },
]

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA64 = re.compile(r"^[0-9a-f]{64}$")

# 非 safetensors 权重识别：≥ 50 MB 且后缀/文件名像权重（Octo 1.5 用单一
# `300000/default/checkpoint`，546,696,551 B）。
MIN_WEIGHT_BYTES = 50 * 1024 * 1024
_WEIGHT_EXTS = (".pt", ".pth", ".bin", ".ckpt", ".msgpack", ".npz", ".h5")

# README 参数量文档声明：如 Octo「a Transformer with 27M parameters」。
_PARAMS_DOC = re.compile(r"(\d+(?:\.\d+)?)\s*([MB])\s+parameters", re.IGNORECASE)


def _is_weight_like(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return name == "checkpoint" or name.endswith(_WEIGHT_EXTS)


def _endpoint() -> str:
    return (os.environ.get("HF_ENDPOINT") or "https://hf-mirror.com").rstrip("/")


class _Redirect307308(urllib.request.HTTPRedirectHandler):
    """hf-mirror 对 API 路径回 308（→ huggingface.co）；py3.10 的
    ``HTTPRedirectHandler`` 只挂了 301/302/303/307 的 ``http_error_*``，
    308 会一路落到 HTTPErrorProcessor 抛错。这里把 308 显式接到 307 的实现上
    （308 的语义就是「永久 + 方法与体不变」）。"""

    def http_error_308(self, req, fp, code, msg, headers):
        return self.http_error_307(req, fp, 307, msg, headers)


_OPENER = urllib.request.build_opener(_Redirect307308)


def _get_json(url: str, *, timeout: float = 60.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "simpraxis-s0/1"})
    with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _get_text(url: str, *, timeout: float = 60.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "simpraxis-s0/1"})
    with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read().decode("utf-8", errors="replace")


def _local_cache_candidates(model_id: str) -> list[Path]:
    """返回本地 HF cache 里该模型的 snapshot 目录（只读，不建目录）。"""
    roots = []
    for env in ("HF_HOME", "HUGGINGFACE_HUB_CACHE"):
        val = os.environ.get(env)
        if val:
            roots.append(Path(val) / "hub" if env == "HF_HOME" else Path(val))
    roots.append(Path.home() / ".cache" / "huggingface" / "hub")
    roots.append(Path("/mnt/caches/hf/hub"))
    out: list[Path] = []
    slug = "models--" + model_id.replace("/", "--")
    for root in roots:
        snap = root / slug / "snapshots"
        if snap.is_dir():
            out.extend(sorted(p for p in snap.iterdir() if p.is_dir()))
    return out


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _tree_files(model_id: str, revision: str) -> list[dict[str, Any]]:
    url = f"{_endpoint()}/api/models/{model_id}/tree/{revision}?recursive=true"
    raw = _get_json(url)
    if not isinstance(raw, list):
        raise RuntimeError(f"{model_id}: tree API 返回非列表：{type(raw)}")
    return [e for e in raw if isinstance(e, dict) and e.get("type") == "file"]


def _github(repo: str, path: str) -> Any:
    return _get_json(f"https://api.github.com/repos/{repo}{path}")


def _github_raw(repo: str, ref: str, path: str) -> str:
    return _get_text(f"https://raw.githubusercontent.com/{repo}/{ref}/{path}")


def probe_repo(cand: dict[str, Any]) -> dict[str, Any]:
    """只读探测 GitHub 仓库：main 的 commit sha、LIBERO 证据、requirements。

    与 HF 元数据分开落盘（`repo_*` 字段），因为两处 commit 是两个不同仓库的
    身份，不能混写成一个 `repo_commit`。
    """
    repo = cand.get("github_repo")
    out: dict[str, Any] = {
        "repo_head_commit": None,
        "repo_requirements": [],
        "repo_libero_paths": [],
        "libero_compat": "unknown",
        "libero_evidence": None,
    }
    if not repo:
        return out
    try:
        out["repo_head_commit"] = _github(repo, "/commits/main").get("sha")
        tree = _github(repo, "/git/trees/main?recursive=1")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        out["libero_evidence"] = f"GitHub 仓库树不可达：{exc}"
        return out
    paths = [t.get("path", "") for t in tree.get("tree", []) if t.get("type") == "blob"]
    libero = [p for p in paths if "libero" in p.lower()]
    out["repo_libero_paths"] = libero
    if libero:
        out["libero_compat"] = "declared"
        out["libero_evidence"] = (
            f"仓库树命中 {len(libero)} 个 LIBERO 路径，含 "
            f"{[p for p in libero if p.endswith('.py')][:3]}")
    else:
        out["libero_compat"] = "unsupported"
        out["libero_evidence"] = "仓库树中 libero 0 命中（无官方 LIBERO 支持路径）"
    # 文件名里带 requirements 的（含 requirements-min.txt：它不以 requirements.txt
    # 结尾，早期用 endswith("requirements.txt") 会漏掉）。
    reqs = [p for p in paths
            if "requirements" in p.rsplit("/", 1)[-1].lower()]
    # 依「与安装直接相关」的程度排序：仓库根的 requirements(-min).txt 优先，
    # 子目录（如 experiments/robot/libero/libero_requirements.txt）次之。
    def _req_rank(p: str) -> tuple[int, int, str]:
        base = p.rsplit("/", 1)[-1]
        top = 0 if "/" not in p else 1
        kind = 0 if base == "requirements-min.txt" else (
            1 if base == "requirements.txt" else 2)
        return (top, kind, p)
    for pick in sorted(reqs, key=_req_rank)[:3]:
        try:
            text = _github_raw(repo, out["repo_head_commit"] or "main", pick)
            out["repo_requirements"].append(
                {"path": pick, "pip": _parse_requirements(text)})
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            out["repo_requirements"].append(
                {"path": pick, "pip": None, "error": "requirements 原文不可达"})
    return out


def _parse_requirements(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith(("-", "git+", "http")):
            out.append(line)
    return out


def probe_candidate(cand: dict[str, Any]) -> dict[str, Any]:
    """只读探测单个候选，返回 AC-T6-1 要求的字段（拿不到的写 null）。"""
    unverified: list[str] = []
    model_id = cand["model_id"]
    rec: dict[str, Any] = {
        "name": cand["name"],
        "model_id": model_id,
        "repo_url": cand["repo_url"],
        "policy_form": cand["policy_form"],
        "note": cand["note"],
        "repo_commit": None,
        "weights_sha256": None,
        "weights_sha256_scope": None,
        "weights_files": [],
        "license": None,
        "params": None,
        "params_source": None,
        "params_doc_claim": None,
        "weights_bytes": None,
        "vram_fp16": None,
        "vram_int4": None,
        "vram_basis": None,
        "requires": None,
        "libero_compat": "unknown",
        "unverified": unverified,
        "source": "hf-mirror api（只读元数据，未下载权重）",
    }
    base = f"{_endpoint()}/api/models/{model_id}"
    try:
        meta = _get_json(base)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        unverified.extend(["repo_commit", "weights_sha256", "license", "params",
                           "vram_fp16", "vram_int4", "requires"])
        rec["error"] = f"模型元数据不可达：{exc}"
        return rec

    sha = str(meta.get("sha") or "")
    if _SHA40.match(sha):
        rec["repo_commit"] = sha
    else:
        unverified.append("repo_commit")

    card = meta.get("cardData") or {}
    lic = card.get("license")
    if isinstance(lic, list):
        lic = ",".join(str(x) for x in lic)
    if isinstance(lic, str) and lic:
        rec["license"] = lic
    else:
        tags = meta.get("tags") or []
        lic_tags = [t.split(":", 1)[1] for t in tags
                    if isinstance(t, str) and t.startswith("license:")]
        if lic_tags:
            rec["license"] = ",".join(lic_tags)
    if not rec["license"]:
        unverified.append("license")

    safet = meta.get("safetensors") or {}
    total = safet.get("total") if isinstance(safet, dict) else None
    if isinstance(total, int) and total > 0:
        rec["params"] = total
        rec["params_source"] = "hf api safetensors.total"
    else:
        unverified.append("params（HF 无 safetensors 元数据，改取 README 文档声明作参考）")

    try:
        files = _tree_files(model_id, sha or "main")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        files = []
        unverified.append("weights_files")
        rec["tree_error"] = f"文件树不可达：{exc}"

    weights = [f for f in files
               if str(f.get("path", "")).endswith(".safetensors")]
    # 非 safetensors 布局（Octo 1.5 是 JAX/msgpack 的单一 `checkpoint`）：把
    # 「大体积且带 LFS oid 的权重样文件」也认作权重，否则 weights_sha256 会
    # 永不可得。识别规则写死、可复算：文件名带权重后缀或正好叫 checkpoint，
    # 且 ≥ 50 MB（排除 example_batch.msgpack 这类 700 KB 样例）。
    if not weights:
        weights = [f for f in files
                   if _is_weight_like(str(f.get("path", "")))
                   and isinstance(f.get("size"), int) and f["size"] >= MIN_WEIGHT_BYTES
                   and isinstance(f.get("lfs"), dict) and f["lfs"].get("oid")]
    rec["weights_files"] = [
        {"path": f["path"], "size": f.get("size"),
         "sha256": (f.get("lfs") or {}).get("oid") if isinstance(f.get("lfs"), dict) else None}
        for f in weights
    ]
    sizes = [f.get("size") for f in weights if isinstance(f.get("size"), int)]
    if sizes:
        rec["weights_bytes"] = int(sum(sizes))
    if len(weights) == 1:
        oid = (weights[0].get("lfs") or {}).get("oid") if isinstance(
            weights[0].get("lfs"), dict) else None
        if isinstance(oid, str) and _SHA64.match(oid):
            rec["weights_sha256"] = oid
            rec["weights_sha256_scope"] = weights[0]["path"]
        else:
            unverified.append("weights_sha256")
    elif weights:
        rec["weights_sha256_scope"] = f"sharded:{len(weights)} 个权重文件（无单一全等哈希）"
        unverified.append("weights_sha256")
    else:
        unverified.append("weights_sha256")

    # 本地缓存互证：命中则用本地文件复算 sha256，与远端 LFS oid 比对（不一致即报错）。
    local = _local_cache_candidates(model_id)
    rec["local_cache_hits"] = [str(p) for p in local]
    if local and weights:
        for w in rec["weights_files"]:
            p = local[-1] / w["path"]
            if p.is_file() and w.get("sha256"):
                if _sha256_file(p) != w["sha256"]:
                    raise SystemExit(
                        f"{model_id}: 本地缓存 {p} 的 sha256 与远端 LFS oid 不一致")

    # README 里的参数量只是文档声明（非机读元数据），单独记到 params_doc_claim，
    # 不冒充 params。
    readme_paths = [f["path"] for f in files
                    if str(f.get("path", "")).lower() == "readme.md"]
    if readme_paths:
        try:
            readme = _get_text(
                f"{_endpoint()}/{model_id}/resolve/{sha or 'main'}/{readme_paths[0]}")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            readme = ""
        m = _PARAMS_DOC.search(readme)
        if m:
            num = float(m.group(1)) * (1e6 if m.group(2).upper() == "M" else 1e9)
            rec["params_doc_claim"] = {
                "value": int(num),
                "source": f"{readme_paths[0]}（文档声明，非机读元数据）",
                "text": m.group(0),
            }

    req_paths = [f["path"] for f in files
                 if str(f.get("path", "")).endswith("requirements.txt")]
    if req_paths:
        try:
            text = _get_text(
                f"{_endpoint()}/{model_id}/resolve/{sha or 'main'}/{req_paths[0]}")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            text = ""
        pkgs = _parse_requirements(text) if text else []
        if pkgs:
            rec["requires"] = {"source": req_paths[0], "pip": pkgs}
    if rec["requires"] is None:
        lib = meta.get("library_name")
        rec["requires"] = ({"source": "hf api library_name", "library_name": lib}
                           if lib else None)
        if rec["requires"] is None or not rec["requires"].get("library_name"):
            unverified.append("requires")

    if rec["params"]:
        n = float(rec["params"])
        rec["vram_fp16"] = round(n * 2.0 / 1e9, 3)
        # 4-bit：4 bit/权重 + 量化 zero-point/scale 余量 15%
        rec["vram_int4"] = round(n * 0.5 / 1e9 * 1.15, 3)
        rec["vram_basis"] = "params（safetensors.total）× 2B / 0.575B"
    elif rec["weights_bytes"]:
        # 无参数量元数据时用文件字节数做**上界**：文件无论是 fp16 还是 fp32，
        # 落地成 fp16 权重都不超过其字节数；4-bit 取该上界的 1/4（+15% 反量化余量）。
        # 口径写进 vram_basis，不冒充精确值。
        b = float(rec["weights_bytes"])
        rec["vram_fp16"] = round(b / 1e9, 3)
        rec["vram_int4"] = round(b * 0.25 / 1e9 * 1.15, 3)
        rec["vram_basis"] = ("weights_bytes 上界（无 safetensors 元数据："
                             "文件字节数 ⇒ fp16 权重上界，4-bit 取其 1/4×1.15）")
        unverified.append("vram_fp16（上界近似，非 safe tensors 精确值）")
    else:
        unverified.extend(["vram_fp16", "vram_int4"])

    # 去重保序
    rec["unverified"] = list(dict.fromkeys(unverified))
    return rec


def _peak_gb(weights_gb: float) -> float:
    budget = CUDA_CONTEXT_GB + RENDER_GB + ACTIVATION_GB
    return round((weights_gb + budget) * (1.0 + FRAGMENT_FRACTION), 3)


def _budget_md(records: list[dict[str, Any]]) -> str:
    lines = [
        "# Task 6 显存预算表（D6-1b，A800-40GB）",
        "",
        "判定规则（计划 §T6.3 D6-1b）：**峰值预估 > 32 GB**（40 GB 留 8 GB 安全垫）",
        "⇒ 该候选默认走 4-bit 或改用更小模型，不得先试 fp16 撞显存。",
        "",
        "非权重项会计口径（逐项标注，均可复算）：",
        "",
        f"- CUDA context + cuBLAS/cuDNN 句柄：{CUDA_CONTEXT_GB} GB",
        f"- MuJoCo EGL 离屏 1080p 渲染：{RENDER_GB} GB",
        f"- 单步推理激活峰值（batch=1）：{ACTIVATION_GB} GB",
        f"- PyTorch caching allocator 碎片余量：{int(FRAGMENT_FRACTION * 100)}%",
        f"- 峰值 = (权重 + 上述三项) × (1 + 碎片余量)，阈值 {VRAM_LIMIT_GB} GB / 卡 {GPU_TOTAL_GB} GB",
        "",
        "| 候选 | 参数量 | 权重 fp16 | 权重 4-bit | 峰值 fp16 | 峰值 4-bit | 判定 |",
        "|---|---|---|---|---|---|---|",
    ]
    for rec in records:
        p = rec.get("params")
        doc = rec.get("params_doc_claim")
        if isinstance(p, int):
            params = f"{p:,}"
        elif isinstance(doc, dict):
            params = f"null（文档声明 {doc['value']:,}，非机读）"
        else:
            params = "null（unverified）"
        w16 = rec.get("vram_fp16")
        w4 = rec.get("vram_int4")
        if w16 is None or w4 is None:
            lines.append(f"| {rec['name']} | {params} | null | null | null | null | "
                         "**无数据 ⇒ 不得开跑**（先补齐 params） |")
            continue
        p16 = _peak_gb(w16)
        p4 = _peak_gb(w4)
        if p16 <= VRAM_LIMIT_GB:
            verdict = f"fp16 可跑（峰值 {p16} GB ≤ {VRAM_LIMIT_GB} GB）"
        elif p4 <= VRAM_LIMIT_GB:
            verdict = (f"**fp16 不可跑（峰值 {p16} GB > {VRAM_LIMIT_GB} GB）**"
                       f" ⇒ 默认 4-bit（峰值 {p4} GB）")
        else:
            verdict = (f"**fp16 与 4-bit 均超阈（{p16} / {p4} GB）** ⇒ 改用更小模型")
        lines.append(
            f"| {rec['name']} | {params} | {w16} GB | {w4} GB | {p16} GB | {p4} GB | {verdict} |")
    lines += ["", "权重口径（逐候选，可复算）：", ""]
    for rec in records:
        basis = rec.get("vram_basis") or "无（参数与权重字节均不可得）"
        wb = rec.get("weights_bytes")
        wb_txt = f"{wb:,} B" if isinstance(wb, int) else "null"
        lines.append(f"- **{rec['name']}**：weights_bytes = {wb_txt}；口径 = {basis}")
    lines += [""]
    lines += [
        "",
        "## 选择顺序（预注册，不得开跑后改序）",
        "",
        "```",
        json.dumps(STRATEGY_ORDER, ensure_ascii=False),
        "```",
        "",
        "首个成功策略必须是序中**最前**可达者；往后跳必须留 `degradations` 条目。",
        "",
    ]
    return "\n".join(lines)


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Phase 2.7 Task6 S0 只读核对")
    ap.add_argument("--out-dir", default=None,
                    help="证据根（默认 $SIMPRAXIS_PHASE27_ROOT/task6_vla/s0）")
    args = ap.parse_args(argv)
    out = Path(args.out_dir) if args.out_dir else Path(
        os.environ.get("SIMPRAXIS_PHASE27_ROOT", "")) / "task6_vla" / "s0"
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)

    records = []
    for cand in CANDIDATES:
        rec = probe_candidate(cand)
        repo = probe_repo(cand)
        rec["github_repo"] = cand.get("github_repo")
        rec["repo_head_commit"] = repo["repo_head_commit"]
        rec["repo_requirements"] = repo["repo_requirements"]
        rec["repo_libero_paths"] = repo["repo_libero_paths"]
        rec["libero_compat"] = repo["libero_compat"]
        rec["libero_evidence"] = repo["libero_evidence"]
        if rec["libero_compat"] == "unknown":
            rec["unverified"].append("libero_compat（GitHub 仓库树不可达）")
        records.append(rec)
    doc = {
        "schema": "simpraxis.vla_candidates.v1",
        "strategy_order": STRATEGY_ORDER,
        "generated_by": "scripts/phase27/s0_vla_candidates.py",
        "paid_calls": 0,
        "candidates": records,
    }
    (out / "vla_candidates.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(out / "vla_candidates.json", 0o600)
    (out / "vram_budget.md").write_text(_budget_md(records), encoding="utf-8")
    os.chmod(out / "vram_budget.md", 0o600)
    print(json.dumps({"out": str(out),
                      "names": [r["name"] for r in records],
                      "unverified": {r["name"]: r["unverified"] for r in records}},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
