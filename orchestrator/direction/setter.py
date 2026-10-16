#!/usr/bin/env python3
"""SimPraxis 方向设定器（Phase 1 Task 2，替换基础计划 §6 heredoc）。

流程（硬规则：禁止按关键词选引擎，禁止硬编码任何引擎名）：
  query_kb(namespace="capabilities", top_k=8) 取能力矩阵证据
  → 引擎目录（可用集）来自 memory_flywheel/capability_matrix.yaml 的数据，
    源码不出现任何引擎名字面量分支
  → 拼 macro prompt（每个候选必须逐行引用证据 [Si]）
  → ModelGateway.call("macro_ideation", ...)（纯文本，永不构造 image block）
  → 严格 JSON 校验（2..3 候选、字段完整、sim_engine ∈ 目录可用集、
    evidence_refs 必须解析到本轮给出的证据行）；失败按 NonRetryable 语义只重问 1 次
  → Gate1 打回时 human_notes 作为最高优先级约束再问；打回 2 次 → human_failure。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

import yaml

from memory_flywheel.vector_engine import VectorEngine
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir
from orchestrator.common.validators import assert_within, validate_thread_id
from orchestrator.gateway.quota_ledger import set_current_evidence
from orchestrator.gateway.router import ModelGateway, redact

STAGE = "macro_ideation"
TOP_K = 8
MAX_REPAIR_ASK = 1          # 坏 JSON/校验失败：只重问 1 次
MAX_GATE1_REJECTS = 2       # D10：打回 2 次即 human_failure
# 锁内适配（2026-10-14 实呼取证 diag_raw_ping.json / d 轮 a0 截断）：macro 模型
# deepseek-v4.1-flash 经方舟 anthropic-messages shim 默认输出 thinking+text 双块
# （Anthropic 扩展思考形态），思考块与正文共享输出预算：2048 被思考耗尽（空响应）、
# 8192 时 3 个长方案在 ~3.5KB 处被截断（Unterminated string）。预算提至 16384，
# 并在系统提示中加逐字段长度上限防截断；thinking 块在 router 文本提取时丢弃。
MAX_TOKENS = 16384
TEMPERATURE = 0.2

REPO_ROOT = Path(__file__).resolve().parents[2]
MATRIX_PATH = REPO_ROOT / "memory_flywheel" / "capability_matrix.yaml"
BLUEPRINT_PATH = REPO_ROOT / "templates" / "tour_blueprint.json"

PROPOSAL_FIELDS = ("name", "sim_engine", "first_principles", "key_params",
                   "controls_vs_experiment", "evidence_refs", "risk",
                   "expected_artifacts")
_STR_FIELDS = ("name", "sim_engine", "first_principles", "key_params",
               "controls_vs_experiment", "risk")
# expected_artifacts 计划 schema 未标类型；真实 macro 输出（2026-10-14 两轮实测）
# 稳定给文件名数组，语义即"清单"——锁内适配：接受非空 list[str]，单字符串归一化。


class DirectionError(RuntimeError):
    """方向设定失败基类。"""


class DirectionSchemaError(DirectionError):
    """两次输出（含 1 次重问）均未通过严格 JSON 校验：不自动放行。"""


# ---------- 引擎目录（唯一事实源是能力矩阵数据文件；源码零引擎名） ----------

def load_engine_catalog(path: "str | Path" = MATRIX_PATH) -> dict[str, dict]:
    """可用引擎目录 = 矩阵中 coverage != absent 的条目；slug → 条目。

    矩阵 meta.kb_commit 钉死 KB HEAD；absent 条目（KB 零命中）不进可用集。
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    engines = data.get("engines") or []
    catalog: dict[str, dict] = {}
    for e in engines:
        slug = str(e.get("slug", "")).strip()
        if not slug:
            continue
        if e.get("coverage") == "absent":
            continue
        catalog[slug] = {
            "slug": slug,
            "display_name": e.get("display_name", slug),
            "kind": e.get("kind", ""),
            "coverage": e.get("coverage", ""),
            "facts": len(e.get("facts") or []),
        }
    if not catalog:
        raise DirectionError(f"能力矩阵无可用引擎（全部 absent？）：{path}")
    return catalog


