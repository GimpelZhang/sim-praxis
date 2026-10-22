#!/usr/bin/env python3
"""批次配额账本 + 双层计数闸门（详细计划 §0.2/§6，D1/D3 签收；Phase 2 Task0 起为 Phase 2 账）。

本模块【不含任何网络代码】：只做批次凭据校验、ledger 记账与预算闸门；
HTTP 分类/退避仍在 router.py，m9/m12 逻辑零改动（§6.3）。

Phase 2（Task0，D-1 独立新账）：台账路径切到 $SIMPRAXIS_PHASE2_ROOT/quota/，
新账 seq 从 0 起；Phase 1 封存账（29 行）保持只读，新代码不以写方式打开。
路径（全部 /mnt，仓外）：
  $SIMPRAXIS_PHASE2_ROOT/quota/ledger.jsonl     # 每行一次实呼或一次 preflight 拦截
  $SIMPRAXIS_PHASE2_ROOT/quota/counters.json    # 连续配额错误计数
  $SIMPRAXIS_PHASE2_ROOT/quota/approvals/<batch_id>.json   # 每批书面批准凭据
  $SIMPRAXIS_PHASE2_ROOT/quota/resume_audit.jsonl          # 人工恢复流水
  $SIMPRAXIS_PHASE1_ROOT/quota/ledger.jsonl     # Phase 1 封存账（只读，见 normalize 脚本）
暂停态文件不在这里：沿用 router.py 的 $SIMPRAXIS_GATEWAY_STATE/quota_pause.json，
is_paused() 直接复用 router 实现（不复制其逻辑）。预算类暂停 resume_at_epoch=null
（无票不自动恢复），在既有 7 键上追加 schema_version=2 的超集键（§6.1）。
Phase 2 新行 ts_iso 全部严格 UTC Z（orchestrator.common.clock）。

接线状态（Task3 已收口，§6.3「倾向并入 Task3」）：Task2 阶段账本先行，
direction_setter 显式 preflight/record；Task3 起同一对门只在
ModelGateway.call/call_visual 内部各执行一次（BillingGate），setter 的显式
记账已移除，杜绝双计数。一次逻辑调用 = 一次 preflight + 至多一行整数 seq
（出口成功/失败都记），call 内部 HTTP 退避重试不重复记账。

口径签收（2026-10-14 代码评审 L1/L4）：
- resume_ticket：§6.1 设想双方把持（人工写票、操作者持票解封）；Phase 1 单机
  单人操作，当前 resume CLI 只校验票非空并记入 resume_audit.jsonl（票仅留痕、
  不与暂停文件中的值比对）。多机/多人运行前必须升级为双向校验。
- 并发：preflight 与 record 分持文件锁（TOCTOU 窗口）。Phase 1 形态是单机
  顺序 CLI；Task3 收口后唯一触网线程是 LangGraph invoke 主线程（飞书 WS 回调
  线程只写 pending_gates + set Event，不调模型），保持单调用方语义，接受该窗口；
  出现并发 runner 前必须在 record() 内复检上限或把 preflight+record 收进同一把锁。
- tokens_in/out：Task3 收口后由 call/call_visual 从 usage 回填
  （litellm input/output_tokens，直 POST body.usage.input_tokens/output_tokens）；
  Task2 的 9 行历史记录维持 null 不回改。
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir
from orchestrator.common.runtime_paths import (
    gateway_state_dir as router_state_dir,  # 统一入口：暂停态目录
    gateway_state_path as router_state_path,
    phase1_root as phase1_root,  # 转出：外部只读使用者取 Phase 1 封存账根
    phase2_approvals_dir,
    phase2_counters_path,
    phase2_ledger_path,
    phase2_quota_dir,
)
from orchestrator.common.validators import validate_batch_id
from orchestrator.gateway.router import (
    GatewayPausedError,
    PauseStateCorruptError,
    is_paused,
    redact,
)

# Phase 2 独立新账预算（§6.2：常态 60、全局硬顶 80；2026-10-16 用户自主授权，
# Agent 可按 §6 SOP 自批提顶；触顶是自批提顶而非停工审批）。Phase 1 封存账
# 的 36/48 双门随 29 行终值冻结，不再由本常量表达。
NORMAL_BUDGET = 60       # 常态批准总量
# 2026-10-21 run2 chunk3 守卫三振后用户裁决首次提全局硬顶 80→104（此前四次
# 提顶均只动 task6 单任务顶，全局 60/80 从未动）；run3 信封 ≤14 seq（出生
# 最坏 3×4=12+修复 2），全局 79→≤93/104。NORMAL_BUDGET 60 不动、模型 ID 不动。
# 2026-10-21 run4 第二次提全局硬顶 104→112（第六次提顶批次；用户裁决
# 「接缝加固后 run4」，sentinel 围栏纯文本定界 + n=4→6 工厂规格变更，设计
# 终稿 task6-sentinel-seam-design.md §4/§10.5，票 T6-B9-run4-birth 信封
# 21 = 出生最坏 3×6=18 + 出生后至多 2 轮单补丁修复 + 1 次 kill-9 重放）：
# 全局已用 89（含 seq89 JSON mode 诊断探针 1 发）→ ≤110/112。NORMAL_BUDGET
# 60 不动，4/7/8 三任务顶不动，模型 ID/32768 输出顶不动。
# 2026-10-22 run5 第七次自批（用户裁决②「run5：引擎加大块定向二拆协议」，
# 设计终稿 task6-sentinel-seam-design.md §15.1-G/§15.4-MINOR3）：闭合大块
# 18001–36000 的机器定向二拆是本地纯计算，0 新增调用路径（callcache/缓存键/
# 网关 kwargs 全不动），信封仍 21 = 出生最坏 3×6=18 + 出生后至多 2 轮单补丁
# 修复 + 1 次 kill-9 重放。2026-10-22 账实：ledger 106 行/全局 100 seq
# （61 ok+15 ok=false 为 task6，ok=false 同样烧 seq）→ 100+21=121≤122；
# 打满后全局余量 1。NORMAL_BUDGET 60、4/7/8 三任务顶、模型 ID 不动；
# 票 T6-B10-run5-frag，常量提交先于出票（write_approval used+approved>
# HARD_CAP 硬校验）。
# 2026-10-22 run6 第八次自批（付费 r1，设计终稿 task6-sentinel-seam-design.md
# §15.8.3/LEAD 裁决 4）：G4 活探针实证 run5 终态 human_failure 节点 2026-10-22
# 追加真实学习行 24681e47…（与 ledger seq107 同秒），learning 渲染块指纹漂移
# 致 r1 六缓存键同名必 miss，零付费重放前提证伪——矛盾即结论（硬规则 7），
# kb 面 6/6 逐字节相等、漂移仅 learning 一进一出，禁删学习行/伪造指纹。非模型
# 变更，硬规则 1 不触发，模型 ID 零变更。新票 T6-B10-run6-paid-r1 approved=19
# beyond_normal（最坏信封 6 r1 付费重出+6 r2+6 r3+1 kill-9 重放）。账实
# 113 行/int-seq 107/task6 83（68 ok+15 ok=false）：三门算术 全局
# 107+19=126≤128、task6 83+19=102≤106、批次 0+19=19 恰顶；打满后全局余 2、
# task6 余 4。NORMAL_BUDGET 60、4/7/8 三任务顶不动；常量必须先于出票提交
# （write_approval used+approved>HARD_CAP 硬门）。
HARD_CAP = 128           # 全局先到先停硬门（2026-10-22 第八次自批，122→128）
LEDGER_PHASE = 2         # 台账期：写入批准件/恢复流水的 phase 断言标记
# Phase 1 各任务局部硬上限（封存值，仅作历史并集保留；Task2 14 为 D1 签收）。
PHASE1_TASK_CAPS = {2: 14, 4: 8, 5: 6, 7: 14, 8: 14, 9: 4}
# Phase 2 task 集（§6.2/§6.5 估算初始值）：Task4 标定占 task=4 账套，编号沿用
# Stage 账套避免新增（Phase 2 限 24，覆盖 Phase 1 的 8）。
# Task6 2026-10-20 因 thinking 失控诊断 + V2 出生实呼触顶（task_used=16/16，
# reason=task_hard_cap），依 §6.3 自批 16→24（2026-10-16 用户预算自主授权）。
# Task6 2026-10-20 晚拆块出生生产首验再触顶（task_used=24/24，seq48 块0 实呼
# 成功但冗长产出吃满 16384 output 被截断，round2 preflight 暂停），依同授权
# 自批 24→40；全局 60/80 双门不动（此刻全局已用 48/80），模型 ID 不动
# （提交见同日 chunked 接缝后）。
# Task6 2026-10-21 凌晨 32k 拆块出生再触顶（task_used=40/40，seq64 为 round2
# chunk1 成功实呼 14005 out，round2 chunk2 写 req 后 preflight 拦停
# reason=task_hard_cap、scope=task，0 多发；此时全局 64/80），依同授权自批
# 40→48。最坏信封 7 seq（节点重放 r1：块0/1/2 缓存命中，块3 因 seq62 网关
# 成功但 code 24661>20000 机器拒绝、未入缓存须重付 1；r2 块0/1 命中、
# 块2/3 重付 2；r3 整轮 4）：task6≤47/48、全局≤71/80；控制票 T6-B5
# （8 额 beyond_normal，T6-B4 6 额未引用即废）。全局 60/80 双门、4/7/8
# 三顶、模型 ID 均不动。
# Task6 2026-10-21 晚第四次自批：seq64 第三次提顶（40→48，f918239）后
# run1 出生成功（seq65 仅重付 1），但 seq66/67 修复后轮次耗尽 human_failure
# （模型自写脚本临时 chdir 只读 site-packages/genesis/assets 后裸调
# start_recording 相对路径，编码器首个 scene.step 才惰性开文件撞只读 cwd）；
# 用户裁决①加通用产物路径纪律合同条款后重挂 run_idx=2，依同授权自批 48→56。
# run2 信封：倒带后 run_idx 重算为 1（run 目录重编号，见 rehang runbook），
# callcache 键名仍是 coding_<thread>_r1_round*_chunk*，但指纹含消息体——合同
# 新增第 7 条+骨架两注后全 miss（runner.py L822/L915 语义，安全方向），出生
# 4 块全付；最坏三轮拆块出生全败 3×4=12（现实：出生 4+至多 2 轮单补丁修复=6）；
# task6 已用 43 → ≤55/56；全局 67 → ≤79/80（HARD_CAP 不动）；控制票 T6-B6
# （12 额 beyond_normal，由 LEAD 自批落 quota/approvals/）。60/80 双门、
# 4/7/8 三顶、模型 ID 均不动；口径 32768/20000/n=4 不动。
# Task6 2026-10-21 晚第五次自批（配套全局硬顶史上首提）：run2（T6-B6
# 12 seq 用满，seq68-79）三轮拆块出生 chunk3 全败于 20000 字符守卫
# （code 21256/24514/22293>20000），用户裁决守卫回提 26000（runner.py
# CHUNK_MAX_CODE_CHARS）并重挂 run3；依同授权本任务顶 56→80 配套。run3
# 信封 14 seq（出生最坏 3×4=12+出生后至多 2 轮单补丁修复），task6
# 55→≤69/80；全局硬顶同日首次 80→104（见上 HARD_CAP 溯源）。60 常态门、
# 4/7/8 三顶、模型 ID、n=4、32768 输出顶均不动。
# Task6 2026-10-21 run4 第六次自批（配套全局硬顶史上第二提 104→112）：
# run3 九发出生调用两次完整流非法手写 JSON（22%，失败轴是转义纪律非预算），
# 出生管道改 sentinel 围栏纯文本定界、n=4→6 工厂规格变更（设计 §3，行预算
# 合计 860）。票 T6-B9-run4-birth 信封 21 = 出生最坏 3×6=18 + 出生后至多
# 2 轮单补丁修复 + 1 次 kill-9 重放；task6 已用 65 → ≤86/86（恰顶），
# 全局 89 → ≤110/112。NORMAL_BUDGET 60、4/7/8 三任务顶、模型 ID、
# 32768 输出顶/600s 超时均不动。
# Task6 2026-10-22 run5 第七次自批（配套全局硬顶史上第三提 112→122）：
# run4 三振于出生体积闸（sentinel 围栏通道 11/11 成功，n=6 体积合同 3/3
# 被证伪），run5 引擎对 18001–36000 闭合多行件做本地确定性定向二拆
# （设计 §15；0 新增调用路径），信封仍 21；task6 已用 76（=61 ok+
# 15 ok=false）→ 76+21=97≤98，打满后 task6 余量 1。60 常态门、4/7/8
# 三任务顶、模型 ID、32768/600/n=6 均不动。
# Task6 2026-10-22 run6 第八次自批（配套全局硬顶史上第四提 122→128）：
# G4 实证 run5 终态真实学习行 24681e47… 致 learning 指纹漂移，零付费 r1
# 重放前提证伪（矛盾即结论，硬规则 7；kb 面 6/6 逐字节相等），run6 改为
# 付费 r1（设计 §15.8.3/裁决 4）。新票 T6-B10-run6-paid-r1 approved=19
# beyond_normal（6 r1 付费重出+6 r2+6 r3+1 kill-9 重放）；task6 已用
# 83（=68 ok+15 ok=false）→ 83+19=102≤106，打满后 task6 余量 4；
# 批次 0+19=19 恰顶，全局 107+19=126≤128（余量 2）。非模型变更、模型 ID
# 零变更；NORMAL_BUDGET 60、4/7/8 三任务顶、32768/600/n=6/温度/种子均不动；
# 常量先于出票落地（write_approval 硬门）。
PHASE2_TASK_CAPS = {4: 24, 6: 106, 7: 16, 8: 16}
# 执行口径：并集，Phase 2 字典对重复 task（4）生效；approve CLI 的 --task
# choices 同样取并集，Phase 1 编号仍可开凭据（全局 60/128 双门：常态门 60
# 始终未动，硬顶 2026-10-21 80→104→112、2026-10-22 112→122→128，见上溯源）。
TASK_HARD_CAPS = {**PHASE1_TASK_CAPS, **PHASE2_TASK_CAPS}
CONSECUTIVE_QUOTA_LIMIT = 3
LOCK_NAME = ".ledger.lock"

# Task0：quota_pause v2 schema 签收冻结。代码只允许写这 12 个键；扩键/改名
# 必须先取得用户对操作件（$PHASE2_ROOT/quota/quota_pause_schema_v2.signed.json，
# 模板 scripts/milestone2/quota_pause_schema_v2.template.json）的书面签收并
# 升 schema_version。顺序即写件键序（_write_budget_pause 必须与此常量一致）。
QUOTA_PAUSE_SCHEMA_V2_KEYS: tuple[str, ...] = (
    "schema_version", "status", "stage", "reason",
    "paused_at_epoch", "paused_at_iso",
    "resume_at_epoch", "resume_at_iso",
    "scope", "trigger", "resume_ticket", "affected_stages",
)


class QuotaError(RuntimeError):
    """账本/凭据自身错误（区别于 GatewayPausedError 的预算熔断）。"""


class LedgerDegradedError(QuotaError):
    """G-4：ledger 行追加失败（磁盘满/IO 错误等）——禁止假装记账成功。

    伴随一个独立 .degraded JSON 告警件（原子 0600）落 quota 目录，供人工核查；
    调用方必须把本次调用当失败处理，绝不允许账外放行付费调用。
    """


# 路径统一走 orchestrator.common.runtime_paths（Phase 2 独立新账）。
# phase1_root 由该模块导入再转出，供 normalize 脚本/外部只读使用者取封存账根。
def quota_dir() -> Path:
    # G-7：配额目录统一 0700（已存在且被放宽时同样收敛）。
    return phase2_quota_dir()


def ledger_path() -> Path:
    return phase2_ledger_path()


def counters_path() -> Path:
    return phase2_counters_path()


def approvals_dir() -> Path:
    # G-7：批次凭据目录 0700。
    return phase2_approvals_dir()


def _now_iso() -> str:
    # P2：Phase 2 所有新行严格 UTC Z。
    return utc_now_iso_z()


class _FileLock:
    """ledger 跨线程/跨进程互斥（fcntl 建议锁；单机单盘 /mnt）。"""

    def __init__(self) -> None:
        self._fh = open(quota_dir() / LOCK_NAME, "a+")  # noqa: SIM115

    def __enter__(self) -> "_FileLock":
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: object) -> None:
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()


def read_lines() -> list[dict[str, Any]]:
    p = ledger_path()
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # 账本只追加、逐行 json；坏行必须显式暴露，不许静默跳过
            raise QuotaError(f"ledger 存在不可解析行：{line[:120]}")
    return rows


def call_lines(rows: "list[dict[str, Any]] | None" = None) -> list[dict[str, Any]]:
    """真正触网的实呼行（seq 为整数）；preflight 拦截行 seq=null 不计数。"""
    return [r for r in (rows if rows is not None else read_lines())
            if isinstance(r.get("seq"), int)]


def load_approval(batch_id: str) -> dict[str, Any]:
    # G-9：batch_id 是路径段，读法先过字符集白名单（拒绝 ../、斜杠等遍历形态）。
    try:
        validate_batch_id(batch_id)
    except (TypeError, ValueError) as exc:
        raise QuotaError(f"batch_id 非法：{exc}") from exc
    p = approvals_dir() / f"{batch_id}.json"
    if not p.exists():
        raise QuotaError(f"批次批准凭据不存在：{p}（实呼前必须有书面批次批准）")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        # 评审 L3：凭据损坏按"凭据缺失"fail-closed，且让 preflight 的审计行不致断链
        raise QuotaError(f"批次批准凭据损坏不可读：{p}（{type(exc).__name__}）") from exc
    if not isinstance(data, dict):
        raise QuotaError(f"批次凭据 {batch_id} 顶层不是 JSON 对象")
    for k in ("batch_id", "task", "stage", "approved", "beyond_normal"):
        if k not in data:
            raise QuotaError(f"批次凭据 {batch_id} 缺字段 {k}")
    if data["batch_id"] != batch_id:
        raise QuotaError(f"批次凭据 batch_id 不一致：{data['batch_id']} != {batch_id}")
    # G-9：bool 是 int 子类（True==1），必须显式拒绝，防止 {"approved": true}
    # 被当成 1 次书面批准。
    approved = data["approved"]
    if not isinstance(approved, int) or isinstance(approved, bool) or approved <= 0:
        raise QuotaError(f"批次凭据 {batch_id} approved 必须为正整数（不接受 bool）")
    return data


def _write_budget_pause(reason: str, trigger: dict[str, Any], *,
                        stage: str = "*", scope: str = "global",
                        affected_stages: "list[str] | None" = None
                        ) -> dict[str, Any]:
    """写 v2 超集暂停态：前 7 键名/原义与 router._write_pause_state 完全一致，
    resume_at_epoch=null 表示不自动恢复（§6.1）。

    G-11：任务局部硬顶用独立 reason=task_hard_cap，stage 写实际 stage（非 "*"）、
    scope="task"、affected_stages=[stage]——任务硬顶是该任务绝对上限，resume()
    据此拒绝以批次扩展恢复。
    G-7：fsutil 原子写（唯一 tmp 名，fchmod 0600 在 replace 之前）。
    """
    now = time.time()
    if affected_stages is None:
        affected_stages = ["*"] if stage == "*" else [stage]
    state = {
        "schema_version": 2,
        "status": "PAUSED_WAITING_QUOTA",
        "stage": stage,
        "reason": reason,
        "paused_at_epoch": now,
        "paused_at_iso": _now_iso(),
        "resume_at_epoch": None,
        "resume_at_iso": None,
        "scope": scope,
        "trigger": trigger,
        "resume_ticket": None,
        "affected_stages": affected_stages,
    }
    # Task0 v2 schema 签收冻结：写件键集必须恰为 12 键常量，多写/漏写即编码错误。
    if tuple(state) != QUOTA_PAUSE_SCHEMA_V2_KEYS or len(state) != 12:
        raise AssertionError(
            f"quota_pause 键集漂移：{tuple(state)} != {QUOTA_PAUSE_SCHEMA_V2_KEYS}"
        )
    ensure_private_dir(router_state_dir())
    atomic_write_text(router_state_path(),
                      json.dumps(state, ensure_ascii=False, indent=2))
    return state


def _append_line(row: dict[str, Any]) -> None:
    p = ledger_path()
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.chmod(p, 0o600)


def _write_degraded_alert(exc: BaseException, meta: dict[str, Any]) -> "Path | None":
    """G-4：ledger 追加失败时落独立 .degraded 告警件（原子 0600），绝不假装成功。

    告警件本身写入失败不得顶掉原始异常（告警是辅助证据，调用失败语义不变）。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    p = quota_dir() / (f"ledger.degraded.{stamp}.{os.getpid()}."
                       f"{uuid.uuid4().hex[:8]}.json")
    doc = {"ts_iso": _now_iso(),
           "exception_type": type(exc).__name__,
           "exception": redact(str(exc))[:500],
           "call_meta": meta}
    try:
        atomic_write_text(p, json.dumps(doc, ensure_ascii=False, indent=2))
    except Exception:  # noqa: BLE001 —— 告警失败只吞告警，原始异常照抛
        return None
    return p


