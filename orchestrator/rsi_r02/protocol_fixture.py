"""RSI-R0.2 协议 fixture 与协议检查器（计划 §6.1，T3 预演 / T4 准入）。

两条 fixture（P1/P2）各自是一条**真实链**：preregister → run_experiment →
analyze → submit。它们只使用公开信息（复用 E-dev 的公开场景参数与动作面；
**不读**其私有 tolerance / fault / reference_gains），判据是这里写死的协议
检查器，不是任务判据。

**nonce 绑定的通道**：``analyze`` 源码由候选写，必须在 stdout 打印一行
``PROTO_STAT <json>``；该行经工具回包 ``analysis.stdout_preview`` 回到候选，
候选在 ``submit`` 的原始 JSON 里逐字引用 ``nonce`` 与该统计值。因此

* 换链、换 nonce 后静态复读旧回复必然对不上（AC22「不能预先写下一轮消息」）；
* 检查器既看原始回复文本，也**独立重算**统计（不信自述）。

两条 fixture 的 nonce 由 campaign id + fixture id + slot 确定性派生并记录在
``protocol_contract.json``：它是反静态复读标记，不是能力样本。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from orchestrator.rsi_r01 import schemas as S
from orchestrator.rsi_r02 import candidate as CD
from orchestrator.rsi_r02 import messages as M

SCHEMA_FIXTURE = "simpraxis.rsi_r02.protocol_fixture.v1"
SCHEMA_VERDICT = "simpraxis.rsi_r02.protocol_verdict.v1"

FIXTURE_IDS = ("P1", "P2")
ACTION_ORDER = ("preregister", "run_experiment", "analyze", "submit")
STAT_KEY = "max_abs_x"
PROTO_LINE = "PROTO_STAT "

#: analyze 契约的**单一来源**（2026-11-10 T4 第二次 BLOCKED_PROTOCOL 的根因）：
#: 检查器要求 analyze 源码逐字含 nonce、PROTO_STAT 的 JSON 带 nonce/stat/run_ids，
#: 而当时的公开文档只在 submit 一句里提 nonce，容器数据布局（``/data/<run_id>.json``
#: 的列式形状）更是完全没写——候选逐条照文档做了，仍被判负，还先花掉三次 analyze
#: 去猜数据在哪。公开文档、机判（``doc_gaps``）、渲染（``messages.build_system``）
#: 与检查器（``verify_run``）现在共用这几个常量，于是「判据要什么」与「公开材料
#: 说什么」不可能再各说各话。**修复只补声明，不动槽数、不放宽判据**。
ANALYZE_SOURCE_MUST_CONTAIN = ("nonce",)
ANALYZE_PAYLOAD_FIELDS = ("nonce", "stat", "run_ids")
DATA_PATH_TEMPLATE = "/data/<run_id>.json"
ANALYZE_OUT_PATH = "/out/analysis.json"

#: 槽数口径的**单一来源**：D-T4-1 给每条链 2 个自修余量，于是每条链 6 个槽、
#: 两条链 12 个 POST。T0（预注册合同／硬帽）、T3（控制链）、T4（付费链）一律从
#: 这里取，禁止各自写 6/12 字面量（评审 MINOR-3）。
REPAIR_SLOTS_PER_CHAIN = 2
SLOTS_PER_CHAIN = len(ACTION_ORDER) + REPAIR_SLOTS_PER_CHAIN
TOTAL_PROTOCOL_SLOTS = SLOTS_PER_CHAIN * len(FIXTURE_IDS)

#: 判据侧（rsi_r01）真实要求的必填字段——文档必须逐一覆盖它们，不许只写一部分。
PREREG_REQUIRED = ("question", "h", "cells", "metric", "formula_version",
                   "delta", "censoring_rule", "analysis_procedure", "stop_rule")
SUBMIT_REQUIRED = ("claim", "run_ids", "analysis_ids", "nonce", "stat",
                   "stat_key")

#: preregister 的 plan 必填字段与约束（逐字写进公开文档；判据取自
#: ``rsi_r01.actions._parse_prereg`` 与 ``rsi_r01.evaluator.check_prereg``，
#: 单测 ``test_task_doc_declares_exactly_the_validator_required_fields``
#: 会与 ``evaluator.PREREG_REQUIRED_FIELDS`` 交叉比对，防止文档与判据漂移）。
PREREG_FIELD_SPEC: "dict[str, str]" = {
    "question": "字符串：本次实验要回答的单一问题",
    "h": "整数 +1 或 −1（方向性假设）",
    "cells": ("非空数组：每项一个对象，键取 interface.keys（本 fixture 计划 1 格，"
              "参数见 prereg.values）"),
    "metric": "字符串：观测量名（本 fixture 取 interface.protocol.stat_key）",
    "formula_version": "字符串：统计公式版本标识",
    "delta": f"数值：必须逐字等于 {float(S.DELTA)}（预规定最小效应）",
    "censoring_rule": "字符串：删失规则",
    "analysis_procedure": ("字符串：分析步骤；须写明将打印 "
                           f"{PROTO_LINE}<json> 行"),
    "stop_rule": "字符串：停止规则",
}

#: submit 的必填字段（判据取自 ``_parse_submit`` 与 ``verify_run``）。
SUBMIT_FIELD_SPEC: "dict[str, str]" = {
    "claim": "字符串：SUPPORTED / REFUTED / INCONCLUSIVE（大写，逐字）",
    "run_ids": "非空数组：本轮 run_experiment 返回的 run_id，逐字",
    "analysis_ids": "数组：本轮 analyze 返回的 analysis_id，逐字且仅一个",
    "nonce": "字符串：逐字等于 interface.protocol.nonce",
    "stat": "数值：逐字等于 analyze 打印的 PROTO_STAT 行里的 stat",
    "stat_key": "字符串：逐字等于 interface.protocol.stat_key",
}

#: 两条链各自的实验参数（公开、可核验；两链不同以证明结果非静态复读）。
FIXTURE_PARAMS: "dict[str, dict[str, Any]]" = {
    "P1": {"kp": 60.0, "kd": 8.0, "max_force": 30.0, "steps": 300},
    "P2": {"kp": 140.0, "kd": 15.0, "max_force": 70.0, "steps": 420},
}

PROTO_SOURCE_TEMPLATE = '''\
import glob, json
NONCE = {nonce!r}
docs = []
for path in sorted(glob.glob("/data/run_*.json")):
    with open(path, "r", encoding="utf-8") as fh:
        docs.append(json.load(fh))
stat = None
run_ids = []
for doc in docs:
    run_ids.append(doc["run_id"])
    if doc.get("x"):
        value = max(abs(v) for v in doc["x"])
        stat = value if stat is None else max(stat, value)
payload = {{"nonce": NONCE, "stat": stat, "run_ids": sorted(run_ids)}}
with open("/out/analysis.json", "w", encoding="utf-8") as fh:
    json.dump(payload, fh, sort_keys=True)
print("{line}" + json.dumps(payload, sort_keys=True))
'''


class ProtocolError(RuntimeError):
    """fixture 配置或协议前提被破坏（不是模型能力结论）。"""


def derive_nonce(*, campaign_id: str, fixture_id: str, slot_id: str) -> str:
    """确定性 nonce：同一 (campaign, fixture, slot) 可被第三方逐字重算。"""
    raw = f"{campaign_id}:protocol:{fixture_id}:{slot_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


@dataclass(frozen=True)
class ProtocolFixture:
    fixture_id: str
    instance_id: str
    slot_id: str
    nonce: str
    params: Mapping[str, Any]
    stat_key: str = STAT_KEY

    def __post_init__(self) -> None:
        if self.fixture_id not in FIXTURE_IDS:
            raise ProtocolError(f"未知 fixture：{self.fixture_id!r}")
        if not self.nonce:
            raise ProtocolError("nonce 不能为空")

    def as_dict(self) -> "dict[str, Any]":
        return {"schema": SCHEMA_FIXTURE, "fixture_id": self.fixture_id,
                "instance_id": self.instance_id, "slot_id": self.slot_id,
                "nonce": self.nonce, "params": dict(self.params),
                "stat_key": self.stat_key,
                "action_order": list(ACTION_ORDER),
                "analyze_line": PROTO_LINE,
                "note": "nonce 为反静态复读标记（确定性派生），非能力样本"}

    def task_doc(self) -> "dict[str, Any]":
        """候选可见的公开任务文档（协议合同放在 interface 里，逐字可见）。

        **R0.2 T4 首跑的教训**（2026-11-10，G-D FAIL）成文于此：系统提示写着
        「plan 的字段要求见任务说明」，而本文档当时没有任何字段要求——候选只能
        用自造字段提交，第 1 轮即被判缺字段。修复方式不是放宽判据，而是让公开
        材料**自己说清楚**：``prereg``/``submit`` 两个块逐字列出必填字段、类型
        约束与固定值（``delta``）。确定性 actor 也改为从本文档读取字段名来构造
        动作，于是「公开信息是否充分」这一步不再是空转。
        """
        return {
            "schema": "simpraxis.rsi_r02.protocol_task.v1",
            "instance_id": self.instance_id,
            "family": "E",
            "public": {
                "goal": (f"协议准入 fixture {self.fixture_id}：按四动作链完成一次公开实验，"
                         f"不得跳步或重排"),
                "prereg": {
                    "note": ("preregister 的 plan 对象必须逐字包含下列全部字段；"
                             "第一次实验之前提交，成功后不得重复登记。"),
                    "required_fields": dict(PREREG_FIELD_SPEC),
                    "delta_fixed": float(S.DELTA),
                    "cells_rule": ("cells 为非空数组，每项是对象，键取 interface.keys；"
                                   "本 fixture 只需计划 1 格，取 values 里的参数。"),
                    "values": dict(self.params),
                },
                "submit": {
                    "note": "submit 必须逐字回引本轮真实标识与统计，不得自述。",
                    "required_fields": dict(SUBMIT_FIELD_SPEC),
                },
                # 运行期 trial 的固定参数（宿主动态注入 timestep）也必须在公开材料
                # 里可复算：``messages.surface_from_doc`` 据此重建与运行期同形的
                # 动作面，合同期冻结的 rendered_gaps 才等于候选读到的文本
                # （root D reviewer MEDIUM-3）。本 fixture 的 trial 是 push_control
                # 实例，恒用场景的 timestep。
                "scene_params": {"timestep": float(S.DT_S)},
                # 2026-11-10 T4 第二次判负的根因在这里补：判据要的每一项都必须
                # 逐字写在公开材料里，否则「候选没做到」与「宿主没说」无法区分。
                "analyze": {
                    "note": ("analyze 的源码在受限容器内运行（无网络、只读根、"
                             "CPU 限制）；源码必须逐字包含本轮 nonce（字符串字面量），"
                             "并把一行 PROTO_STAT <json> 打印到 stdout。"),
                    "source_must_contain": list(ANALYZE_SOURCE_MUST_CONTAIN),
                    "prints_line": PROTO_LINE.strip() + " <json>",
                    "payload_fields": {
                        "nonce": "逐字等于 interface.protocol.nonce",
                        "stat": "本轮统计值（数值类型，算法见 data.stat_rule）",
                        "run_ids": ("本次分析覆盖的 run_id 数组（升序），"
                                    "必须与该 analyze 动作的 run_ids 相同"),
                    },
                    "output": ANALYZE_OUT_PATH,
                    "data": {
                        "path": DATA_PATH_TEMPLATE,
                        "note": ("容器内 /data 只读；文件名里的 <run_id> 就是 "
                                 "run_experiment 回包里给你的 run_id"),
                        "shape": "列式 JSON：每个键是一个等长数组",
                        "keys": ["t", *S.OBS_SERIES["push_control"], "schema",
                                 "kind", "run_id", "trial_id", "instance_id",
                                 "params"],
                        "stat_rule": (f"{STAT_KEY} = 对覆盖的每个 run 取 "
                                      f"max(abs(x_i))，多 run 取最大值"),
                    },
                },
                "interface": {
                    "kind": "push_control",
                    "keys": sorted(self.params),
                    "limits": {"kp": [0, 5000], "kd": [0, 500],
                               "max_force": [0, 500],
                               "steps": [1, 3000]},
                    "steps_max": 3000,
                    "protocol": {
                        "fixture_id": self.fixture_id,
                        "nonce": self.nonce,
                        "action_order": list(ACTION_ORDER),
                        "stat_key": self.stat_key,
                        "analyze_line": PROTO_LINE,
                        "posts_budget": 6,
                        "bind": ("analyze 源码必须逐字包含 nonce 并打印 PROTO_STAT <json>"
                                 "（json 字段见 public.analyze.payload_fields，数据见 "
                                 "public.analyze.data）；submit 必须逐字引用 nonce、"
                                 "STDOUT 里的 stat 与本轮 run_id/analysis_id"),
                        "retry_rule": (
                            "每次**已发网并收到回复**的动作计一次 POST（本链上限 "
                            "posts_budget；发网前被拒的请求不占 POST）。"
                            "分两类：**可自修失败**（JSON 解析失败、缺字段、类型不符，"
                            "以及动作跳步或重排、重复登记——那一次被拒且已计一次 POST）"
                            "的回包会说明原因，你可在剩余预算内改正，动作顺序要求不变；"
                            "**策略违规是终态**（params 含未授权键、数值越界），"
                            "本次协议链立即终止，不再接受任何动作。"
                            "无论哪类，成功的动作序列必须恰好等于动作顺序、每步一次。"),
                    },
                },
            },
        }


#: 每链第一个槽（= 公开文档里 nonce 的派生输入之一）；与 t0 槽位表一致。
_FIRST_SLOT = {"P1": "P-A-1", "P2": "P-B-1"}


def doc_gaps(doc: Mapping[str, Any]) -> "list[str]":
    """公开文档是否自足：逐字覆盖 prereg/submit 判据侧要求的全部字段。

    这是「公开信息充分性」的机判形式——判据侧（``PREREG_REQUIRED`` /
    ``SUBMIT_REQUIRED``）要什么，公开文档就必须声明什么。返回缺失项清单，
    空列表才算自足。``ProtocolStub._plan_from_doc`` 用同一组常量，故控制预演
    与契约检查不会各说各话。
    """
    pub = (doc or {}).get("public") or {}
    missing: "list[str]" = []
    # 动作面的白名单与协议块：``rendered_gaps`` 在缺协议块时会跳过检查（非协议
    # trial 不适用），所以「协议文档丢了协议块」必须由这里拦下，否则两道闸一起漏。
    iface = pub.get("interface") or {}
    if not iface.get("keys"):
        missing.append("public.interface.keys 缺失")
    proto = iface.get("protocol")
    if not isinstance(proto, Mapping) or not proto:
        missing.append("public.interface.protocol 缺失")
    else:
        missing += [f"public.interface.protocol.{k}"
                    for k in ("nonce", "stat_key", "analyze_line", "action_order")
                    if not proto.get(k)]
    prereg = (pub.get("prereg") or {}).get("required_fields")
    if not isinstance(prereg, Mapping):
        missing.append("public.prereg.required_fields 缺失")
    else:
        missing += [f"public.prereg.required_fields.{k}"
                    for k in PREREG_REQUIRED if k not in prereg]
    # cells 必须取实例参数；文档没给取值，候选只能自造（票一 T4 首跑即此类）。
    if not ((pub.get("prereg") or {}).get("values") or {}):
        missing.append("public.prereg.values 缺失")
    submit = (pub.get("submit") or {}).get("required_fields")
    if not isinstance(submit, Mapping):
        missing.append("public.submit.required_fields 缺失")
    else:
        missing += [f"public.submit.required_fields.{k}"
                    for k in SUBMIT_REQUIRED if k not in submit]
    # analyze 契约：检查器（verify_run）读源码里的 nonce、PROTO_STAT 载荷的
    # nonce/stat/run_ids、以及容器数据布局。这些以前**只存在于宿主的私有知识里**
    # （参考解与确定性 actor），公开文档一个字都没写——T4 第二次判负就是这条缝。
    analyze = pub.get("analyze")
    if not isinstance(analyze, Mapping):
        missing.append("public.analyze 缺失")
        return missing
    literal = analyze.get("source_must_contain")
    if not isinstance(literal, (list, tuple)):
        missing.append("public.analyze.source_must_contain 缺失")
    else:
        missing += [f"public.analyze.source_must_contain.{k}"
                    for k in ANALYZE_SOURCE_MUST_CONTAIN if k not in literal]
    payload = analyze.get("payload_fields")
    if not isinstance(payload, Mapping):
        missing.append("public.analyze.payload_fields 缺失")
    else:
        missing += [f"public.analyze.payload_fields.{k}"
                    for k in ANALYZE_PAYLOAD_FIELDS if k not in payload]
        # 只校验键名不够：值为空串等于没告诉候选这个字段是什么（评审 MINOR）。
        missing += [f"public.analyze.payload_fields.{k}.描述"
                    for k in ANALYZE_PAYLOAD_FIELDS
                    if k in payload and not str(payload.get(k) or "").strip()]
    # 打印行与产物路径同样要守：删掉它们校验器仍判负，但 doc_gaps 会误报自足。
    if not str(analyze.get("prints_line") or "").strip():
        missing.append("public.analyze.prints_line 缺失")
    if analyze.get("output") != ANALYZE_OUT_PATH:
        missing.append("public.analyze.output")
    if not str(analyze.get("note") or "").strip():
        missing.append("public.analyze.note 缺失")
    data = analyze.get("data")
    if not isinstance(data, Mapping):
        missing.append("public.analyze.data 缺失")
    else:
        if data.get("path") != DATA_PATH_TEMPLATE:
            missing.append("public.analyze.data.path")
        keys = data.get("keys")
        need = ("t", *S.OBS_SERIES["push_control"], "run_id")
        if not isinstance(keys, (list, tuple)):
            missing.append("public.analyze.data.keys 缺失")
        else:
            missing += [f"public.analyze.data.keys.{k}" for k in need
                        if k not in keys]
        if not str(data.get("stat_rule") or "").strip():
            missing.append("public.analyze.data.stat_rule")
    return missing


def contract_payload(*, campaign_id: str) -> "dict[str, Any]":
    """``contract/protocol_contract.json`` 的内容（D-T4-2：以前从未物化）。

    冻结面需要一份**可机读**的协议合同：动作顺序、每链槽数、必填字段表、
    两链公开文档的 sha256 与自足性检查结果。它在 T0 落盘、被 LOCK 逐件钉哈希，
    于是「候选看到的那份文档是否自足」变成签署过的断言，而不是事后再解释。
    """
    per_chain: "dict[str, Any]" = {}
    for fid in FIXTURE_IDS:
        fx = make_fixture(campaign_id=campaign_id, fixture_id=fid,
                          slot_id=_FIRST_SLOT[fid])
        doc = fx.task_doc()
        blob = json.dumps(doc, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
        per_chain[fid] = {
            "slot_id": _FIRST_SLOT[fid],
            "nonce": fx.nonce,
            "task_doc_sha256": hashlib.sha256(blob).hexdigest(),
            "doc_gaps": doc_gaps(doc),
            # N-12 子项三：doc_gaps 只查文档 dict，候选读到的却是渲染文本。
            # 这里把「渲染后是否逐字带着实例值」也冻结进合同（离线可重算）。
            "rendered_gaps": M.rendered_gaps(
                M.build_system(doc, M.surface_from_doc(doc)), doc),
        }
    return {
        "schema": "simpraxis.rsi_r02.protocol_contract.v1",
        "campaign_id": campaign_id,
        "action_order": list(ACTION_ORDER),
        "slots_per_chain": SLOTS_PER_CHAIN,
        "required_actions": len(ACTION_ORDER),
        "repair_slots_per_chain": REPAIR_SLOTS_PER_CHAIN,
        "total_posts_cap": TOTAL_PROTOCOL_SLOTS,
        "doc_must_be_self_sufficient": True,
        "prereg_required": list(PREREG_REQUIRED),
        "submit_required": list(SUBMIT_REQUIRED),
        "chains": per_chain,
        "note": ("坏 JSON/缺字段/不合法 prereg 属帽内可见错误，可在余量槽自修、"
                 "各计一次 POST；成功动作序列必须恰好等于 action_order。"),
    }


def check_contract(payload: Mapping[str, Any], *, campaign_id: str) -> "list[str]":
    """合同自查：重算一次并逐项比对（漂移即返回problems，不静默）。

    比对面覆盖**整份合同**而不只是槽数：字段表、自足性开关、campaign 绑定与
    schema 名都必须在盘上件里逐字一致，否则 LOCK 钉住的就不是本链实际运行
    的那份合同（评审 MINOR-4）。
    """
    problems: "list[str]" = []
    live = contract_payload(campaign_id=campaign_id)
    for key in ("action_order", "slots_per_chain", "required_actions",
                "repair_slots_per_chain", "total_posts_cap",
                "schema", "campaign_id", "prereg_required", "submit_required",
                "doc_must_be_self_sufficient", "chains"):
        if (payload or {}).get(key) != live[key]:
            problems.append(f"协议合同与实机不符：{key}")
    for fid, chain in (live["chains"] or {}).items():
        if chain.get("doc_gaps"):
            problems.append(f"{fid} 公开文档不自足：{chain['doc_gaps']}")
        if chain.get("rendered_gaps"):
            problems.append(f"{fid} 渲染文本未带实例值：{chain['rendered_gaps']}")
    return problems


def make_fixture(*, campaign_id: str, fixture_id: str, slot_id: str,
                 instance_id: str = "E-dev") -> ProtocolFixture:
    params = FIXTURE_PARAMS[fixture_id]
    return ProtocolFixture(fixture_id=fixture_id, instance_id=instance_id,
                           slot_id=slot_id,
                           nonce=derive_nonce(campaign_id=campaign_id,
                                              fixture_id=fixture_id,
                                              slot_id=slot_id),
                           params=dict(params))


def analyze_source(fixture: ProtocolFixture) -> str:
    return PROTO_SOURCE_TEMPLATE.format(nonce=fixture.nonce, line=PROTO_LINE)


# ---------------------------------------------------------------------------
# 确定性 actor（T3 预演用；T4 换成真实模型，检查器不变）
# ---------------------------------------------------------------------------


@dataclass
class ProtocolStub:
    """四动作确定性 actor：只读渲染后的工具结果，逐轮绑定本轮真实 id。"""

    fixture: ProtocolFixture
    usage: Mapping[str, Any] = field(
        default_factory=lambda: {"in": 1000, "out": 200,
                                 "cache_read": 0, "cache_create": 0})
    requests: "list[M.Request]" = field(default_factory=list)
    results: "list[dict[str, Any]]" = field(default_factory=list)
    decisions: "list[dict[str, Any]]" = field(default_factory=list)
    # 注入式坏回合（第几次 POST 从 1 起 → 原样回给宿主的文本）。T3/T4 用它真走
    # 一遍「解析级失败 → 余量内自修」的 loop，而不是只在检查器上打桩（D-T4-1）。
    inject: Mapping[int, str] = field(default_factory=dict)

    def __call__(self, request: M.Request) -> CD.Reply:
        self.requests.append(request)
        n = _turn_count(request.messages)
        if n > len(self.results):
            last = request.messages[-1] if request.messages else {}
            if last.get("role") != "user":
                raise ProtocolError("轮次计数与消息结构不符")
            raw = str(last.get("content") or "")
            try:
                doc = json.loads(raw)
            except ValueError:
                # 解析级失败时宿主回给模型的是**结构化错误**，不是 JSON 动作；
                # actor 必须能读它并继续修，而不是把预演判式自己崩掉。
                doc = {"raw_error": raw[:200]}
            self.results.append(doc if isinstance(doc, dict) else {})
        if n != len(self.results):
            raise ProtocolError(f"轮次计数漂移：turns={n} results={len(self.results)}")
        # 注入式坏回合：键是**第几次 POST**（从 1 起，= 本次请求里的 assistant
        # 消息数 + 1），值是原样回给宿主的文本。T3/T4 用它真走一遍「解析级失败
        # → 余量内自修」的 loop，而不是只在检查器上打桩（D-T4-1）。
        bad = self.inject.get(n + 1)
        text = bad if bad is not None else json.dumps(
            self._decide(), ensure_ascii=False, sort_keys=True)
        self.decisions.append({"turn": n, "text_sha256":
                               hashlib.sha256(text.encode()).hexdigest()[:16]})
        return CD.Reply(text=text, capture={
            "usage": dict(self.usage), "attempts_http": 1, "stop_reason": "stop",
            "provider_model": "protocol_stub",
            "provider_request_id": f"proto-{n}",
            "body_sha256": "0" * 64, "content_bytes": len(text),
            "producer": "protocol_stub_control"})

    def _decide(self) -> "dict[str, Any]":
        res = self.results
        # 找**第一次成功的** preregister，而不是硬认 res[0]：解析级失败、字段被拒
        # 的回包都不带 ``action``，而交付一个更多动作的响应体也可能产生任意回包；
        # 预演 actor 必须按「已成功的事实」推进，否则自修路径无法被真正走过。
        if not any(r.get("action") == "preregister" and r.get("ok") for r in res):
            if any(r.get("action") == "run_experiment" for r in res):
                raise ProtocolError("登记未成功却已在跑实验（actor 状态自相矛盾）")
            return {"schema": S.SCHEMA_ACTION, "action": "preregister",
                    "plan": self._plan_from_doc()}
        run = [r for r in res if r.get("action") == "run_experiment" and r.get("ok")]
        if not run:
            return {"schema": S.SCHEMA_ACTION, "action": "run_experiment",
                    "params": dict(self.fixture.params)}
        analyzed = [r for r in res if r.get("action") == "analyze" and r.get("ok")]
        if not analyzed:
            return {"schema": S.SCHEMA_ACTION, "action": "analyze",
                    "source": analyze_source(self.fixture),
                    "run_ids": [str(run[0]["run_id"])]}
        payload = _parse_proto_line(str((analyzed[0].get("analysis") or {})
                                        .get("stdout_preview") or ""))
        return {"schema": S.SCHEMA_ACTION, "action": "submit",
                "claim": S.ClaimStatus.SUPPORTED,
                "run_ids": [str(run[0]["run_id"])],
                "analysis_ids": [str((analyzed[0].get("analysis") or {})
                                     .get("analysis_id"))],
                "nonce": self.fixture.nonce,
                "stat": payload.get("stat"),
                "stat_key": self.fixture.stat_key}

    def _plan_from_doc(self) -> "dict[str, Any]":
        """从**公开文档**构造合法 plan：文档没写全就报错，而不是偷偷知道。

        T4 首跑的根因是确定性 actor 把合法 plan 硬编码，于是「公开信息充分性」
        预演永远通过。现在字段名必须由文档 ``public.prereg.required_fields``
        逐一声明，缺一个即 ``ProtocolError``——预演失败会在 LOCK 之前暴露，
        而不是花真实 POST 才暴露。
        """
        pub = self.fixture.task_doc().get("public") or {}
        spec = pub.get("prereg") or {}
        declared = spec.get("required_fields")
        if not isinstance(declared, Mapping):
            raise ProtocolError("公开文档缺 prereg.required_fields：候选无从得知必填字段")
        absent = [k for k in PREREG_REQUIRED if k not in declared]
        if absent:
            raise ProtocolError(f"公开文档未声明必填 plan 字段 {absent}")
        submit_spec = (pub.get("submit") or {}).get("required_fields")
        if not isinstance(submit_spec, Mapping):
            raise ProtocolError("公开文档缺 submit.required_fields")
        absent_submit = [k for k in SUBMIT_REQUIRED if k not in submit_spec]
        if absent_submit:
            raise ProtocolError(f"公开文档未声明必填 submit 字段 {absent_submit}")
        values = spec.get("values") or dict(self.fixture.params)
        return {
            "question": f"协议 fixture {self.fixture.fixture_id}：给定参数下 {self.fixture.stat_key} 是多少",
            "h": 1,
            "cells": [dict(values)],
            "metric": self.fixture.stat_key,
            "formula_version": "rsi_r02.protocol.v1",
            "delta": float(spec.get("delta_fixed", S.DELTA)),
            "censoring_rule": "无删失；实验失败或未产生观测记 INCONCLUSIVE",
            "analysis_procedure": (f"读取本轮 run 的 x 序列取 max|x|，"
                                   f"打印 {PROTO_LINE}<json> 行"),
            "stop_rule": "四动作链完成即停",
        }


# ---------------------------------------------------------------------------
# 自足性探针 actor（N-12 子项二）：只读**渲染后的公开文本**，不认识参考解
# ---------------------------------------------------------------------------


def _sections(text: str) -> "dict[str, list[str]]":
    """按 ``### 标题`` 切块（标题原样保序）。"""
    out: "dict[str, list[str]]" = {}
    cur = ""
    for line in text.splitlines():
        if line.startswith("### "):
            cur = line[4:].strip()
            out.setdefault(cur, [])
        elif cur:
            out[cur].append(line)
    return out


