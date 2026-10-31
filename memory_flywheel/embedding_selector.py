"""嵌入模型实测选型：中文语义 sanity 通过才落盘配置。

锁内适配（Phase 2.5 换嵌入）：Task6 Stage A（2026-10-31，冻结预注册
fcb3fcd6 §3.1）把候选结构从 ``(model_name, dim, min_bytes)`` 三元组升级为
显式登记件：每个候选钉死 fastembed 注册表 ``sources.hf`` 仓名、refs/main
快照 sha、快照内逐件精确 relpath 的 bytes/sha256。Qwen3 为 fp 主件
(onnx/model.onnx) + 外部权重件 (onnx/model.onnx_data) 双件；量化变体
Qwen/Qwen3-Embedding-0.6B-Q 与之**同仓**（onnx/model_quantized.onnx），
出现即拒绝，防止量化件混入 fp 路径静默改变嵌入语义。
双件 bytes/sha 是代码常量，不接受任何 env/CLI 覆盖（旧
SIMPRAXIS_ONNX_SHA256_* 覆盖口已按预注册删除）。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

CONFIG_PATH = Path(__file__).with_name("embedding_config.json")


@dataclass(frozen=True)
class _Candidate:
    # fastembed 注册表模型名（TextEmbedding(model_name=...) 入参）
    model_name: str
    dim: int
    # fastembed 注册表 sources.hf 仓名；缓存子目录只准由它推导
    # （models--{org}--{repo}），禁止沿用 model_name 推导
    # （mpnet 实体在 models--Xenova--…，与注册名前缀不同）。
    hf_repo: str
    # refs/main 期望内容（钉死快照）；None 表示不预钉（仅 MiniLM 末位候选）
    snapshot: str | None
    # 快照内逐件 (relpath, bytes, sha256) 精确登记；空元组走 legacy 行为
    files: tuple[tuple[str, int, str], ...]
    # 同仓禁件（量化变体陷阱）
    reject_files: tuple[str, ...] = ()
    # legacy 末位候选专用：缓存缺席、无法预钉 sha，只保留历史最小字节闸
    legacy_file: str | None = None
    legacy_min_bytes: int | None = None


# 顺序即优先级：Qwen3（Stage A 新首选）→ mpnet（封存基线）→ bge → MiniLM。
# 2026-10-12 经用户明确批准 bge→mpnet 换型（AskUserQuestion 决策）：bge 对
# §12.4 Q3（cudaErrorIllegalAddress 精确错误名召回）存在模型能力天花板——
# 681 个滑动窗穷举证明相关内容得分上限 0.511 < top5 门槛 0.533。
# 2026-10-31 Task6 Stage A 经用户「2+3」裁决与门 1 双签（预注册 fcb3fcd6），
# Qwen3-Embedding-0.6B（fp，1024 维）置为首选，mpnet 行保留为二选与封存基线。
PREFERENCES: list[_Candidate] = [
    _Candidate(
        model_name="Qwen/Qwen3-Embedding-0.6B",
        dim=1024,
        hf_repo="Qdrant/Qwen3-Embedding-0.6B-onnx",
        snapshot="af95f2c416ffe9379369ad64f9113e865db6112c",
        files=(
            ("onnx/model.onnx", 4031521,
             "754d6ba1c7a323d7b3af0d85b8268f8b716b0cd362ce73295e0f71d3dd831639"),
            ("onnx/model.onnx_data", 2383106048,
             "15d02a315e3b95296700c73d72a436e79facf4e17d919345bed730e98433bcb4"),
        ),
        reject_files=("onnx/model_quantized.onnx",),
    ),
    _Candidate(
        model_name="sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
        dim=768,
        hf_repo="Xenova/paraphrase-multilingual-mpnet-base-v2",
        snapshot="e5d116277351513fd260955ece953ecddde7046e",
        files=(
            ("onnx/model.onnx", 1110059084,
             # 2026-10-31 gate2 引擎批在本机实测（跟随符号链接流式 sha256）：
             # sha256sum /mnt/caches/fastembed/models--Xenova--paraphrase-
             # multilingual-mpnet-base-v2/snapshots/e5d11627…/onnx/model.onnx
             "92f682c55d39e728187c6bffd488f17462d4e05e3c5fe86ef9ba6f38d66122ed"),
        ),
    ),
    _Candidate(
        model_name="BAAI/bge-small-zh-v1.5",
        dim=512,
        hf_repo="Qdrant/bge-small-zh-v1.5",
        snapshot="46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59",
        files=(
            ("model_optimized.onnx", 94781076,
             # 2026-10-31 gate2 引擎批在本机实测：
             # sha256sum /mnt/caches/fastembed/models--Qdrant--bge-small-zh-
             # v1.5/snapshots/46fbe35f…/model_optimized.onnx
             "1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38"),
        ),
    ),
    # 锁内适配（Phase 2.5 换嵌入）：2026-10-31 本机无 MiniLM 缓存、无法预钉
    # sha，保留历史末位候选行为（注册表 model_file + 最小字节闸，不钉 sha）；
    # 禁止编造 bytes/sha 常量。
    _Candidate(
        model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        dim=384,
        hf_repo="Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q",
        snapshot=None,
        files=(),
        legacy_file="model_optimized.onnx",
        legacy_min_bytes=300 * 1024 * 1024,
    ),
]


def _sha256_file(p: Path) -> str:
    # open/stat 默认跟随符号链接：snapshot 内文件是 → ../../../blobs/<sha>
    # 的两级符号链接，取值必须跟随到 blob 实体。
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _repo_dir(root: Path, cand: _Candidate) -> Path:
    return root / ("models--" + cand.hf_repo.replace("/", "--"))


def _resolve_snapshot(root: Path, cand: _Candidate) -> Path:
    """root/models--<hf_repo>/refs/main 两级解析到 snapshot 目录。"""
    if not root.exists():
        raise RuntimeError(
            f"fastembed 缓存目录不存在：{root}（检查 /mnt 是否挂载）")
    repo_dir = _repo_dir(root, cand)
    if not repo_dir.is_dir():
        raise RuntimeError(
            f"{cand.model_name} 缓存子目录缺失：{repo_dir}（按 fastembed 注册表 "
            f"sources.hf 仓名 {cand.hf_repo} 推导，非 model_name）。处置："
            f"确认 HF_ENDPOINT={os.environ.get('HF_ENDPOINT')} 可达后经 "
            f"hf-mirror 重下；旧布局目录名不可手改复用")
    refs_main = repo_dir / "refs" / "main"
    if not refs_main.is_file():
        raise RuntimeError(
            f"{cand.model_name} 缓存 refs/main 缺失：{refs_main}——下载不完整。"
            f"处置：rm -rf {repo_dir} 后经 hf-mirror 重下")
    snap_name = refs_main.read_text(encoding="utf-8").strip()
    snap_dir = repo_dir / "snapshots" / snap_name
    if not snap_dir.is_dir():
        raise RuntimeError(
            f"{cand.model_name} refs/main 指向的快照目录不存在：{snap_dir}"
            f"（refs/main={snap_name!r}）——下载不完整。处置：rm -rf {repo_dir}"
            f" 后经 hf-mirror 重下")
    if cand.snapshot is not None and snap_name != cand.snapshot:
        raise RuntimeError(
            f"{cand.model_name} refs/main={snap_name} 与预钉快照 "
            f"{cand.snapshot} 不符：缓存可能已被供应侧更新。按硬规则不得猜用新"
            f"快照；处置：经审批更新代码常量后再选模型")
    return snap_dir


def _verify_registered_file(cand: _Candidate, snap_dir: Path,
                            relpath: str, expected_bytes: int,
                            expected_sha: str) -> dict:
    p = snap_dir / relpath
    if not p.is_file():  # is_file/stat/open 均跟随符号链接
        raise RuntimeError(
            f"{cand.model_name} 缓存件缺失：{p}（登记 relpath={relpath}）——"
            f"下载不完整。处置：rm -rf {_repo_dir(snap_dir.parent.parent.parent, cand)}"
            f" 后经 hf-mirror 重下；禁止以同名他件顶替")
    size = p.stat().st_size
    if size != expected_bytes:
        raise RuntimeError(
            f"{cand.model_name} 缓存件 {relpath} 字节数不符（疑似截断）："
            f"实测 {size} != 预钉 {expected_bytes}（{p}）。处置：删除该缓存目录"
            f"并经 hf-mirror 重下；禁止带着截断文件入库")
    sha = _sha256_file(p)
    if sha != expected_sha:
        raise RuntimeError(
            f"{cand.model_name} 缓存件 {relpath} SHA256 掉包：实测 {sha} != "
            f"预钉 {expected_sha}（{p}）。禁止猜测供应侧变更；处置：经审批更新"
            f"代码常量后再选模型")
    return {"relpath": relpath, "bytes": size, "sha256": sha,
            "resolved_path": str(p.resolve())}


def _verify_onnx_artifact(cache_dir: str, cand: _Candidate) -> dict:
    """按显式登记件校验缓存：refs/main 两级解析 + 逐件精确 relpath/字节/sha。

    缺失/截断/掉包分别给可读 RuntimeError；禁件（量化同仓件）出现即拒。
    不使用任何 glob/rglob/sorted(...)[-1] 取末写法。
    """
    root = Path(cache_dir)
    snap_dir = _resolve_snapshot(root, cand)

    if cand.files:
        onnx_files = [
            _verify_registered_file(cand, snap_dir, rel, size, sha)
            for rel, size, sha in cand.files
        ]
        for bad in cand.reject_files:
            if (snap_dir / bad).exists():
                raise RuntimeError(
                    f"{cand.model_name} 快照目录出现禁用量化件 {bad}："
                    f"{snap_dir / bad}。量化变体 Qwen/Qwen3-Embedding-0.6B-Q 与"
                    f" fp 模型同仓（model_file=onnx/model_quantized.onnx），量化件"
                    f"进入 fp 路径会静默改变嵌入语义；处置：删除该文件或整缓存目录"
                    f"重下，保持快照内仅登记的 fp 件")
        main = onnx_files[0]
        data_files = [f for f in onnx_files[1:]
                      if f["relpath"].endswith("_data")]
        return {
            # 主件键名保留：现有 config 消费者（如 task0a 离线物证测试）依赖；
            # 沿用旧值口径＝snapshot 内符号链接路径（非 resolve 后的 blob 实体名）。
            "onnx_path": str(snap_dir / main["relpath"]),
            "onnx_bytes": main["bytes"],
            "onnx_sha256": main["sha256"],
            "onnx_files": onnx_files,
            "onnx_data_path": (str(snap_dir / data_files[0]["relpath"])
                               if data_files else None),
        }

    # legacy 末位候选（MiniLM）：不预钉 sha，保留历史最小字节闸
    if cand.legacy_file is None or cand.legacy_min_bytes is None:
        raise RuntimeError(
            f"{cand.model_name} 候选既无登记件也无 legacy 配置（代码错误）")
    p = snap_dir / cand.legacy_file
    if not p.is_file():
        raise RuntimeError(
            f"{cand.model_name} 下载后在快照目录找不到 {cand.legacy_file}：{p}"
            f"——镜像可能返回了错误页。处置：rm -rf {_repo_dir(root, cand)} 后，"
            f"确认 HF_ENDPOINT={os.environ.get('HF_ENDPOINT')} 可达再重跑")
    size = p.stat().st_size
    if size < cand.legacy_min_bytes:
        raise RuntimeError(
            f"{cand.model_name} onnx 文件疑似截断：{p} 仅 {size} 字节"
            f"（期望 ≥{cand.legacy_min_bytes}）。删除该缓存目录并经 hf-mirror "
            f"重下；禁止带着损坏文件入库")
    sha = _sha256_file(p)
    return {
        "onnx_path": str(p),
        "onnx_bytes": size,
        "onnx_sha256": sha,
        "onnx_files": [{"relpath": cand.legacy_file, "bytes": size,
                        "sha256": sha, "resolved_path": str(p.resolve())}],
        "onnx_data_path": None,
    }


_PROBES = (
    "EGL 离屏渲染初始化失败、headless 黑屏如何排查",
    "无显示器环境下 EGL headless 渲染起不来、黑屏",
    "今天食堂的午饭有番茄炒蛋和紫菜蛋花汤",
)


def _cos(a, b) -> float:
    a, b = np.asarray(a), np.asarray(b)
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))


def select_embedding(force: bool = False) -> dict:
    if CONFIG_PATH.exists() and not force:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        print(f"[embed] 复用既有配置：{cfg['model_name']} dim={cfg['dim']}")
        return cfg

    from fastembed import TextEmbedding

    cache_dir = os.environ.get("FASTEMBED_CACHE_PATH", "/mnt/caches/fastembed")
    attempts = []
    for cand in PREFERENCES:
        model_name = cand.model_name
        try:
            print(f"[embed] 尝试 {model_name}（缓存 {cache_dir}，HF_ENDPOINT={os.environ.get('HF_ENDPOINT')}）")
            # 先物证后加载：登记件缺失/截断/掉包时不必拉起 2.3GB 外部权重。
            artifact = _verify_onnx_artifact(cache_dir, cand)
            try:
                model = TextEmbedding(model_name=model_name, cache_dir=cache_dir)
                vectors = [np.asarray(v, dtype=np.float32)
                           for v in model.embed(list(_PROBES))]
            except Exception as exc:  # 下载/加载失败统一转成可读的损坏/镜像错误
                raise RuntimeError(
                    f"{model_name} 下载或加载失败：{type(exc).__name__}: {exc}。"
                    f"处置：检查 {cache_dir} 磁盘与 {os.environ.get('HF_ENDPOINT')}，"
                    f"删除 {_repo_dir(Path(cache_dir), cand)} 后经 hf-mirror 重下") from exc
            dims = {len(v) for v in vectors}
            assert dims == {cand.dim}, f"维度异常：{dims}，期望 {cand.dim}"
            for i, v in enumerate(vectors):
                if not np.isfinite(v).all():
                    raise RuntimeError(
                        f"{model_name} 探针 {i} 向量含非有限值（nan/inf），拒绝选用")
                if np.count_nonzero(v) == 0:
                    raise RuntimeError(
                        f"{model_name} 探针 {i} 为零向量，拒绝选用")
            sim_related = _cos(vectors[0], vectors[1])
            sim_unrelated = _cos(vectors[0], vectors[2])
            assert sim_related > sim_unrelated + 0.05, (
                f"中文语义 sanity 失败：相关 {sim_related:.3f} <= 无关 {sim_unrelated:.3f}")
            cfg = {
                # critic#5：cache_dir/onnx_path 为本机 /mnt 绝对路径，属机器绑定产物，
                # 跨机移植时以 model_name/dim 重新下载即可，本文件不构成跨机路径契约。
                "_machine_note": "cache_dir/onnx_path/onnx_files.resolved_path 为本机"
                                 " /mnt 绝对路径（机器绑定）；跨机只认 model_name 与 "
                                 "dim，路径需重新生成。onnx_files 为逐件预钉登记"
                                 "（relpath/bytes/sha256），Qwen3 为 fp 主件+外部"
                                 "权重件双件；onnx_data_path 为外部权重件路径（无则 null）",
                "model_name": model_name,
                "dim": cand.dim,
                "cache_dir": cache_dir,
                "hf_endpoint": os.environ.get("HF_ENDPOINT", "https://hf-mirror.com"),
                "selected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "sanity": {"sim_related": round(sim_related, 4),
                           "sim_unrelated": round(sim_unrelated, 4)},
                **artifact,
            }
            CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[embed] 选定 {model_name}，配置已写入 {CONFIG_PATH}")
            return cfg
        except Exception as exc:  # noqa: BLE001
            attempts.append({"model_name": model_name, "error": f"{type(exc).__name__}: {exc}"})
            print(f"[embed] {model_name} 不可用：{attempts[-1]['error']}")
    raise RuntimeError("所有候选嵌入模型均不可用：" + json.dumps(attempts, ensure_ascii=False))


if __name__ == "__main__":
    select_embedding(force=bool(os.environ.get("SIMPRAXIS_RESELECT")))
