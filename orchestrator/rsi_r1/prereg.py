"""RSI-R1 仪器校准预注册（T1a；AC06 的「先注册」半边）。

计划 §3.4：任务规格/阈值/校准种子必须在**第一次真实校准之前**锁定。本模块把
「用什么仪器、量什么、判什么界、在哪一步会 DESIGN_NOT_READY」写死成一份件，
并给出两个机械断言：

* ``assert_prereg_matches_sources``：预注册里的量表/场景/指标 hash 必须与当前
  代码逐字节一致（改 δ/改域 ⇒ 旧预注册立即失效，AC09）；
* ``assert_prereg_precedes_raw``：预注册的 ``registered_at_utc`` 必须早于该
  study 的第一件 raw 的 mtime（同秒也算不合格：宁可重注册，不可事后补）。

本模块只读代码常量、只写本 campaign 的 ``contract/``；不触网、不跑仿真。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import observations as O
from orchestrator.rsi_r1 import rubric as R
from orchestrator.rsi_r1 import scenes as S

PREREG_SCHEMA = f"{C.SCHEMA_PREFIX}.instrument_prereg.v1"


class PreregError(RuntimeError):
    """预注册缺失/失配（fail-closed：不允许用事后件冒充事前件）。"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _scene_digest(doc: S.SceneDoc) -> str:
    return C.canonical_sha256(doc.to_dict())


def _metric_table() -> "dict[str, Any]":
    return {k: {"unit": v["unit"], "delta": str(v["delta"]),
                "study": v["study"], "censoring": v["censoring"]}
            for k, v in sorted(R.METRICS.items())}


def build_instrument_prereg(*, envelope_id: str,
                            plan_sha256: "str | None" = None,
                            model_config_sha256: "str | None" = None,
                            registered_at_utc: "str | None" = None) -> "dict[str, Any]":
    """构造仪器预注册文档（纯函数；不写盘）。"""
    now = registered_at_utc or utc_now_iso_z()
    doc = {
        "schema": PREREG_SCHEMA,
        "schema_version": C.SCHEMA_VERSION,
        "envelope_id": C.require_safe_id(envelope_id, where="envelope_id"),
        "registered_at_utc": now,
        "plan_sha256": plan_sha256,
        "model_config_sha256": model_config_sha256,
        "rubric_sha256": R.rubric_sha256(),
        "rubric": R.rubric_document(),
        "scenes": {
            sid: {"digest": _scene_digest(doc_), "doc": doc_.to_dict(),
                  "raw_fields": list(O.required_fields(sid))}
            for sid, doc_ in sorted(S.SCENE_REGISTRY.items())
        },
        "calibration": {
            "study_a": {
                "negative_control": dict(S.SCENE_A_NEGATIVE_CONTROL),
                "sensitivity_control": {
                    "scene_id": S.SCENE_A,
                    "mu_low": str(R.SENSITIVITY_MU_LOW),
                    "mu_high": str(R.SENSITIVITY_MU_HIGH),
                    "mass_kg": str(R.SENSITIVITY_MASS_KG),
                    "v0_mps": str(R.SENSITIVITY_V0_MPS),
                    "min_displacement_diff_m": str(
                        R.SENSITIVITY_MIN_DISPLACEMENT_DIFF_M),
                    "verdict_if_failed": "DESIGN_NOT_READY",
                    "note": "不换 seed、不换阈值救场；判负即停",
                },
                "exposure_checks": [
                    "物体确实在动（初速生效）",
                    "两侧有效接触且实际 condim 恒为 3",
                    "两条件只有许可字段不同（仅 torsional_friction_m）",
                ],
            },
            "study_b": {
                "note": ("维护校准只检查执行/测量/可辨识性，不为开放主张选赢家；"
                         "不登记任何 expected_label"),
                "execution_checks": [
                    "Tc/dt 用整数微秒整除，t=0 更新、零阶保持",
                    "扭矩在每次更新后限幅，记录饱和次数",
                    "失稳定义（|q|>4 rad / 非有限）事前固定",
                ],
            },
        },
        "claims": {
            "propositions": list(R.PROPOSITIONS),
            "ci_primary": str(R.CI_PRIMARY),
            "ci_negative": str(R.CI_NEGATIVE),
            "bootstrap": {"resamples": R.BOOTSTRAP_RESAMPLES,
                          "unit": R.BOOTSTRAP_UNIT,
                          "quantile": R.BOOTSTRAP_QUANTILE},
            "claim_tolerance": {"abs": str(R.CLAIM_ABS_TOL),
                                "rel": str(R.CLAIM_REL_TOL)},
        },
        "replay": {
            "tolerances": {k: {"abs": str(a), "rel": str(r)}
                           for k, (a, r) in sorted(R.REPLAY_TOLERANCES.items())},
            "default": {"abs": str(R.REPLAY_DEFAULT_TOLERANCE[0]),
                        "rel": str(R.REPLAY_DEFAULT_TOLERANCE[1])},
        },
        "data_split": R.rubric_document()["data_split"],
        "units": {
            "length": "m", "speed": "m/s", "mass": "kg", "force": "N",
            "torque": "Nm", "angle": "rad", "time": "s", "friction": "1",
            "torsional_friction": "m", "inertia": "kg·m^2",
        },
        "no_reference_solution": True,
        "no_expected_label": True,
    }
    return doc


