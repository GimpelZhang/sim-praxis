"""scripts/phase2/provision_v1_vram.py 纯函数单测（0 付费、不跑真实回放）。

只测路径/命令构造、幂等与 --force 判定、sidecar 契约校验；run_replay/main
（真实宿主 GPU 回放）在 lead 三签后由主会话触发，本测绝不进入。
SIMPRAXIS_PHASE2_ROOT 由 tests/conftest.py autouse 夹具重定向到 tmp。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from orchestrator.graph import task8
from orchestrator.graph import task6 as task6_mod

_REPO_ROOT = Path(__file__).resolve().parents[2]

THREAD = "t6-genesis-ascent-20261016a"


def _load_provision():
    path = _REPO_ROOT / "scripts" / "phase2" / "provision_v1_vram.py"
    spec = importlib.util.spec_from_file_location(
        "phase2_provision_v1_vram", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


prov = _load_provision()


# ---------------------------------------------------------------------------
# 路径构造
# ---------------------------------------------------------------------------

def test_replay_workdir_and_target_paths():
    wd = prov.replay_workdir(THREAD)
    assert wd == (task6_mod.phase2_runs_root() / "task6_ascent"
                  / f"v1_vram_replay_{THREAD}")
    assert wd.name == f"v1_vram_replay_{THREAD}"
    dst = prov.target_sidecar(THREAD)
    assert dst == (task6_mod.sources_dir(THREAD) / "v1_vram_peak.json")
    assert dst.name == "v1_vram_peak.json"
    # 与 task6 接缝读取的 V1 sidecar 路径逐字同构（materialize_v1_summary）。
    assert dst == task6_mod.sources_dir(THREAD) / f"v1_{task6_mod.VRAM_NAME}"


def test_default_thread_matches_lead_design():
    # main 的 --thread default 必须引用同一常量（argparse 在 main 内构造，
    # 此处钉死默认值本身，防漂移到他线程 evolution_sources）。
    assert prov.DEFAULT_THREAD == THREAD


# ---------------------------------------------------------------------------
# wrapper cmd 与 env_extra：必须与 task6 工厂 genesis 场景同形同源
# ---------------------------------------------------------------------------

def test_build_wrapper_cmd_matches_factory_shape(tmp_path):
    wd = tmp_path / "replay"
    wd.mkdir()
    entry = wd / "genesis_humanoid_dr.py"
    entry.write_text("# frozen v1\n")
    cal = wd / task8.CALIBRATION_NAME
    cmd = prov.build_wrapper_cmd(wd, entry, cal)
    assert len(cmd) == 14
    assert cmd[0] == task8.VENV_PY
    assert Path(cmd[1]) == prov.GPU_SAMPLER
    assert prov.GPU_SAMPLER.is_file()
    assert cmd[2:4] == ["--sidecar", str(wd / task6_mod.VRAM_NAME)]
    assert cmd[4] == "--"
    assert cmd[5:] == [
        task8.VENV_PY, str(entry), "batch",
        "--out-dir", str(wd), "--seeds", str(task8.SEEDS),
        "--calibration-json", str(cal)]


def test_genesis_env_extra_same_as_factory():
    env = prov.genesis_env_extra()
    assert env == {
        "HOME": "/mnt/caches/home",
        "HF_HOME": "/mnt/caches/hf",
        "HF_ENDPOINT": "https://hf-mirror.com",
        "SIMPRAXIS_USE_PROXY": "0",
        "GS_ENABLE_ZEROCOPY": "0",
        "PYOPENGL_PLATFORM": "egl",
    }
    # 同源断言：ENGINE_SCENARIOS 改字，本工具与 task6 工厂必须同时被带到。
    assert dict(env) == {
        **{"HOME": "/mnt/caches/home", "HF_HOME": "/mnt/caches/hf",
           "HF_ENDPOINT": "https://hf-mirror.com",
           "SIMPRAXIS_USE_PROXY": "0"},
        **task8.ENGINE_SCENARIOS["genesis"]["env"]}


# ---------------------------------------------------------------------------
# prepare_workdir：幂等/拒绝/--force
# ---------------------------------------------------------------------------

def test_prepare_workdir_fresh_creates():
    wd = prov.replay_workdir("t6-prep-fresh")
    assert not wd.exists()
    out = prov.prepare_workdir("t6-prep-fresh", force=False)
    assert out == wd and wd.is_dir()


def test_prepare_workdir_refuses_when_sidecar_present():
    thread = "t6-prep-refuse"
    wd = prov.prepare_workdir(thread, force=False)
    (wd / task6_mod.VRAM_NAME).write_text('{"vram_peak_gb": 20.0}')
    (wd / "junk.txt").write_text("x")
    with pytest.raises(FileExistsError, match="--force"):
        prov.prepare_workdir(thread, force=False)
    # 拒绝时原产物一字未动。
    assert (wd / task6_mod.VRAM_NAME).is_file()
    assert (wd / "junk.txt").read_text() == "x"


def test_prepare_workdir_force_clears_own_dir():
    thread = "t6-prep-force"
    wd = prov.prepare_workdir(thread, force=False)
    (wd / task6_mod.VRAM_NAME).write_text('{"vram_peak_gb": 20.0}')
    nested = wd / "telemetry"
    nested.mkdir()
    (nested / "x.json").write_text("{}")
    out = prov.prepare_workdir(thread, force=True)
    assert out == wd and wd.is_dir()
    # 目录被清空重建：旧 sidecar/子目录全部消失。
    assert list(wd.rglob("*")) == []


def test_prepare_workdir_reuses_dir_without_sidecar():
    # 中断的半截回放（无 sidecar）：不加 --force 也复用，stage 幂等不覆盖。
    thread = "t6-prep-partial"
    wd = prov.prepare_workdir(thread, force=False)
    (wd / "partial.log").write_text("partial")
    out = prov.prepare_workdir(thread, force=False)
    assert out == wd and (wd / "partial.log").is_file()


def test_guarded_replay_path_rejects_foreign_and_wrong_prefix(tmp_path):
    # 自有 base 之外：拒绝。
    foreign = tmp_path / "v1_vram_replay_evil"
    foreign.mkdir()
    with pytest.raises(RuntimeError, match="删除守卫"):
        prov._guarded_replay_path(foreign)
    # base 内但前缀不符：拒绝。
    base = task6_mod.phase2_runs_root() / "task6_ascent"
    base.mkdir(parents=True, exist_ok=True)
    wrong = base / "run_other_999"
    wrong.mkdir()
    with pytest.raises(RuntimeError, match="删除守卫"):
        prov._guarded_replay_path(wrong)
    # 合字面路径：通过（本测不删任何东西）。
    good = base / "v1_vram_replay_guarded-ok"
    good.mkdir()
    prov._guarded_replay_path(good)


# ---------------------------------------------------------------------------
# install_sidecar：契约校验 + 0600 + 幂等/异内容 --force
# ---------------------------------------------------------------------------

def _wrapper_sidecar(workdir: Path, peak=20.5, *, mutate_key=None) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    doc = {"vram_peak_gb": peak, "vram_baseline_gb": 0.014,
           "samples": 128, "device_name": "A800",
           "child_rc": 0, "elapsed_s": 812.4}
    if mutate_key is not None:
        doc[task6_mod.VRAM_KEY] = mutate_key
    p = workdir / task6_mod.VRAM_NAME
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def test_install_sidecar_written_0600_and_summary(tmp_path):
    wd = tmp_path / "replay"
    _wrapper_sidecar(wd, 20.5)
    info = prov.install_sidecar("t6-install", wd, force=False)
    dst = Path(info["installed_sidecar"])
    assert dst == prov.target_sidecar("t6-install")
    assert dst.stat().st_mode & 0o777 == 0o600
    assert info["action"] == "written"
    assert info["vram_peak_gb"] == pytest.approx(20.5)
    assert info["samples"] == 128 and info["device_name"] == "A800"
    assert json.loads(dst.read_text())["child_rc"] == 0


def test_install_sidecar_idempotent_same_bytes(tmp_path):
    wd = tmp_path / "replay"
    _wrapper_sidecar(wd, 20.5)
    first = prov.install_sidecar("t6-idem", wd, force=False)
    mtime = Path(first["installed_sidecar"]).stat().st_mtime_ns
    second = prov.install_sidecar("t6-idem", wd, force=False)
    assert second["action"] == "idempotent"
    assert second["installed_sidecar"] == first["installed_sidecar"]
    assert Path(second["installed_sidecar"]).stat().st_mtime_ns == mtime


def test_install_sidecar_different_content_needs_force(tmp_path):
    wd = tmp_path / "replay"
    p1 = _wrapper_sidecar(wd, 20.5)
    prov.install_sidecar("t6-diff", wd, force=False)
    # 新回放产出异内容（峰值变化）：无 --force 拒绝，旧件保留。
    p1.write_text(json.dumps({"vram_peak_gb": 21.25}), encoding="utf-8")
    with pytest.raises(FileExistsError, match="--force"):
        prov.install_sidecar("t6-diff", wd, force=False)
    info = prov.install_sidecar("t6-diff", wd, force=True)
    assert info["action"] == "overwritten"
    assert json.loads(Path(info["installed_sidecar"]).read_text())[
        "vram_peak_gb"] == pytest.approx(21.25)
    assert Path(info["installed_sidecar"]).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf"), "20"])
def test_install_sidecar_rejects_bad_contract(tmp_path, bad):
    wd = tmp_path / "replay"
    _wrapper_sidecar(wd, mutate_key=bad)
    with pytest.raises((ValueError, TypeError)):
        prov.install_sidecar("t6-badcontract", wd, force=False)
    assert not prov.target_sidecar("t6-badcontract").is_file()