def _section(sections: "dict[str, list[str]]", prefix: str) -> "list[str] | None":
    for title, lines in sections.items():
        if title.startswith(prefix):
            return lines
    return None


def _bullets(lines: Sequence[str]) -> "list[tuple[str, str]]":
    """``* 名字：说明`` → [(名字, 说明)]；只收标识符名，顺序按出现。

    同一节里还夹着 ``* cells 规则：…``、``* 本实例的参数取值：…``、``* delta
    必须逐字等于 …`` 这类说明行；字段名的形态是**标识符**，据此把它们排除，
    免得把说明行误当必填字段。
    """
    out: "list[tuple[str, str]]" = []
    for line in lines:
        raw = line.strip()
        if not raw.startswith("* "):
            continue
        body = raw[2:]
        if "：" not in body:
            continue
        name, desc = body.split("：", 1)
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        out.append((name, desc.strip()))
    return out


@dataclass(frozen=True)
class ProbeView:
    """探针从渲染文本里解析出的全部实例事实（没有第二处来源）。"""
    nonce: str
    stat_key: str
    values: "dict[str, Any]"
    delta: "float | None"
    action_order: "tuple[str, ...]"
    prereg_fields: "tuple[str, ...]"
    submit_fields: "tuple[str, ...]"
    data_path: str
    data_keys: "tuple[str, ...]"
    stat_rule: str
    out_path: str
    print_line: str
    obs_key: str


