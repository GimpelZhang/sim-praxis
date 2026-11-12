"""RSI-R1.2 T1：最终请求审计（Plan §2.2）——公开合同必须真正进入实际 request。

纪律：

* 审计对象是**完整最终** :class:`orchestrator.common.research_transport.Request`
  的实际字节：system/task/history 逐段 hash、公开合同块逐字比对、资源数值从
  request 文本里**解析**出来再与 validator 的同源 typed limits 比较。不做
  「某份 README 里有 64」式的旁证。
* 公开合同块以固定 marker 嵌入 task brief；块字节必须与
  ``profiles.render_public_contract_v2`` 的当前渲染**逐字相等**——裁剪/改写/
  混入旧合同都在发送前拒绝。
* 动态 SHA 引用（plan/analysis/confirm manifest/nonce）必须来自真实公开结果：
  缺失、非 64 hex、或与可见状态不一致即 ``DYNAMIC_REF_INVALID``。
* 私有 canary/隐藏 seed/宿主判决标记混入 request 即 ``PUBLIC_CONTRACT_PRIVATE``。
* 审计件只含 hash 与公开数值（无凭据、无题面正文、无隐藏观测），每 POST 绑定
  ``request_sha256``；发送前写盘，拒绝也留证（verdict=refused）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from orchestrator.common import research_transport as T
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import resources as RS

AUDIT_SCHEMA = "simpraxis.rsi_r12.request_audit.v1"
CONTRACT_BEGIN = "### 公开资源合同（逐字嵌入；缺失/篡改/旧版混入即发送前拒绝）"
CONTRACT_END = "### 公开资源合同结束（以下不得含私有信息）"
STATE_HEADER = "### 当前可见状态（宿主事实；不含隐藏结果）"

#: 固定私有标记：候选可见面上永远不该出现这些词（泄漏即拒绝）。
PRIVATE_MARKERS = ("hidden_seed", "private_seed", "keeper_seed", "host_verdict",
                   "gold_answer", "judge_label", "reference_solution")

_STATE_REQUIRED_RESOURCE_KEYS = ("explore", "confirm", "replay", "runs_total",
                                 "steps", "sandbox", "wallclock", "aux")


class AuditError(C.ContractError):
    """最终请求审计拒绝（发送前，code 已登记公开条款）。"""


def _utc_now_iso_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_doc(doc: Any) -> str:
    return _sha(_canonical_bytes(doc))


def contract_text(contract_doc: Mapping[str, Any]) -> str:
    """当前合同的确定性渲染（校验与嵌入共用同一函数，不复制字面量）。"""
    return P.render_public_contract_v2(contract_doc)


def contract_block(contract_doc: Mapping[str, Any]) -> str:
    return CONTRACT_BEGIN + "\n" + contract_text(contract_doc) + "\n" + CONTRACT_END


def task_brief_with_contract(state: Any, contract_doc: Mapping[str, Any]) -> str:
    """可见状态 + 逐字公开合同块：候选在每次请求里都能读到帽/单位/规则。"""
    body = state.as_public()
    return (STATE_HEADER + "\n"
            + json.dumps(body, ensure_ascii=False, sort_keys=True, indent=1)
            + "\n\n" + contract_block(contract_doc)
            + "\n\n请给出你的下一个动作（单条 JSON）。")


# ---------------------------------------------------------------------------
# 从 request 实际字节里取数
# ---------------------------------------------------------------------------

def _first_content(request: T.Request) -> str:
    if not request.messages:
        raise AuditError("PUBLIC_CONTRACT_MISSING", "request 没有 messages")
    return str(request.messages[0].get("content") or "")


def parse_state_block(request: T.Request) -> "tuple[dict[str, Any], str]":
    """切出 task brief 的可见状态 JSON 与其余文本（供审计与测试共用）。"""
    content = _first_content(request)
    if not content.startswith(STATE_HEADER):
        raise AuditError("PUBLIC_CONTRACT_MISSING",
                         "task brief 不以可见状态块开头（精确 public view 缺失）")
    rest = content[len(STATE_HEADER):].lstrip("\n")
    head, sep, tail = rest.partition("\n\n")
    if not sep:
        raise AuditError("PUBLIC_CONTRACT_MISSING", "可见状态块后缺合同块")
    try:
        doc = json.loads(head)
    except ValueError as exc:
        raise AuditError("PUBLIC_CONTRACT_MISSING",
                         f"可见状态不是 JSON：{exc}") from None
    if not isinstance(doc, dict):
        raise AuditError("PUBLIC_CONTRACT_MISSING", "可见状态不是对象")
    return doc, tail


def _extract_block(request: T.Request) -> str:
    content = _first_content(request)
    if content.count(CONTRACT_BEGIN) != 1 or content.count(CONTRACT_END) != 1:
        raise AuditError(
            "PUBLIC_CONTRACT_MISSING",
            f"合同块 marker 不唯一（begin={content.count(CONTRACT_BEGIN)}, "
            f"end={content.count(CONTRACT_END)}）")
    start = content.index(CONTRACT_BEGIN)
    end = content.index(CONTRACT_END)
    if end < start:
        raise AuditError("PUBLIC_CONTRACT_MISSING", "合同块 marker 顺序颠倒")
    return content[start:end + len(CONTRACT_END)]


def _require(report: "dict[str, Any]", name: str, pattern: str, text: str,
             *, flags: int = 0) -> "list[str]":
    match = re.search(pattern, text, flags)
    if match is None:
        report["checks"].append({"name": name, "ok": False,
                                 "detail": "缺失（被删除/裁剪或旧合同混入）"})
        raise AuditError("PUBLIC_CONTRACT_MISSING",
                         f"公开合同块缺必需条目 {name}（pattern={pattern!r}）")
    report["checks"].append({"name": name, "ok": True, "detail": ""})
    return list(match.groups())


def _expect(report: "dict[str, Any]", name: str, got: Any, want: Any) -> None:
    if got != want:
        report["checks"].append({"name": name, "ok": False,
                                 "detail": f"validator={want!r} request={got!r}"})
        raise AuditError("PUBLIC_CONTRACT_DRIFT",
                         f"{name}: validator={want!r} 但 request 里是 {got!r}")
    report["checks"].append({"name": name, "ok": True, "detail": ""})


def expected_values(profile: P.CampaignProfile) -> "dict[str, Any]":
    """validator 的同源期望值（只从 profile/typed limits 派生）。"""
    limits = profile.resource_limits
    return {
        "lane_caps": {lane: int(profile.lane_post_caps[lane])
                      for lane in profile.lane_order},
        "posts_total": int(profile.hard_cap),
        "family_cap": int(profile.family_cap),
        "family_history": int(profile.family_history_posts),
        "explore_runs": int(limits.explore_runs_per_study),
        "explore_batches": int(limits.explore_batches_per_study),
        "confirm_blocks": int(limits.confirm_blocks),
        "confirm_conditions": int(limits.confirm_max_conditions),
        "confirm_runs": int(limits.confirm_runs_per_study),
        "replay_runs": int(limits.replay_runs_per_study),
        "total_runs": int(limits.total_runs),
        "total_steps": int(limits.total_steps),
        "steps_per_run": int(limits.steps_per_run),
        "sandbox": int(limits.sandbox_executions_per_study),
        "active_s": int(limits.active_wallclock_s),
        "container_s": int(limits.container_wallclock_s),
        "gpu_s": int(limits.gpu_wallclock_s),
        "request_timeout_s": int(limits.request_timeout_s),
        "analysis_timeout_s": int(limits.analysis_timeout_s),
        "physics_run_timeout_s": int(limits.physics_run_timeout_s),
        "aux_total": int(P.AUX_SUBENVELOPE_TOTAL),
        "aux_subenvelopes": {k: int(v) for k, v in P.AUX_SUBENVELOPES.items()},
    }


def _parse_values(report: "dict[str, Any]", block: str) -> "dict[str, Any]":
    """从合同块文本解析出与 validator 同口径的数值（正控/负控都走这里）。"""
    got: "dict[str, Any]" = {}
    got["schema_header"] = _require(report, "schema_header",
                                    r"^(# .+)$", block, flags=re.M)[0]
    lanes: "dict[str, int]" = {}
    for lane in P.LANE_ORDER_R12:
        lanes[lane] = int(_require(
            report, f"lane_cap:{lane}",
            rf"^- {re.escape(lane)}: 最大 (\d+) 次 POST$", block, flags=re.M)[0])
    got["lane_caps"] = lanes
    got["posts_total"] = int(_require(
        report, "posts_total", r"^- 本轮合计: (\d+)$", block, flags=re.M)[0])
    fam = _require(report, "family",
                   r"^- 跨轮研究族: 硬帽 (\d+) = 历史 (\d+) \+ 本轮 (\d+)$",
                   block, flags=re.M)
    got["family_cap"], got["family_history"], family_this = (int(x) for x in fam)
    got["family_this_round"] = family_this
    got["explore_runs"] = int(_require(
        report, "explore_runs", r"^- 累计上限: (\d+) run$", block, flags=re.M)[0])
    got["explore_batches"] = int(_require(
        report, "explore_batches", r"^- batch 上限: (\d+) 个", block,
        flags=re.M)[0])
    got["explore_cumulative_rule"] = _require(
        report, "explore_cumulative_rule",
        r"^- (\d+) 是每 study 累计上限，不是每条件 \d+，也不要求 32 block$",
        block, flags=re.M)[0]
    got["explore_no_autotruncate"] = _require(
        report, "explore_no_autotruncate", r"不得自动截断", block, flags=re.M)
    conf = _require(report, "confirm",
                    r"^- 确认: (\d+) 配对 block，最多 (\d+) 条件，≤ (\d+) run$",
                    block, flags=re.M)
    got["confirm_blocks"], got["confirm_conditions"], got["confirm_runs"] = (
        int(x) for x in conf)
    got["replay_runs"] = int(_require(
        report, "replay_runs", r"^- 复跑: ≤ (\d+) run", block, flags=re.M)[0])
    total = _require(report, "runs_total",
                     r"^- 总量: (\d+) run / (\d+) step（每 run ≤ (\d+) step）$",
                     block, flags=re.M)
    got["total_runs"], got["total_steps"], got["steps_per_run"] = (
        int(x) for x in total)
    got["sandbox"] = int(_require(
        report, "sandbox", r"^- 每 study 候选沙盒执行 ≤ (\d+) 次$", block,
        flags=re.M)[0])
    wall = _require(
        report, "wallclock",
        r"^- 每 study 活跃 ≤ (\d+) s；容器累计 ≤ (\d+) s；GPU 累计 ≤ (\d+) s$",
        block, flags=re.M)
    got["active_s"], got["container_s"], got["gpu_s"] = (int(x) for x in wall)
    io = _require(report, "io_timeouts",
                  r"^- 请求 (\d+) s；分析 (\d+) s；物理单 run (\d+) s$",
                  block, flags=re.M)
    got["request_timeout_s"], got["analysis_timeout_s"], \
        got["physics_run_timeout_s"] = (int(x) for x in io)
    aux = dict(re.findall(r"^- (\w+): (\d+) step$", block, flags=re.M))
    got["aux_subenvelopes"] = {k: int(v) for k, v in aux.items()
                               if k != "合计"}
    got["aux_total"] = int(aux.get("合计", -1))
    got["units"] = sorted(set(re.findall(r"^- (run|step|post|block): ", block,
                                         flags=re.M)))
    return got


def _check_dynamic(report: "dict[str, Any]", state_doc: Mapping[str, Any],
                   phase: str, expected_dynamic: Mapping[str, Any],
                   fields: "tuple[str, ...]") -> None:
    got_phase = state_doc.get("phase")
    _expect(report, "phase", got_phase, phase)
    latest = state_doc.get("latest_sha")
    if not isinstance(latest, Mapping):
        raise AuditError("PUBLIC_CONTRACT_MISSING", "可见状态缺 latest_sha")
    for field in fields:
        if field not in latest:
            raise AuditError("DYNAMIC_REF_INVALID",
                             f"可见状态缺动态引用 {field}")
        value = latest.get(field)
        if field not in expected_dynamic:
            raise AuditError("DYNAMIC_REF_INVALID",
                             f"审计方没有 {field} 的真实公开值")
        if value != expected_dynamic.get(field):
            raise AuditError(
                "DYNAMIC_REF_INVALID",
                f"{field}: 可见状态={value!r} 与真实公开结果="
                f"{expected_dynamic.get(field)!r} 不一致（不得预填/硬编码）")
        if value is None:
            continue
        if not isinstance(value, str) or not value:
            raise AuditError("DYNAMIC_REF_INVALID", f"{field} 非法：{value!r}")
        if field != "nonce":
            if len(value) != 64 or any(ch not in "0123456789abcdef"
                                       for ch in value):
                raise AuditError("DYNAMIC_REF_INVALID",
                                 f"{field} 不是 64 位小写十六进制：{value!r}")
    refs = state_doc.get("references")
    if not isinstance(refs, Mapping):
        raise AuditError("PUBLIC_CONTRACT_MISSING", "可见状态缺 references")
    known = {v for v in latest.values() if isinstance(v, str)}
    for name, ref in refs.items():
        if ref is None:
            continue
        if name == "nonce":
            if not isinstance(ref, str) or not ref or ref != latest.get("nonce"):
                raise AuditError("DYNAMIC_REF_INVALID",
                                 "references.nonce 与可见状态 nonce 不一致")
            continue
        if not isinstance(ref, str) or len(ref) != 64 or any(
                ch not in "0123456789abcdef" for ch in ref):
            raise AuditError("DYNAMIC_REF_INVALID",
                             f"references.{name} 非法：{ref!r}")
        if ref not in known:
            raise AuditError(
                "DYNAMIC_REF_INVALID",
                f"references.{name}={ref[:12]}… 不在当前有效动态 SHA 集合里")
    report["checks"].append({"name": "dynamic_refs", "ok": True, "detail": ""})


def _check_private(report: "dict[str, Any]", request: T.Request,
                   forbidden: "tuple[str, ...]") -> None:
    text = request.serialized
    hits = [canary for canary in forbidden if canary and canary in text]
    lower = text.lower()
    fixed = [marker for marker in PRIVATE_MARKERS if marker in lower]
    hits += fixed
    if hits:
        # 报错不回显 canary 值（私有内容不得进日志）：只报命中数与固定标记名
        detail = (f"命中 {len(hits)} 处（canary {len(hits) - len(fixed)}"
                  f" + 固定标记 {fixed[:5]}）")
        report["checks"].append({"name": "private_scan", "ok": False,
                                 "detail": detail})
        raise AuditError("PUBLIC_CONTRACT_PRIVATE",
                         f"request 含私有内容标记：{detail}")
    report["checks"].append({"name": "private_scan", "ok": True,
                             "detail": f"canary={len(forbidden)} "
                                       f"markers={len(PRIVATE_MARKERS)}"})


# ---------------------------------------------------------------------------
# 审计主体
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AuditReport:
    ok: bool
    code: "str | None"
    detail: str
    doc: Mapping[str, Any]
    sha256: str


def _history_sha(request: T.Request) -> str:
    return _sha_doc([{"role": str(m.get("role")), "content": str(m.get("content"))}
                     for m in request.messages])


def audit_request(request: T.Request, *, profile: P.CampaignProfile,
                  contract_doc: Mapping[str, Any], phase: str,
                  expected_dynamic: Mapping[str, Any],
                  forbidden: "tuple[str, ...]" = (),
                  raise_on_fail: bool = True) -> AuditReport:
    """对完整最终 request 生成审计件；拒绝时（默认）抛 :class:`AuditError`。"""
    if not profile.is_r12:
        raise AuditError("PROFILE_MISMATCH", "最终请求审计只接受 rsi-r1.2")
    P.assert_public_contract_v2_complete(contract_doc)
    want = expected_values(profile)
    report: "dict[str, Any]" = {
        "schema": AUDIT_SCHEMA, "schema_version": 1,
        "profile_id": profile.profile_id,
        "request_sha256": request.sha256,
        "request_n_bytes": int(request.n_bytes),
        "local_tokens": int(request.local_tokens),
        "phase": phase,
        "hashes": {
            "serialized_sha256": request.sha256,
            "system_sha256": _sha(request.system.encode("utf-8")),
            "task_sha256": _sha(_first_content(request).encode("utf-8")),
            "history_sha256": _history_sha(request),
        },
        "profile_sha256": C.document_hash(profile.to_dict()),
        "limits_sha256": RS.limits_document(profile)["limits_sha256"],
        "public_contract_sha256": C.document_hash(contract_doc),
        "resource_values": want,
        "values_in_request": {},
        "dynamic_refs": {k: expected_dynamic.get(k) for k in expected_dynamic},
        "forbidden": {"n_canaries": len(forbidden),
                      "markers": list(PRIVATE_MARKERS)},
        "sources": [
            f"profile:{profile.profile_id}",
            f"limits:{RS.limits_document(profile)['limits_sha256']}",
            f"public_contract:{C.document_hash(contract_doc)}",
            f"state:{request.sha256}",
        ],
        "checks": [],
        "verdict": "ok", "code": None, "detail": "",
    }
    try:
        state_doc, tail = parse_state_block(request)
        block = _extract_block(request)
        report["hashes"]["contract_block_sha256"] = _sha(block.encode("utf-8"))
        report["hashes"]["state_sha256"] = _sha_doc(state_doc)
        # 1) 逐字节：块必须等于当前合同渲染（旧合同/裁剪/夹带都会破）
        rendered = contract_block(contract_doc)
        if block != rendered:
            raise AuditError(
                "PUBLIC_CONTRACT_DRIFT",
                "合同块与当前渲染不逐字相等（旧版混入/被裁剪/夹带额外内容）")
        report["checks"].append({"name": "contract_bytes_exact", "ok": True,
                                 "detail": ""})
        # 2) 语义：从块内解析出的数值 == validator 同源值
        got = _parse_values(report, block)
        report["values_in_request"] = got
        _expect(report, "contract_schema", got["schema_header"],
                contract_text(contract_doc).splitlines()[0])
        for lane, cap in want["lane_caps"].items():
            _expect(report, f"lane_cap:{lane}", got["lane_caps"][lane], cap)
        for key in ("posts_total", "family_cap", "family_history",
                    "explore_runs", "explore_batches", "confirm_blocks",
                    "confirm_conditions", "confirm_runs", "replay_runs",
                    "total_runs", "total_steps", "steps_per_run", "sandbox",
                    "active_s", "container_s", "gpu_s", "request_timeout_s",
                    "analysis_timeout_s", "physics_run_timeout_s"):
            _expect(report, key, got[key], want[key])
        _expect(report, "family_this_round", got["family_this_round"],
                int(profile.hard_cap))
        _expect(report, "explore_cumulative_rule",
                int(got["explore_cumulative_rule"]), want["explore_runs"])
        _expect(report, "aux_total", got["aux_total"], want["aux_total"])
        for name, cap in want["aux_subenvelopes"].items():
            _expect(report, f"aux:{name}", got["aux_subenvelopes"].get(name), cap)
        got_units = set(got["units"])
        if not {"run", "step", "post", "block"} <= got_units:
            raise AuditError("PUBLIC_CONTRACT_MISSING",
                             f"单位条目缺失：{sorted(got_units)}")
        report["checks"].append({"name": "units", "ok": True, "detail": ""})
        # 3) 可见状态必须带精确资源快照（used/reserved/remaining）
        resources = state_doc.get("resources")
        if not isinstance(resources, Mapping):
            raise AuditError("PUBLIC_CONTRACT_MISSING",
                             "可见状态缺 resources 快照")
        for name in _STATE_REQUIRED_RESOURCE_KEYS:
            view = resources.get(name)
            if not isinstance(view, Mapping):
                raise AuditError("PUBLIC_CONTRACT_MISSING",
                                 f"resources.{name} 缺失")
            if name != "wallclock" and name != "aux":
                for field in ("used", "reserved", "remaining"):
                    if field not in view:
                        raise AuditError(
                            "PUBLIC_CONTRACT_MISSING",
                            f"resources.{name}.{field} 缺失")
        if "remaining" not in resources["wallclock"] and \
                "active_remaining_s" not in resources["wallclock"]:
            raise AuditError("PUBLIC_CONTRACT_MISSING",
                             "resources.wallclock 缺 remaining")
        if "total" not in resources["aux"] or \
                "remaining" not in resources["aux"]["total"]:
            raise AuditError("PUBLIC_CONTRACT_MISSING", "resources.aux 缺 total")
        report["checks"].append({"name": "resources_snapshot", "ok": True,
                                 "detail": ""})
        # 4) 动态引用 + 5) 私有内容
        _check_dynamic(report, state_doc, phase, expected_dynamic,
                       P.DYNAMIC_REF_FIELDS)
        _check_private(report, request, forbidden)
        if not tail.strip().endswith("请给出你的下一个动作（单条 JSON）。"):
            raise AuditError("PUBLIC_CONTRACT_MISSING",
                             "合同块之后缺动作提示（task brief 被截断）")
    except AuditError as exc:
        report["verdict"] = "refused"
        report["code"] = exc.code
        report["detail"] = str(exc)
        doc_sha = _sha_doc(report)
        if raise_on_fail:
            raise
        return AuditReport(ok=False, code=exc.code, detail=str(exc),
                           doc=report, sha256=doc_sha)
    return AuditReport(ok=True, code=None, detail="", doc=report,
                       sha256=_sha_doc(report))


class AuditedSender:
    """在真正 transport 之前生成并落盘审计件；拒绝即不触网。"""

    def __init__(self, transport: Callable[[T.Request], Any], *,
                 audit_dir: "Path | str", profile: P.CampaignProfile,
                 contract_doc: Mapping[str, Any], phase: str,
                 expected_dynamic: Mapping[str, Any],
                 forbidden: "tuple[str, ...]" = (),
                 clock: Callable[[], str] = _utc_now_iso_z) -> None:
        self.transport = transport
        self.profile = profile
        self.contract_doc = contract_doc
        self.phase = phase
        self.expected_dynamic = expected_dynamic
        self.forbidden = tuple(forbidden)
        self.audit_dir = Path(audit_dir)
        self.clock = clock

    def audit(self, request: T.Request) -> AuditReport:
        report = audit_request(
            request, profile=self.profile, contract_doc=self.contract_doc,
            phase=self.phase, expected_dynamic=self.expected_dynamic,
            forbidden=self.forbidden, raise_on_fail=False)
        self._write(report)
        if not report.ok:
            raise AuditError(str(report.code), "；".join(
                str(c["detail"]) for c in report.doc["checks"]
                if not c["ok"]) or str(report.detail))
        return report

    def _write(self, report: AuditReport) -> Path:
        self.audit_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.audit_dir / f"{report.doc['request_sha256']}.json"
        payload = json.dumps(report.doc, ensure_ascii=False, sort_keys=True,
                             indent=1).encode("utf-8") + b"\n"
        tmp = target.with_name(target.name + f".tmp.{os.getpid()}")
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, target)
        return target

    def send_once(self, request: T.Request) -> Any:
        self.audit(request)  # 拒绝在此抛出，transport 永不被调用
        return self.transport(request)


__all__ = [
    "AUDIT_SCHEMA", "CONTRACT_BEGIN", "CONTRACT_END", "STATE_HEADER",
    "PRIVATE_MARKERS", "AuditError", "AuditReport", "AuditedSender",
    "contract_text", "contract_block", "task_brief_with_contract",
    "parse_state_block", "audit_request", "expected_values",
]