def _normalize_engine(value: Any, catalog: dict[str, dict]) -> "str | None":
    """把模型回的值规范化为目录 slug（精确/忽略大小写/匹配展示名）。"""
    if not isinstance(value, str):
        return None
    v = re.sub(r"\s+", " ", value.strip()).lower()
    if not v:
        return None
    if v in catalog:
        return v
    for slug, e in catalog.items():
        if v == slug or v == re.sub(r"\s+", " ", e["display_name"]).lower():
            return slug
    return None


# ---------- prompt 构造 ----------

def _evidence_block(hits: list[dict]) -> str:
    lines = []
    for i, h in enumerate(hits, 1):
        locator = f"{h.get('kb_relpath', '?')}#{h.get('section_path', '?')}"
        text = str(h.get("text", "")).strip().replace("\n", " ")
        lines.append(f"[S{i}] [来源: {locator}] (doc_type={h.get('doc_type', '?')})\n{text}")
    return "\n\n".join(lines)


def _catalog_block(catalog: dict[str, dict]) -> str:
    return "\n".join(
        f"- slug={e['slug']}｜{e['display_name']}｜{e['kind']}｜"
        f"证据事实 {e['facts']} 条（coverage={e['coverage']}）"
        for e in catalog.values())


def _json_schema_text() -> str:
    return (
        '{"question": string（原样复述任务/题目）, "proposals": ['
        '{"name": string（候选方案名，候选间不得同名）, '
        '"sim_engine": string（必须且只能是下列引擎目录中的一个 slug）, '
        '"first_principles": string（第一性原理/物理依据，每个关键论断后标注 [Si]）, '
        '"key_params": string（关键物理/环境参数与取值或分布）, '
        '"controls_vs_experiment": string（对照组 vs 实验组/能力探针对照设计）, '
        '"evidence_refs": string[]（引用的证据编号，形如 ["S1","S2"]，'
        '只能引用本提示给出的 [S1]..[Sn]，至少 1 条）, '
        '"risk": string（主要失败风险与验证方式）, '
        '"expected_artifacts": string[]（预期产物文件名清单，非空数组，'
        '每个元素是一个具体文件名）}]}')


def _system_prompt(catalog: dict[str, dict], n_hits: int) -> str:
    return (
        "你是 SimPraxis 的具身智能仿真方向设定器，输出将进入人工 Gate1 审批。\n"
        "铁律：\n"
        "1. 只能使用下列【引擎目录】中的 slug 作为 sim_engine；目录外或证据为 absent "
        "的引擎一律不得出现；选择必须由给定知识库证据 [S1]..[S{n}] 支撑。\n"
        "2. 给出 2 到 3 个【实质不同解题路径】的候选（不同接触模型/不同后端/不同实验"
        "构造），禁止换名不换实的多样性坍塌。\n"
        "3. 每个候选的 evidence_refs 至少 1 条且必须能在本提示证据中找到；"
        "first_principles 的关键论断后逐行标注 [Si]。\n"
        "4. 只输出一个 JSON 对象，不要 markdown 代码围栏、不要解释文字。\n"
        "5. 长度纪律（防输出截断）：first_principles 每个候选 ≤350 字，"
        "key_params/controls_vs_experiment/risk 各不超过 200 字，"
        "evidence_refs 每个候选 ≤6 条，expected_artifacts 4..8 个文件名；"
        "信息密度优先，不要复述题目。\n"
        "JSON schema：\n{schema}\n\n"
        "【引擎目录（sim_engine 合法值）】\n{catalog}"
    ).format(n=n_hits, schema=_json_schema_text(), catalog=_catalog_block(catalog))


