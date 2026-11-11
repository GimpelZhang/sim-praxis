"""RSI-R1 可信 raw 观测：逐 run 规范化记录、完整性校验与 manifest（AC07 半边）。

纪律（Plan §3.1、§5.4）：

* raw 由 **trusted host** 记录，候选只拿只读副本；候选自报 readback 永不入库；
* 数组一律 JSONL（逐行一个时点）或 NPZ（``allow_pickle=False``），**禁 pickle**；
* 每件 raw 进 manifest 时算 sha256/行数/步数；缺件/不符即 fail-closed；
* 非有限值、时间轴非单调、长度不齐、步数与 spec 不符——一律判 INVALID，
  不允许「截掉坏点」美化；
* 本模块不 import mujoco、不联网；只做数据结构与完整性。

字段分为「通用（两 study 都有）」与「study 专属」两组；公开合同渲染直接
从这里取，避免第二份字段表。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from orchestrator.rsi_r1.scenes import SCENE_A, SCENE_B, scene_doc

SCHEMA_PREFIX = "simpraxis.rsi_r1"
RAW_SCHEMA = f"{SCHEMA_PREFIX}.raw_run.v1"
MANIFEST_SCHEMA = f"{SCHEMA_PREFIX}.raw_manifest.v1"

#: 通用必填键（两 study）。
COMMON_FIELDS: "tuple[str, ...]" = (
    "time_s",            # 物理时间（由 trusted host 记录，按物理时间积分）
    "qpos",              # 广义位置，与 time_s 等长
    "qvel",              # 广义速度，与 time_s 等长
    "params_applied",    # 实际应用的物理/控制参数（不是候选请求值）
    "engine",            # {name, version}
    "run_id",
    "spec_hash",
    "block_id",
    "nsteps",
    "nonfinite_count",   # 宿主统计的非有限采样数
    "solver_warnings",   # 列表；空即无
)

#: Study A 专属。
STUDY_A_FIELDS: "tuple[str, ...]" = (
    "contact_pair",
    "contact_dim_per_step",     # 每步实际 contact 维度
    "friction_sliding_per_step",
    "friction_torsional_per_step",
    "contact_force_n",          # 每步接触力
)

#: Study B 专属。
STUDY_B_FIELDS: "tuple[str, ...]" = (
    "target_q",
    "command_torque",           # 控制器输出（限幅前）
    "applied_torque",           # 实际施加（限幅后）
    "control_update_index",     # 每个时点所属的控制更新序号
    "saturation_count",
    "instability_events",       # 列表：{time_s, reason}
)

SCENE_FIELDS: "Mapping[str, tuple[str, ...]]" = {
    SCENE_A: STUDY_A_FIELDS,
    SCENE_B: STUDY_B_FIELDS,
}


class ObservationError(RuntimeError):
    """观测件不自洽（缺字段/非有限/长度不齐/manifest 失配）。"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def required_fields(scene_id: str) -> "tuple[str, ...]":
    if scene_id not in SCENE_FIELDS:
        raise ObservationError("UNKNOWN_SCENE", scene_id)
    return COMMON_FIELDS + SCENE_FIELDS[scene_id]


# ---------------------------------------------------------------------------
# 记录校验
# ---------------------------------------------------------------------------

def _is_finite_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _check_series(values: Any, name: str) -> int:
    """标量序列或定宽向量序列都接受（A 的 qpos/qvel 是 7/6 维向量）。"""
    if not isinstance(values, list):
        raise ObservationError("FIELD_TYPE", f"{name} 必须是数组")
    width: "int | None" = None
    for i, v in enumerate(values):
        if isinstance(v, list):
            if width is None:
                width = len(v)
                if width == 0:
                    raise ObservationError("LENGTH_MISMATCH", f"{name}[0] 是空向量")
            elif len(v) != width:
                raise ObservationError("LENGTH_MISMATCH",
                                       f"{name}[{i}] 宽度 {len(v)} != {width}")
            if not all(_is_finite_number(x) for x in v):
                raise ObservationError("NONFINITE", f"{name}[{i}] 含非有限分量")
        elif not _is_finite_number(v):
            raise ObservationError("NONFINITE", f"{name}[{i}]={v!r} 不是有限实数")
        elif width is not None:
            raise ObservationError("FIELD_TYPE", f"{name} 混用标量与向量")
    return len(values)


