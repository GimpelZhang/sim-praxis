"""orchestrator/common/gpu_sampler.py 全 hermetic 单测（0 付费、离线、禁真 GPU）。

假 pynvml 经 sys.modules 注入（wrapper 在 main() 内延迟 import），被包裹子进程
是 sys.executable 的轻量 ``-c``（sleep/打印/退出码），全程不碰真 NVML/真显卡。
覆盖：
  a) 正常退出：sidecar 五额外键齐全、vram_peak_gb 正有限、rc 透传；
  b) child rc=7：仍写 sidecar，wrapper rc=7；
  c) nvmlInit 抛错：退出 3 且 subprocess.Popen 绝不被调用；
  d) ``--`` 后空 argv：退出 2；
  e) sidecar 文件模式严格 0600；
  f) 设备读数全 0 / NaN：退出 4 且不写 sidecar；
  g) SIGTERM/SIGINT 转发：处理函数级单测（不真发进程组信号，避免 flaky）。
"""
from __future__ import annotations

import json
import math
import signal
import sys
import types

import pytest

from orchestrator.common import gpu_sampler

_MB = 1024 ** 2
_GB = 1024 ** 3


class _FakeMem:
    def __init__(self, used):
        self.used = used


class FakeNvml:
    """脚本化 usedMemory 序列的假 pynvml（设备 0 单设备口径）。"""

    def __init__(self, used_sequence, *, init_error=None,
                 device_name="FakeGPU-0"):
        self._seq = list(used_sequence)
        self._init_error = init_error
        self._device_name = device_name
        self.inited = False
        self.shutdown = False
        self.queries = 0

    def nvmlInit(self):
        if self._init_error is not None:
            raise self._init_error
        self.inited = True

    def nvmlDeviceGetHandleByIndex(self, idx):
        assert idx == 0
        return ("fake-handle",)

    def nvmlDeviceGetName(self, handle):
        return self._device_name

    def nvmlDeviceGetMemoryInfo(self, handle):
        assert handle == ("fake-handle",)
        # 序列耗尽后重复末值（真实采样次数随调度抖动，只钉死峰值语义）。
        v = self._seq[min(self.queries, len(self._seq) - 1)]
        self.queries += 1
        if isinstance(v, float) and math.isnan(v):
            return _FakeMem(float("nan"))
        return _FakeMem(v)

    def nvmlShutdown(self):
        self.shutdown = True


@pytest.fixture
def fake_nvml(monkeypatch):
    def _install(used_sequence, **kw):
        fake = FakeNvml(used_sequence, **kw)
        monkeypatch.setitem(sys.modules, "pynvml", fake)
        return fake
    return _install


def _run(sidecar, *child_code, interval="0.01"):
    return gpu_sampler.main(
        ["--sidecar", str(sidecar), "--interval", interval, "--",
         sys.executable, "-c", *child_code])


# ---------------------------------------------------------------------------
# a) 正常退出：sidecar 五额外键齐全 + 峰值正有限 + rc 透传
# ---------------------------------------------------------------------------

def test_normal_exit_writes_full_sidecar(tmp_path, fake_nvml):
    fake_nvml([14 * _MB, 512 * _MB, 2 * _GB, 2 * _GB])
    sidecar = tmp_path / "vram_peak.json"
    code = "import sys,time; sys.stdout.write('child-ready\\n'); time.sleep(0.25)"
    rc = _run(sidecar, code)
    assert rc == 0
    doc = json.loads(sidecar.read_text(encoding="utf-8"))
    assert set(doc) == {
        "vram_peak_gb", "vram_baseline_gb", "samples",
        "device_name", "child_rc", "elapsed_s"}
    assert doc["vram_peak_gb"] == pytest.approx(2.0)
    assert math.isfinite(doc["vram_peak_gb"]) and doc["vram_peak_gb"] > 0
    assert doc["vram_baseline_gb"] == pytest.approx(14 / 1024)
    assert isinstance(doc["samples"], int) and doc["samples"] >= 1
    assert doc["device_name"] == "FakeGPU-0"
    assert doc["child_rc"] == 0
    assert doc["elapsed_s"] >= 0.25 - 0.05
    assert fake_nvml  # fixture installed fake; real GPU never opened


# ---------------------------------------------------------------------------
# b) child 非零退出：sidecar 仍写（峰值是设备实测），wrapper 透传 rc
# ---------------------------------------------------------------------------

def test_child_nonzero_rc_still_writes_sidecar(tmp_path, fake_nvml):
    fake_nvml([14 * _MB, 3 * _GB])
    sidecar = tmp_path / "vram_peak.json"
    rc = _run(sidecar, "import time; time.sleep(0.2); import sys; sys.exit(7)")
    assert rc == 7
    doc = json.loads(sidecar.read_text(encoding="utf-8"))
    assert doc["child_rc"] == 7
    assert doc["vram_peak_gb"] == pytest.approx(3.0)
    assert doc["samples"] >= 1