def _track1_user(payload: dict, hits: list[dict]) -> str:
    urls = "\n".join(f"- {u}" for u in payload.get("urls", []))
    spine = json.loads(BLUEPRINT_PATH.read_text(encoding="utf-8"))["canonical_spine"]
    spine_text = "\n".join(
        f"{s['stage_id']} {s['name']}：{s['objective']}" for s in spine)
    return (
        f"【Track 1：开源工具链深度巡礼】\n工具：{payload['name']}\n链接：\n{urls}\n\n"
        "任务：围绕该工具链设计 2-3 个实质不同的「工具链最小复现 + 能力探针」方案，"
        "沿 Stage 0 环境探活 → Stage 1 最小闭环 → Stage 2 泛化/批量 → "
        "Stage 3 评测递进（canonical spine 见下）。key_params 写关键依赖/渲染/物理参数；"
        "controls_vs_experiment 写探针对照项（如 CPU/GPU 后端、不同接触求解器对照）；"
        "expected_artifacts 写具体产物文件名（探针视频/JSON/报告）。\n\n"
        f"【canonical spine】\n{spine_text}\n\n"
        f"【知识库证据】\n{_evidence_block(hits)}")


def _track2_user(payload: dict, hits: list[dict]) -> str:
    return (
        f"【Track 2：具身工程难题第一性原理实证】\n题目：{payload['question']}\n\n"
        "任务：先在 first_principles 中给出严密的第一性原理/物理推导（公式与假设），"
        "再给出 2-3 个实质不同的仿真实证方案。每个方案必须：\n"
        "- key_params 列出待随机化/标定的物理量及其分布或取值（量级要可执行）；\n"
        "- controls_vs_experiment 明确 Baseline 对照组（参数崩溃/失真）与 Ours 实验组"
        "（理论模型/高保真）及判别指标；\n"
        "- sim_engine 的选择由证据 [Si] 支撑；expected_artifacts 含对照视频与指标 JSON。\n\n"
        f"【知识库证据】\n{_evidence_block(hits)}")


def build_first_messages(*, track: str, payload: dict, hits: list[dict],
                         catalog: dict[str, dict]) -> list[dict[str, str]]:
    system = _system_prompt(catalog, len(hits))
    if track == "track1":
        user = _track1_user(payload, hits)
    elif track == "track2":
        user = _track2_user(payload, hits)
    else:
        raise DirectionError(f"未知 track={track!r}")
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def build_repair_messages(messages: list[dict[str, str]], raw: str,
                          errors: list[str]) -> list[dict[str, str]]:
    """校验失败后的唯一一次重问（NonRetryable 语义：不触网重试，只修正内容 1 次）。"""
    return messages + [
        {"role": "assistant", "content": raw[:4000]},
        {"role": "user", "content":
         "上一输出未通过严格校验：\n- " + "\n- ".join(errors)
         + "\n\n请输出修正后的【单个 JSON 对象】，不要代码围栏、不要解释。"},
    ]


def build_reject_messages(*, track: str, payload: dict, hits: list[dict],
                          catalog: dict[str, dict], human_notes: str,
                          prev_proposals: list[dict]) -> list[dict[str, str]]:
    """Gate1 打回轮：人工意见最高优先级，必须逐条响应。"""
    messages = build_first_messages(track=track, payload=payload, hits=hits,
                                    catalog=catalog)
    messages.append({"role": "user", "content": (
        "【人工评审意见（最高优先级，必须逐条响应）】\n"
        f"{human_notes.strip()}\n\n"
        "上一轮候选方案（已被打回）：\n"
        f"{json.dumps(prev_proposals, ensure_ascii=False, indent=2)}\n\n"
        "请按人工意见给出修订后的 2-3 个方案：必要时更换解题路径（但 sim_engine "
        "仍限于引擎目录、仍须引用 [Si] 证据），只输出单个 JSON 对象。")})
    return messages


# ---------- 解析与严格校验 ----------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def parse_json_object(raw: str) -> dict:
    """从模型输出提取唯一 JSON 对象（围栏/前后噪声都容忍一次提取，不猜测内容）。"""
    text = raw.strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        obj = json.loads(text[start:end + 1])  # 再失败抛 json 错给上层
        if isinstance(obj, dict):
            return obj
    raise json.JSONDecodeError("未找到 JSON 对象", text, 0)


