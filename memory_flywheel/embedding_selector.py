"""嵌入模型实测选型：中文语义 sanity 通过才落盘配置。"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import numpy as np

CONFIG_PATH = Path(__file__).with_name("embedding_config.json")

# 顺序即优先级；(模型名, 标称维度, onnx 最小字节数)
# bge-small-zh onnx 约 96MB；multilingual-MiniLM-L12-v2 onnx 约 470MB。
# 下载截断/损坏常常表现为几十 KB 的 HTML/错误页，min_bytes 直接拦掉。
PREFERENCES = [
    ("BAAI/bge-small-zh-v1.5", 512, 80 * 1024 * 1024),
    ("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", 384, 300 * 1024 * 1024),
]


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_onnx_artifact(cache_dir: str, model_name: str, min_bytes: int) -> dict:
    """校验 fastembed 实际加载的 onnx 文件：存在、大小达标、SHA256 可记录/可复核。"""
    root = Path(cache_dir)
    if not root.exists():
        raise RuntimeError(f"fastembed 缓存目录不存在：{root}（检查 /mnt 是否挂载）")
    onnx_files = sorted(root.rglob("*.onnx"))
    if not onnx_files:
        raise RuntimeError(
            f"{model_name} 下载后在 {root} 下找不到 *.onnx——镜像可能返回了错误页。"
            f"处置：rm -rf {root}/models--{model_name.replace('/', '--')} 后，"
            f"确认 HF_ENDPOINT={os.environ.get('HF_ENDPOINT')} 可达再重跑")
    onnx = onnx_files[-1]
    size = onnx.stat().st_size
    if size < min_bytes:
        raise RuntimeError(
            f"{model_name} onnx 文件疑似截断：{onnx} 仅 {size} 字节（期望 ≥{min_bytes}）。"
            f"删除该缓存目录并经 hf-mirror 重下；禁止带着损坏文件入库")
    sha = _sha256_file(onnx)
    # 可选硬钉：执行期若通过环境变量给了官方 SHA256，则强制比对
    expected = os.environ.get("SIMPRAXIS_ONNX_SHA256_" + model_name.replace("/", "_").replace("-", "_"))
    if expected and expected.lower() != sha:
        raise RuntimeError(f"{model_name} onnx SHA256 不符：{sha} != {expected}")
    return {"onnx_path": str(onnx), "onnx_bytes": size, "onnx_sha256": sha}

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
    for model_name, expect_dim, min_bytes in PREFERENCES:
        try:
            print(f"[embed] 尝试 {model_name}（缓存 {cache_dir}，HF_ENDPOINT={os.environ.get('HF_ENDPOINT')}）")
            try:
                model = TextEmbedding(model_name=model_name, cache_dir=cache_dir)
                vectors = [np.asarray(v, dtype=np.float32) for v in model.embed(list(_PROBES))]
            except Exception as exc:  # 下载/加载失败统一转成可读的损坏/镜像错误
                raise RuntimeError(
                    f"{model_name} 下载或加载失败：{type(exc).__name__}: {exc}。"
                    f"处置：检查 {cache_dir} 磁盘与 {os.environ.get('HF_ENDPOINT')}，"
                    f"删除 {cache_dir}/models--{model_name.replace('/', '--')} 后经 hf-mirror 重下") from exc
            artifact = _verify_onnx_artifact(cache_dir, model_name, min_bytes)
            dims = {len(v) for v in vectors}
            assert dims == {expect_dim}, f"维度异常：{dims}，期望 {expect_dim}"
            sim_related = _cos(vectors[0], vectors[1])
            sim_unrelated = _cos(vectors[0], vectors[2])
            assert sim_related > sim_unrelated + 0.05, (
                f"中文语义 sanity 失败：相关 {sim_related:.3f} <= 无关 {sim_unrelated:.3f}")
            cfg = {
                # critic#5：cache_dir/onnx_path 为本机 /mnt 绝对路径，属机器绑定产物，
                # 跨机移植时以 model_name/dim 重新下载即可，本文件不构成跨机路径契约。
                "_machine_note": "cache_dir/onnx_path 为本机 /mnt 绝对路径（机器绑定）；"
                                 "跨机只认 model_name 与 dim，路径需重新生成",
                "model_name": model_name,
                "dim": expect_dim,
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
