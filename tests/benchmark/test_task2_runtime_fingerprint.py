"""Phase 2.8.0 Task 2 机判：AC-T2-1..8（Runtime Fingerprint 与执行环境身份）。

八条 AC 里七条走宿主车道（``HostBackend`` 真起子进程采集），**容器车道**
（AC-T2-6）打 ``container`` marker：裸跑由 ``tests/conftest.py`` 在收集期
deselect，显式跑法见 Plan §8：

    SIMPRAXIS_CONTAINER=1 pytest tests/benchmark/test_task2_runtime_fingerprint.py \
        -m container -q

纪律：不 mock 探针、不编造版本号——``simulator.mujoco`` 与
``python.packages`` 全部来自真实解释器输出；十类 mismatch 用**真实指纹**做
基底构造，断言的是 ``compare()`` 的识别能力而不是常量对照表。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from orchestrator.benchmark import registry, runtime_fingerprint as rf
from orchestrator.benchmark import task_spec as ts

_REPO_ROOT = Path(__file__).resolve().parents[2]
_M1 = "mujoco_contact_push_v1"
_L1 = "libero_env_adapter_v1"
_LIBERO_IMAGE = (
    "sim-praxis/libero:task6@sha256:"
    "83304574517980a2b5f94d8e45b5c3f361182d0fa40fdd27f6c1713040eca624")
_LIBERO_DIGEST = "sha256:83304574517980a2b5f94d8e45b5c3f361182d0fa40fdd27f6c1713040eca624"
_SECRET_RE = re.compile(
    r"ANTHROPIC_|ARK_|OPENAI_|API_KEY|SECRET|PASSWORD|sk-[A-Za-z0-9]{8,}")


def _spec(task_id: str):
    return registry.BenchmarkRegistry().get(task_id)


@pytest.fixture(scope="module")
def host_capture() -> rf.Capture:
    """一次真宿主采集（模块内复用，避免重复起子进程）。"""
    return rf.HostBackend().capture()


@pytest.fixture(scope="module")
def host_fingerprint(host_capture: rf.Capture):
    return rf.build_fingerprint(_spec(_M1), host_capture)


# --------------------------------------------------------------------------
# AC-T2-1 宿主指纹六段齐全
# --------------------------------------------------------------------------

def test_ac_p0_2_1_host_fingerprint(host_fingerprint: dict):
    doc = host_fingerprint
    for segment in rf.SEGMENTS:
        assert segment in doc, segment
    assert doc["schema"] == rf.FINGERPRINT_SCHEMA_ID
    assert doc["captured_in"] == "host"
    assert doc["task_id"] == _M1
    assert doc["runtime_id"] == "mujoco_315"

    exe = doc["python"]["executable"]
    assert exe.endswith("/mnt/venvs/simpraxis/bin/python"), exe
    assert doc["python"]["workdir"] == str(_REPO_ROOT)
    assert doc["python"]["packages"]["mujoco"] == "3.15.0"
    assert doc["simulator"]["name"] == "mujoco"
    assert doc["simulator"]["version"] == "3.15.0"

    # sha 自洽（不含自身）
    assert doc["fingerprint_sha256"] == rf.fingerprint_sha256(doc)
    # 六段齐全 + schema 机判：字段一个不多一个不少
    rf.validate_fingerprint(doc)
    assert set(doc) == set(rf.SEGMENTS) | {
        "schema", "captured_in", "task_id", "runtime_id", "fingerprint_sha256"}


# --------------------------------------------------------------------------
# AC-T2-2 同环境两次采集 sha 相同；时间戳只在旁路
# --------------------------------------------------------------------------

def test_ac_p0_2_2_deterministic(tmp_path: Path):
    spec = _spec(_M1)
    caps = [rf.HostBackend().capture() for _ in range(2)]
    docs = [rf.build_fingerprint(spec, cap) for cap in caps]
    assert docs[0]["fingerprint_sha256"] == docs[1]["fingerprint_sha256"]
    assert ts.canonical_json(docs[0]) == ts.canonical_json(docs[1])
    # 采集耗时/时间戳确实存在，但**不在**指纹里
    metas = []
    for index, (doc, cap) in enumerate(zip(docs, caps)):
        out = rf.write_fingerprint(tmp_path / f"cap{index}", doc, cap)
        meta = json.loads(Path(out["meta_path"]).read_text(encoding="utf-8"))
        metas.append(meta)
        assert meta["ts_iso"] not in ts.canonical_json(doc)
        # 侧车的两个时间维度都不得以任何形式进指纹本体
        assert "ts_iso" not in json.dumps(doc)
        assert "duration_s" not in json.dumps(doc)
    assert metas[0]["ts_iso"] != metas[1]["ts_iso"]


# --------------------------------------------------------------------------
# AC-T2-3 冻结不覆盖（O_EXCL）
# --------------------------------------------------------------------------

def test_ac_p0_2_3_no_overwrite(tmp_path: Path, host_fingerprint: dict,
                                host_capture: rf.Capture):
    target = tmp_path / "frozen"
    first = rf.write_fingerprint(target, host_fingerprint, host_capture)
    body = Path(first["fingerprint_path"]).read_text(encoding="utf-8")
    with pytest.raises(rf.FingerprintError) as excinfo:
        rf.write_fingerprint(target, host_fingerprint, host_capture)
    assert "拒绝覆盖" in str(excinfo.value)
    # 原件逐字节未动
    assert Path(first["fingerprint_path"]).read_text(encoding="utf-8") == body


# --------------------------------------------------------------------------
# AC-T2-4 十类 mismatch 全部识别
# --------------------------------------------------------------------------

def _codes(mismatches) -> "list[str]":
    return [m.code for m in mismatches]


def test_ac_p0_2_4_ten_mismatches(host_fingerprint: dict):
    spec = _spec(_M1)
    seen = set()

    cases = [
        ({"python_version": "3.9"}, {}, rf.MismatchCode.PYTHON_VERSION),
        ({"packages": {"mujoco": "9.9.9"}}, {}, rf.MismatchCode.MUJOCO_VERSION),
        ({"image_digest": "sha256:" + "0" * 64}, {},
         rf.MismatchCode.IMAGE_DIGEST),
        ({"workdir": "/definitely/not/here"}, {}, rf.MismatchCode.WORKDIR),
        ({"mounts": [{"destination": "/work", "mode": "ro"}]}, {},
         rf.MismatchCode.MOUNT_MISSING),
    ]
    for requirement, observed, code in cases:
        got = _codes(rf.compare(requirement, host_fingerprint, observed=observed))
        assert code in got, (code, got)
        seen.add(code)

    # 只读变可写：真实指纹里放一个 rw 挂载，要求它 ro
    rw_doc = rf.build_fingerprint(
        spec, rf.HostBackend().capture(),
        mounts=[{"source": "/tmp/assets", "destination": "/work", "mode": "rw"}])
    got = _codes(rf.compare({"mounts": [{"destination": "/work", "mode": "ro"}]},
                            rw_doc))
    assert rf.MismatchCode.MOUNT_READONLY in got, got
    seen.add(rf.MismatchCode.MOUNT_READONLY)
    # 反过来（要求 rw、实测 ro）不算不符
    ro_doc = rf.build_fingerprint(
        spec, rf.HostBackend().capture(),
        mounts=[{"source": "/tmp/assets", "destination": "/work", "mode": "ro"}])
    assert _codes(rf.compare({"mounts": [{"destination": "/work", "mode": "ro"}]},
                             ro_doc)) == []

    cases2 = [
        ({"action_dim": 3}, {"action_dim": 7}, rf.MismatchCode.ACTION_DIM),
        ({"obs_keys": ["rgb", "proprio"]}, {"obs_keys": ["rgb"]},
         rf.MismatchCode.OBS_SCHEMA),
        ({"skills_manifest_sha256": "0" * 64}, {},
         rf.MismatchCode.SKILL_MANIFEST),
        ({"assets_manifest_sha256": "1" * 64}, {},
         rf.MismatchCode.ASSET_MANIFEST),
    ]
    for requirement, observed, code in cases2:
        got = _codes(rf.compare(requirement, host_fingerprint, observed=observed))
        assert code in got, (code, got)
        seen.add(code)

    assert set(rf.REQUIRED_CODES) <= seen
    assert len(rf.REQUIRED_CODES) == 10


# --------------------------------------------------------------------------
# AC-T2-5 返回全部不符项（不只第一处）
# --------------------------------------------------------------------------

def test_ac_p0_2_5_all_mismatches(host_fingerprint: dict):
    requirement = {
        "python_version": "3.9",
        "packages": {"mujoco": "9.9.9"},
        "workdir": "/nowhere",
    }
    mismatches = rf.compare(requirement, host_fingerprint)
    assert len(mismatches) == 3, _codes(mismatches)
    assert _codes(mismatches) == [
        rf.MismatchCode.PYTHON_VERSION,
        rf.MismatchCode.MUJOCO_VERSION,
        rf.MismatchCode.WORKDIR,
    ]
    for item in mismatches:
        assert item.detail and item.code in item.to_json()["code"]
    with pytest.raises(rf.FingerprintError) as excinfo:
        rf.require_match(requirement, host_fingerprint)
    assert "BLOCKED_RUNTIME_MISMATCH" in str(excinfo.value)
    # 完全相符时 require_match 不抛
    rf.require_match({"python_version": "3.10", "packages": {"mujoco": "3.15.0"}},
                     host_fingerprint)


# --------------------------------------------------------------------------
# AC-T2-6 容器指纹（container marker；宿主不得冒充）
# --------------------------------------------------------------------------

@pytest.mark.container
def test_ac_p0_2_6_container_fingerprint(tmp_path: Path):
    spec = _spec(_L1)
    cap = rf.ContainerBackend(_LIBERO_IMAGE).capture()
    doc = rf.build_fingerprint(spec, cap)
    assert doc["captured_in"] == "container"
    assert doc["container"]["backend"] == "docker"
    assert doc["container"]["image_digest"] == _LIBERO_DIGEST
    assert doc["container"]["image_name"] == "sim-praxis/libero:task6"
    # 容器内实测（不是 Dockerfile 声明值的照抄）
    assert doc["python"]["packages"]["robosuite"] == "1.4.0"
    assert doc["python"]["packages"]["mujoco"] == "2.3.7"
    assert doc["python"]["version"].startswith("3.10")
    assert doc["simulator"]["name"] == "libero"
    # 未传 --gpus：容器内看不到 GPU —— 这是「没给」的事实
    assert doc["container"]["gpu_visible"] is False
    rf.validate_fingerprint(doc)
    written = rf.write_fingerprint(tmp_path / _L1, doc, cap)
    reloaded = rf.load_fingerprint(written["fingerprint_path"])
    assert reloaded["fingerprint_sha256"] == doc["fingerprint_sha256"]


# --------------------------------------------------------------------------
# AC-T2-7 指纹里没有凭据
# --------------------------------------------------------------------------

def test_ac_p0_2_7_no_secrets(host_fingerprint: dict):
    body = ts.canonical_json(host_fingerprint)
    assert _SECRET_RE.search(body) is None, _SECRET_RE.search(body).group(0)
    # 落盘件同样干净（含 sha/元数据侧车）
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        out = rf.write_fingerprint(
            tmp, host_fingerprint, rf.HostBackend().capture())
        for key in ("fingerprint_path", "sha256_path", "meta_path"):
            text = Path(out[key]).read_text(encoding="utf-8")
            assert _SECRET_RE.search(text) is None, (key, text[:200])


def test_ac_p0_2_7b_sanitized_env_strips_credentials():
    fake = {
        "ANTHROPIC_AUTH_TOKEN": "x", "ANTHROPIC_BASE_URL": "https://x",
        "ARK_API_KEY": "x", "OPENAI_API_KEY": "x",
        "HTTPS_PROXY": "http://proxy", "PATH": "/usr/bin", "LANG": "C.UTF-8",
        "MUJOCO_GL": "egl",
    }
    env = rf.sanitized_env(fake)
    assert env == {"PATH": "/usr/bin", "LANG": "C.UTF-8", "MUJOCO_GL": "egl"}


# --------------------------------------------------------------------------
# AC-T2-8 宿主后端不得填容器身份
# --------------------------------------------------------------------------

def test_ac_p0_2_8_host_not_container(host_fingerprint: dict):
    container = host_fingerprint["container"]
    assert container["backend"] == "host"
    assert container["image_name"] is None
    assert container["image_digest"] is None
    assert container["image_id"] is None
    assert container["container_id"] is None
    assert container["container_python"] is None
    # 宿主内核只出现在 host 段（如上）；不得被写进 container_python 冒充容器
    assert container["kernel"]
    # schema 的 allOf if/then 也机判这条（反例必须被拒）
    bad = json.loads(json.dumps(host_fingerprint))
    bad["container"]["image_digest"] = _LIBERO_DIGEST
    bad["fingerprint_sha256"] = rf.fingerprint_sha256(bad)
    with pytest.raises(rf.FingerprintError) as excinfo:
        rf.validate_fingerprint(bad)
    assert "不合规" in str(excinfo.value)


# --------------------------------------------------------------------------
# 支撑断言：Task 2 指纹证据件在场且可复验
# --------------------------------------------------------------------------

@pytest.fixture
def bench(monkeypatch) -> Path:
    monkeypatch.delenv("SIMPRAXIS_PHASE2_ROOT", raising=False)
    from orchestrator.benchmark import paths
    root = paths.bench_root()
    assert root.is_dir(), f"数据根不在场：{root}"
    return root


def test_task2_fingerprint_evidence(bench: Path):
    for task_id in (_M1, _L1):
        directory = bench / "fingerprints" / task_id
        doc = rf.load_fingerprint(directory / rf.FINGERPRINT_NAME)
        assert doc["task_id"] == task_id
        sidecar = (directory / rf.SHA256_NAME).read_text(encoding="utf-8").split()
        assert sidecar == [doc["fingerprint_sha256"], rf.FINGERPRINT_NAME]
        meta = json.loads((directory / rf.META_NAME).read_text(encoding="utf-8"))
        assert meta["backend"] in ("host", "docker")
        assert meta["fingerprint_sha256"] == doc["fingerprint_sha256"]
        # 冻结件与当下环境身份必须仍相符（task.yaml 的 runtime 段）
        spec = _spec(task_id)
        requirement = dict(spec.runtime)
        mismatches = rf.compare(requirement, doc, spec=spec)
        assert mismatches == [], [m.to_json() for m in mismatches]