# ---------------------------------------------------------------------------
# c) nvmlInit 失败：退出 3，绝不启动子进程、绝不写 sidecar
# ---------------------------------------------------------------------------

def test_nvml_init_failure_exit3_no_popen_no_sidecar(tmp_path, fake_nvml,
                                                     monkeypatch):
    fake_nvml([1], init_error=RuntimeError("nvml not available"))
    popped: list[list] = []

    def _spy(*a, **kw):
        popped.append([a, kw])
        raise AssertionError("仪器未就绪不得 Popen")

    monkeypatch.setattr(gpu_sampler.subprocess, "Popen", _spy)
    sidecar = tmp_path / "vram_peak.json"
    rc = _run(sidecar, "import time; time.sleep(1)")
    assert rc == gpu_sampler.EXIT_NVML == 3
    assert popped == []
    assert not sidecar.exists()


# ---------------------------------------------------------------------------
# d) `--` 后空 argv：退出 2（usage）
# ---------------------------------------------------------------------------

def test_empty_child_argv_exit2(tmp_path, fake_nvml):
    fake_nvml([1])
    sidecar = tmp_path / "vram_peak.json"
    rc = gpu_sampler.main(
        ["--sidecar", str(sidecar), "--interval", "0.01", "--"])
    assert rc == gpu_sampler.EXIT_USAGE == 2
    assert not sidecar.exists()


def test_bad_interval_exit2(tmp_path, fake_nvml):
    fake_nvml([1])
    sidecar = tmp_path / "vram_peak.json"
    rc = gpu_sampler.main(
        ["--sidecar", str(sidecar), "--interval", "0", "--",
         sys.executable, "-c", "print('x')"])
    assert rc == 2
    assert not sidecar.exists()


# ---------------------------------------------------------------------------
# e) sidecar 权限严格 0600
# ---------------------------------------------------------------------------

def test_sidecar_mode_0600(tmp_path, fake_nvml):
    fake_nvml([14 * _MB, _GB])
    sidecar = tmp_path / "nested" / "vram_peak.json"
    sidecar.parent.mkdir()
    rc = _run(sidecar, "import time; time.sleep(0.15)")
    assert rc == 0
    assert sidecar.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# f) 设备读数非法（全 0 / NaN）：退出 4 且不写 sidecar
# ---------------------------------------------------------------------------

def test_zero_peak_exit4_without_sidecar(tmp_path, fake_nvml):
    fake_nvml([0, 0, 0])
    sidecar = tmp_path / "vram_peak.json"
    rc = _run(sidecar, "import time; time.sleep(0.2)")
    assert rc == gpu_sampler.EXIT_BAD_PEAK == 4
    assert not sidecar.exists()


def test_nan_reading_exit4_without_sidecar(tmp_path, fake_nvml):
    fake_nvml([float("nan")])
    sidecar = tmp_path / "vram_peak.json"
    rc = _run(sidecar, "import time; time.sleep(0.2)")
    assert rc == 4
    assert not sidecar.exists()


# ---------------------------------------------------------------------------
# Popen 失败（入口不存在）：退出 127，不写 sidecar（真 Popen，假 NVML）
# ---------------------------------------------------------------------------

def test_missing_child_exit127(tmp_path, fake_nvml):
    fake_nvml([14 * _MB, _GB])
    sidecar = tmp_path / "vram_peak.json"
    rc = gpu_sampler.main(
        ["--sidecar", str(sidecar), "--interval", "0.01", "--",
         str(tmp_path / "definitely-not-a-real-binary-xyz")])
    assert rc == gpu_sampler.EXIT_NOT_FOUND == 127
    assert not sidecar.exists()


# ---------------------------------------------------------------------------
# g) 信号转发：处理函数级单测（不真发信号给进程组，避免 flaky）
# ---------------------------------------------------------------------------

def test_forward_signal_kills_whole_group(monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(gpu_sampler.os, "killpg",
                        lambda pgid, sig: sent.append((pgid, sig)))
    proc = types.SimpleNamespace(pid=4242)
    gpu_sampler._forward_signal(proc, signal.SIGTERM)
    gpu_sampler._forward_signal(proc, signal.SIGINT)
    assert sent == [(4242, signal.SIGTERM), (4242, signal.SIGINT)]

    # 子进程已退出（ProcessLookupError）：handler 静默，不打断主 wait 收尾。
    def _gone(pgid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(gpu_sampler.os, "killpg", _gone)
    gpu_sampler._forward_signal(proc, signal.SIGTERM)