def _read_counters() -> dict[str, Any]:
    p = counters_path()
    if not p.exists():
        return {"consecutive_quota_errors": 0}
    return json.loads(p.read_text(encoding="utf-8"))


def _quarantine_counters() -> dict[str, Any]:
    """G-4：counters.json 损坏（不可解析/非对象/IO 错误）时把原件改名隔离，
    回退零值——成功追加的账行绝不允许被计数件读失败反向抹掉。"""
    cp = counters_path()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = cp.with_name(f"counters.corrupt.{stamp}.{os.getpid()}."
                          f"{uuid.uuid4().hex[:8]}.json")
    try:
        if cp.exists():
            os.replace(cp, backup)
    except OSError:
        # 隔离失败也不阻塞记账：零值回退语义不变（损坏件下次仍会被读到并再试）。
        return {"consecutive_quota_errors": 0}
    return {"consecutive_quota_errors": 0}


def _write_counters(c: dict[str, Any]) -> None:
    # G-7：原子 0600 tmp+replace（唯一 tmp 名，杜绝固定 .json.tmp 互踩与权限窗口）。
    c["updated_at_iso"] = _now_iso()
    atomic_write_text(counters_path(),
                      json.dumps(c, ensure_ascii=False, indent=2))


def _is_quota_err(err: "str | None") -> bool:
    if not err:
        return False
    text = err.lower()
    return any(k in text for k in ("quota", "balance", "billing", "欠费", "余额",
                                  "insufficient", "resource exhausted"))


