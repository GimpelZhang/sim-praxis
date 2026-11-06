"""Phase 2.8.1 T4：两臂冻结表 ``ARMS`` 与**唯一分派点**（计划 §8.5.1 / §8.5.7）。

本件是实验自变量面的单点真值。除了它，没有任何地方可以写「哪一臂用哪个
引擎、给多少帽、授予什么工具」——``trial_harness`` 只消费 :data:`ARMS`，
测试按 :func:`arm_config_digest` 反查，预注册（P281-5 的 ``arms.yaml``）
与本表互为镜像并由机判等值。

两臂的等价性（AC-P281-4-01，双向）：

* **必须逐值相同**：``max_coding_rounds``、``call_cap``。它们定义「一次 Trial
  允许花多少」——不同就等于两个实验组拿到的预算不同，比较失去意义
  （STOP-15 禁止事后调参凑结果）。
* **必须逐值不同**：``engine``、``runspec_agentic``、``tool_grants``、
  ``step_cap``、``token_cap``、``agentic_config``。它们**就是**自变量本身
  （旧引擎 vs agentic 内环）；若某两项「看起来不同其实相同」，说明有一臂
  根本没切换过去。

授予串为什么是这七项（现场核对 ``orchestrator/agentic/wiring.py:118-179``
的解析规则）：

* ``write:`` 而不是裸 ``write_file`` / ``apply_patch``。裸名虽在 ``TOOL_NAMES``
  内，但**不会**往 ``GrantTable.write_roots`` 里放任何前缀；``policy._check_write``
  在空根上一律拒绝。若按裸名写，``agentic_v1`` 臂会在第一次落盘就被自己的
  策略拒——整个臂的溃败会被误读成「能力不足」，那是纯粹的基础设施假阴性。
  ``write:`` 的空前缀 = 整个 workdir，与 ``legacy_v1`` 臂「workdir 内自由写」
  的能力面对齐（AC-P281-4-17 双向机判：workdir 内 allow、``/tmp`` deny）。
* ``recall`` **不给**（§8.3 有效性裁定：``solutions/track1_tools/03_libero``
  就是 L1 的同题答案，两臂同等地不给召回；给了就是答案泄漏）。
* ``run_expr`` / ``update_scratchpad`` / ``run_probe`` / ``probe:*`` 不给：
  本实验 ``AgenticConfig`` 全关，授予了也调不动；为避免「授予与实际能力
  不一致」的表里不一，直接不授予。
* ``netprobe:*`` / ``run:net`` 与任务约束 ``network: disabled`` 冲突，排除。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from . import constants

__all__ = (
    "AGENTIC_GRANTS",
    "ARM_IDS",
    "ARMS",
    "ArmSpec",
    "ArmSpecError",
    "NullRecallEngine",
    "arm_config_digest",
    "arm_params",
    "run_arm",
)

#: 计划 §4.3 的导出名（同一对象，不是第二份）
ARM_IDS = constants.ARMS

#: ``agentic_v1`` 的冻结授予串（顺序即渲染顺序，不可重排——``config_digest`` 逐字吃它）
AGENTIC_GRANTS: "tuple[str, ...]" = (
    "read_file", "list_dir", "grep", "write:", "run:python", "finish", "fail",
)

_SUPPORTED_ENGINES = ("agent_runner", "agentic_loop")


class ArmSpecError(ValueError):
    """臂表非法（未知引擎/字段类型不对/臂与常量表不一致）。"""


@dataclass(frozen=True)
class ArmSpec:
    """一个实验臂的冻结参数（字段顺序即计划 §8.5.1 的字段顺序）。"""

    arm_id: str
    engine: str
    runspec_agentic: bool
    max_coding_rounds: int
    tool_grants: "tuple[str, ...]"
    step_cap: int
    token_cap: int
    call_cap: int
    #: ``None`` = ``AgenticConfig`` 全关（``cache_epoch_for(None)`` 与
    #: ``cache_epoch_for(AgenticConfig())`` 同纪元，AC-P281-4-15）。
    #: 刻意把类型写成 ``Any``：RunSpec 侧也不 import agentic 包，
    #: 依赖方向保持单向（引擎逐字段鸭子读）。
    agentic_config: Any = None

    def __post_init__(self) -> None:
        if self.arm_id not in constants.ARMS:
            raise ArmSpecError(
                f"未知臂 id：{self.arm_id!r}（合法值 {constants.ARMS}）")
        if self.engine not in _SUPPORTED_ENGINES:
            raise ArmSpecError(
                f"未知引擎：{self.engine!r}（合法值 {_SUPPORTED_ENGINES}）")
        if type(self.runspec_agentic) is not bool:
            raise ArmSpecError("runspec_agentic 必须是 bool")
        if self.runspec_agentic != (self.engine == "agentic_loop"):
            # engine 与 runspec_agentic 必须同向：否则「说是 agentic 臂却跑了
            # 旧引擎」，或反之——这是最有欺骗性的一类臂表错误。
            raise ArmSpecError(
                f"{self.arm_id}: engine={self.engine!r} 与 "
                f"runspec_agentic={self.runspec_agentic!r} 不同向")
        for name in ("max_coding_rounds", "step_cap", "token_cap", "call_cap"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ArmSpecError(f"{self.arm_id}.{name} 必须是非负整数：{value!r}")
        if self.max_coding_rounds < 1:
            raise ArmSpecError(f"{self.arm_id}.max_coding_rounds 必须 >= 1")
        if self.call_cap < 1:
            raise ArmSpecError(f"{self.arm_id}.call_cap 必须 >= 1")
        if type(self.tool_grants) is not tuple or not all(
                type(g) is str and g for g in self.tool_grants):
            raise ArmSpecError(
                f"{self.arm_id}.tool_grants 必须是字符串元组")
        # 非 agentic 臂带授予串/步帽/token 帽 = 表里不一：旧引擎根本不看它们，
        # 留着只会让「两臂参数」看起来比实际更不等价。
        if not self.runspec_agentic:
            if self.tool_grants or self.step_cap or self.token_cap:
                raise ArmSpecError(
                    f"{self.arm_id}: 非 agentic 臂不得带 tool_grants/step_cap/"
                    "token_cap（旧引擎不消费，留着即表里不一）")
            if self.agentic_config is not None:
                raise ArmSpecError(
                    f"{self.arm_id}: 非 agentic 臂不得带 agentic_config")

    # ---- 序列化 ----

    def to_json(self) -> dict:
        """规范化可读件（``agentic_config`` 折成类名/None，避免依赖 agentic 包）。"""
        return {
            "arm_id": self.arm_id,
            "engine": self.engine,
            "runspec_agentic": self.runspec_agentic,
            "max_coding_rounds": self.max_coding_rounds,
            "tool_grants": list(self.tool_grants),
            "step_cap": self.step_cap,
            "token_cap": self.token_cap,
            "call_cap": self.call_cap,
            "agentic_config": (None if self.agentic_config is None
                               else type(self.agentic_config).__name__),
        }


#: 冻结表（``MappingProxyType``：运行期不可改，改只能改源码 + 重跑 AC）
ARMS: "MappingProxyType[str, ArmSpec]" = MappingProxyType({
    "legacy_v1": ArmSpec(
        arm_id="legacy_v1",
        engine="agent_runner",
        runspec_agentic=False,
        max_coding_rounds=constants.MAX_CODING_ROUNDS,
        tool_grants=(),
        step_cap=0,                      # 旧引擎无工具步概念
        token_cap=0,                     # 旧引擎无 token 帽概念
        call_cap=constants.CALL_CAP_PER_TRIAL,
        agentic_config=None,
    ),
    "agentic_v1": ArmSpec(
        arm_id="agentic_v1",
        engine="agentic_loop",
        runspec_agentic=True,
        max_coding_rounds=constants.MAX_CODING_ROUNDS,
        tool_grants=AGENTIC_GRANTS,
        step_cap=constants.STEP_CAP_PER_TRIAL,
        token_cap=constants.TOKEN_CAP_PER_TRIAL,
        call_cap=constants.CALL_CAP_PER_TRIAL,
        agentic_config=None,             # None = 全关 = cache_epoch v2
    ),
})


def arm_params(arm: "ArmSpec | str") -> dict:
    """取一个臂的**可规范化**参数面（``config_digest`` 的唯一输入）。"""
    spec = ARMS[arm] if isinstance(arm, str) else arm
    if not isinstance(spec, ArmSpec):
        raise ArmSpecError(f"需要 ArmSpec 或臂 id，收到 {type(spec).__name__}")
    return spec.to_json()


def canonical_json(doc: Any) -> str:
    """与 ``task_spec.canonical_json`` 同口径（键序无关、无多余空白）。"""
    return json.dumps(doc, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def arm_config_digest(arm: "ArmSpec | str") -> str:
    """臂参数的规范化 JSON sha256 —— manifest 的 ``config_digest`` 单点来源。

    AC-P281-4-16：清单里引擎自述的 ``config_digest`` 必须与本函数现算值逐字
    相等；不等即说明「跑的配置」和「记下的配置」是两个东西。
    """
    return hashlib.sha256(
        canonical_json(arm_params(arm)).encode("utf-8")).hexdigest()


class NullRecallEngine:
    """零召回的 ``VectorEngine`` 鸭子替身（计划 §8.3 有效性裁定）。

    为什么要有它：``AgentRunner`` 的 ``vector_engine`` 槽位在构造期一填就是
    真引擎，运行期每条失败链都会 ``query_kb(...)``。实验设计上两臂都不许看
    KB（``solutions/`` 里有 L1 的同题答案），所以给旧引擎喂这个替身：接口
    同形、恒返 ``[]``、**永不抛**、不发网络、不碰 LanceDB。

    ``calls`` 便于测试断言「引擎确实问过、但问的是一个恒空的替身」，以及
    断言「真实 ``VectorEngine.query_kb`` 零调用」（AC-P281-4-02）。

    接口面是**两条路都要齐**的：``AgentRunner`` 在 ``recall is not None`` 时走
    ``retrieve(...)`` / ``record_adoption(...)``（``runner.py`` 的 recall 注入
    分支），在 ``recall is None`` 时退回 ``vector_engine.query_kb(...)``——只实现
    后两条（``query_kb``/``query_solutions``）会让旧引擎在**第一个编码轮**就
    ``AttributeError`` 崩掉，且崩因看起来像候选问题。故这里四个入口全部给足：
    两条旧入口（``query_kb``/``query_solutions``）+ 两条注入入口
    （``retrieve``/``record_adoption``），全部恒空/空操作。
    """

    __slots__ = ("adoptions", "calls", "queries")

    def __init__(self) -> None:
        self.calls = 0
        self.queries: "list[dict]" = []
        #: 采纳遥测的替身落点（恒空召回 ⇒ 恒空；留字段供测试断言「零采纳」）
        self.adoptions: "list[dict]" = []

    def query_kb(self, query: str, *, namespace: str = "coding",
                 top_k: int = 3) -> list:
        self.calls += 1
        # 记录形态便于事后审计（不含任何 KB 内容——本来就没有）
        self.queries.append({"namespace": namespace, "top_k": int(top_k),
                             "query_len": len(query or "")})
        return []

    def query_solutions(self, *_args: Any, **_kwargs: Any) -> list:
        """第三表（``solution_chunks``）同等的零召回面。"""
        self.calls += 1
        return []

    def retrieve(self, coding_hint: str, *, site: str = "coding") -> dict:
        """:class:`ProductionRecall` 的端口形状（``runner.py`` 的注入分支）。

        恒返两路空命中；``calls`` 与 ``queries`` 照记，便于断言「问过、问的是
        替身」而不是「根本没问」。
        """
        self.calls += 1
        self.queries.append({"site": site, "namespace": "retrieve",
                             "top_k": 0, "query_len": len(coding_hint or "")})
        return {"kb_hits": [], "learning_hits": []}

    def record_adoption(self, *, thread_id: str, site: str,
                        learning_id: str, prompt_text: str,
                        learning_text: str) -> None:
        """采纳遥测的空操作（零召回 ⇒ 理论上永不触发；触发即说明有人给了命中）。"""
        self.adoptions.append({"thread_id": thread_id, "site": site,
                               "learning_id": learning_id})


def run_arm(arm: "ArmSpec | str", ctx: Any) -> dict:
    """**唯一分叉点**（计划 §8.5.7）：按 ``engine`` 把同一个 Trial 上下文派给两引擎。

    ``ctx`` 是 ``trial_harness.TrialContext``（鸭子类型）：

    - ``runspec``：两臂共享的同一份 ``RunSpec`` 形态（harness 已按臂参数 clone）
    - ``gateway``：``CapGuardGateway``（预算单点执行者，**必须在网关外**）
    - ``call_cache``：本 Trial 独占的 ``CallReplay``（STOP-10）
    - ``ledger``：本阶段账本（``QuotaLedger.phase281()``）
    - ``sandbox``：``sandbox.SandboxPort``（提供 ``subprocess_runner`` /
      ``run_backend``）
    - ``evidence_dir``：旧引擎的编码轮次证据落点
    - ``recall``：``NullRecallEngine``（两臂同等地零召回）

    两个引擎都**在模块内懒导入**：``arm_adapter`` 本身不拽
    ``orchestrator.agentic.*`` 与 ``orchestrator.agent_runner.runner``，
    这样纯参数面测试（AC-01/15/16/17）不必付出整包导入代价，也保住旧路径
    「零 agentic 依赖」的 golden 断言。
    """
    spec = ARMS[arm] if isinstance(arm, str) else arm
    if not isinstance(spec, ArmSpec):
        raise ArmSpecError(f"需要 ArmSpec 或臂 id，收到 {type(spec).__name__}")
    if spec.engine == "agent_runner":                       # legacy_v1
        from orchestrator.agent_runner.runner import AgentRunner

        runner = AgentRunner(
            ctx.gateway, ctx.recall,
            evidence_root=ctx.evidence_dir,
            subprocess_runner=ctx.sandbox.subprocess_runner(),
            call_cache=ctx.call_cache)
        return runner.run(ctx.runspec)
    if spec.engine == "agentic_loop":                       # agentic_v1
        from orchestrator.agentic.wiring import run_agentic_for_runspec

        return run_agentic_for_runspec(
            ctx.runspec, gateway=ctx.gateway,
            run_backend=ctx.sandbox.run_backend(),
            call_cache=ctx.call_cache,
            ledger=ctx.ledger)
    raise ArmSpecError(f"未知引擎：{spec.engine!r}")  # pragma: no cover（构造期已挡）
