"""Phase 2.8.1 T2：**每 Trial 一份**运行时证明（计划 §6.5，C6 的三件补强）。

Phase 2.8.0 已经把「采集指纹 / 与 ``task.yaml.runtime`` 比对 / 不符即 fail-closed」
做成生产件（``orchestrator.benchmark.runtime_fingerprint``）。本模块**不重写**任何
比对逻辑，只补计划 §6.5 点名的三件事：

1. **粒度**：由「每任务一份」改为「**每 Trial 一份**」，落在
   ``<trial_dir>/runtime_attestation.json``，且**必须在候选启动之前**采集
   （AC-P281-2-13 用「attestation 的 ``written_at`` 早于事件流首事件时间戳」机判
   ——采完再跑，而不是跑完再补）；
2. **判定归一**：``FingerprintError`` 一律翻译成 ``verdict=BLOCKED_INFRA``
   （AC-P281-2-14）。环境不符**不是**能力失败，不许落成 ``FAIL``，更不许让异常
   逃逸出去把一次 Trial 记成崩溃；
3. **不可绕过**：入口先调 ``assert_no_benchmark_root_env()``（P281-0 已锁），
   免得有人把 ``SIMPRAXIS_BENCHMARK_ROOT`` 指进来。

**诚实边界（报告里不得写强，与 §6.5 同口径）**：冻结 schema 的
``task.yaml.runtime`` 只收 ``runtime_id / image_digest / python / packages`` 四键，
所以 ``compare()`` 在 2.8.1 里**真正生效的仍是那四类码**
（``PRODUCTION_WIRED_CODES``），其余八类只是该函数的 API 能力、生产恒不触发。
**不得**写「运行时十类全覆盖」。

另一条要如实登记的边界：本模块证明的是「**采集那一刻**的环境身份与要求相符」。
它不阻止候选在采集之后改环境（回环挂载、``pip install --user``、改
``sitecustomize`` 等）——那需要 OS 级沙箱，属 Phase 3 backlog；这里能保证的是
「采集早于启动」这条时序，以及事件流里没有出现与环境身份矛盾的证据。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from orchestrator.benchmark import runtime_fingerprint as rf
from orchestrator.benchmark import task_spec as ts
from orchestrator.benchmark.phase281 import birth_protocol as P
from orchestrator.benchmark.phase281.paths import assert_no_benchmark_root_env
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

__all__ = (
    "ATTESTATION_NAME", "ATTESTATION_SCHEMA", "VERDICT_BLOCKED", "VERDICT_PASS",
    "InfraBlocked", "RuntimeAttestation",
)

ATTESTATION_NAME = "runtime_attestation.json"
ATTESTATION_SCHEMA = "simpraxis.phase281.runtime_attestation.v1"
VERDICT_PASS = "PASS"
VERDICT_BLOCKED = "BLOCKED_INFRA"


class InfraBlocked(RuntimeError):
    """环境与要求不符：**基础设施阻塞**，不是能力失败（AC-P281-2-14）。"""

    def __init__(self, problems: "list[Mapping[str, Any]]", detail: str = "") -> None:
        self.problems = [dict(item) for item in problems]
        text = detail or "; ".join(f"{p.get('code')}: {p.get('detail')}"
                                   for p in self.problems)
        super().__init__(f"BLOCKED_RUNTIME_MISMATCH: {text}")
        self.detail = text

    def to_json(self) -> dict:
        return {"verdict": VERDICT_BLOCKED, "reason": "blocked_infra",
                "detail": self.detail, "problems": list(self.problems)}


def _as_problems(mismatches: "list[rf.Mismatch]") -> "list[dict]":
    return [{"code": m.code, "detail": m.detail} for m in mismatches]


class RuntimeAttestation:
    """薄封装：``capture_for_trial`` / ``check`` / ``require`` / ``gate``。"""

    # -- 采集 ---------------------------------------------------------------
    @staticmethod
    def capture_for_trial(spec: ts.TaskSpec, trial_dir: "str | Path", *,
                          image: "str | None" = None,
                          python: "str | None" = None) -> dict:
        """采集 → 组装 → 落盘 → 返回 ``{"fingerprint", "sha256", ...}``。

        必须在**候选启动之前**调用（AC-P281-2-13）。同一 Trial 目录重复采集时：
        指纹逐字节相同 → 幂等返回既有件；不同 → 拒绝（Trial 的环境身份一旦冻结
        不得换，换了就是两次不同条件的运行）。
        """
        assert_no_benchmark_root_env()
        directory = ensure_private_dir(Path(trial_dir))
        doc, capture = rf.capture_and_build(spec, image=image, python=python)
        written_at = P.now_wall()
        attestation = {
            "schema": ATTESTATION_SCHEMA,
            "task_id": spec.task_id,
            "trial_id": directory.name,
            "written_at": written_at,
            "fingerprint": doc,
            "fingerprint_sha256": str(doc["fingerprint_sha256"]),
            "capture": capture.meta(),
        }
        target = directory / ATTESTATION_NAME
        body = json.dumps(attestation, ensure_ascii=False, indent=2,
                          sort_keys=True) + "\n"
        if target.is_symlink():
            raise InfraBlocked([{"code": "symlink",
                                 "detail": f"{target} 是符号链接：拒绝写入"}])
        if target.is_file():
            existing = json.loads(target.read_text(encoding="utf-8"))
            if (existing.get("fingerprint_sha256")
                    == attestation["fingerprint_sha256"]):
                # 幂等：同一份环境身份重采一次不算换条件（不重写、不刷新时点）
                return {"fingerprint": existing["fingerprint"],
                        "sha256": str(existing["fingerprint_sha256"]),
                        "written_at": existing.get("written_at"),
                        "path": str(target), "reused": True}
            raise InfraBlocked([{
                "code": "fingerprint_frozen",
                "detail": (f"{target} 已冻结另一份环境身份 "
                           f"({existing.get('fingerprint_sha256')} != "
                           f"{attestation['fingerprint_sha256']})：拒绝覆盖")}])
        path = atomic_write_text(target, body, 0o600)
        return {"fingerprint": doc, "sha256": attestation["fingerprint_sha256"],
                "written_at": written_at, "path": str(path), "reused": False}

    # -- 读回 ---------------------------------------------------------------
    @staticmethod
    def load(trial_dir: "str | Path") -> dict:
        """读回 ``runtime_attestation.json``（缺失即 ``InfraBlocked``）。"""
        target = Path(trial_dir) / ATTESTATION_NAME
        if not target.is_file():
            raise InfraBlocked([{"code": "attestation_missing",
                                 "detail": f"{target} 不存在"}])
        doc = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or doc.get("schema") != ATTESTATION_SCHEMA:
            raise InfraBlocked([{
                "code": "attestation_invalid",
                "detail": f"{target}: schema={doc.get('schema')!r}"
                          f" 非 {ATTESTATION_SCHEMA}"}])
        return doc

    # -- 运行后回填契约段（锁内适配，P281-6） ---------------------------------
    @staticmethod
    def backfill_contract(trial_dir: "str | Path", *,
                          probe: Mapping) -> dict:
        """把**候选探针**实测的观测/动作契约回填进已冻结指纹的 ``simulator`` 段。

        为什么必须在运行**之后**补：``capture_and_build`` 在候选启动前采集，
        那时 ``env_probe.json`` 还不存在，``_simulator_segment`` 只能把两段契约
        留成 ``None``（它没有 ``simulator=`` 入口）。而 ``task_prompt`` 的
        ``HARNESS_OUTPUTS`` 注释写的就是本阶段的语义——「L1 的动作/观测契约取自
        候选的 ``env_probe.json``」。不回填，``scoring.libero_facts`` 的
        ``contract_match`` 恒为 ``False``（「无可对契约」），0.2 权重的契约维度
        永远拿不到满分：L1 结构上不可能 PASS，H1 会变成空假设。

        这是**自洽检查**而不是真值检查（与 Phase 2.8.0 的参考运行同语义）：比的是
        「候选自报的探针」与「候选自报的运行身份」是否一致。指纹的环境身份段
        （container / python / mount / config / repository）一个字节都不动——动的
        只有 ``simulator.observation_contract`` / ``action_contract`` 两个空位，
        且回填后**重新封印**（``fingerprint_sha256`` 重算），并同步
        ``runtime_attestation.json`` 的副本，留下 before/after 与来源。
        ``written_at`` 不刷新：采集时点仍然早于候选启动（AC-P281-2-13 的时序不破）。

        探针不可用（缺键、类型不符）⇒ 原样返回、不改文件：候选自己没给出可用契约，
        就让它照实留 ``False``，不许评测侧替它编一个。
        """
        directory = Path(trial_dir)
        delivered = directory / "delivered"
        fp_path = delivered / rf.FINGERPRINT_NAME
        if fp_path.is_symlink() or not fp_path.is_file():
            return {"updated": False, "reason": "fingerprint_absent"}
        doc = json.loads(fp_path.read_text(encoding="utf-8"))
        try:
            rf.validate_fingerprint(doc)
        except rf.FingerprintError as exc:
            return {"updated": False, "reason": "fingerprint_invalid",
                    "detail": str(exc)}

        keys = probe.get("observation_keys") if isinstance(probe, Mapping) else None
        dim = probe.get("action_dim") if isinstance(probe, Mapping) else None
        keys_ok = isinstance(keys, (list, tuple)) and len(keys) > 0
        dim_ok = (isinstance(dim, int) and not isinstance(dim, bool) and dim > 0)

        segment = dict(doc.get("simulator") or {})
        changed = False
        if keys_ok:
            obs = dict(segment.get("observation_contract") or {})
            if obs.get("keys") is None:
                obs = {"keys": [str(key) for key in keys],
                       "dtype": obs.get("dtype"),
                       "shape": list(obs.get("shape") or [])}
                segment["observation_contract"] = obs
                changed = True
        if dim_ok:
            act = dict(segment.get("action_contract") or {})
            if act.get("dim") is None:
                act = {"space": act.get("space") or "Box", "dim": int(dim),
                       "low": list(act.get("low") or []),
                       "high": list(act.get("high") or [])}
                segment["action_contract"] = act
                changed = True
        if not changed:
            return {"updated": False, "reason": "no_contract_in_probe"}

        before = str(doc.get("fingerprint_sha256") or "")
        doc["simulator"] = segment
        doc["fingerprint_sha256"] = rf.fingerprint_sha256(doc)
        rf.validate_fingerprint(doc)
        atomic_write_text(fp_path, ts.canonical_json(doc) + "\n", 0o600)
        atomic_write_text(delivered / rf.SHA256_NAME,
                          f"{doc['fingerprint_sha256']}  {rf.FINGERPRINT_NAME}\n",
                          0o600)

        att_path = directory / ATTESTATION_NAME
        if att_path.is_file() and not att_path.is_symlink():
            attestation = json.loads(att_path.read_text(encoding="utf-8"))
            attestation["fingerprint"] = doc
            attestation["fingerprint_sha256"] = doc["fingerprint_sha256"]
            attestation["contract_backfill"] = {
                "source": "env_probe.json",
                "before": before,
                "after": doc["fingerprint_sha256"],
                "at": P.now_wall(),
            }
            atomic_write_text(att_path, json.dumps(
                attestation, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                0o600)
        return {"updated": True, "source": "env_probe.json", "before": before,
                "fingerprint_sha256": doc["fingerprint_sha256"]}

    # -- 比对 ---------------------------------------------------------------
    @staticmethod
    def check(spec: ts.TaskSpec, attestation: Mapping) -> "list[dict]":
        """``compare()`` → ``[{"code","detail"}]``；**空列表**才表示相符。

        指纹自身的封印（``fingerprint_sha256``）先验一遍：一份「被改过但没重新
        封印」的指纹本身就是基础设施问题，不是「不符项」，故如实报
        ``attestation_invalid`` 而不是拿它去比对。
        """
        doc = dict(attestation.get("fingerprint") or {})
        try:
            rf.validate_fingerprint(doc)
        except rf.FingerprintError as exc:
            return [{"code": "attestation_invalid", "detail": str(exc)}]
        try:
            return _as_problems(rf.compare(spec.runtime, doc, spec=spec))
        except rf.FingerprintError as exc:              # pragma: no cover - 防御
            return [{"code": "attestation_invalid", "detail": str(exc)}]

    @staticmethod
    def require(spec: ts.TaskSpec, attestation: Mapping) -> None:
        """不符即抛 :class:`InfraBlocked`（调用方翻译成 ``BLOCKED_INFRA``）。"""
        problems = RuntimeAttestation.check(spec, attestation)
        if problems:
            raise InfraBlocked(problems)

    # -- 跑步器入口（恒不抛） -------------------------------------------------
    @staticmethod
    def gate(spec: ts.TaskSpec, trial_dir: "str | Path", *,
             image: "str | None" = None,
             attestation: "Mapping | None" = None) -> dict:
        """采集或读回 + 比对，**把一切 ``FingerprintError`` 翻译成 ``BLOCKED_INFRA``**。

        返回值恒是判决件而不是异常——AC-P281-2-14 要的就是这个：环境不符时
        ``verdict`` 为 ``BLOCKED_INFRA``，**不是** ``FAIL``，也不是崩溃。
        捕获失败（探针起不来、docker 不在）同样归 ``BLOCKED_INFRA``。
        """
        out = {"verdict": VERDICT_PASS, "problems": [], "attestation": None,
               "written_at": None, "fingerprint_sha256": None,
               "spec_task": spec.task_id}
        try:
            if attestation is None:
                captured = RuntimeAttestation.capture_for_trial(
                    spec, trial_dir, image=image)
                attestation = RuntimeAttestation.load(trial_dir)
                out["written_at"] = captured.get("written_at")
            out["attestation"] = dict(attestation)
            out["fingerprint_sha256"] = attestation.get("fingerprint_sha256")
            out["written_at"] = out["written_at"] or attestation.get("written_at")
            problems = RuntimeAttestation.check(spec, attestation)
        except InfraBlocked as exc:
            out["verdict"] = VERDICT_BLOCKED
            out["problems"] = list(exc.problems)
            return out
        except rf.FingerprintError as exc:
            out["verdict"] = VERDICT_BLOCKED
            out["problems"] = [{"code": "capture_failed", "detail": str(exc)}]
            return out
        if problems:
            out["verdict"] = VERDICT_BLOCKED
            out["problems"] = problems
        return out
