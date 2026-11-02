#!/usr/bin/env python3
"""Phase 2.5 Task6 Stage B（r4）— 四表向量身份机产器单元测试（hermetic）。

``scripts/phase25/gen_task6_r4_vector_identity.py`` 是 §8 条件 4「嵌入身份不变」
的**唯一**观测手段（GATE1-LANE-A C-1：快照逐行哈希不覆盖 ``vector`` 列）。
它若把口径写错（排序、dtype、维度守卫、id 守卫），窗口就会在"看起来干净"的
态下跑完——所以口径必须逐条钉死。

本文件全 hermetic：不连活库、不联网、0 付费。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

vec = importlib.import_module("scripts.phase25.gen_task6_r4_vector_identity")


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def select(self, _cols):
        return self

    def limit(self, _n):
        return self

    def to_list(self):
        return list(self._rows)


class _Table:
    def __init__(self, rows):
        self._rows = rows

    def search(self, _q):
        return _Query(self._rows)


def _vec(dim=768, value=0.5):
    return [value] * dim


def test_set_hash_is_order_independent():
    """行序由 str(id) 排序决定，与物理顺序无关（活库扫描顺序不承诺）。"""
    a = vec.vector_set_sha256([("b", _vec()), ("a", _vec(value=0.25))])
    b = vec.vector_set_sha256([("a", _vec(value=0.25)), ("b", _vec())])
    assert a == b


def test_set_hash_changes_when_any_vector_value_changes():
    """同维、同 id、只改一个分量也要变——否则重嵌入会隐身。"""
    base = vec.vector_set_sha256([("a", _vec())])
    moved = _vec()
    moved[17] = 0.5000001
    assert vec.vector_set_sha256([("a", moved)]) != base


def test_set_hash_changes_when_id_changes():
    assert vec.vector_set_sha256([("a", _vec())]) != \
        vec.vector_set_sha256([("b", _vec())])


def test_set_hash_uses_float32_bytes_so_tiny_noise_below_eps_is_invisible():
    """口径是 float32（与 LanceDB 存储同宽）：1e-9 的扰动不改变字节。

    这是**如实登记**的边界，不是缺陷：向量本就以 float32 落库，float64 精度
    的"变化"不可能出现在库里。
    """
    v = _vec()
    v[3] = 0.25
    v64 = list(v)
    v64[3] = 0.25 + 1e-9
    assert vec.vector_set_sha256([("a", v)]) == \
        vec.vector_set_sha256([("a", v64)])


def test_table_identity_reports_rows_and_dim():
    node = vec.table_identity(_Table([{"id": "x", "vector": _vec()},
                                      {"id": "y", "vector": _vec()}]), "t")
    assert node["rows"] == 2 and node["dim"] == 768
    assert len(node["vector_set_sha256"]) == 64


def test_table_identity_rejects_empty_table():
    with pytest.raises(vec.IdentityError, match="零行"):
        vec.table_identity(_Table([]), "t")


def test_table_identity_rejects_missing_id():
    with pytest.raises(vec.IdentityError, match="无 id"):
        vec.table_identity(_Table([{"id": None, "vector": _vec()}]), "t")


def test_table_identity_rejects_missing_vector():
    with pytest.raises(vec.IdentityError, match="无向量"):
        vec.table_identity(_Table([{"id": "a", "vector": None}]), "t")


def test_table_identity_rejects_mixed_dims():
    with pytest.raises(vec.IdentityError, match="维度不一致"):
        vec.table_identity(_Table([{"id": "a", "vector": [0.1] * 768},
                                   {"id": "b", "vector": [0.1] * 512}]), "t")


def test_table_identity_rejects_wrong_dim():
    """跨维度混用必须报错，不能"看着是单维"就放行（768 是冻结值）。"""
    with pytest.raises(vec.IdentityError, match="≠ 冻结值"):
        vec.table_identity(_Table([{"id": "a", "vector": [0.1] * 1024}]), "t")


def test_dump_is_deterministic_and_one_trailing_newline():
    payload = vec.build  # noqa: F841  (导入即可，不跑活库)
    text = vec._dump({"schema": vec.SCHEMA, "tables": {}, "total_rows": 0})
    assert text == vec._dump({"tables": {}, "total_rows": 0,
                              "schema": vec.SCHEMA})
    assert text.endswith("}\n") and not text.endswith("}\n\n")
