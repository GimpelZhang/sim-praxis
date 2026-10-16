"""orchestrator.common 基础工具回归（G-7/G-8/G-9/G-5/L-1 基础设施，0 付费）。"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from orchestrator.common import callcache, fsutil, secrets, validators


# ---------------------------------------------------------------- fsutil

def test_atomic_write_text_is_0600_and_no_tmp_left(tmp_path: Path):
    p = tmp_path / "evidence" / "x.json"
    p.parent.mkdir()
    fsutil.atomic_write_text(p, "你好")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert p.read_text(encoding="utf-8") == "你好"
    assert list(tmp_path.rglob("*.tmp")) == []


def test_atomic_write_tmp_prefix_includes_target_and_pid(tmp_path: Path,
                                                         monkeypatch):
    # 固定 .tmp 名会让并发写者互踩（G-7）：临时件名必须带目标名+pid 且随机。
    seen: list[str] = []
    orig_mkstemp = fsutil.tempfile.mkstemp

    def spy(prefix, dir, suffix):  # noqa: ANN001
        seen.append(prefix)
        return orig_mkstemp(prefix=prefix, dir=dir, suffix=suffix)

    monkeypatch.setattr(fsutil.tempfile, "mkstemp", spy)
    fsutil.atomic_write_text(tmp_path / "quota_pause.json", "{}")
    assert seen == [f".quota_pause.json.{os.getpid()}."]
    assert not [n for n in os.listdir(tmp_path) if n.startswith(".")]


def test_failed_write_preserves_existing_content(tmp_path: Path,
                                                 monkeypatch):
    p = tmp_path / "c.json"
    fsutil.atomic_write_text(p, "old")

    def boom(_path, _data, _mode):
        raise OSError("disk full")

    monkeypatch.setattr(fsutil, "_atomic_write", boom)
    with pytest.raises(OSError):
        fsutil.atomic_write_text(p, "new")
    assert p.read_text(encoding="utf-8") == "old"


def test_atomic_write_bytes_and_custom_mode(tmp_path: Path):
    p = tmp_path / "b.bin"
    fsutil.atomic_write_bytes(p, b"\x00\x01", mode=0o640)
    assert stat.S_IMODE(p.stat().st_mode) == 0o640
    assert p.read_bytes() == b"\x00\x01"


def test_atomic_write_missing_parent_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        fsutil.atomic_write_text(tmp_path / "nope" / "x", "v")


def test_ensure_private_dir_sets_0700(tmp_path: Path):
    d = fsutil.ensure_private_dir(tmp_path / "ev")
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    # 已存在且被外部放宽时同样收敛。
    os.chmod(d, 0o755)
    fsutil.ensure_private_dir(d)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_chmod_keep_user_exec_preserves_exec_bit(tmp_path: Path):
    s = tmp_path / "s.py"
    s.write_text("#!/bin/sh\n")
    os.chmod(s, 0o700)
    assert stat.S_IMODE(s.stat().st_mode) & 0o100
    new_mode = fsutil.chmod_keep_user_exec(s)
    assert new_mode == 0o700  # L-11：不再被抹成 0600
    os.chmod(s, 0o755)
    new_mode = fsutil.chmod_keep_user_exec(s)
    assert new_mode == 0o700  # 组/其他位不保留


# ---------------------------------------------------------------- secrets

STRICT_CASES = [
    ("hf_token", "hf_" + "a" * 34),
    ("github_token", "ghp_" + "b" * 36),
    ("nvidia_token", "nvapi-" + "c" * 20),
    ("volc_ark_token", "ark-" + "d" * 20),
    ("openai_like_key", "sk-" + "e" * 48),
    ("aws_access_key", "AKIA" + "ABCDEF0123456789"),
    ("slack_token", "xoxb-" + "f" * 20),
    ("pem_private_key", "-----BEGIN RSA PRIVATE KEY-----"),
    ("assigned_secret_kv", 'api_key="%s"' % ("g" * 30)),
    ("assigned_secret_kv", "AUTHORIZATION: '%s'" % ("h" * 30)),
    ("assigned_secret_kv", "app_secret = '%s'" % ("i" * 30)),
    ("assigned_secret_kv", 'FEISHU_APP_SECRET="%s"' % ("j" * 32)),
    ("lark_cli_app_id", "cli_" + "a0b1c2d3e4f5a6b7"),
]


@pytest.mark.parametrize("name,sample", STRICT_CASES)
def test_scan_detects_all_strict_forms(name, sample):
    hits = secrets.scan_text(sample)
    assert any(n == name for n, _ in hits), (name, sample, hits)


def test_redact_preserves_known_prefixes():
    assert secrets.redact_text("Bearer " + "k" * 20) == "Bearer " + secrets.REDACTED
    out = secrets.redact_text("x-api-key: " + "m" * 20)
    assert out == "x-api-key: " + secrets.REDACTED
    out = secrets.redact_text("ark-" + "n" * 20)
    assert out.startswith("ark-") and secrets.REDACTED in out and "n" * 20 not in out


def test_redact_replaces_whole_match_for_bare_tokens():
    assert secrets.redact_text("cli_" + "a0b1c2d3e4f5a6b7") == secrets.REDACTED


def test_redact_handles_kv_keeping_prefix():
    s = 'token line api_key="%s" trailing' % ("q" * 30)
    out = secrets.redact_text(s)
    assert "q" * 30 not in out
    assert out.startswith("token line api_key=")
    assert "trailing" in out


def test_scan_excludes_redact_only_loose_patterns():
    # 短 ark- 串只过日志宽脱敏，不进证据 FATAL 扫描（避免误杀正常文本）。
    loose = "ark-" + "a" * 12
    assert secrets.scan_text(loose) == []
    assert secrets.REDACTED in secrets.redact_text(f"x {loose} y")


def test_scan_patterns_is_single_source():
    # build_evidence_pack / router 必须都从本常量取正则（H-12/G-8）。
    names = {sp.name for sp in SECRET_PATTERN_ORDER}
    assert {"hf_token", "lark_cli_app_id", "assigned_secret_kv"} <= names


# 顺序引用（模块级常量，测试显式锁定）
SECRET_PATTERN_ORDER = secrets.SECRET_PATTERNS


def test_benign_strings_pass_scan():
    benign = [
        "thread-t7-mujoco-20261015",
        "T8-B1-visual",
        "模型校验通过 max_tokens=8",
        "cli_help 命令",  # 短 cli_ 不是 app_id
        "normal_vs_ice_nominal",
        "Qback=0.4（未标定）",
    ]
    for s in benign:
        assert secrets.scan_text(s) == [], s


# ---------------------------------------------------------------- validators

@pytest.mark.parametrize("bad", [
    "../etc/passwd", "a/b", "..", "x" * 65,
    "", "a b", "a:b", "a.b", None, 7, True,
])
def test_validate_id_rejects_bad_values(bad):
    with pytest.raises(ValueError):
        validators.validate_thread_id(bad)
    with pytest.raises(ValueError):
        validators.validate_batch_id(bad)


def test_validate_id_accepts_documented_ids():
    assert validators.validate_thread_id("t7-mujoco-20261015") == "t7-mujoco-20261015"
    assert validators.validate_batch_id("T8-B1-visual") == "T8-B1-visual"


def test_safe_join_blocks_traversal(tmp_path: Path):
    root = tmp_path / "ev"
    root.mkdir()
    (tmp_path / "other.json").write_text("x")
    with pytest.raises(ValueError):
        validators.safe_join(root, "../other.json")
    with pytest.raises(ValueError):
        validators.safe_join(root, "a/b.json")
    p = validators.safe_join(root, "macro_t7_r0_a0.req.json")
    assert p.parent == root


def test_assert_within_detects_symlink_escape(tmp_path: Path):
    root = tmp_path / "ev"
    root.mkdir()
    outside = tmp_path / "outside.json"
    link = root / "link.json"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink unsupported")
    with pytest.raises(ValueError):
        validators.assert_within(link, root)


# ---------------------------------------------------------------- callcache

def _fp(call):
    return callcache.request_fingerprint("prompt", "img", {"max_tokens": 8})


def test_callcache_miss_put_hit_roundtrip(tmp_path: Path):
    cc = callcache.CallReplay(tmp_path / ".call_cache")
    fp = callcache.request_fingerprint("p", "i", {"max_tokens": 8})
    assert cc.get("task8_rung4_t1_split1", fp) is None
    cc.put("task8_rung4_t1_split1", kind="visual", model="m1",
           raw='{"ok":1}', usage=(10, 20), fingerprint=fp)
    hit = cc.get("task8_rung4_t1_split1", fp)
    assert hit is not None
    assert hit["raw"] == '{"ok":1}' and tuple(hit["usage"]) == (10, 20)
    f = next((tmp_path / ".call_cache").iterdir())
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / ".call_cache").stat().st_mode) == 0o700


def test_callcache_fingerprint_mismatch_is_miss(tmp_path: Path):
    cc = callcache.CallReplay(tmp_path / ".call_cache")
    fp1 = callcache.request_fingerprint("p1")
    fp2 = callcache.request_fingerprint("p2")
    cc.put("k", kind="coding", model="m", raw="r", usage=(1, 2), fingerprint=fp1)
    assert cc.get("k", fp2) is None


def test_callcache_corrupt_file_is_miss(tmp_path: Path):
    cc = callcache.CallReplay(tmp_path / ".call_cache")
    fp = callcache.request_fingerprint("p")
    (cc.dir / "k.json").write_text("{broken", encoding="utf-8")
    assert cc.get("k", fp) is None
    rec = {"schema": callcache.SCHEMA, "model": "m", "raw": "r",
           "usage": [1, -3], "req_sha256": fp}  # 负 usage 非法
    (cc.dir / "k2.json").write_text(json.dumps(rec), encoding="utf-8")
    assert cc.get("k2", fp) is None


def test_callcache_bad_key_rejected(tmp_path: Path):
    cc = callcache.CallReplay(tmp_path / ".call_cache")
    with pytest.raises(ValueError):
        cc.get("../escape", "fp")
    with pytest.raises(ValueError):
        cc.put("a/b", kind="x", model="m", raw="r", usage=(1, 1),
               fingerprint="fp")


def test_callcache_bool_usage_rejected_on_get_and_put(tmp_path: Path):
    # C2 严口径（评审 LOW-1）：usage 必须是非布尔非负整数。bool 是 int 子类，
    # 被篡改/手滑写成 [true, 20] 的缓存记录按 miss 处理（安全方向：正常重呼）。
    cc = callcache.CallReplay(tmp_path / ".call_cache")
    fp = callcache.request_fingerprint("p")
    for bad in ([True, 20], [10, False], [True, False]):
        rec = {"schema": callcache.SCHEMA, "model": "m", "raw": "r",
               "usage": bad, "req_sha256": fp}
        key = "k_" + "_".join("t" if x else "f" for x in bad)
        (cc.dir / f"{key}.json").write_text(json.dumps(rec), encoding="utf-8")
        assert cc.get(key, fp) is None
    # 写路径同口径：布尔 usage 直接拒绝，绝不落盘
    with pytest.raises(ValueError):
        cc.put("kb", kind="x", model="m", raw="r", usage=(True, 1),
               fingerprint=fp)
    assert not (cc.dir / "kb.json").exists()