class QuotaLedger:
    """批次闸门 + 记账（方向 setter/runner 在每次实呼前后各调一次）。"""

    def __init__(self) -> None:
        self.quota_dir = quota_dir()

    # ---------- 调用前：不发网拦截（命中另写一行 ok=false err=preflight_blocked） ----------

    def preflight(self, stage: str, *, task: int, batch_id: str) -> dict[str, Any]:
        with _FileLock():
            # 1) router 既有暂停态（m9 长熔断/人工暂停）——复用 is_paused，不复制逻辑
            try:
                paused = is_paused(stage)
            except PauseStateCorruptError:
                # G-14：暂停件损坏 fail-closed——不触网、不覆盖/删除损坏件，
                # 留 seq=null blocked 审计行后原样抛，交人工核查。
                self._record_blocked(
                    stage, task, batch_id,
                    "preflight_blocked: pause_state_corrupt：熔断状态文件损坏，人工核查")
                raise
            if paused:
                self._record_blocked(stage, task, batch_id,
                                     f"preflight_blocked: paused:{paused.get('reason')}")
                raise GatewayPausedError(paused)

            # 2) 批次书面批准凭据必须存在。
            # 锁内适配（计划 §6.2）：凭据缺失/不匹配不属于 §6.2 列举的持久熔断原因，
            # 只做当次不发网拦截 + 审计行（内存态、不落 quota_pause.json）；
            # 凭据补齐后下一次 preflight 自然放行，无需 resume ticket。
            try:
                approval = load_approval(batch_id)
            except QuotaError as exc:
                self._record_blocked(stage, task, batch_id,
                                     f"preflight_blocked: {redact(str(exc))[:200]}")
                raise GatewayPausedError({
                    "status": "BLOCKED_BATCH_APPROVAL_MISSING",
                    "persisted": False, "stage": stage, "reason": "batch_approval_missing",
                    "trigger": {"task": task, "batch_id": batch_id},
                }) from exc
            if approval["task"] != task or approval["stage"] != stage:
                self._record_blocked(stage, task, batch_id,
                                     "preflight_blocked: batch credential task/stage mismatch")
                raise GatewayPausedError({
                    "status": "BLOCKED_BATCH_APPROVAL_MISMATCH",
                    "persisted": False, "stage": stage,
                    "reason": "batch_approval_mismatch",
                    "trigger": {"task": task, "batch_id": batch_id,
                                "approval_task": approval["task"],
                                "approval_stage": approval["stage"]},
                })

            rows = call_lines()
            global_used = max((r["global_used_after"] for r in rows), default=0)
            batch_used = sum(1 for r in rows if r.get("batch_id") == batch_id)
            task_used = sum(1 for r in rows if r.get("task") == task)

            trigger = {"stage": stage, "task": task, "batch_id": batch_id,
                       "batch_approved": approval["approved"], "batch_used": batch_used,
                       "global_used": global_used, "global_cap": HARD_CAP}

            # 3) 全局硬门 48（先到先停，与单任务列无关）
            if global_used >= HARD_CAP:
                state = _write_budget_pause("global_hard_cap", trigger)
                self._record_blocked(stage, task, batch_id,
                                     f"preflight_blocked: global_hard_cap {global_used}")
                raise GatewayPausedError(state)

            # 4) 单任务局部硬上限。G-11：独立 reason=task_hard_cap + scope=task +
            # 实际 stage（历史误写 global_hard_cap 导致 resume 把任务顶当全局顶处理；
            # 任务顶是该任务绝对上限，批次扩展不能恢复——resume() 见 reason 直接拒绝）。
            cap = TASK_HARD_CAPS.get(task)
            if cap is not None and task_used >= cap:
                state = _write_budget_pause(
                    "task_hard_cap",
                    {**trigger, "task_used": task_used, "task_hard_cap": cap},
                    stage=stage, scope="task", affected_stages=[stage])
                self._record_blocked(stage, task, batch_id,
                                     f"preflight_blocked: task{task}_hard_cap {task_used}>={cap}")
                raise GatewayPausedError(state)

            # 5) 常态 36：越过区间必须持单独书面批准的 beyond_normal 凭据
            if global_used >= NORMAL_BUDGET and not approval.get("beyond_normal"):
                state = _write_budget_pause("batch_budget_reached", trigger)
                self._record_blocked(stage, task, batch_id,
                                     f"preflight_blocked: normal budget {global_used}"
                                     " without beyond_normal approval")
                raise GatewayPausedError(state)

            # 6) 本批次计数
            if batch_used >= approval["approved"]:
                state = _write_budget_pause("batch_budget_reached", trigger)
                self._record_blocked(stage, task, batch_id,
                                     f"preflight_blocked: batch {batch_id} used "
                                     f"{batch_used}>={approval['approved']}")
                raise GatewayPausedError(state)

            return {"global_used": global_used, "batch_used": batch_used,
                    "task_used": task_used, "batch_approved": approval["approved"]}

    # ---------- 调用出口：成功与配额类失败都记账 ----------

    def record(self, stage: str, *, task: int, batch_id: str, ok: bool,
               model: "str | None" = None, thread_id: str = "",
               tokens_in: "int | None" = None, tokens_out: "int | None" = None,
               tokens_cache_read: "int | None" = None,
               tokens_cache_create: "int | None" = None,
               err: "str | None" = None, evidence: "str | None" = None) -> dict[str, Any]:
        with _FileLock():
            rows = call_lines()
            seq = max((int(r["seq"]) for r in rows), default=0) + 1
            global_after = max((r["global_used_after"] for r in rows), default=0) + 1
            try:
                approval = load_approval(batch_id)
                batch_approved = approval["approved"]
                approval_ref = f"quota/approvals/{batch_id}.json"
            except QuotaError:
                # 正常不会发生（preflight 已拦）；记账不允许因凭据缺失而丢失
                batch_approved, approval_ref = None, None
            batch_after = sum(1 for r in rows if r.get("batch_id") == batch_id) + 1
            row = {
                "ts_iso": _now_iso(), "event": "call",
                "stage": stage, "model": model, "thread_id": thread_id,
                "task": task, "batch_id": batch_id, "seq": seq,
                "global_used_after": global_after,
                "batch_approved": batch_approved,
                "batch_used_after": batch_after,
                "tokens_in": tokens_in, "tokens_out": tokens_out,
                # Task0：prompt cache 两列（Anthropic cache_read/cache_create），
                # 取不到为 None，不猜不补。
                "tokens_cache_read": tokens_cache_read,
                "tokens_cache_create": tokens_cache_create,
                "ok": bool(ok), "err": redact(err)[:500] if err else None,
                "approval_ref": approval_ref, "evidence": evidence,
            }
            # G-4：账行追加失败（磁盘满/IO 错误）禁止假装成功——落独立 .degraded
            # 告警件后抛 LedgerDegradedError；调用方必须按失败处理，不得账外放行。
            try:
                _append_line(row)
            except Exception as exc:  # noqa: BLE001
                alert = _write_degraded_alert(exc, {
                    "time": _now_iso(), "stage": stage, "task": task,
                    "batch_id": batch_id, "seq": seq, "model": model,
                    "ok": bool(ok), "thread_id": thread_id,
                    "tokens_in": tokens_in, "tokens_out": tokens_out,
                    "tokens_cache_read": tokens_cache_read,
                    "tokens_cache_create": tokens_cache_create,
                    "err": redact(err)[:300] if err else None,
                })
                where = f"，告警件 {alert}" if alert is not None else "（告警件同样写入失败）"
                raise LedgerDegradedError(
                    f"ledger 账行追加失败（seq={seq}，{type(exc).__name__}）{where}："
                    "本次调用未入账，禁止按记账成功处理") from exc

            # 连续配额错误计数（成功归零；连续 3 次 → 长熔断，§6 阈值）
            # G-4：counters.json 损坏时改名隔离 + 零值回退——已成功追加的账行
            # 绝不允许被计数件读失败反向抹掉（调用方已付费，账必须算成功）。
            try:
                counters = _read_counters()
                if not isinstance(counters, dict):
                    raise TypeError("counters.json 顶层不是 JSON 对象")
            except (json.JSONDecodeError, TypeError, OSError):
                counters = _quarantine_counters()
            if ok:
                counters["consecutive_quota_errors"] = 0
            elif _is_quota_err(err):
                counters["consecutive_quota_errors"] = \
                    int(counters.get("consecutive_quota_errors", 0)) + 1
            _write_counters(counters)
            if counters.get("consecutive_quota_errors", 0) >= CONSECUTIVE_QUOTA_LIMIT:
                # G-14：暂停件已损坏时不覆盖（损坏件优先交人工核查）。
                try:
                    already_paused = is_paused(stage)
                except PauseStateCorruptError:
                    already_paused = True
                if already_paused is None:
                    _write_budget_pause("consecutive_quota_errors",
                                        {"consecutive_errors":
                                         counters["consecutive_quota_errors"],
                                         "stage": stage})
            return row

    def _record_blocked(self, stage: str, task: int, batch_id: str,
                        err: str, thread_id: str = "") -> None:
        """preflight 拦截审计行：不触网、不递增 seq/全局计数（§6.3）。

        LOW-5：审计行自身追加失败不得静默——落独立 .degraded 告警件；但拦截
        语义不变（调用方紧接着抛 GatewayPausedError，本次本就不发网），故告警
        后吞掉写入异常，避免顶掉真正的拦截异常。
        """
        rows = call_lines()
        global_used = max((r["global_used_after"] for r in rows), default=0)
        batch_used = sum(1 for r in rows if r.get("batch_id") == batch_id)
        row = {
            "ts_iso": _now_iso(), "event": "preflight_blocked",
            "stage": stage, "model": None, "thread_id": thread_id,
            "task": task, "batch_id": batch_id, "seq": None,
            "global_used_after": global_used,
            "batch_approved": None, "batch_used_after": batch_used,
            "tokens_in": None, "tokens_out": None,
            "tokens_cache_read": None, "tokens_cache_create": None,
            "ok": False, "err": err, "approval_ref": None, "evidence": None,
        }
        try:
            _append_line(row)
        except Exception as exc:  # noqa: BLE001 —— LOW-5：见 docstring
            _write_degraded_alert(exc, {"degraded": True,
                                        "audit_event": "preflight_blocked",
                                        "time": _now_iso(), "stage": stage,
                                        "task": task, "batch_id": batch_id,
                                        "err": redact(err)[:300]})