def parse_rendered(system: str) -> ProbeView:
    """只从候选可见 system 文本抽取实例事实；抽不到就逐条报缺。

    这是「公开信息是否充分」的**行为级**检验：探针没有 fixture 对象，拿不到
    nonce/参数/源码模板，一切必须来自文本。
    """
    gaps: "list[str]" = []
    iface: "dict[str, Any]" = {}
    values: "dict[str, Any]" = {}
    delta: "float | None" = None
    order: "list[str]" = []
    for line in (system or "").splitlines():
        line = line.strip()
        if line.startswith("公开接口："):
            try:
                iface = json.loads(line[len("公开接口："):])
            except ValueError:
                gaps.append("公开接口 JSON 不可解析")
        elif line.startswith("* 本实例的参数取值："):
            try:
                values = json.loads(line[len("* 本实例的参数取值："):])
            except ValueError:
                gaps.append("本实例参数取值 JSON 不可解析")
        elif line.startswith("* delta 必须逐字等于"):
            raw = line.split("等于", 1)[-1].strip()
            try:
                delta = float(raw)
            except ValueError:
                gaps.append("delta 不可解析")
        elif re.match(r"^\d\)\s", line):
            m = re.search(r'"action"\s*:\s*"([a-z_]+)"', line)
            if m:
                order.append(m.group(1))
    proto = (iface or {}).get("protocol") or {}
    if not order:
        order = [str(x) for x in proto.get("action_order") or []]
    if not order:
        gaps.append("动作顺序（动作协议编号行或 interface.protocol.action_order）")
    if not values:
        gaps.append("本实例参数取值")
    sections = _sections(system or "")
    prereg = _section(sections, "preregister 的 plan 必填字段")
    submit = _section(sections, "submit 必填字段")
    analyze = _section(sections, "analyze 契约")
    prereg_fields = tuple(n for n, _ in _bullets(prereg or []))
    submit_fields = tuple(n for n, _ in _bullets(submit or []))
    for label, names, required in (("prereg", prereg_fields, PREREG_REQUIRED),
                                   ("submit", submit_fields, SUBMIT_REQUIRED)):
        absent = [k for k in required if k not in names]
        if absent:
            gaps.append(f"{label} 渲染文本缺必填字段 {absent}")

    data_path = out_path = print_line = stat_rule = ""
    data_keys: "tuple[str, ...]" = ()
    payload_names: "list[str]" = []
    for raw in (analyze or []):
        line = raw.strip()
        if not line.startswith("* "):
            continue
        body = line[2:]
        if body.startswith("容器内数据文件："):
            data_path = body.split("：", 1)[1].split("（", 1)[0].strip()
        elif body.startswith("数据文件键："):
            data_keys = tuple(x.strip() for x in
                              body.split("：", 1)[1].replace("、", ",").split(",")
                              if x.strip())
        elif body.startswith("统计算法："):
            stat_rule = body.split("：", 1)[1].strip()
        elif body.startswith("分析产物写到："):
            out_path = body.split("：", 1)[1].strip()
        elif body.startswith("必须向 stdout 打印一行："):
            print_line = body.split("：", 1)[1].strip()
        else:
            m = re.match(r"^(.*) 里的 ([A-Za-z_][A-Za-z0-9_]*)：", body)
            if m:
                payload_names.append(m.group(2))
    if not data_path:
        gaps.append("容器内数据文件路径")
    if not print_line:
        gaps.append("stdout 打印行格式")
    if not out_path:
        gaps.append("分析产物路径")
    if not stat_rule:
        gaps.append("统计算法")
    absent_payload = [k for k in ANALYZE_PAYLOAD_FIELDS
                      if k not in payload_names]
    if absent_payload:
        gaps.append(f"打印行载荷字段 {absent_payload}")
    m = re.search(r"max\(abs\((\w+?)(?:_i)?\)\)", stat_rule or "")
    if not m:
        gaps.append("统计规则不可解析（需要形如 max(abs(x_i)) 的写法）")
    if gaps:
        raise ProtocolError(f"渲染文本不足以驱动探针：{sorted(set(gaps))}")
    return ProbeView(
        nonce=str(proto.get("nonce") or ""),
        stat_key=str(proto.get("stat_key") or ""),
        values=dict(values), delta=delta, action_order=tuple(order),
        prereg_fields=prereg_fields, submit_fields=submit_fields,
        data_path=data_path, data_keys=data_keys, stat_rule=stat_rule,
        out_path=out_path, print_line=print_line,
        obs_key=m.group(1))