def validate_record(record: Mapping[str, Any], *, scene_id: str) -> "dict[str, Any]":
    """逐字段校验一条 run 记录；返回其规范化副本（不修改入参）。"""
    need = required_fields(scene_id)
    missing = [k for k in need if k not in record]
    if missing:
        raise ObservationError("MISSING_FIELD", f"缺 {missing}")
    extra = sorted(set(record) - set(need))
    if extra:
        # raw 只允许合同内字段：引擎额外信息必须通过 solver_warnings/engine 表达。
        raise ObservationError("UNKNOWN_FIELD", f"多余字段 {extra}")

    n = len(record["time_s"])
    if n < 2:
        raise ObservationError("TOO_SHORT", f"time_s 只有 {n} 个时点")
    for name in ("qpos", "qvel"):
        got = _check_series(record[name], name)
        if got != n:
            raise ObservationError("LENGTH_MISMATCH",
                                   f"{name} 长度 {got} != time_s {n}")
    times = record["time_s"]
    for i, v in enumerate(times):
        if not _is_finite_number(v):
            raise ObservationError("NONFINITE", f"time_s[{i}]={v!r}")
    for i in range(1, n):
        if not times[i] > times[i - 1]:
            raise ObservationError("TIME_NOT_MONOTONIC",
                                   f"time_s 在 [{i-1}]→[{i}] 未严格递增")
    if int(record["nsteps"]) != n:
        raise ObservationError("STEP_MISMATCH",
                               f"nsteps={record['nsteps']} != 采样点数 {n}")
    if int(record["nonfinite_count"]) != 0:
        raise ObservationError("NONFINITE_STATE",
                               f"宿主统计到 {record['nonfinite_count']} 个非有限采样")
    if not isinstance(record["params_applied"], Mapping):
        raise ObservationError("FIELD_TYPE", "params_applied 必须是对象")
    if not isinstance(record["engine"], Mapping):
        raise ObservationError("FIELD_TYPE", "engine 必须是对象")
    for k in ("run_id", "spec_hash", "block_id"):
        if not isinstance(record[k], str) or not record[k]:
            raise ObservationError("FIELD_TYPE", f"{k} 必须是非空字符串")

    if scene_id == SCENE_A:
        dim = record["contact_dim_per_step"]
        if not isinstance(dim, list) or len(dim) != n:
            raise ObservationError("LENGTH_MISMATCH", "contact_dim_per_step 长度不符")
        for name in ("contact_force_n", "friction_sliding_per_step",
                     "friction_torsional_per_step"):
            got = _check_series(record[name], name)
            if got != n:
                raise ObservationError("LENGTH_MISMATCH", f"{name} 长度不符")
        force = record["contact_force_n"]
        # 每步记录该步**实际**接触维度：0 表示此步无 contact（MuJoCo 软接触下
        # 刚体在滑行中会出现亚毫米弹跳，实测最坏 1.25% 步），有 contact 时必须
        # 恰为 3。无 contact 步的法向力必须为 0——不允许"有维度没力"或反之。
        # 接触占比/最大间隙/力平衡的暴露门在 calibration.exposure 里按注册阈值判。
        for i, d in enumerate(dim):
            if isinstance(d, bool) or not isinstance(d, int):
                raise ObservationError("CONTACT_DIM", f"step {i} dim={d!r} 非整数")
            if d == 0:
                if float(force[i]) != 0.0:
                    raise ObservationError(
                        "CONTACT_DIM",
                        f"step {i} 无 contact 但接触力 {force[i]!r} 非 0")
                continue
            if d != 3:
                raise ObservationError(
                    "CONTACT_DIM", f"step {i} 实际 condim={d!r} 不等于 3（暴露检查）")
    else:
        n_ctl = len(record["control_update_index"])
        if n_ctl != n:
            raise ObservationError("LENGTH_MISMATCH", "control_update_index 长度不符")
        for i in range(1, n_ctl):
            if record["control_update_index"][i] < record["control_update_index"][i - 1]:
                raise ObservationError("CONTROL_INDEX", "control_update_index 倒退")
        for name in ("applied_torque", "command_torque", "target_q"):
            got = _check_series(record[name], name)
            if got != n:
                raise ObservationError("LENGTH_MISMATCH", f"{name} 长度不符")

    return dict(record)