# ---------- Task3：调用方记账上下文 + 网关统一记账门 ----------

@dataclass
class BillingContext:
    """一次业务运行（CLI 进程 / graph 线程）的记账归属。

    task/batch_id 是 preflight 校验批次凭据的输入；thread_id/evidence 写入每行
    ledger。evidence 是每次实呼粒度，由调用方在 call() 前通过
    set_current_evidence() 刷新（如 direction 的 *.req.json 落盘后）。
    """

    task: int
    batch_id: str
    thread_id: str = ""
    evidence: "str | None" = None


_BILLING_CTX: ContextVar["BillingContext | None"] = ContextVar(
    "simpraxis_billing_ctx", default=None)


@contextmanager
def billing_context(*, task: int, batch_id: str, thread_id: str = ""):
    """生产入口必须用本上下文包住实呼；无上下文的挂账本网关 fail-closed。"""
    ctx = BillingContext(task=task, batch_id=batch_id, thread_id=thread_id)
    token = _BILLING_CTX.set(ctx)
    try:
        yield ctx
    finally:
        _BILLING_CTX.reset(token)


def current_billing() -> "BillingContext | None":
    return _BILLING_CTX.get()


def set_current_evidence(ref: "str | None") -> None:
    """把「本次实呼」的证据引用挂到当前上下文（无上下文时静默 no-op，
    供 monkeypatch/未挂账本的测试路径使用）。"""
    ctx = _BILLING_CTX.get()
    if ctx is not None:
        ctx.evidence = ref


