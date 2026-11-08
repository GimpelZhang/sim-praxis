"""RSI-R0 固定镜像**单一真值**：按 digest 引用，不按 tag 引用。

计划 §6.1：所有参考与试次都必须跑在同一枚镜像上，因此引用形式固定为
``<repo>@sha256:<digest>``——tag 可以被重新指向，digest 不行。本模块是唯一
出处，脚本与测试都不许抄字面量。

2026-11-08 取证：`docker image inspect sim-praxis/sandbox:phase0` 的 RepoDigest
即 :data:`IMAGE_DIGEST`；容器内 ``env_probe`` 回报 python 3.10.12 / numpy 1.26.4
/ mujoco 3.15.0，与 :data:`EXPECTED` 逐项比对（不符即 BLOCKED_INFRA，不降级）。
"""
from __future__ import annotations

from typing import Any

from orchestrator.rsi_r0.trusted import call_service

REPO = "sim-praxis/sandbox"
DIGEST = "sha256:50b6e10df854db40ea41e33f3ed9731a9d5d35b2d9d2ccf9b9f178d8565e4bfe"

#: 唯一合法的镜像引用（tag 形式一律不用，避免被重新指向）。
IMAGE_REF = f"{REPO}@{DIGEST}"

#: 期望的运行时版本（AC15 同镜像取证；不符 ⇒ BLOCKED_INFRA，绝不换镜像凑）。
EXPECTED = {"python": "3.10.12", "numpy": "1.26.4", "mujoco": "3.15.0"}

#: 环境探针用的最小场景（无光照需求，env_probe 只取版本与 GL 串）。
PROBE_SCENE = """<mujoco model="rsi_r0_env">
  <worldbody><geom name="floor" type="plane" size="1 1 0.1"/>
    <body name="b" pos="0 0 0.1"><freejoint/><geom type="box" size="0.05 0.05 0.05"/></body>
  </worldbody>
</mujoco>
"""


def probe_environment(*, gpus: bool = False, timeout_s: int = 180) -> dict[str, Any]:
    """在固定镜像里跑一次 ``env_probe``，回传版本/GL 事实（不判负，判负在 :func:`verify`）。"""
    return call_service(
        {"schema": "rsi_r0.service.v1", "kind": "env_probe",
         "scene_xml": PROBE_SCENE, "steps": 1},
        image_digest=IMAGE_REF, gpus=gpus, timeout_s=timeout_s)


def verify(*, gpus: bool = False) -> dict[str, Any]:
    """版本逐项比对；任一不符即 ``accepted=False``（调用方须落 BLOCKED_INFRA）。"""
    fact = probe_environment(gpus=gpus)
    mismatches = []
    for key, want in EXPECTED.items():
        got = str(fact.get(f"{key}_version", ""))
        if got != want:
            mismatches.append({"field": key, "expected": want, "actual": got})
    return {
        "image_ref": IMAGE_REF,
        "digest": DIGEST,
        "gpus": bool(gpus),
        "python_version": fact.get("python_version"),
        "numpy_version": fact.get("numpy_version"),
        "mujoco_version": fact.get("mujoco_version"),
        "mujoco_gl_env": fact.get("mujoco_gl_env"),
        "gl_vendor": fact.get("gl_vendor"),
        "gl_renderer": fact.get("gl_renderer"),
        "mismatches": mismatches,
        "accepted": not mismatches,
    }
