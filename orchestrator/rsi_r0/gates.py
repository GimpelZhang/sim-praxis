"""RSI-R0 门（G-M / G-P / G-V）与**不可覆盖**批准件（AC24）。

设计要点（计划 §1.2、§9）：

* 门是**条件清单**而不是一个布尔量。缺哪一条就报哪一条，不允许把"整体通过"当
  结论——否则任何一条被悄悄放松都看不出来。
* 条件清单本身进 `definition_hash`：改清单等于改门，必须留痕。
* 批准件**写一次即锁**：同内容重复写幂等；不同内容一律拒（不许覆盖、不许回填）。
  这一条正是 AC24 的"批准件不可覆盖"。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from orchestrator.rsi_r0.contracts import sha256_of
from orchestrator.rsi_r0.attempts import _utc_now


class GateRefused(RuntimeError):
    """门不成立：拒绝进入下一步（fail-closed）。"""


class ApprovalError(RuntimeError):
    """批准件非法：已存在且内容不同，或结构缺项。"""


@dataclass(frozen=True)
class GateCondition:
    key: str
    description: str
    hard_rule: str = ""          # 命中的项目硬规则/计划条目（便于报告引用）


#: G-M（钱与模型）：没通过就不发任何付费请求。
GATE_M: tuple[GateCondition, ...] = (
    GateCondition("budget_contract_approved", "预算合同已落批准件（批次/帽/币种/汇率）", "§6.2"),
    GateCondition("model_ids_approved", "三阶段模型 ID 经用户逐字裁决并写入 stage_models.yaml", "硬规则 1"),
    GateCondition("cost_upper_bound_known", "单请求最坏成本可上界（不可上界即 BLOCKED_COST）", "§6.2"),
    GateCondition("namespace_isolated", "R0 账套 namespace=2900 与旧账隔离、旧账拒写", "AC05"),
    GateCondition("seals_verified", "旧资产封存集内容哈希已核对", "§6.4"),
)

#: G-P（协议准入）：T5 的六次协议探针只能在前四条齐备且 G-M 已过时发。
GATE_P: tuple[GateCondition, ...] = (
    GateCondition("gate_m_passed", "G-M 全部条件成立", "§1.2"),
    GateCondition("approval_artifact", "本批次批准件存在且不可覆盖", "AC24"),
    GateCondition("no_unknown_attempts", "发网事实账无 UNKNOWN_SENT 未核销", "AC09"),
    GateCondition("criteria_frozen", "T3 判据已冻结且当前定义与之相等", "AC20"),
    GateCondition("lock_present", "T4 快照锁存在且校验通过", "§10"),
    # 以下四条在旧版门里缺位（评审 MAJOR-2/MAJOR-5）：检查表写着"T0–T4 验收 +
    # 独立评审"，可执行门却只认"有个批准件 + 有个锁"。结果是没有跑过控制矩阵、
    # 没有 dry-run、没人评审的 campaign 也能放行六次真实发网。
    GateCondition("t3_acceptance", "T3 控制矩阵成品已跑齐（21 负控/6 mutation/权限全 ok）", "§T3"),
    GateCondition("t4_acceptance", "T4 零付费端到端已成（POST=0 且 13 slot 全 COMPLETED）", "AC21–AC24"),
    GateCondition("independent_review", "独立评审件存在且绑定的 code 集与当前锁相同、verdict=APPROVE", "§T3/§T6"),
    GateCondition("ledger_attempts_reconciled", "账本/attempts/预留逐 request 对账无漂移", "AC31"),
)

#: G-V（验证准入）：开发没过门就一个 POST 都不许发到验证（AC27）。
GATE_V: tuple[GateCondition, ...] = (
    GateCondition("gate_p_passed", "G-P 全部条件成立", "§1.2"),
    GateCondition("development_report_present", "开发报告存在且三族 slot 终态齐全", "AC26"),
    GateCondition("development_gate_met", "E/D 有 L4 PASS 且 R 有 L5 PASS、仪器未坏", "§T6"),
    GateCondition("snapshot_frozen", "验证快照（代码/配置/任务/评测/镜像）已冻结", "§T6"),
)

GATES: Mapping[str, tuple[GateCondition, ...]] = {
    "G-M": GATE_M, "G-P": GATE_P, "G-V": GATE_V,
}

#: 门名 → 模块级变量名。**每次按变量名现取**，这样清单被改（含运行期被替换）时
#: 判门与定义哈希立刻反映新清单，而不是悄悄沿用 import 时的旧副本。
_GATE_VARS: Mapping[str, str] = {"G-M": "GATE_M", "G-P": "GATE_P", "G-V": "GATE_V"}


def gate_conditions(gate: str) -> tuple[GateCondition, ...]:
    name = _GATE_VARS.get(gate)
    if name is None:
        raise GateRefused(f"未声明的门：{gate}")
    return globals()[name]


def gate_definition_hash(gate: str) -> str:
    conds = gate_conditions(gate)
    return sha256_of([[c.key, c.description, c.hard_rule] for c in conds])


def evaluate_gate(gate: str, facts: Mapping[str, bool]) -> dict[str, Any]:
    """按条件清单判门；缺项逐条列出，绝不返回"大概过了"。"""
    conds = gate_conditions(gate)
    rows = [{"key": c.key, "description": c.description,
             "hard_rule": c.hard_rule, "ok": bool(facts.get(c.key))}
            for c in conds]
    missing = [r["key"] for r in rows if not r["ok"]]
    return {"gate": gate, "passed": not missing, "missing": missing,
            "conditions": rows, "definition_hash": gate_definition_hash(gate)}


def assert_gate(gate: str, facts: Mapping[str, bool]) -> dict[str, Any]:
    verdict = evaluate_gate(gate, facts)
    if not verdict["passed"]:
        raise GateRefused(
            f"{gate} 未通过，缺 {len(verdict['missing'])} 条：{verdict['missing']}")
    return verdict


# ---------------------------------------------------------------- 批准件
#
# 批准件是**钱与模型的授权凭证**：写一次即锁，之后只能读。允许两种合法写入：
# ① 目标不存在 ⇒ 新建（0600）；② 已存在且**逐字节相同** ⇒ 幂等返回。
# 其它情况一律 `ApprovalError`，绝不覆盖（AC24）。

APPROVAL_SCHEMA = "rsi_r0.approval.v1"
APPROVAL_REQUIRED = ("batch_id", "task_cap", "hard_cap", "currency",
                     "usd_ceiling", "approved_by", "approved_at_utc", "scope")


def approval_body(**fields: Any) -> dict[str, Any]:
    missing = [k for k in APPROVAL_REQUIRED if not fields.get(k)]
    if missing:
        raise ApprovalError(f"批准件缺字段：{missing}")
    body = {"schema": APPROVAL_SCHEMA, **{k: fields[k] for k in APPROVAL_REQUIRED}}
    body["approval_sha256"] = sha256_of(body)
    return body


def write_approval(path: Path, body: Mapping[str, Any]) -> dict[str, Any]:
    """写一次即锁。返回 {path, sha256, created}；同内容重复写幂等。"""
    data = (json.dumps(dict(body), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n").encode("utf-8")
    digest = sha256_of(json.loads(data))
    if path.exists():
        old = path.read_bytes()
        if old != data:
            raise ApprovalError(
                f"批准件已存在且内容不同，拒绝覆盖：{path}（旧 sha256="
                f"{sha256_of(json.loads(old or b'{}'))[:16]}…）")
        return {"path": str(path), "sha256": digest, "created": False}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    return {"path": str(path), "sha256": digest, "created": True}


def read_approval(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ApprovalError(f"批准件不存在：{path}")
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("schema") != APPROVAL_SCHEMA:
        raise ApprovalError(f"批准件 schema 不符：{doc.get('schema')!r}")
    body = {k: v for k, v in doc.items() if k != "approval_sha256"}
    if sha256_of(body) != doc.get("approval_sha256"):
        raise ApprovalError("批准件自哈希不符（被改过）")
    if doc.get("approved_at_utc", "") > _utc_now():
        raise ApprovalError("批准件时间在未来（时钟异常或伪造）")
    return doc


def approval_ok(path: Path, *, batch_id: str) -> bool:
    """G-P 的 approval_artifact 事实：存在、自哈希对、批次对得上。"""
    try:
        doc = read_approval(path)
    except (ApprovalError, OSError, json.JSONDecodeError):
        return False
    return str(doc.get("batch_id")) == batch_id
