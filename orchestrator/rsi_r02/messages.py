"""RSI-R0.2 上下文构造与本地输入门（计划 §4.1，AC12）。

三条纪律：

1. **本地计数是上下文控制，不是供应端 token**。计数对象是**完整序列化请求**
   （system + 全部 messages + max_tokens 包装），结果只用于本地 20000/128KiB 闸。
2. **确定性**：同一历史必得同一请求字节；更早轮次压缩为固定字段事件摘要，不由
   第二模型代写；数据文件不展开（只给路径/hash/状态）。
3. **fail-closed**：分词器缺件或哈希不符即拒绝计数与放行，不用字节估算冒充 token。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from orchestrator.rsi_r01 import actions as AC
from orchestrator.rsi_r01 import protocol as PR
from orchestrator.rsi_r01 import schemas as S

LOCAL_TOKEN_CAP = 20000
SERIALIZED_BYTE_CAP = 128 * 1024
MAX_OUTPUT_TOKENS = 8192
TOOL_REPLY_MAX_BYTES = 4096
ERROR_REPLY_MAX_BYTES = 2048
FULL_HISTORY_ROUNDS = 2

VERDICT_OK = "OK"
VERDICT_TOKEN_OVER_CAP = "TOKEN_OVER_CAP"
VERDICT_BYTE_OVER_CAP = "BYTE_OVER_CAP"
VERDICT_COUNTER_UNAVAILABLE = "COUNTER_UNAVAILABLE"


class CounterUnavailable(RuntimeError):
    """分词器不可用：禁止用未钉哈希的计数器放行。"""


# ---------------------------------------------------------------------------
# 本地计数器
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalCounter:
    """钉 sha256 的分词器（复用 R0.1 ``protocol.Tokenizer``，不复制实现）。"""

    tokenizer: PR.Tokenizer

    @classmethod
    def load(cls, path: str | Any, sha256: str) -> "LocalCounter":
        try:
            return cls(tokenizer=PR.Tokenizer.load(path, sha256))
        except PR.TokenizerUnavailable as exc:
            raise CounterUnavailable(str(exc)) from exc

    @property
    def sha256(self) -> str:
        return self.tokenizer.sha256

    def count(self, text: str) -> int:
        return int(self.tokenizer.count(text))

    def describe(self) -> dict[str, str]:
        return {"kind": "hf_tokenizers", "path": str(self.tokenizer.path),
                "sha256": self.tokenizer.sha256}


def require_counter(counter: LocalCounter | Callable[[str], int] | None
                    ) -> Callable[[str], int]:
    """接受 ``LocalCounter`` 或可调用；None 一律拒绝（fail-closed）。"""
    if counter is None:
        raise CounterUnavailable("没有钉哈希的本地分词器，禁止构造/计数请求")
    if isinstance(counter, LocalCounter):
        return counter.count
    if callable(counter):
        return counter
    raise TypeError(f"不支持的计数器：{type(counter)!r}")


# ---------------------------------------------------------------------------
# system 文本（任务/API/边界/动作协议；schema 作为文本，不用原生 tools）
# ---------------------------------------------------------------------------


def action_schema_text(surface: AC.ActionSurface, *,
                       protocol: bool = False, has_prereg: bool = False,
                       has_submit: bool = False) -> str:
    """动作协议段。``protocol``/``has_prereg``/``has_submit`` 必须来自**文档实况**。

    N-12 子项四（2026-11-10 root D reviewer HIGH/MEDIUM）：以前无论文档里有没有
    ``public.prereg`` / ``public.submit`` 块，动作清单都写着「必填字段逐字见任务
    说明 public.prereg.required_fields」——而 dev/validation 文档（``public`` 只有
    goal/interface/scene_params/task）根本没有这两块，候选被指向不存在的章节。
    指向必须按章节是否存在条件渲染。
    """
    bounds = {k: [v[0], v[1]] for k, v in sorted(surface.param_bounds.items())}
    fixed = {k: v for k, v in sorted(surface.fixed_params.items())}
    # N-12 子项一（2026-11-10 R0.2 结案尾债）：以前这里只印 ``固定参数：{"timestep": …}``，
    # 却从未说「不要写进 params」。候选照抄了它就触发 ACTION_UNAUTHORIZED_KEY，而这是
    # 终态策略违规——公开文本把宿主注入项印进行动协议段且不给禁令，是仪器侧的
    # but-for 贡献。白名单直接取动作验证器的唯一真值 ``param_bounds``（见
    # ``rsi_r01.actions._parse_run``：``illegal = set(raw) - set(bounds)``），
    # 不另抄一份字面量，避免门禁与渲染漂移。
    allowed = sorted(surface.param_bounds)
    return "\n".join([
        "### 动作协议（每次回复只允许一条 JSON 对象，不要解释文字）",
        f'每条动作必须带 "schema": "{S.SCHEMA_ACTION}"。',
        "可用动作与必填字段：",
        '1) {"schema":..., "action":"preregister", "plan":{...}} —— 必须在第一个实验之前，'
        "成功后不得重复"
        + ("；plan 的必填字段逐字见任务说明 public.prereg.required_fields。"
           if has_prereg else "。"),
        '2) {"schema":..., "action":"run_experiment", "params":{参数名:数值}}',
        '3) {"schema":..., "action":"analyze", "source":"<python源码>", "run_ids":["<run_id>",...]}',
        '4) {"schema":..., "action":"submit", "claim":"SUPPORTED|REFUTED|INCONCLUSIVE", '
        '"run_ids":[...], "analysis_ids":[...]'
        # nonce/stat/stat_key 只有协议链会核验（``protocol_fixture.verify_run``）；
        # dev/validation 文档没有 nonce，把它们印出来等于给候选一个不存在的必填项
        # （root E reviewer E-LOW-1）。
        + (', "nonce":"<逐字>", "stat":数值, "stat_key":"<逐字>"}' if protocol else '}')
        + (" —— 必填字段逐字见任务说明 public.submit.required_fields。"
           if has_submit else ""),
        "",
        f"参数合法区间：{json.dumps(bounds, ensure_ascii=False, sort_keys=True)}",
        ("固定参数（**由宿主自动注入，禁止写进 params**；写进去即终态策略违规）："
         f"{json.dumps(fixed, ensure_ascii=False, sort_keys=True)}"),
        (f"params 允许的键**仅有**这些（多写一个键即终态策略违规）："
         f"{json.dumps(allowed, ensure_ascii=False)}"),
        f"单次实验步数上限：{surface.max_steps}",
        "",
        "### 边界",
        "* 你没有网络、仓库、知识库、私有真值或其他试次的访问权；只能使用本对话给出的公开信息。",
        "* 每次回复恰好一个动作。**空回复会直接终止本 trial**（按已响应的失败处理，"
        "不隐式重试）。格式类失败（JSON 写坏、缺字段、类型不符）"
        "以及会被宿主拒绝的动作（重复登记、重复跑同一格、登记内容不合规、引用不存在的 "
        "run_id / analysis_id）都以 `ok=false` 返回、**不终止**本 trial，你可以在剩余"
        "预算内改正（submit 的结构性拒绝也在此列，见下一条）；唯一不可挽回的是 "
        "submit 被接收之后——它立刻结束本 trial。",
        # 2026-11-10 root E reviewer HIGH：上面那句原先无例外，而 ``submit`` 完全不同——
        # ``rsi_r01.actions._parse_submit`` 不查 nonce/stat/stat_key（只查 claim、
        # run_ids、analysis_ids 的形状，analysis_ids 缺省为 []），``trial._submit``
        # 只查引用的编号是否存在就 ``ok=True``，``candidate.py:440-450`` 随即把
        # status 置 TRIAL_SUBMITTED 并返回——**trial 到此结束，候选再也拿不到改正机会**。
        # 实测（零付费重放）：submit 去掉 nonce/stat 仍 4 POST 全收、status=SUBMITTED、
        # 判决面 BLOCKED_PROTOCOL；跳过 analyze 提前 submit 也照样被接受并终态。
        # 故必须把 submit 单独说成终局动作，并要求它自查。
        "* **例外：submit 是终局动作**——只要结构合法（`claim` 属于允许值、`run_ids` "
        "是非空且全为字符串的数组、`analysis_ids` 是字符串数组、引用的编号真实存在，"
        "可选的 `mean_effect`/`z` 必须是数值），宿主就接受它并**立刻结束本 trial**，"
        "此后没有任何改正机会；提前提交（例如跳过 analyze）也一样会被接受。"
        + ("本链对 submit 的核验发生在**结链时**：`nonce` 必须逐字等于公开合同给出的值、"
           "`stat` 必须等于你 analyze 打印的数值（宿主会用真实数据独立重算并逐位比对）、"
           "`stat_key` 必须逐字相符、动作序列必须"
           "恰好是 preregister→run_experiment→analyze→submit 各一次——**任何一项不符，"
           "整条链直接判失败**。请在 submit 之前逐项自查。"
           if protocol else
           "提交后宿主会按你的真实数据复核结论；结论与数据不符，本 trial 的结论判为不成立。"),
        "* **策略违规是终态，没有修正机会**：params 里出现上面白名单之外的键、数值越界，"
        "本次实验链立即终止，后续动作一律不再接受。",
        # 2026-11-10 root D reviewer HIGH：本句原为「…自行修正——但仅限这一类」，
        # 与紧邻的协议链 bullet（跳步/重排/重复登记也可改正）自相矛盾，也与宿主
        # 语义相反——``rsi_r01/trial.py:152-158`` 把 PREREG_INVALID/CELL_DUPLICATED/
        # RUN_ID_UNKNOWN 等一律转成 ``ok=False`` 的非致命事件，只有
        # ``candidate.POLICY_VIOLATION_CODES``（UNAUTHORIZED_KEY/OUT_OF_BOUNDS）
        # 才终止。故改为枚举真实可改正类，并写明空回复才是致命的（candidate.py
        # ``EMPTY_RESPONSE``：trial 置 BLOCKED，不重试）。
        # 「序列各一次」是**协议链专属**判据（protocol_fixture.verify_run 才判）。
        # dev/validation 的宿主允许同一 trial 多次实验（计划 §7：≤12 次实验 /
        # ≤8 次分析），把它们无条件印进 system 会给出不实承诺——票八 root C 的
        # reviewer 实测 E-dev（无 protocol 块）也出现该句，而 T3 的 D-dev 成功跑了
        # 6 次 run_experiment 且 judge_pass=true。故只在带 protocol 块的文档里渲染。
        *([("* **动作跳步或重排、重复登记不是终态**（submit 除外，它见上一条）：那一次"
             "动作会被宿主拒绝并返回 `ok=false`，但**已经计掉一次 POST**；你可以在剩余"
             "预算内改正。但本链成功的动作序列必须**恰好**是 preregister→run_experiment"
             "→analyze→submit 各一次、顺序不变——多一次或少一次都判整条链失败。")]
          if protocol else []),
        # 2026-11-10 root F reviewer F2（MED）：本句原写「submit 缺字段**不会**在接收时
        # 被拒…没有改正机会」，对 claim/run_ids 是假的——``_parse_submit`` 先查
        # claim ∈ 允许值（BAD_VALUE）、再查 run_ids 非空字符串数组（MISSING_FIELD），
        # 二者都走 ``TrialRejected`` ⇒ ``ok=false`` **可改正**；只有 analysis_ids /
        # nonce / stat / stat_key 不在接收面校验、随被接收的 submit 一并终局。
        *([("* 任务说明里的 required_fields 是字段要求的**唯一权威**，请照抄字段名，"
             "不要自创字段。preregister / run_experiment / analyze 缺字段或类型不符会被拒"
             "并返回 `ok=false`（可改正）；submit 的 `claim` / `run_ids` 缺失或类型不符"
             "同样会被拒（可改正），但 `analysis_ids` / `nonce` / `stat` / `stat_key` "
             "缺失**不会**在接收时被拒（`analysis_ids` 类型不符仍会被拒）——submit 一旦"
             "被接收就立刻终局，这些字段只在结链"
             "核验时判失败，没有改正机会。")]
          if (has_prereg or has_submit) else []),
        "* analyze 的源码在受限容器内运行（无网络、只读根、CPU 限制），只能写 analysis.json；"
        "数值必须来自你本次实验的真实观测。",
        "* submit 的数字必须由你之前的 analyze 结果给出，并逐字引用 run_id / analysis_id。",
    ])


def build_system(public_doc: Mapping[str, Any], surface: AC.ActionSurface) -> str:
    pub = public_doc.get("public") or {}
    head = [
        "你是自主研究/工程候选。你只能通过给定的文本 JSON 动作与任务交互。",
        "",
        "### 任务",
        f"实例：{public_doc.get('instance_id')}（族：{public_doc.get('family')}）",
    ]
    # 目标/研究设定必须**整块**落到文本里，不许手挑几个字段（root E reviewer
    # E-MED-1）：R 族公开文档带 damping_levels/initial_angles，而
    # ``evaluator.check_prereg`` 恰好按这两条轴做四格集合相等——旧渲染只印
    # h/band/window，把判据要用的公开数据丢在纸面外，还印出 ``目标：None``。
    goal = pub.get("goal")
    research = pub.get("research")
    if goal is not None:
        head.append("目标：" + (json.dumps(goal, ensure_ascii=False, sort_keys=True)
                               if isinstance(goal, Mapping) else str(goal)))
    elif isinstance(research, Mapping) and research.get("question"):
        head.append(f"目标：{research['question']}")
    if research:
        head.append("公开研究设定：" + json.dumps(research, ensure_ascii=False,
                                                 sort_keys=True))
    iface = pub.get("interface") or {}
    head.append(f"公开接口：{json.dumps(iface, ensure_ascii=False, sort_keys=True)}")
    # 公开文档里的字段表必须**真的渲染进 system**：候选唯一的输入通道就是这份
    # 文本（``candidate.CandidateLoop.system``），只把 required_fields 放进文档
    # dict 而渲染时丢掉，等于 action_schema_text 指向一个候选看不见的章节——
    # 这正是 T4 首跑判 BLOCKED_PROTOCOL 的那类自相矛盾（评审 CRITICAL-1）。
    for key, title in (("prereg", "preregister 的 plan 必填字段"),
                       ("submit", "submit 必填字段")):
        spec = pub.get(key)
        if not isinstance(spec, Mapping) or not spec:
            continue
        head.append("")
        head.append(f"### {title}（逐字照抄字段名，不得增删或改名）")
        note = str(spec.get("note") or "").strip()
        if note:
            head.append(note)
        fields = spec.get("required_fields") or {}
        if isinstance(fields, Mapping):
            for name, desc in fields.items():
                head.append(f"* {name}：{desc}")
        if spec.get("delta_fixed") is not None:
            head.append(f"* delta 必须逐字等于 {spec['delta_fixed']}")
        if spec.get("cells_rule"):
            head.append(f"* cells 规则：{spec['cells_rule']}")
        values = spec.get("values")
        if values:
            head.append("* 本实例的参数取值：" + json.dumps(
                values, ensure_ascii=False, sort_keys=True))
    # analyze 契约同样必须落到文本里（2026-11-10 T4 第二次判负的根因）：
    # 检查器读源码里的 nonce、PROTO_STAT 载荷的 nonce/stat/run_ids、容器数据布局，
    # 这些以前只在宿主私有知识里。候选唯一的输入通道就是这段文本。
    analyze = pub.get("analyze")
    if isinstance(analyze, Mapping) and analyze:
        head.append("")
        head.append("### analyze 契约（源码在容器内执行，逐条遵守）")
        note = str(analyze.get("note") or "").strip()
        if note:
            head.append(note)
        literal = analyze.get("source_must_contain") or []
        if literal:
            head.append("* 源码必须逐字包含：" + "、".join(str(x) for x in literal))
        line = str(analyze.get("prints_line") or "").strip()
        if line:
            head.append(f"* 必须向 stdout 打印一行：{line}")
        payload = analyze.get("payload_fields") or {}
        if isinstance(payload, Mapping):
            for name, desc in payload.items():
                head.append(f"* {line or 'PROTO_STAT <json>'} 里的 {name}：{desc}")
        data = analyze.get("data")
        if isinstance(data, Mapping) and data:
            head.append(f"* 容器内数据文件：{data.get('path')}"
                        f"（{data.get('shape') or '列式 JSON'}）")
            if data.get("note"):
                head.append(f"* {data['note']}")
            keys = data.get("keys") or []
            if keys:
                head.append("* 数据文件键：" + "、".join(str(k) for k in keys))
            if data.get("stat_rule"):
                head.append(f"* 统计算法：{data['stat_rule']}")
        if analyze.get("output"):
            head.append(f"* 分析产物写到：{analyze['output']}")
    proto = (pub.get("interface") or {}).get("protocol") or {}

    def _section(name: str) -> bool:
        spec = pub.get(name)
        return isinstance(spec, Mapping) and bool(spec)

    return ("\n".join(head) + "\n\n"
            + action_schema_text(surface, protocol=bool(proto),
                                 has_prereg=_section("prereg"),
                                 has_submit=_section("submit")))


# ---------------------------------------------------------------------------
# 历史压缩与请求构造
# ---------------------------------------------------------------------------


def _spellings(value: Any) -> "set[str]":
    """一个值在文本里**可能**的合法写法（数值/字符串都算）。

    模板用 f-string 印 float、用 json.dumps 印 dict，两种写法对同一数值可能不同
    （``0.002`` vs ``0.0020``）。比对用「任一写法逐字出现」，避免把渲染格式差异
    误报成自足性缺口。
    """
    out: "set[str]" = {str(value)}
    try:
        out.add(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        pass
    if isinstance(value, float):
        out.add(repr(value))
    return {s for s in out if s}


#: 纯数值写法（含负号与小数）。这类 token 必须按「整段数字」比对。
_NUMERIC_TOKEN = re.compile(r"^-?\d+(?:\.\d+)?$")


def _present(rendered: str, spelling: str) -> bool:
    """``spelling`` 是否作为**独立 token** 出现在渲染文本里。

    RSI-R0.2 票八评审（2026-11-10，reviewer 车道）实测的反例：删掉「本实例的参数
    取值」整行后，``steps=300`` 仍被判为「在场」——因为 ``"300" in "3000"``。
    子串比对对数值会假阴性（数值前缀是另一个数值的前缀），而这条闸的全部价值就在
    「缺值必须报缺」；因此数值按 token 比对：前后不许紧邻数字或小数点。
    """
    if _NUMERIC_TOKEN.match(spelling):
        return re.search(r"(?<![\d.])" + re.escape(spelling) + r"(?![\d.])",
                         rendered) is not None
    return spelling in rendered


def rendered_gaps(rendered: str, public_doc: Mapping[str, Any],
                  surface: "AC.ActionSurface | None" = None) -> "list[str]":
    """候选可见文本是否**逐字**带着判据要用的实例值（N-12 子项三，2026-11-10）。

    ``protocol_fixture.doc_gaps`` 只证明文档 dict 里有这些块；候选唯一的输入
    通道却是本模块 ``build_system`` 的**渲染文本**。两者之间掉一段（块在 dict
    里、渲染时被丢掉）会让「候选没做到」与「宿主没说」无法区分。本函数从文档
    取值，到渲染文本里逐字找；缺失即报 gap，空列表才算自足。

    与 ``doc_gaps`` 分工：那个查「文档块结构」，这个查「渲染后实例值是否在场」。
    """
    pub = (public_doc or {}).get("public") or {}
    iface = pub.get("interface") or {}
    proto = iface.get("protocol") or {}
    gaps: "list[str]" = []

    def need(value: Any, label: str) -> None:
        if not any(_present(rendered, s) for s in _spellings(value)):
            gaps.append(label)

    # 2026-11-10 root E reviewer E-MED-1：``goal``/``research`` 这两块**不属协议
    # 专属**，dev/validation 文档同样带着，而且 ``evaluator.check_prereg`` 会按
    # R 族 ``research.damping_levels × initial_angles`` 做四格集合相等——旧实现在
    # 非协议文档上直接 ``return gaps``（空），于是「文档里有、渲染丢掉了」这件事
    # 在 dev/validation 上完全没有闸。两者都按**整块 canonical JSON** 比对：渲染
    # 用的就是同一串字节，逐字在场才算过。
    if isinstance(pub.get("goal"), Mapping) and pub.get("goal"):
        need(json.dumps(pub["goal"], ensure_ascii=False, sort_keys=True),
             "public.goal")
    if isinstance(pub.get("research"), Mapping) and pub.get("research"):
        need(json.dumps(pub["research"], ensure_ascii=False, sort_keys=True),
             "public.research")
    if not proto:
        # 其余条目是协议专属（nonce/stat_key/analyze_line/action_order 等）；
        # 「协议文档丢了协议块」由 protocol_fixture.doc_gaps 在合同/预演期拦下。
        return gaps

    need(public_doc.get("instance_id"), "instance_id")
    # 固定参数行也属「候选可用判据」：宿主自动注入它的值，写进 params 却是终态
    # 违规。缺省用 ``surface_from_doc`` 重建（合同期/离线口径）；运行期由
    # ``CandidateLoop`` 传入**真正用来渲染 system 的那个 surface**，确保比对的是
    # 同一份动作面，而不是两份近似面。
    for fixed_name, fixed_value in (surface or surface_from_doc(public_doc)
                                    ).fixed_params.items():
        need(fixed_name, f"fixed_params.{fixed_name}")
        need(fixed_value, f"fixed_params.{fixed_name}.值")
    for key in iface.get("keys") or []:
        need(key, f"interface.keys.{key}")
    need(proto.get("nonce"), "interface.protocol.nonce")
    need(proto.get("stat_key"), "interface.protocol.stat_key")
    need(proto.get("analyze_line"), "interface.protocol.analyze_line")
    for name in proto.get("action_order") or []:
        need(name, f"interface.protocol.action_order.{name}")
    prereg = pub.get("prereg") or {}
    for name in prereg.get("required_fields") or {}:
        need(name, f"public.prereg.required_fields.{name}")
    if prereg.get("delta_fixed") is not None:
        need(prereg["delta_fixed"], "public.prereg.delta_fixed")
    values = prereg.get("values") or {}
    if not values:
        # 计划里的 cells 必须取实例参数；渲染文本没有取值，候选只能编（票一事故）。
        gaps.append("public.prereg.values 缺失")
    for name, value in values.items():
        need(name, f"public.prereg.values.{name}")
        need(value, f"public.prereg.values.{name}.值")
    for name in (pub.get("submit") or {}).get("required_fields") or {}:
        need(name, f"public.submit.required_fields.{name}")
    analyze = pub.get("analyze") or {}
    for lit in analyze.get("source_must_contain") or []:
        need(lit, f"public.analyze.source_must_contain.{lit}")
    need(analyze.get("prints_line"), "public.analyze.prints_line")
    for name in analyze.get("payload_fields") or {}:
        need(name, f"public.analyze.payload_fields.{name}")
    need(analyze.get("output"), "public.analyze.output")
    data = analyze.get("data") or {}
    need(data.get("path"), "public.analyze.data.path")
    for key in data.get("keys") or []:
        need(key, f"public.analyze.data.keys.{key}")
    need(data.get("stat_rule"), "public.analyze.data.stat_rule")
    return gaps


def surface_from_doc(public_doc: Mapping[str, Any]) -> "AC.ActionSurface":
    """仅凭公开文档重建动作面（供离线自足性检查用，不参与真实运行）。

    真实 trial 的 surface 由 ``rsi_r01.trial.action_surface(instance)`` 给出；
    这里只是让「文档 → 渲染文本」这条路可以在没有实例的情况下被机判走到。
    白名单口径与验证器一致：``interface.keys`` + ``steps``。
    """
    iface = ((public_doc or {}).get("public") or {}).get("interface") or {}
    limits = iface.get("limits") or {}
    bounds: "dict[str, tuple[float, float]]" = {}
    for key in iface.get("keys") or []:
        lo_hi = limits.get(key)
        bounds[str(key)] = (float(lo_hi[0]), float(lo_hi[1])) if lo_hi \
            else (0.0, 0.0)
    bounds["steps"] = (1.0, float(iface.get("steps_max") or S.N_STEPS))
    # 固定参数必须与**运行期** ``rsi_r01.trial.action_surface`` 同形，否则合同期
    # 冻结的自足性证据（rendered_gaps）与候选真正读到的文本不是同一份——
    # root D reviewer MEDIUM-3 实测：合同期印 ``{}``、运行期印
    # ``{"timestep": 0.002}``。运行期口径：pendulum 恒用 ``S.DT_S``，其余取实例
    # ``public.scene_params.timestep``；文档缺这个值就保持空（真实 trial 会同处
    # KeyError，不会静默漂移）。
    kind = str(iface.get("kind") or "push_control")
    fixed: "dict[str, float]" = {}
    if kind == "pendulum_run":
        fixed["timestep"] = float(S.DT_S)
    else:
        ts = (((public_doc or {}).get("public") or {}).get("scene_params")
              or {}).get("timestep")
        if ts is not None:
            fixed["timestep"] = float(ts)
    return AC.ActionSurface(kind=kind,
                            param_bounds=bounds,
                            fixed_params=fixed,
                            max_steps=int(iface.get("steps_max")
                                          or S.N_STEPS))


def summarize_event(evt: Mapping[str, Any]) -> str:
    """更早轮次的固定字段摘要（不由模型代写）。"""
    keep = ("action", "ok", "run_id", "analysis_id", "path", "sha256",
            "status", "code", "n_rows")
    parts = [f"{k}={evt[k]}" for k in keep if evt.get(k) is not None]
    return "- " + (" ".join(parts) if parts else json.dumps(evt, sort_keys=True)[:200])


def _clip(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    # 按字节截断且不截断半个字符；标记截断，不静默丢内容
    cut = raw[:limit].decode("utf-8", errors="ignore")
    return cut + "\n[TRUNCATED_BY_HOST]"


@dataclass(frozen=True)
class Turn:
    """一次真实往返：模型原始回复 + 宿主结构化结果。"""

    model_text: str
    result: Mapping[str, Any]

    def clipped(self) -> "Turn":
        return Turn(model_text=_clip(self.model_text, TOOL_REPLY_MAX_BYTES),
                    result=self._clip_result(self.result))

    @staticmethod
    def _clip_result(result: Mapping[str, Any]) -> Mapping[str, Any]:
        ok = bool(result.get("ok"))
        limit = TOOL_REPLY_MAX_BYTES if ok else ERROR_REPLY_MAX_BYTES
        raw = json.dumps(result, ensure_ascii=False, sort_keys=True)
        if len(raw.encode("utf-8")) <= limit:
            return dict(result)
        return {"ok": ok, "code": result.get("code") or "RESULT_TRUNCATED",
                "message": _clip(str(result.get("message") or ""), limit // 2),
                "truncated": True}


@dataclass(frozen=True)
class Request:
    system: str
    messages: tuple[dict[str, str], ...]
    max_tokens: int
    serialized: str = field(compare=False)
    sha256: str
    n_bytes: int
    local_tokens: int

    def as_messages(self) -> list[dict[str, str]]:
        return [dict(m) for m in self.messages]


def build_request(*, system: str, task_brief: str, turns: Sequence[Turn],
                  counter: Callable[[str], int],
                  max_tokens: int = MAX_OUTPUT_TOKENS) -> Request:
    """确定性构造完整请求：任务 + 最近两轮全文 + 更早固定字段摘要。"""
    messages: list[dict[str, str]] = [{"role": "user", "content": task_brief}]
    clipped = [t.clipped() for t in turns]
    split = max(0, len(clipped) - FULL_HISTORY_ROUNDS)
    older = [summarize_event({"action": _action_of(t), **_result_fields(t)})
             for t in clipped[:split]]
    if older:
        messages.append({"role": "user",
                         "content": "### 更早轮次（固定字段摘要）\n" + "\n".join(older)})
    for t in clipped[split:]:
        messages.append({"role": "assistant", "content": t.model_text})
        messages.append({"role": "user",
                         "content": json.dumps(t.result, ensure_ascii=False,
                                               sort_keys=True)})
    payload = {"system": system, "messages": messages, "max_tokens": int(max_tokens)}
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    n_bytes = len(serialized.encode("utf-8"))
    return Request(system=system, messages=tuple(messages), max_tokens=int(max_tokens),
                   serialized=serialized,
                   sha256=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                   n_bytes=n_bytes, local_tokens=int(counter(serialized)))


def _action_of(turn: Turn) -> str:
    parsed = json.loads(turn.model_text) if _is_json(turn.model_text) else {}
    return str(parsed.get("action") or turn.result.get("action") or "?") \
        if isinstance(parsed, dict) else "?"


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
    except (ValueError, TypeError):
        return False
    return True


def _result_fields(turn: Turn) -> dict[str, Any]:
    r = turn.result
    out: dict[str, Any] = {"ok": bool(r.get("ok"))}
    for key in ("run_id", "analysis_id", "path", "sha256", "status", "code"):
        if r.get(key) is not None:
            out[key] = r[key]
    return out


@dataclass(frozen=True)
class Accounting:
    verdict: str
    n_bytes: int
    local_tokens: int
    token_cap: int
    byte_cap: int

    @property
    def ok(self) -> bool:
        return self.verdict == VERDICT_OK

    def as_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "n_bytes": self.n_bytes,
                "local_tokens": self.local_tokens, "token_cap": self.token_cap,
                "byte_cap": self.byte_cap,
                "note": "本地计数=上下文控制，不是供应端 input_tokens"}


def check_request(req: Request, *, token_cap: int = LOCAL_TOKEN_CAP,
                  byte_cap: int = SERIALIZED_BYTE_CAP) -> Accounting:
    """先判字节后判 token；越界即阻断（不缩帽、不截断后重发）。"""
    if req.n_bytes > byte_cap:
        verdict = VERDICT_BYTE_OVER_CAP
    elif req.local_tokens > token_cap:
        verdict = VERDICT_TOKEN_OVER_CAP
    else:
        verdict = VERDICT_OK
    return Accounting(verdict=verdict, n_bytes=req.n_bytes,
                      local_tokens=req.local_tokens,
                      token_cap=token_cap, byte_cap=byte_cap)


def count_available(counter: LocalCounter | Callable[[str], int] | None) -> bool:
    try:
        require_counter(counter)
    except CounterUnavailable:
        return False
    return True