def document_sha256(doc: Mapping[str, Any]) -> str:
    return C.canonical_sha256(doc)


def assert_prereg_matches_sources(doc: Mapping[str, Any]) -> None:
    """预注册必须与当前量表/场景逐字节一致（改 δ/域 ⇒ 旧件作废）。"""
    if doc.get("schema") != PREREG_SCHEMA:
        raise PreregError("SCHEMA_MISMATCH", f"schema={doc.get('schema')!r}")
    current_rubric = R.rubric_sha256()
    if doc.get("rubric_sha256") != current_rubric:
        raise PreregError(
            "RUBRIC_DRIFT",
            f"预注册量表 sha={doc.get('rubric_sha256')} 与当前 {current_rubric} 不符；"
            "量表在注册后被改动，必须重注册（AC09）")
    stored = doc.get("rubric")
    if not isinstance(stored, Mapping):
        raise PreregError("MISSING_KEY", "缺 rubric 正文")
    if C.canonical_sha256(stored) != current_rubric:
        raise PreregError("RUBRIC_DRIFT", "预注册 rubric 正文与 rubric_sha256 不自洽")
    for sid, entry in (doc.get("scenes") or {}).items():
        doc_now = S.scene_doc(sid)
        if entry.get("digest") != _scene_digest(doc_now):
            raise PreregError("SCENE_DRIFT", f"场景 {sid} 在注册后被改动")
        if entry.get("doc") != doc_now.to_dict():
            raise PreregError("SCENE_DRIFT", f"场景 {sid} 正文与 digest 不自洽")
    if set(doc.get("scenes") or {}) != set(S.SCENE_REGISTRY):
        raise PreregError("SCENE_DRIFT", "预注册场景集合与当前注册表不符")


def _parse_z(ts: str) -> datetime:
    if not isinstance(ts, str) or not ts.endswith("Z"):
        raise PreregError("TIMESTAMP", f"时间戳必须严格以 Z 结尾：{ts!r}")
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def assert_prereg_precedes_raw(doc: Mapping[str, Any],
                               raw_paths: "list[Path] | tuple[Path, ...]") -> None:
    """预注册时刻必须**严格早于**任何 raw 文件的 mtime（AC06）。"""
    registered = _parse_z(doc["registered_at_utc"])
    for p in raw_paths:
        path = Path(p)
        if not path.exists():
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if mtime <= registered:
            raise PreregError(
                "PREREG_NOT_FIRST",
                f"{path} 的 mtime {mtime.isoformat()} 不晚于预注册 "
                f"{registered.isoformat()}；不可用事后件冒充事前件")


def write_prereg(ctx, *, envelope_id: str, plan_sha256: str,
                 model_config_sha256: "str | None") -> "dict[str, Any]":
    """写 ``contract/instrument_prereg.json``；已存在且来源未变则幂等复用。"""
    rel = "contract/instrument_prereg.json"
    if ctx.exists(rel):
        existing = ctx.read_json(rel)
        try:
            assert_prereg_matches_sources(existing)
        except PreregError:
            raise PreregError(
                "PREREG_DRIFT",
                "已存在预注册与当前量表/场景不符；删除旧件并重新注册（不得静默覆盖）")
        if existing.get("envelope_id") != envelope_id:
            raise PreregError("ENVELOPE_MISMATCH", "预注册属于另一个 envelope")
        return existing
    doc = build_instrument_prereg(
        envelope_id=envelope_id, plan_sha256=plan_sha256,
        model_config_sha256=model_config_sha256)
    doc["document_sha256"] = document_sha256(
        {k: v for k, v in doc.items() if k != "document_sha256"})
    ctx.write_json(rel, doc)
    return doc