def _resolve_refs(value: Any, n_hits: int) -> "list[str] | None":
    if not isinstance(value, list) or not value:
        return None
    resolved: list[str] = []
    for v in value:
        if not isinstance(v, str):
            return None
        m = re.search(r"\[?\s*S(\d+)\s*\]?", v.strip(), re.IGNORECASE)
        if not m:
            return None
        idx = int(m.group(1))
        if not 1 <= idx <= n_hits or f"S{idx}" in resolved:
            return None
        resolved.append(f"S{idx}")
    return resolved


def _normalize_artifacts(value: Any) -> "list[str] | None":
    """expected_artifacts：非空文件名数组；容忍单个非空字符串（归一化为 1 元素）。"""
    if isinstance(value, str):
        v = value.strip()
        return [v] if v else None
    if not isinstance(value, list) or not value:
        return None
    items = [str(v).strip() for v in value]
    if any(not v for v in items):
        return None
    return items


def validate_proposals(obj: dict, *, hits: list[dict],
                       catalog: dict[str, dict]) -> "tuple[list[dict], list[str]]":
    """返回 (规范化 proposals, 错误列表)；错误为空即通过。"""
    errors: list[str] = []
    if not isinstance(obj, dict):
        return [], ["顶层不是 JSON 对象"]
    q = obj.get("question")
    if not isinstance(q, str) or not q.strip():
        errors.append("question 必须是非空字符串")
    raw_props = obj.get("proposals")
    if not isinstance(raw_props, list) or not (2 <= len(raw_props) <= 3):
        errors.append("proposals 必须是 2..3 个候选的数组")
        return [], errors
    names: list[str] = []
    out: list[dict] = []
    for i, p in enumerate(raw_props):
        tag = f"proposals[{i}]"
        if not isinstance(p, dict):
            errors.append(f"{tag} 不是对象")
            continue
        missing = [f for f in _STR_FIELDS if not isinstance(p.get(f), str)
                   or not p.get(f, "").strip()]
        if missing:
            errors.append(f"{tag} 缺/空字符串字段：{missing}")
        if not isinstance(p.get("evidence_refs"), list):
            errors.append(f"{tag} evidence_refs 必须是数组")
        artifacts = _normalize_artifacts(p.get("expected_artifacts"))
        if artifacts is None:
            errors.append(f"{tag} expected_artifacts 必须是非空文件名数组"
                          "（每项为非空字符串）")
        if (missing or not isinstance(p.get("evidence_refs"), list)
                or artifacts is None):
            continue
        refs = _resolve_refs(p["evidence_refs"], len(hits))
        if refs is None:
            errors.append(f"{tag} evidence_refs 非法：至少 1 条且只能是本轮 [S1]..[S{len(hits)}]")
        slug = _normalize_engine(p.get("sim_engine"), catalog)
        if slug is None:
            errors.append(
                f"{tag} sim_engine={p.get('sim_engine')!r} 不在引擎目录可用集 "
                f"{sorted(catalog)}（absent/未提及引擎禁用）")
        if p["name"].strip() in names:
            errors.append(f"{tag} 候选重名 {p['name']!r}（禁止多样性坍塌）")
        names.append(p["name"].strip())
        if refs is None or slug is None:
            continue
        qp = {f: p[f].strip() for f in _STR_FIELDS}
        qp["sim_engine"] = slug
        qp["evidence_refs"] = refs
        qp["expected_artifacts"] = artifacts
        out.append(qp)
    return (out if not errors else []), errors


# ---------- 证据落盘 ----------

def _evidence_dir() -> Path:
    # G-7：证据目录统一 0700（已存在且被放宽时同样收敛）。
    d = Path(os.environ.get("SIMPRAXIS_PHASE1_ROOT",
                            "/mnt/artifacts/simpraxis/phase1")) / "task2_direction"
    return ensure_private_dir(d)


def _write_evidence(stem: str, kind: str, payload: dict) -> str:
    p = _evidence_dir() / f"{stem}.{kind}.json"
    # G-7：0600 tmp+replace 原子写；G-5：写后 containment 断言（路径段不可信，
    # thread_id 即便上游漏校验也不允许写出证据目录）。
    atomic_write_text(p, json.dumps(payload, ensure_ascii=False, indent=2))
    assert_within(p, _evidence_dir())
    return f"phase1/task2_direction/{p.name}"