class BillingGate:
    """ModelGateway.call/call_visual 入口/出口的唯一记账门（§1.5/§6.3）。

    入口：复用 router.is_paused → ledger.preflight（含凭据/全局/任务/批次六段）；
    出口：成功与失败都 ledger.record 一行整数 seq。preflight 拦截自身的
    seq=null 审计行由 QuotaLedger 负责，出口不重复记账。
    BillingGate 自身不含任何网络代码。
    """

    def __init__(self, ledger: "QuotaLedger | None" = None) -> None:
        self.ledger = ledger or QuotaLedger()

    @staticmethod
    def _ctx() -> BillingContext:
        ctx = _BILLING_CTX.get()
        if ctx is None:
            raise QuotaError(
                "网关挂了账本但缺少 billing_context（task/batch_id/thread_id）："
                "生产入口必须用 quota_ledger.billing_context(...) 包住实呼，"
                "无记账上下文的付费调用 fail-closed（禁止裸跑烧预算）")
        return ctx

    def preflight(self, stage: str) -> dict[str, Any]:
        ctx = self._ctx()
        return self.ledger.preflight(stage, task=ctx.task, batch_id=ctx.batch_id)

    def record(self, stage: str, *, ok: bool,
               model: "str | None" = None,
               tokens_in: "int | None" = None, tokens_out: "int | None" = None,
               tokens_cache_read: "int | None" = None,
               tokens_cache_create: "int | None" = None,
               err: "str | None" = None) -> dict[str, Any]:
        ctx = self._ctx()
        return self.ledger.record(
            stage, task=ctx.task, batch_id=ctx.batch_id, ok=ok, model=model,
            thread_id=ctx.thread_id, tokens_in=tokens_in, tokens_out=tokens_out,
            tokens_cache_read=tokens_cache_read,
            tokens_cache_create=tokens_cache_create,
            err=err, evidence=ctx.evidence)

    def record_blocked(self, stage: str, reason: str) -> None:
        """G-2/G-10/G-14：preflight 之前的不发网拦截（凭据缺失/入口暂停态/暂停件
        损坏）也要留 seq=null blocked 审计行。无 billing_context（账外 validate /
        monkeypatch 单测）时无账可写，静默 no-op——与 record() 的 fail-closed 不同，
        这些路径本就不挂账本（G-13 显式 unguarded=True）。
        """
        ctx = _BILLING_CTX.get()
        if ctx is None:
            return
        self.ledger._record_blocked(
            stage, ctx.task, ctx.batch_id,
            f"preflight_blocked: {reason}", thread_id=ctx.thread_id)