class ProbeActor:
    """只读渲染文本的四动作 actor（自足性探针，不是能力测量）。

    与 ``ProtocolStub`` 的分界：Stub 手里握着 ``ProtocolFixture``，nonce、参数、
    analyze 源码模板都是现成的，于是它证明不了「公开文本够不够用」。本 actor
    在**第一次被调用时**才从 ``request.system`` 解析实例事实（``parse_rendered``），
    解析不出即 ``ProtocolError``——预演阶段就暴露，而不是花真实 POST 才发现。
    唯一保留的先验是字段的**语义**（plan 里 question/h/cells 各是什么），这属于
    任何候选的常识，不属于本 fixture 的私有真值。
    """

    def __init__(self) -> None:
        self.view: "ProbeView | None" = None
        self.requests: "list[M.Request]" = []
        self.results: "list[dict[str, Any]]" = []
        self.decisions: "list[dict[str, Any]]" = []

    # -- 动作 -------------------------------------------------------------

    def _source(self) -> str:
        v = self.view
        assert v is not None
        glob_pat = re.sub(r"<run_id>", "*", v.data_path)
        line = v.print_line.split("<json>")[0] if "<json>" in v.print_line \
            else v.print_line + " "
        if v.data_keys and v.obs_key not in v.data_keys:
            # 统计规则指向的观测列必须在文档声明的数据键里；否则探针写出的源码
            # 与「公开的数据布局」自相矛盾。
            raise ProtocolError(
                f"统计规则要求观测列 {v.obs_key!r}，但公开数据键是 {v.data_keys}")
        return "\n".join([
            "import glob, json",
            f"NONCE = {v.nonce!r}",
            "docs = []",
            f"for path in sorted(glob.glob({glob_pat!r})):",
            "    with open(path, 'r', encoding='utf-8') as fh:",
            "        docs.append(json.load(fh))",
            "stat = None",
            "run_ids = []",
            "for doc in docs:",
            "    run_ids.append(doc['run_id'])",
            f"    if doc.get({v.obs_key!r}):",
            f"        value = max(abs(v) for v in doc[{v.obs_key!r}])",
            "        stat = value if stat is None else max(stat, value)",
            "payload = {'nonce': NONCE, 'stat': stat, 'run_ids': sorted(run_ids)}",
            f"with open({v.out_path!r}, 'w', encoding='utf-8') as fh:",
            "    json.dump(payload, fh, sort_keys=True)",
            f"print({line!r} + json.dumps(payload, sort_keys=True))",
        ])

    def _plan(self) -> "dict[str, Any]":
        v = self.view
        assert v is not None
        body = {
            "question": "这一次实验里给定参数下观测量是多少",
            "h": 1,
            "cells": [dict(v.values)],
            "metric": "本链的全部 run 的核心统计（见 analyze）",
            "formula_version": "probe/derived-from-text",
            "delta": float(v.delta if v.delta is not None else S.DELTA),
            "censoring_rule": "无删失；实验失败或未产生观测记 INCONCLUSIVE",
            "analysis_procedure": f"打印 {v.print_line} 一行",
            "stop_rule": "四动作链完成即停",
        }
        unknown = [k for k in v.prereg_fields if k not in body]
        if unknown:
            # 文档要求一个探针产不出的字段 ⇒ 自足性不成立（不是探针能力问题）：
            # 公开材料把「宿主私有语义」写进了必填面。
            raise ProtocolError(f"公开文档要求了无法从文本推导的 plan 字段 {unknown}")
        return {k: body[k] for k in v.prereg_fields if k in body}

    def _decide(self) -> "dict[str, Any]":
        v = self.view
        assert v is not None
        res = self.results
        done = {r.get("action") for r in res if r.get("ok")}
        if "preregister" not in done:
            return {"schema": S.SCHEMA_ACTION, "action": "preregister",
                    "plan": self._plan()}
        if "run_experiment" not in done:
            return {"schema": S.SCHEMA_ACTION, "action": "run_experiment",
                    "params": dict(v.values)}
        run = [r for r in res if r.get("action") == "run_experiment" and r.get("ok")]
        if "analyze" not in done:
            return {"schema": S.SCHEMA_ACTION, "action": "analyze",
                    "source": self._source(), "run_ids": [str(run[0]["run_id"])]}
        ana = [r for r in res if r.get("action") == "analyze" and r.get("ok")]
        analysis = ana[0].get("analysis") or {}
        payload = _parse_proto_line(str(analysis.get("stdout_preview") or ""))
        return {"schema": S.SCHEMA_ACTION, "action": "submit",
                "claim": S.ClaimStatus.SUPPORTED,
                "run_ids": [str(run[0]["run_id"])],
                "analysis_ids": [str(analysis.get("analysis_id"))],
                "nonce": v.nonce,
                "stat": payload.get("stat"),
                "stat_key": v.stat_key}

    # -- 调用 -------------------------------------------------------------

    def __call__(self, request: M.Request) -> CD.Reply:
        self.requests.append(request)
        if self.view is None:
            self.view = parse_rendered(request.system)
        n = _turn_count(request.messages)
        if n > len(self.results):
            last = request.messages[-1] if request.messages else {}
            raw = str(last.get("content") or "")
            try:
                doc = json.loads(raw)
            except ValueError:
                doc = {"raw_error": raw[:200]}
            self.results.append(doc if isinstance(doc, dict) else {})
        if n != len(self.results):
            raise ProtocolError(f"轮次计数漂移：turns={n} results={len(self.results)}")
        text = json.dumps(self._decide(), ensure_ascii=False, sort_keys=True)
        self.decisions.append({"turn": n, "text_sha256":
                               hashlib.sha256(text.encode()).hexdigest()[:16]})
        return CD.Reply(text=text, capture={
            "usage": {"in": 1000, "out": 200, "cache_read": 0, "cache_create": 0},
            "attempts_http": 1, "stop_reason": "stop",
            "provider_model": "protocol_probe",
            "provider_request_id": f"probe-{n}",
            "body_sha256": "0" * 64, "content_bytes": len(text),
            "producer": "protocol_probe_control"})