# ---------- 主类 ----------

class DirectionSetter:
    def __init__(self, gateway: ModelGateway, vector_engine: VectorEngine,
                 *, evidence_writer: "Callable | None" = None):
        # Task3（§6.3）：本类不再持账本——preflight/记账统一由 ModelGateway.call
        # 经 BillingGate 在 billing_context 内完成；构造网关时必须传 ledger=，
        # 生产 CLI 用 billing_context(task=2, batch_id=...) 包住整个运行。
        self.gateway = gateway
        self.vector_engine = vector_engine
        self._write_evidence = evidence_writer or _write_evidence
        self.catalog = load_engine_catalog()

    # 一次 macro 往返（含严格校验 + 最多 1 次重问），不做 Gate1 循环
    def run_direction(self, *, track: str, payload: dict, thread_id: str,
                      batch_id: str, gate_round: int = 0,
                      human_notes: "str | None" = None,
                      prev_proposals: "list[dict] | None" = None,
                      bypass_stamp: bool = False) -> dict:
        # G-5：thread_id 直接拼进证据文件名（macro_<thread>_* / proposals_<thread>_*），
        # 入口先过字符集白名单 ^[A-Za-z0-9_-]{1,64}$，拒绝遍历/分隔符形态。
        validate_thread_id(thread_id)
        query = payload["question"] if track == "track2" else \
            f"{payload['name']} 架构 物理引擎 特性 闭环仿真"
        hits = self.vector_engine.query_kb(query, namespace="capabilities",
                                           top_k=TOP_K)
        if not hits:
            raise DirectionError("capabilities 检索 0 行：无证据不得产地方向")
        kb_sources = [{
            "ref": f"S{i}", "id": h.get("id"),
            "kb_relpath": h.get("kb_relpath"),
            "section_path": h.get("section_path"),
            "doc_type": h.get("doc_type"),
        } for i, h in enumerate(hits, 1)]

        if human_notes and prev_proposals:
            messages = build_reject_messages(track=track, payload=payload, hits=hits,
                                             catalog=self.catalog,
                                             human_notes=human_notes,
                                             prev_proposals=prev_proposals)
        else:
            messages = build_first_messages(track=track, payload=payload, hits=hits,
                                            catalog=self.catalog)

        round_meta: list[dict] = []
        raw = ""
        obj: dict = {}
        proposals: list[dict] = []
        errors: list[str] = ["首轮"]
        for attempt in range(MAX_REPAIR_ASK + 1):
            stem = f"macro_{thread_id}_r{gate_round}_a{attempt}"
            # 内容守卫：每次实呼前都断言消息只能是纯文本块（硬规则1：macro 永不发 image）
            for mm in messages:
                if not isinstance(mm.get("content"), str):
                    raise DirectionError(
                        "macro_ideation 消息含非文本块（硬规则1 禁止 image）")
            # Task3：preflight/记账在 gateway.call 内部（BillingGate）；这里只把
            # 本次实呼的 req 证据引用挂到 billing_context，成功/失败账行都能指回它。
            model = self.gateway.model_id(STAGE)
            req_ref = self._write_evidence(stem, "req", {
                "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "stage": STAGE, "model": model, "thread_id": thread_id,
                "batch_id": batch_id, "track": track, "gate_round": gate_round,
                "attempt": attempt, "max_tokens": MAX_TOKENS,
                "temperature": TEMPERATURE,
                "gate_bypassed_locally": bypass_stamp,
                "messages": json.loads(redact(json.dumps(
                    messages, ensure_ascii=False))),
            })
            set_current_evidence(req_ref)
            try:
                raw, _usage = self.gateway.call(STAGE, messages,
                                                max_tokens=MAX_TOKENS,
                                                temperature=TEMPERATURE)
            except Exception as exc:
                # 记账已在 call() 出口完成（成功一行 ok=true；任何越过 preflight 的
                # 失败一行 ok=false，含 GatewayPausedError；StageUnresolvedError 在
                # preflight 之前抛出，不占整数 seq）。setter 只补失败响应证据后原样抛。
                self._write_evidence(stem, "resp", {"ok": False,
                                                    "error": redact(str(exc))[:1000]})
                raise
            parse_error = None
            try:
                obj = parse_json_object(raw)
                proposals, errors = validate_proposals(
                    obj, hits=hits, catalog=self.catalog)
            except (json.JSONDecodeError, ValueError) as exc:
                parse_error = str(exc)
                errors = [f"JSON 解析失败：{parse_error[:200]}"]
            resp_ref = self._write_evidence(stem, "resp", {
                "ok": True, "text": redact(raw),
                "parse_error": parse_error, "validation_errors": errors,
            })
            round_meta.append({"attempt": attempt, "req": req_ref, "resp": resp_ref,
                               "parse_ok": parse_error is None,
                               "validation_errors": errors})
            if not errors:
                break
            if attempt < MAX_REPAIR_ASK:
                messages = build_repair_messages(messages, raw, errors)
        if errors:
            raise DirectionSchemaError(
                f"macro 输出经 {MAX_REPAIR_ASK + 1} 次尝试仍未通过校验：{errors}")

        result = {
            "track": track, "thread_id": thread_id, "batch_id": batch_id,
            "gate_round": gate_round,
            "gate_bypassed_locally": bypass_stamp,
            "question": obj.get("question") or (payload.get("question") or payload.get("name")),
            "proposals": proposals, "kb_sources": kb_sources,
            "attempts": round_meta,
        }
        out = _evidence_dir() / f"proposals_{thread_id}_r{gate_round}.json"
        # G-7/G-5：结果件 0600 原子写 + containment 断言。
        atomic_write_text(out, json.dumps(result, ensure_ascii=False, indent=2))
        assert_within(out, _evidence_dir())
        return result

    # Gate1 循环：打回意见注入下一轮；2 次打回 → human_failure（D10）
    def run_gate1_loop(self, feishu: Any, *, track: str, payload: dict,
                       thread_id: str, batch_id: str,
                       task_title: "str | None" = None) -> dict:
        title = task_title or (
            f"Track1 工具链巡礼：{payload['name']}" if track == "track1"
            else f"Track2 实证：{payload['question'][:30]}")
        notes: "str | None" = None
        prev: "list[dict] | None" = None
        last: "dict | None" = None
        for gate_round in range(MAX_GATE1_REJECTS + 1):
            last = self.run_direction(track=track, payload=payload,
                                      thread_id=thread_id, batch_id=batch_id,
                                      gate_round=gate_round, human_notes=notes,
                                      prev_proposals=prev)
            decision = feishu.request_gate1_approval(
                thread_id, title, last["proposals"])
            verdict = decision.get("decision")
            if verdict not in ("approve", "reject_with_comments"):
                # 评审 M2：决策值白名单，未知值不得被默默当成 reject 再烧一次付费调用
                raise DirectionError(f"Gate1 决策值非法：{verdict!r}（决策字典：{decision}）")
            if verdict == "approve":
                return {"status": "approved", "direction": last,
                        "decision": decision, "gate1_rejects": gate_round}
            notes = (decision.get("human_notes") or "").strip()
            if not notes:
                # 评审 M2：打回必须带意见（卡片路径上游已强制；此处守 p2p/未来来源）。
                # 空意见不允许静默退回首轮 prompt 白耗 macro，直接转人工失败。
                return {"status": "human_failure", "direction": last,
                        "decision": decision, "gate1_rejects": gate_round + 1,
                        "reason": "Gate1 打回但 human_notes 为空（协议要求打回必须带意见）"}
            prev = last["proposals"]
            # 首轮 + 打回后再问最多 MAX_GATE1_REJECTS(=2) 轮；
            # 第 3 张卡（gate_round==2）仍被打回 → human_failure（D10）
            if gate_round >= MAX_GATE1_REJECTS:
                return {"status": "human_failure", "direction": last,
                        "decision": decision,
                        "gate1_rejects": gate_round + 1,
                        "reason": "Gate1 第 3 次仍打回（D10 上限），结案转人工"}
        # 不可达（循环内必返回）
        raise DirectionError("run_gate1_loop 内部错误")
