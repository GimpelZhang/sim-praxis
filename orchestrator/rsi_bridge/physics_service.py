"""RSI-S1 受信物理服务（S0 残尾①：物理执行面收权）。

口径（计划 S1-1 第 1 条）：

* **host 进程独占 mujoco**：本模块在受信进程里 import mujoco、加载 MJCF、步进、
  观测；候选/actor 沙箱只提交有界参数（经 ``build_host_spec`` 校验），从不执行
  物理（R0「证据语义由可信服务执行」同款）。
* **单点复用**：物理步进仍走已审计的固定脚本
  ``orchestrator.rsi_r1.experiment_service``（run_a/run_b 原文），raw 记录的
  组装/校验仍走 ``backend.PhysicsBackend._build_record``（含
  ``observations.validate_record``），指标由 ``metrics.metrics_for`` 从 raw 重算。
  本模块不复制任何物理/统计实现，只做「进程收权 + 批次编排 + 落盘」。
* **AST 检查降级为辅助证据**：S0 的 ``mj_step`` 源码扫描保留在探针侧，但安全语义
  由本服务的进程边界承担（``mujoco_denial_probe`` 负控三连取证沙箱内不可导入）。

锁内适配（计划 S1-1）：计划要求「host 进程独占 mujoco」；R1 原路径是容器内执行
同一固定脚本。本实现把同一脚本跑在受信宿主进程（simpraxis venv，mujoco 3.15.0
钉死）——脚本字节、场景 XML、raw 校验与指标定义全部不变，只换执行载体；
容器路径（``backend.PhysicsBackend``）保留为历史证据，不改写。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import metrics as M
from orchestrator.rsi_r1 import observations as O
from orchestrator.rsi_r1 import physics as P
from orchestrator.rsi_r1 import scenes as S

__all__ = [
    "SCHEMA", "PhysicsServiceError", "RunResult", "PhysicsService",
    "build_block_assignment", "mujoco_denial_probe", "DENIAL_ROUTES",
]

SCHEMA = "simpraxis.rsi_s1.physics_service.v1"

#: 沙箱收权负控的三条尝试路径（计划 S1-1「负控三连」）。
DENIAL_ROUTES = ("import", "importlib", "getattr")

#: 受信服务绑定的 MuJoCo 版本（与 physics.MUJOCO_VERSION 同一真值）。
_MUJOCO_VERSION = P.MUJOCO_VERSION


class PhysicsServiceError(RuntimeError):
    """受信服务拒绝/失败（带机器码；不吞成科学结果）。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class RunResult:
    """一条受信 run 的结果句柄（record 本体已落盘，句柄只带引用与摘要）。"""

    run_id: str
    study: str
    condition_id: str
    block_id: str
    metric_id: str
    metric_value: str
    record_path: Path
    record_sha256: str
    nsteps: int
    report: Mapping[str, Any] = field(default_factory=dict)

    def row(self) -> dict[str, Any]:
        """run_manifest 行（``statistics.iter_block_pairs`` 直接消费）。"""
        return {"run_id": self.run_id, "study": self.study,
                "condition_id": self.condition_id, "block_id": self.block_id,
                "metric_id": self.metric_id, "value": self.metric_value,
                "record_path": str(self.record_path),
                "record_sha256": self.record_sha256, "nsteps": self.nsteps}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _canonical_sha(doc: Any) -> str:
    payload = json.dumps(doc, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# 私有 block 分配（host 拥有 nuisance 采样权；候选/actor 不可见种子）
# ---------------------------------------------------------------------------

def build_block_assignment(plan_sha256: str, *, study: str, n_block: int,
                           nuisance: Sequence[Mapping[str, Any]],
                           salt: str | None = None) -> list[dict[str, Any]]:
    """确定性私有 block 分配：每 block 一个 nuisance 取值（块内同值）。

    ``nuisance`` 每项形如 ``{"param": "v0_mps", "low": "0.1", "high": "0.6",
    "precision": "0.001"}``。采样种子 = sha256(plan‖study‖block index)，只由 host
    计算；同一 plan 重跑逐位相同（可复现），换 plan 即换分配。
    """
    if not isinstance(plan_sha256, str) or len(plan_sha256) != 64:
        raise PhysicsServiceError("PLAN_SHA_INVALID", f"{plan_sha256!r}")
    if isinstance(n_block, bool) or not isinstance(n_block, int) or n_block <= 0:
        raise PhysicsServiceError("N_BLOCK_INVALID", f"{n_block!r}")
    if not nuisance:
        raise PhysicsServiceError("NUISANCE_EMPTY", "至少一个 nuisance 参数")
    blocks: list[dict[str, Any]] = []
    for index in range(n_block):
        block_id = f"b{index + 1:02d}"
        values: dict[str, str] = {}
        for item in nuisance:
            param = str(item["param"])
            low = Decimal(str(item["low"]))
            high = Decimal(str(item["high"]))
            step = Decimal(str(item.get("precision", "0.001")))
            if not (low < high) or step <= 0:
                raise PhysicsServiceError("NUISANCE_RANGE", f"{param}: {low}..{high}")
            material = f"{plan_sha256}|{study}|{block_id}|{param}|{salt or ''}".encode(
                "utf-8")
            digest = hashlib.sha256(material).hexdigest()
            # 均匀落在 [low, high] 的 step 网格上；只由哈希决定，无 RNG 状态。
            span = int(((high - low) / step).to_integral_value())
            slot = int(digest[:16], 16) % (span + 1)
            value = min(low + step * slot, high)   # 非整除支撑上夹到 high（评审 LOW）
            values[param] = str(value.normalize())
        blocks.append({"block_id": block_id, "nuisance": values})
    return blocks


# ---------------------------------------------------------------------------
# 沙箱收权负控（import / importlib / getattr 三条路径都必须失败）
# ---------------------------------------------------------------------------

_DENIAL_PROBE_SOURCE = r"""
import json
import sys

out = {"python": sys.executable, "version": sys.version.split()[0], "routes": {}}


def _attempt(name, fn):
    try:
        fn()
    except BaseException as exc:  # noqa: BLE001 - 任何成功导入都算放行，必须记
        out["routes"][name] = {"denied": True,
                               "error": type(exc).__name__,
                               "detail": str(exc)[:200]}
        return
    out["routes"][name] = {"denied": False, "error": None, "detail": "mujoco 被导入"}


def _route_import():
    __import__("mujoco")


def _route_importlib():
    import importlib
    importlib.import_module("mujoco")


def _route_getattr():
    import builtins
    importer = getattr(builtins, "__import__")
    importer("mujoco")


_attempt("import", _route_import)
_attempt("importlib", _route_importlib)
_attempt("getattr", _route_getattr)
out["all_denied"] = all(v["denied"] for v in out["routes"].values())
print(json.dumps(out, sort_keys=True))
"""


def _bwrap_bin() -> str | None:
    return shutil.which("bwrap")


def mujoco_denial_probe(*, mode: str = "auto", python_exe: str | None = None,
                        workspace: Path | None = None) -> dict[str, Any]:
    """在**无 mujoco 的解释器面**上验证三条绕过路径全部失败，并返回审计件。

    mode:
    * ``bwrap``  —— 在 bwrap 内用系统 python3（只挂 /usr,/bin,/lib,/lib64,/etc 与
      工作区；**不挂** /mnt/venvs）执行探针——与 actor 会话的挂载面同构；
    * ``isolated`` —— ``python -I -S``（禁 site-packages/PYTHONPATH），确定性负控面；
    * ``auto`` —— 有 bwrap 用 bwrap，否则 isolated。

    返回 ``{"schema", "mode", "routes", "all_denied", "checked_at_utc"}``；
    任一放行即 ``all_denied=False``（调用方必须当失败处理）。
    """
    chosen = mode
    if mode == "auto":
        chosen = "bwrap" if _bwrap_bin() else "isolated"
    if chosen not in ("bwrap", "isolated"):
        raise PhysicsServiceError("DENIAL_MODE", f"{mode!r}")
    work = Path(workspace) if workspace is not None else Path(
        tempfile.mkdtemp(prefix="s1-denial-"))
    ensure_private_dir(work, mode=0o700)
    probe = work / "denial_probe.py"
    atomic_write_text(probe, _DENIAL_PROBE_SOURCE, mode=0o600)
    try:
        if chosen == "bwrap":
            bw = _bwrap_bin()
            if bw is None:
                raise PhysicsServiceError("BWRAP_MISSING", "bwrap 不可用")
            exe = python_exe or "/usr/bin/python3"
            cmd = [bw, "--die-with-parent", "--new-session",
                   "--unshare-user", "--unshare-pid", "--unshare-ipc",
                   "--unshare-uts", "--unshare-net",
                   "--ro-bind", "/usr", "/usr",
                   "--ro-bind", "/bin", "/bin",
                   "--ro-bind", "/sbin", "/sbin",
                   "--ro-bind", "/lib", "/lib",
                   "--ro-bind", "/lib64", "/lib64",
                   "--ro-bind", "/etc", "/etc",
                   "--bind", str(work), "/work",
                   "--tmpfs", "/tmp", "--proc", "/proc", "--dev", "/dev",
                   "--chdir", "/work",
                   # 评审 MEDIUM：bwrap 负控镜像 actor 运行时（site 开启），只禁
                   # user-site/环境路径；isolated 模式才用 -S 模拟「无 site-packages」。
                   exe, "-I", "/work/denial_probe.py"]
        else:
            exe = python_exe or sys.executable
            cmd = [exe, "-I", "-S", str(probe)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                                  env={"PATH": "/usr/local/bin:/usr/bin:/bin",
                                       "LANG": "C.UTF-8"})
        except OSError as exc:  # 评审 LOW：坏解释器路径不得以 UnboundLocal 掩盖
            raise PhysicsServiceError("DENIAL_PROBE_FAILED", str(exc)) from None
    finally:
        if workspace is None:
            shutil.rmtree(work, ignore_errors=True)
    if proc.returncode != 0:
        raise PhysicsServiceError(
            "DENIAL_PROBE_FAILED",
            f"rc={proc.returncode} stderr={proc.stderr.strip()[:400]}")
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        raise PhysicsServiceError(
            "DENIAL_PROBE_PARSE", f"stdout={proc.stdout[:400]!r}") from None
    routes = payload.get("routes") or {}
    missing = [r for r in DENIAL_ROUTES if r not in routes]
    if missing:
        raise PhysicsServiceError("DENIAL_PROBE_INCOMPLETE", f"缺路径 {missing}")
    audit = {
        "schema": f"{SCHEMA}.denial_probe",
        "mode": chosen,
        "python": payload.get("python"),
        "python_version": payload.get("version"),
        "routes": routes,
        "all_denied": bool(payload.get("all_denied"))
        and all(routes[r].get("denied") is True for r in DENIAL_ROUTES),
        "checked_at_utc": utc_now_iso_z(),
    }
    return audit


# ---------------------------------------------------------------------------
# 受信执行
# ---------------------------------------------------------------------------

class PhysicsService:
    """受信物理服务：唯一执行 mujoco 的进程面。

    ``observations_root`` 下按 ``batch_id`` 分目录落 raw 记录（JSONL）；
    ``evidence_root`` 只做原子件（audit/denial 等）与临时 out 目录。
    """

    def __init__(self, *, observations_root: Path, evidence_root: Path):
        self.observations_root = Path(observations_root)
        self.evidence_root = Path(evidence_root)

    # -- 单 run ---------------------------------------------------------
    def run_one(self, *, spec: E.ExperimentSpec, batch_id: str = "s1") -> RunResult:
        import mujoco  # 受信面唯一 import 点（沙箱内不存在，负控见 denial probe）

        if mujoco.__version__ != _MUJOCO_VERSION:
            raise PhysicsServiceError(
                "ENGINE_DRIFT", f"{mujoco.__version__} != {_MUJOCO_VERSION}")
        doc = P.build_spec_document(spec)
        out_dir = Path(tempfile.mkdtemp(prefix=f"s1-out-{spec.run_id[:12]}-"))
        try:
            model = mujoco.MjModel.from_xml_string(doc["scene_xml"])
            data = mujoco.MjData(model)
            if abs(float(model.opt.timestep) - float(spec.dt_s)) > 1e-15:
                raise PhysicsServiceError("DT_MISMATCH", "XML timestep != spec dt")
            from orchestrator.rsi_r1 import experiment_service as ES

            if spec.study_id == "A":
                status, stop, arrays, extra, warns = ES.run_a(doc, model, data)
            elif spec.study_id == "B":
                status, stop, arrays, extra, warns = ES.run_b(doc, model, data)
            elif spec.study_id == "C":
                status, stop, arrays, extra, warns = ES.run_c(doc, model, data)
            elif spec.study_id == "D":
                status, stop, arrays, extra, warns = ES.run_d(doc, model, data)
            else:
                raise PhysicsServiceError("STUDY_UNKNOWN", spec.study_id)
            reply = self._materialise_reply(doc, status, stop, arrays, extra, warns,
                                            out_dir)
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)
        import numpy as np
        arrays_list = {k: np.asarray(v).tolist() for k, v in arrays.items()}
        record = self._build_record(spec, reply, arrays_list)
        # raw 落盘（每 run 一件，原子 0600；重跑不改写，冲突即拒）
        target_dir = ensure_private_dir(self.observations_root / batch_id)
        record_path = target_dir / f"{spec.run_id}.jsonl"
        if record_path.exists():
            raise PhysicsServiceError(
                "RAW_EXISTS", f"{record_path} 已存在；拒绝覆盖（换 batch_id 重跑）")
        O.write_record_jsonl(record_path, record)
        metric_id = "displacement_x_m" if spec.study_id == "A" else "normalized_tracking_rmse"
        if spec.study_id == "A":
            study_metrics = M.metrics_for(record, study_id="A",
                                         initial_x_m=float(extra["x0_m"]))
        else:
            study_metrics = M.metrics_for(
                record, study_id="B",
                amplitude_rad=float(spec.physical_params["target_amplitude_rad"]))
        value = study_metrics.values.get(metric_id)
        if value is None:
            raise PhysicsServiceError("METRIC_CENSORED",
                                      f"{metric_id} 在 {spec.run_id} 不可用")
        return RunResult(
            run_id=spec.run_id, study=spec.study_id,
            condition_id=spec.condition_id, block_id=spec.block_id,
            metric_id=metric_id, metric_value=str(value),
            record_path=record_path, record_sha256=_sha256_file(record_path),
            nsteps=int(record["nsteps"]),
            report={"status": status, "stop_index": stop,
                    "metric_details": study_metrics.to_dict()},
        )

    # -- 回复材料化（与 experiment_service.write_outputs 同形，仍走同一校验） --
    def _materialise_reply(self, doc, status, stop, arrays, extra, warns, out_dir):
        from orchestrator.rsi_r1 import experiment_service as ES

        old = ES.OUT_DIR
        ES.OUT_DIR = str(out_dir)
        try:
            reply = ES.write_outputs(doc, status, stop, arrays, extra, warns)
        finally:
            ES.OUT_DIR = old
        return reply

    # -- 记录组装（复用 backend 的已审计实现，不复制映射） -------------------
    def _build_record(self, spec: E.ExperimentSpec, reply: Mapping[str, Any],
                      arrays: Mapping[str, Any]) -> dict[str, Any]:
        from orchestrator.rsi_r1.backend import PhysicsBackend

        backend = PhysicsBackend(observations_root=self.observations_root,
                                 evidence_root=self.evidence_root,
                                 work_root=self.evidence_root,
                                 runner=None, image="", batch_id="s1")
        try:
            return backend._build_record(spec, reply, arrays, envelope={})
        except Exception as exc:  # noqa: BLE001 - 基础设施失败原样升级
            raise PhysicsServiceError(
                "RECORD_INVALID", f"{type(exc).__name__}: {exc}") from None

    # -- 批次 -----------------------------------------------------------
    def run_batch(self, *, study: str, task: Mapping[str, Any],
                  plan_sha256: str, blocks: Sequence[Mapping[str, Any]],
                  batch_id: str = "s1",
                  record_root: Path | None = None) -> dict[str, Any]:
        """条件 × 私有 block 全批执行 → run_manifest（含 contrasts/confirm_plan）。

        ``task`` 是 ``build_r1n_a``/``build_r1n_b`` 的 ``public_document`` 形状
        （conditions + primary + negative_control_spec + nuisance_schema）。
        """
        study_id = {"A_N": "A", "B_N": "B"}.get(study, study)
        if study_id not in ("A", "B"):
            raise PhysicsServiceError("STUDY_UNKNOWN", str(study))
        if not blocks:
            raise PhysicsServiceError("BLOCKS_EMPTY", "block 分配为空")
        conditions = task.get("conditions")
        if not isinstance(conditions, list) or not conditions:
            raise PhysicsServiceError("CONDITIONS_EMPTY", "conditions 为空")
        records_dir = ensure_private_dir(Path(record_root)
                                         if record_root else self.observations_root)
        # 评审 LOW：不改实例状态；本批观测一律落 records_dir（run_one 用副本服务）。
        batch_service = PhysicsService(observations_root=records_dir,
                                       evidence_root=self.evidence_root)
        primary = task.get("primary")
        if not isinstance(primary, Mapping):
            raise PhysicsServiceError("PRIMARY_MISSING", "缺主对比定义")
        contradictions = [dict(primary)]
        nc = task.get("negative_control_spec")
        if isinstance(nc, Mapping) and isinstance(nc.get("contrast"), Mapping):
            contradictions.append(dict(nc["contrast"]))
        runs: list[dict[str, Any]] = []
        for cond in conditions:
            cid = str(cond["id"])
            params_base = dict(cond.get("params") or {})
            for block in blocks:
                bid = str(block["block_id"])
                params = {**params_base, **dict(block.get("nuisance") or {})}
                spec = E.build_host_spec(
                    plan_sha256=plan_sha256, study_id=study_id,
                    condition_id=cid, block_id=bid,
                    scene_id=str(cond["scene_id"]), params=params)
                result = batch_service.run_one(spec=spec, batch_id=batch_id)
                runs.append(result.row())
        contrasts = [self._contrast_doc(c) for c in contradictions]
        manifest = {
            "schema": f"{SCHEMA}.run_manifest",
            "study": study,
            "plan_sha256": plan_sha256,
            "batch_id": batch_id,
            "blocks": [dict(b) for b in blocks],
            "conditions": [dict(c) for c in conditions],
            "contrasts": contrasts,
            "runs": runs,
            "n_runs": len(runs),
            "generated_at_utc": utc_now_iso_z(),
            "manifest_sha256": "",
        }
        manifest["manifest_sha256"] = _canonical_sha(
            {k: v for k, v in manifest.items() if k != "manifest_sha256"})
        return manifest

    @staticmethod
    def _contrast_doc(contrast: Mapping[str, Any]) -> dict[str, Any]:
        """ContrastV13 → 分析器行（id/kind/terms/metric/proposition/delta）。"""
        terms = contrast.get("terms")
        if isinstance(terms, Mapping):
            terms = [[k, str(v)] for k, v in terms.items()]
        doc = {"id": contrast.get("id"),
               "kind": contrast.get("kind"),
               "metric_id": contrast.get("metric"),
               "proposition": contrast.get("proposition"),
               "delta": str(contrast.get("delta")),
               "terms": terms}
        if not doc["id"] or not doc["metric_id"]:
            raise PhysicsServiceError("CONTRAST_INVALID", f"{contrast!r}")
        return doc