def _turn_count(messages: Sequence[Mapping[str, str]]) -> int:
    n = 0
    for m in messages:
        if m.get("role") == "assistant":
            n += 1
        elif (m.get("role") == "user"
              and str(m.get("content") or "").startswith("### 更早轮次")):
            n += sum(1 for line in str(m["content"]).splitlines()
                     if line.startswith("- "))
    return n


def _parse_proto_line(stdout: str) -> "dict[str, Any]":
    for line in stdout.splitlines():
        if line.startswith(PROTO_LINE):
            try:
                doc = json.loads(line[len(PROTO_LINE):])
            except ValueError as exc:
                raise ProtocolError(f"PROTO_STAT 行不是合法 JSON：{exc}") from exc
            if not isinstance(doc, dict):
                raise ProtocolError("PROTO_STAT 必须是 JSON 对象")
            return doc
    raise ProtocolError("stdout 里没有 PROTO_STAT 行")


# ---------------------------------------------------------------------------
# 协议检查器（T4 对真实模型用同一个）
# ---------------------------------------------------------------------------


def recovered_runs(trusted_dir: "str | Path", trial_id: str,
                   problems: "list[str] | None" = None) -> "list[Any]":
    """已终态 trial 的 run 记录复原（重跑幂等；评审 H4 重跑暴露的缺口）。

    恢复语义下 ``TrialRunner.runs`` 是空的（本进程没跑物理动作），但观测原件
    与 manifest 仍躺在 ``observations/trusted/<instance>/``。这里**只**重建
    定位信息（run_id + manifest 原文），统计重算仍由 :func:`_host_stat` 经
    ``store.load_trusted`` 逐件验 hash/trial/instance——重建不降低任何校验。

    ``cell`` 从受信 manifest 原样取回；manifest 没有该键时按 **manifest 里的
    params + instance_id** 用与真实分派同一条 ``trial.cell_of`` 归类复原——
    恒置 None 会让 R 族独立 oracle 在重跑时整族跳过（评审 LOW-1：那个分支
    看似复原、实际空转）。读不了的 manifest 不静默跳过（评审 N3）：
    给 ``problems`` 就逐条记账，不给就直接抛。
    """
    from orchestrator.rsi_r01 import task_contract as TC
    from orchestrator.rsi_r01 import trial as TRIAL
    from orchestrator.rsi_r01.trial import RunRecord

    def _cell(doc: Mapping[str, Any], name: str) -> Any:
        # manifest 自带 cell 就原样用（E/D 族是参数字典，R 族是格标签），
        # 不做任何类型转换。
        if doc.get("cell") is not None:
            return doc["cell"]
        params = doc.get("params")
        inst_id = doc.get("instance_id")
        if not isinstance(params, Mapping) or not inst_id:
            return None
        try:
            return TRIAL.cell_of(TC.get(str(inst_id)), params)
        except Exception as exc:  # noqa: BLE001 —— 含 TrialRejected/KeyError
            if problems is None:
                raise ProtocolError(
                    f"清单 cell 不可复原：{name}: {exc}") from exc
            problems.append(f"CELL_UNRESOLVED: {name}: {exc}")
            return None

    out: "list[Any]" = []
    for path in sorted(Path(trusted_dir).glob("run_*.manifest.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            if problems is None:
                raise ProtocolError(f"清单不可读：{path.name}: {exc}") from exc
            problems.append(f"MANIFEST_UNREADABLE: {path.name}: {exc}")
            continue
        if str(doc.get("trial_id")) != str(trial_id):
            continue
        out.append(RunRecord(run_id=str(doc.get("run_id")
                                       or path.name.split(".")[0]),
                             cell=_cell(doc, path.name), params={}, manifest=doc,
                             summary={}))
    return out


def verify_run(fixture: ProtocolFixture, *, events: Sequence[Mapping[str, Any]],
               trial: Any, post_budget: int = 4) -> "dict[str, Any]":
    """检查四动作链 + nonce/统计绑定；统计由宿主独立重算。

    ``post_budget`` 是本链允许消耗的 POST 上限（plan/protocol 的槽数）。默认 4＝
    无自修余量（确定性 actor 的控制链）；付费链按预注册传更大值，坏 JSON /
    缺字段 / 不合法 prereg 各占一次 POST、可在余量内自修——但**成功动作序列**
    仍必须恰好等于 ``ACTION_ORDER``（多一次或少一次都算链 FAIL）。

    判据分两层，别混：**成功动作序列 + POST 数区间**是链成立的判据；
    不可解析的回复只计入 ``evidence["unusable_replies"]``，不单独判错——
    它们本身就是余量要吸收的东西，而「余量被打光」会在成功序列上现形。
    """
    problems: "list[str]" = []
    evidence: "dict[str, Any]" = {"fixture_id": fixture.fixture_id,
                                  "nonce": fixture.nonce}

    results = [e for e in events
               if e.get("kind") == "result" and e.get("action") == "post"]
    texts = [str((e.get("payload") or {}).get("assistant_text") or "")
             for e in results]
    acts = [e for e in events
            if e.get("kind") == "result" and e.get("ok") is True
            and e.get("action") not in (None, "post")]
    evidence["n_posts"] = len(results)
    evidence["actions"] = [str(a.get("action")) for a in acts]
    if not 4 <= len(results) <= max(4, int(post_budget)):
        problems.append(f"POST 数须在 4..{max(4, int(post_budget))} 之间"
                        f"（四个必做动作 + 自修余量），实际 {len(results)}")
    if tuple(evidence["actions"]) != ACTION_ORDER:
        problems.append(f"动作顺序不符（成功动作须恰为 {list(ACTION_ORDER)}）："
                        f"{evidence['actions']}")

    parsed: "list[dict[str, Any]]" = []
    unusable = 0
    for text in texts:
        try:
            doc = json.loads(text)
        except ValueError:
            # 解析级失败在 §10.2 里是「帽内可见错误，计一次 POST」，不是链的
            # 硬错：它恰好就是 D-T4-1 给余量的那条路。这里只记数；链是否成立
            # 仍由「成功动作序列 == ACTION_ORDER」与 POST 数区间把关——若模型
            # 把余量全花在坏 JSON 上，成功序列自然不齐，链照样 FAIL。
            unusable += 1
            parsed.append({})
            continue
        parsed.append(doc if isinstance(doc, dict) else {})
    evidence["unusable_replies"] = unusable
    by_action = {str(d.get("action")): d for d in parsed if d}

    analyze = by_action.get("analyze") or {}
    source = str(analyze.get("source") or "")
    evidence["analyze_has_nonce"] = fixture.nonce in source
    if fixture.nonce not in source:
        problems.append("analyze 源码没有逐字包含 nonce")

    stats = {}
    analysis_id = None
    stdout = ""
    for evt in events:
        if evt.get("kind") != "result" or evt.get("action") != "analyze":
            continue
        pay = evt.get("payload") or {}
        if isinstance(pay, Mapping) and pay.get("stdout_preview"):
            stdout = str(pay["stdout_preview"])
        analysis_id = str((pay or {}).get("analysis_id") or "") or analysis_id
    if not stdout:
        # journal 里没有分析回包时，退回读 trial 自己的事件记录（同源）
        for ev in getattr(trial, "events", []):
            if ev.get("action") == "analyze" and ev.get("analysis"):
                stdout = str(ev["analysis"].get("stdout_preview") or "")
                analysis_id = str(ev["analysis"].get("analysis_id") or "") \
                    or analysis_id
    try:
        payload = _parse_proto_line(stdout)
    except ProtocolError as exc:
        problems.append(str(exc))
        payload = {}
    evidence["proto_payload"] = payload
    evidence["analysis_id"] = analysis_id
    if payload.get("nonce") != fixture.nonce:
        problems.append("PROTO_STAT 的 nonce 与 fixture 不符")

    submit = by_action.get("submit") or {}
    run_ids = list(submit.get("run_ids") or [])
    analysis_ids = list(submit.get("analysis_ids") or [])
    evidence["submit_run_ids"] = run_ids
    evidence["submit_analysis_ids"] = analysis_ids
    if str(submit.get("nonce")) != fixture.nonce:
        problems.append("submit 没有逐字引用 nonce")
    # 统计量名也必须逐字对得上公开接口：只比数值不比名，等于允许候选把别的
    # 量顶替 ``interface.protocol.stat_key``（评审 MINOR-5）。
    if str(submit.get("stat_key") or "") != fixture.stat_key:
        problems.append("submit 的 stat_key 与公开接口不符："
                        f"{submit.get('stat_key')!r} != {fixture.stat_key!r}")
    if run_ids != list(payload.get("run_ids") or []):
        problems.append("submit 的 run_id 与分析产物不一致")
    if analysis_id is None or analysis_ids != [analysis_id]:
        problems.append("submit 的 analysis_id 与本轮分析不符")
    if submit.get("stat") is None or submit.get("stat") != payload.get("stat"):
        problems.append("submit 的 stat 与 STDOUT 统计不一致")

    # 宿主独立重算统计（不信自述）
    expected = _host_stat(trial, run_ids, key=fixture.stat_key)
    evidence["host_stat"] = expected
    if expected is None:
        problems.append("宿主无法重算统计（缺可信观测）")
    elif payload.get("stat") is None or abs(float(payload["stat"]) - expected) > 1e-12:
        problems.append(f"统计与分析产物不符：{payload.get('stat')} != {expected}")

    return {"schema": SCHEMA_VERDICT, "fixture_id": fixture.fixture_id,
            "ok": not problems, "problems": problems, "evidence": evidence}


def _host_stat(trial: Any, run_ids: Sequence[str], *, key: str) -> "float | None":
    if key != STAT_KEY:
        raise ProtocolError(f"未知统计量 {key!r}")
    want = set(run_ids)
    best = None
    for rec in getattr(trial, "runs", []):
        if want and rec.run_id not in want:
            continue
        doc = trial.store.load_trusted(
            rec.run_id, rec.manifest,
            expected_trial=getattr(trial, "trial_id", None),
            expected_instance=getattr(getattr(trial, "instance", None),
                                      "instance_id", None))
        value = max(abs(float(v)) for v in doc["x"])
        best = value if best is None else max(best, value)
    return None if best is None else float(best)