def check_dt_consistency(record: Mapping[str, Any], *, dt_s: Decimal,
                         atol: Decimal = Decimal("1e-9")) -> None:
    """采样间隔必须等于声明 dt（宿主记录，不容忍变量步）。"""
    times = record["time_s"]
    dt = float(dt_s)
    for i in range(1, len(times)):
        step = times[i] - times[i - 1]
        if abs(step - dt) > float(atol):
            raise ObservationError("DT_MISMATCH",
                                   f"[{i-1}]→[{i}] 间隔 {step} != 声明 dt {dt}")


# ---------------------------------------------------------------------------
# 写 / 读（JSONL；禁 pickle）
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return path


def write_record_jsonl(path: "Path | str", record: Mapping[str, Any]) -> Path:
    """一条 run 落成一行 JSON（大数组仍是整体一行的 compact JSON）。"""
    text = json.dumps(record, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False) + "\n"
    if len(text.encode("utf-8")) > 32 * 1024 * 1024:
        raise ObservationError("RAW_TOO_LARGE", "单条 raw 记录超过 32 MiB 上限")
    return _atomic_write(Path(path), text)


def read_record_jsonl(path: "Path | str") -> "dict[str, Any]":
    rows = Path(path).read_text(encoding="utf-8").splitlines()
    if len(rows) != 1:
        raise ObservationError("RAW_SHAPE", f"{path} 应恰有 1 行，实有 {len(rows)}")
    try:
        obj = json.loads(rows[0])
    except json.JSONDecodeError as exc:
        raise ObservationError("RAW_PARSE", f"{path}: {exc}") from None
    if not isinstance(obj, dict):
        raise ObservationError("RAW_SHAPE", f"{path} 顶层不是对象")
    return obj


def read_npz_arrays(path: "Path | str") -> "dict[str, Any]":
    """NPZ 读取：显式 ``allow_pickle=False``（禁 pickle 的唯一入口）。"""
    import numpy as np  # 局部 import：只有真要读 NPZ 时才需要 numpy

    with np.load(Path(path), allow_pickle=False) as data:
        return {k: data[k].tolist() for k in data.files}


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class ManifestEntry:
    run_id: str
    relpath: str
    sha256: str
    bytes: int
    nsteps: int

    def to_dict(self) -> "dict[str, Any]":
        return {"run_id": self.run_id, "relpath": self.relpath,
                "sha256": self.sha256, "bytes": self.bytes, "nsteps": self.nsteps}


def build_manifest(entries: Iterable[ManifestEntry], *, study: str,
                   batch_id: str) -> "dict[str, Any]":
    items = [e.to_dict() for e in entries]
    ids = [e["run_id"] for e in items]
    if len(set(ids)) != len(ids):
        raise ObservationError("DUPLICATE_RUN_ID", "manifest 内 run_id 重复")
    return {
        "schema": MANIFEST_SCHEMA,
        "schema_version": 1,
        "study": study,
        "batch_id": batch_id,
        "count": len(items),
        "total_steps": sum(e["nsteps"] for e in items),
        "entries": items,
    }


def manifest_entry(run_id: str, relpath: str, path: "Path | str",
                   nsteps: int) -> ManifestEntry:
    p = Path(path)
    return ManifestEntry(run_id=run_id, relpath=relpath, sha256=_sha256_file(p),
                         bytes=p.stat().st_size, nsteps=int(nsteps))


def verify_manifest(manifest: Mapping[str, Any], root: "Path | str") -> None:
    """逐件重算 hash/大小；任何漂移即 fail-closed。"""
    base = Path(root)
    if manifest.get("count") != len(manifest.get("entries", [])):
        raise ObservationError("MANIFEST_COUNT", "count 与 entries 条数不符")
    total = 0
    for entry in manifest["entries"]:
        p = base / entry["relpath"]
        if p.is_symlink():
            raise ObservationError("MANIFEST_SYMLINK", f"{p} 是符号链接")
        if not p.is_file():
            raise ObservationError("MANIFEST_MISSING", f"缺件 {p}")
        if _sha256_file(p) != entry["sha256"]:
            raise ObservationError("MANIFEST_HASH", f"{p} sha256 不符")
        if p.stat().st_size != entry["bytes"]:
            raise ObservationError("MANIFEST_SIZE", f"{p} 字节数不符")
        total += int(entry["nsteps"])
    if total != manifest.get("total_steps"):
        raise ObservationError("MANIFEST_STEPS", "total_steps 与逐件和不符")


def point_count(record: Mapping[str, Any]) -> int:
    return len(record["time_s"])


def scene_metric_hint(scene_id: str) -> "Sequence[str]":
    return scene_doc(scene_id).metrics