# ---------- 批次批准凭据 / 恢复（只接受显式参数的 CLI） ----------

def write_approval(*, batch_id: str, task: int, stage: str, approved: int,
                   beyond_normal: bool, note: str) -> Path:
    # G-9：写侧与读侧同一字符集白名单（路径段不可信）。
    try:
        validate_batch_id(batch_id)
    except (TypeError, ValueError) as exc:
        raise QuotaError(f"batch_id 非法：{exc}") from exc
    # G-9：bool 是 int 子类，显式拒绝 approved=True/False。
    if isinstance(approved, bool) or not isinstance(approved, int) or approved <= 0:
        raise QuotaError("approved 必须为正整数（不接受 bool）")
    if beyond_normal:
        rows = call_lines()
        used = max((r["global_used_after"] for r in rows), default=0)
        if used < NORMAL_BUDGET:
            raise QuotaError(
                f"beyond_normal 凭据只允许在全局已用 >= {NORMAL_BUDGET} 时开具"
                f"（当前 {used}）；36→48 每批单独书面批准（D1）")
        if used + approved > HARD_CAP:
            raise QuotaError(
                f"本批 {approved} 次将越过全局硬顶 {HARD_CAP}（当前已用 {used}）")
    p = approvals_dir() / f"{batch_id}.json"
    if p.exists():
        raise QuotaError(f"批次凭据已存在，禁止覆盖：{p}")
    doc = {"batch_id": batch_id, "task": task, "stage": stage,
           "approved": int(approved), "beyond_normal": bool(beyond_normal),
           "ledger_phase": LEDGER_PHASE,
           "created_at_iso": _now_iso(), "note": redact(note)[:1000]}
    # G-7：0600 tmp+replace 原子写（approvals 目录已由 approvals_dir() 收敛 0700）。
    atomic_write_text(p, json.dumps(doc, ensure_ascii=False, indent=2))
    return p


