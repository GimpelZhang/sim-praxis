"""锁内适配 env 闸批大小 hermetic 单测。

背景：Task6 Stage A 换嵌入窗口，Qwen3-Embedding-0.6B 在本机 47GiB 下
以 fastembed 默认 batch_size=256 嵌入 94 个长方案块时确定性 OOM
（R2 harness 收割、R3 内核 global OOM 46.97GiB）。用户 2026-11-01
裁决 (a)：加 SIMPRAXIS_EMBED_BATCH_SIZE env 闸，未设时行为与冻结版本
逐字一致（helper 不得向 model.embed 透传该形参），窗内置 16。

本文件只用假模型，不联网、不加载真实嵌入、不触活库。
"""
import pytest

from memory_flywheel.embedding_selector import (
    EMBED_BATCH_ENV,
    env_embed_batch_size,
    fastembed_embed,
)


class DefaultSigModel:
    """embed 只有 documents 形参：默认路径必须逐字不传 batch_size。"""

    def __init__(self):
        self.received = None

    def embed(self, documents):
        self.received = list(documents)
        return iter([[0.0, 0.0] for _ in documents])


class RecordingModel:
    """fastembed 同形签名，记录 batch_size 是否被透传。"""

    def __init__(self):
        self.calls = []

    def embed(self, documents, batch_size=256):
        self.calls.append((len(list(documents)), batch_size))
        return iter([[0.0, 0.0] for _ in range(self.calls[-1][0])])


def test_unset_env_returns_none(monkeypatch):
    monkeypatch.delenv(EMBED_BATCH_ENV, raising=False)
    assert env_embed_batch_size() is None


def test_blank_env_returns_none(monkeypatch):
    monkeypatch.setenv(EMBED_BATCH_ENV, "   ")
    assert env_embed_batch_size() is None


@pytest.mark.parametrize("value", ["1", "16", "256"])
def test_positive_value_parsed(monkeypatch, value):
    monkeypatch.setenv(EMBED_BATCH_ENV, value)
    assert env_embed_batch_size() == int(value)


@pytest.mark.parametrize("value", ["0", "-1", "abc", "16.0", "16x"])
def test_invalid_value_raises(monkeypatch, value):
    monkeypatch.setenv(EMBED_BATCH_ENV, value)
    with pytest.raises(RuntimeError, match="正整数"):
        env_embed_batch_size()


def test_default_path_does_not_forward_batch_size(monkeypatch):
    monkeypatch.delenv(EMBED_BATCH_ENV, raising=False)
    model = DefaultSigModel()
    out = list(fastembed_embed(model, ["a", "b", "c"]))
    assert model.received == ["a", "b", "c"]
    assert len(out) == 3


def test_env_path_forwards_batch_size(monkeypatch):
    monkeypatch.setenv(EMBED_BATCH_ENV, "16")
    model = RecordingModel()
    out = list(fastembed_embed(model, ["x"] * 40))
    assert model.calls == [(40, 16)]
    assert len(out) == 40


def test_invalid_env_surfaces_from_embed(monkeypatch):
    monkeypatch.setenv(EMBED_BATCH_ENV, "0")
    with pytest.raises(RuntimeError, match="正整数"):
        list(fastembed_embed(RecordingModel(), ["x"]))