def resume(*, ticket: str, batch_id: "str | None" = None) -> Path:
    """唯一恢复路径：显式 uuid 票 + 预算暂停时须带新批次凭据（§6.2）。"""
    if not ticket or len(ticket) < 8:
        raise QuotaError("--ticket 必须是非空 uuid（>=8 字符）")
    pause = router_state_path()
    if not pause.exists():
        raise QuotaError("无暂停态文件，无需 resume")
    # G-14：暂停件损坏 fail-closed——resume 绝不删除损坏件，交人工核查。
    try:
        state = json.loads(pause.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise QuotaError(
            "熔断状态文件损坏，人工核查（resume 不删除损坏件）："
            f"{pause}：{type(exc).__name__}") from exc
    if not isinstance(state, dict):
        raise QuotaError(f"熔断状态文件损坏（顶层不是 JSON 对象），人工核查：{pause}")
    reason = state.get("reason", "")
    # G-11：任务局部硬顶是该任务的绝对上限，新批次凭据（批次扩展）不能恢复；
    # 只能在调高 TASK_HARD_CAPS 后人工处置或换任务。必须在预算/批次逻辑之前拒绝。
    if reason == "task_hard_cap":
        trig = state.get("trigger") if isinstance(state.get("trigger"), dict) else {}
        raise QuotaError(
            f"任务硬顶不可恢复（reason=task_hard_cap，scope=task，stage={state.get('stage')}，"
            f"task={trig.get('task')}，已用 {trig.get('task_used')}/"
            f"上限 {trig.get('task_hard_cap')}）：任务局部硬顶是绝对上限，"
            "不能用 --batch-id 批次扩展恢复；请人工核查（必要时调整 TASK_HARD_CAPS"
            "并重开审批），暂停件保留不删。")
    rows = call_lines()
    used = max((r["global_used_after"] for r in rows), default=0)
    if reason in ("batch_budget_reached", "global_hard_cap") or used >= NORMAL_BUDGET:
        if not batch_id:
            raise QuotaError("预算类恢复必须同时给 --batch-id 指向新批次批准凭据")
        approval = load_approval(batch_id)
        if used >= NORMAL_BUDGET and not approval.get("beyond_normal"):
            raise QuotaError(
                f"{NORMAL_BUDGET}→{HARD_CAP} 区间恢复：新批次凭据必须 "
                "--beyond-normal 开具")
    audit = quota_dir() / "resume_audit.jsonl"
    with open(audit, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts_iso": _now_iso(), "ledger_phase": LEDGER_PHASE,
                             "ticket": ticket,
                             "reason": reason, "batch_id": batch_id,
                             "global_used": used}, ensure_ascii=False) + "\n")
    os.chmod(audit, 0o600)
    pause.unlink()
    return audit


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(description="Phase 2 配额账本（批准凭据/恢复/计数；Phase 1 账已封存）")
    sub = p.add_subparsers(dest="cmd", required=True)

    # Task0：--task choices 取 Phase1/Phase2 caps 并集（编号沿用 Stage 账套）。
    all_tasks = sorted(set(PHASE1_TASK_CAPS) | set(PHASE2_TASK_CAPS))
    pa = sub.add_parser("approve", help="写入一批书面批准凭据（显式参数，无默认值）")
    pa.add_argument("--batch-id", required=True)
    pa.add_argument("--task", type=int, required=True, choices=all_tasks)
    pa.add_argument("--stage", required=True,
                    choices=("macro_ideation", "coding_debug", "visual_rollout"))
    pa.add_argument("--approved", type=int, required=True)
    pa.add_argument("--beyond-normal", action="store_true",
                    help="36→48 缓冲区间批次（全局已用>=36 才允许开具）")
    pa.add_argument("--note", required=True, help="书面批准出处/要点（必填）")

    pr = sub.add_parser("resume", help="凭显式 uuid 票解除暂停态")
    pr.add_argument("--ticket", required=True)
    pr.add_argument("--batch-id", default=None)

    st = sub.add_parser("status", help="打印当前计数与暂停态，不触网不写账")

    args = p.parse_args(argv)
    try:
        if args.cmd == "approve":
            path = write_approval(batch_id=args.batch_id, task=args.task,
                                  stage=args.stage, approved=args.approved,
                                  beyond_normal=args.beyond_normal, note=args.note)
            print(f"[quota] 批次凭据已写（0600）：{path}")
            return 0
        if args.cmd == "resume":
            path = resume(ticket=args.ticket, batch_id=args.batch_id)
            print(f"[quota] 暂停已解除，恢复流水：{path}")
            return 0
    except QuotaError as exc:
        # G-11/G-14：任务硬顶/损坏件/凭据问题给明确文案 + 退出码 2（不打 traceback）。
        print(f"[quota ERROR] {exc}", file=sys.stderr)
        return 2
    rows = read_lines()
    calls = call_lines(rows)
    used = max((r["global_used_after"] for r in calls), default=0)
    blocked = sum(1 for r in rows if r.get("event") == "preflight_blocked")
    # G-14：只读 status 也要把损坏件显式报成 pause_corrupt，不许崩溃或当无暂停。
    pause_view: Any
    try:
        pause_view = is_paused()
    except PauseStateCorruptError as exc:
        pause_view = {"corrupt": str(exc)}
    print(json.dumps({"ledger_phase": LEDGER_PHASE,
                      "ledger_path": str(ledger_path()),
                      "global_used": used, "normal_budget": NORMAL_BUDGET,
                      "hard_cap": HARD_CAP, "blocked_lines": blocked,
                      "paused": pause_view}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
