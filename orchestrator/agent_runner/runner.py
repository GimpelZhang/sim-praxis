#!/usr/bin/env python3
"""Task4 agent_runner：真实脚本执行 + coding_debug 多轮补救（计划 L496-507）。

契约要点：
- 每 run 最多 5 轮 coding_debug（D10 v1.1）；第 5 次调用后仍败 → 写
  FAILURE.json 带完整轨迹，run 结案转人工；结构上不可能发出第 6 次调用。
- 检索 troubleshooting top_k=6，取行字段 **text**（非幻影字段 solution_steps）
  + evidence_refs，prompt 以 [来源: kb_relpath#section_path] 列注。
- Gate2 打回意见与 traceback 一起进 prompt，意见标「最高优先级」。
- 补丁安全：只允许写 workdir 下运行目录；禁改仓库 tracked 文件、
  stage_models.yaml、00_env.sh、~/access；越界补丁拒绝且不发起重试。
- 视觉禁用：runner 构造的消息只含纯文本（测试断言无 image 类型）。
- coding_round 计数仅在单次 run 内有效；Gate2 reject 计数在图侧独立维护。
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
from ast import (AsyncFunctionDef, BoolOp, Call, ClassDef, Compare, Constant,
                 DictComp, Eq, For, FunctionDef, GeneratorExp, If, IfExp,
                 Lambda, ListComp, Load, Name, SetComp, Try, While, With)
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Callable, Optional

from memory_flywheel.recall import ProductionRecall, prompt_embedded_text
from memory_flywheel.vector_engine import VectorEngine
from orchestrator.common.callcache import CallReplay, request_fingerprint
from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import atomic_write_text, chmod_keep_user_exec
from orchestrator.common.host_shim.shim_core import redact_for_model
from orchestrator.gateway.quota_ledger import (
    billing_context,
    phase1_root,
    set_current_evidence,
)
from orchestrator.gateway.router import (
    GatewayPausedError,
    ModelGateway,
    redact,
)

STAGE = "coding_debug"
TOP_K = 6
MAX_CODING_ROUNDS = 5          # D10 v1.1：每 run 最多 5 次 coding_debug
MAX_TOKENS = 8192
# 锁内适配（2026-10-27 Task8 fluid；攢批90 立、攢批92 修订）：
# 攢批90（seq312 实呼取证）：coding 模型经硬规则 1 用户逐字裁决换为
# kimi-k3.1-pro（b20c45e）后，方舟 anthropic-messages shim 对【缺省思考】
# 形态硬约束 temperature 只允许 1，故 0.1→1。
# 攢批92（T8-D2 账外取证，2026-10-27 晚）：后续 seq313 楔死/seq314 五次
# HTTP200 空回答证实缺省形态下该模型强制思考且预算不被强制——生产出生
# prompt 流式实证 792s/32758 事件/正文 0 字/stop=max_tokens。供应侧约束
# 矩阵补全（账外探针 C1/C2/C4/E）：thinking={"type","disabled"} 时
# temperature 只允许 0.6，此时小调用 1.9s 出正文；但 probe E 实证完整出生
# prompt 下该模型把全部规划独白改走 text 通道：605.1s/32759 事件/126682 字
# 正文/stop=max_tokens（32768），JSON 补丁未开始即截断——单次出生对该模型
# 结构性不可行，流体出生必须走 n=6 拆块通道（见 task8_fluid 工厂）。
# 故 coding 唯一采样温度 1→0.6，并显式关思考（与温度成对，
# 不可只改其一：C1 证实 disabled+1 被 400 拒）。不涉 model_id 变更
# （硬规则 1 不触发、用户裁决的 kimi-k3.1-pro 不动）。callcache 指纹并入
# temperature+thinking，改值自动 miss 旧缓存，安全。
TEMPERATURE = 0.6
CODING_THINKING = {"type": "disabled"}
# 锁内适配（计划 v1.2 外，seq42-47 拆块出生，用户 2026-10-20 裁决）：
# Task6 V2 出生拆 4 个顺序小编排调用；网关 call 的 timeout 旧路径隐式吃
# router 默认 120，现经 RunSpec 显式化（默认 120，行为不变）。
DEFAULT_CODING_TIMEOUT_S = 120
CHUNK_MIN_CODE_CHARS = 800          # 单块 code 下限（不变；n=6 边界更碎，800 只拦裸逃）
CHUNK_MIN_ASSEMBLED_CHARS = 30000   # n=6 拼接总长下限（Python 字符 len(str)，非字节）
# run5 语义变更（规格 .omc/handoffs/task6-sentinel-seam-design.md §15.1/§15.3；
# 锁内适配：模型面一行，用户裁决②（2026-10-22 run5 定向二拆））：
# 18000 不再是「超即硬拒」，而是单片提示目标窗/可行窗界——18001–36000 的
# 多行闭合件由模块级纯函数 _classify_closed_body 机器二拆（碎片不离开该
# 函数，codes 恒为 6 个逻辑块）；模型面措辞见出生 prompt 的 H1 bullet。
CHUNK_MAX_CODE_CHARS = 18000
# 锁内适配：模型面一行，用户裁决②（2026-10-22 run5 定向二拆）。
# 闭合件行政保守顶（算术定义域上界 36001，取 36000；run5，§15.2-M1/§15.5）：
# 两片各 ≤18000 的算术定义域上界为 18000+1+18000=36001（唯一点 {18000}），
# 行政顶刻意保守 1 字符取 36000=2×18000，L=36001 即使存在唯一可行切点也按
# 类3拒（over_split_max）。
CHUNK_CLOSED_SPLIT_MAX_CHARS = 36000
# 溯源：seq48 首发 24000；2026-10-21 用户选项 A（seq56 探针放行，commit
# f2f5b44，随输出顶 16384→32768）收紧到 20000；同日晚 run2 三轮拆块出生
# chunk3 全败于本守卫。跨 run1/run2 该块 5 次尝试：seq62=24661 拒、
# seq65=18009 过（唯一一次）、run2 三轮 21256/24514/22293 全拒；
# out token 从未触 32768，守卫是唯一约束；用户裁决回提到 26000
# （实观察 max 24661（seq62）全纳；32768 output token 容纳 JSON/转义余量仍足）重挂 run3。超长即整轮失败语义不变。
# 2026-10-21 run4 接缝加固（用户裁决「接缝加固后 run4」；设计终稿
# .omc/handoffs/task6-sentinel-seam-design.md，两轮 fresh reviewer APPROVE）：
# run1-3 九发出生调用两次完整流非法 JSON（seq83 char 18604 未转义引号、
# seq88 char 139 串内裸 LF，九发两中=22%；out token 17143/13450 远未触
# 32768，失败轴是手写 JSON 转义纪律而非预算），seq87 code 29769 chars 合法
# 闭合后被本地守卫拒（反证旧 prompt「超限必然在闭合前被截断」经验为假）；
# JSON mode 诊断探针 seq89（票 T6-B8，exit 11 content_failed_enforcement）
# 实测服务端默认 thinking 吃满 32768、json_schema tool input 截断成 137
# 字符，json_schema 通道废弃（探针脚本保留为一次性诊断证据，永不接线），
# 出生改 sentinel 围栏纯文本定界（见 _extract_chunk；协议见上设计 §2）。
# n 是工厂规格变更（task6.py ChunkSpec n=4→6，六块行预算合计 860）：n=6
# 各块观测推算 ≤15k，18000 留 ~20% 余量（n=5 必留 17-18k 整块，余量 <6%）；
# 拼接下限 30000 挡住 18000+5×800=22000 的逃单块组合。长度一律 Python
# 字符 len(str)（V1 基线实测 898 行/43324 字节/38669 字符/43.1 字符每行；
# 字节/字符口径不得混用，禁止写 48.2/43.3k 字符）。六标题/六边界/三窗
# run4 起飞前冻结，撞窗不调参、不挪边界，n=7/调窗属 run5 新提案（硬规则 7）。
# 2026-10-22 run5（用户裁决②「引擎加大块定向二拆协议」，规格 §15.1–§15.6）：
# run4 三失败点 19492/18029/20141 chars 均围栏闭合（内容已完整生成，仅超 18000
# 行政窗），故删除 _extract_chunk 的 >18000 硬拒分支，长度分类唯一收口到
# _classify_closed_body：18001–36000 闭合件求可行 LF 切点出 record（code 原样
# 接受），无可行 LF 或 L>36000 给真值外串 fail-closed；n=6/32768/三常量其余
# 口径/两 graph 文件全部不动，0 新增网关调用（分类为本地纯计算，缓存 raw
# 确定性重算，0 重付）。模型面仅一条 bullet 真值替换（§15.3-H1）。

# sentinel 围栏字面值（独占整行；协议规范见上设计 §2.1/§2.2 EBNF）。
# 七连尖括号（与 git conflict marker 同族）+ 项目专属核记 simpraxis.chunk.v1，
# Python 代码正文整行自发出现概率≈0；起止不对称（begin 带元数据、end 固定
# 串）。纯 ASCII、无引号——反斜杠/三引号/裸换行/中文/control 在围栏内都是
# 普通正文字符，消灭手写 JSON 转义失败轴。就地定义在 runner.py 同信任域，
# 不新建模块、不改 common/validators.py（其私有字符类不含 ./ 且语义不适用）。
CHUNK_FENCE_BEGIN = "<<<<<<< simpraxis.chunk.v1"
CHUNK_FENCE_END = ">>>>>>> simpraxis.chunk.v1.end"
CHUNK_FENCE_NUCLEUS = "simpraxis.chunk.v1"
# path 回显白名单（path 真闸仍是与 target 逐字比对；字符类只决定机器原因里
# 能否安全逐字回显模型给值，防注入下一轮块 0 prompt）；回显判定 fullmatch only（禁 match/search），否则 "evil x.py" 类合法前缀值无锚 fail-open 被回显。
CHUNK_FENCE_PATH_RE = re.compile(r"[A-Za-z0-9_./-]{1,128}")  # fullmatch only
TB_TAIL_CHARS = 4096           # traceback 尾部 ≤4KB
# run7（/tmp/a3gen 与 W1 原型 2026-10-23 实测，见
# .omc/handoffs/task6-run7-host-shim-design.md v1.1 §3.2）：纯 stderr
# atexit 锚独立尾窗（字符数；Gate2 offset 闸按 UTF-8 字节计）。显式撤销
# v1.0 D4「merged stderr+stdout 8192」——genesis 日志走 stdout（≥30721B），
# merged 窗必丢锚。旧 stderr_tail[-500:] merged 键一字节不动。
STDERR_ONLY_TAIL_CHARS = 8192
# run7 §3.6 卫生预检（MINOR，非安全门）：仅 marker=1 形态 exec 前非递归扫
# workdir 顶层三件宿主引导同名件，加 __pycache__ 内三 stem 的 pyc。
_HOST_SHIM_MARKER = "SIMPRAXIS_HOST_SHIM"
_WORKDIR_HOOK_BASENAMES = (
    "sitecustomize.py", "shim_core.py", "usercustomize.py")
_WORKDIR_HOOK_STEMS = ("sitecustomize", "shim_core", "usercustomize")
SNIPPET_CTX_LINES = 8
SNIPPET_TOTAL_CHARS = 4096
RUN_TIMEOUT_S = 1800
FAILURE_JSON = "FAILURE.json"

# 禁改的字面文件名（任何目录层级都拒）
FORBIDDEN_BASENAMES = frozenset({"stage_models.yaml", "00_env.sh"})

# run6（2026-10-22 用户裁决 A；设计 .omc/handoffs/task6-sentinel-seam-design.md
# §15.7-H2/H3、§15.8.2 H5）：出生拼接体必须含 V1 形可执行入口守卫，末位 AST
# 硬闸 _validate_entry_guard 在 parse/重名/30000 地板之后执行；拒绝理由是冻结
# 真值外串（逐字见 H3），只经 str(exc) 进下一轮块 0 反馈，不回显模型源码。
# 作用域八类整枝剪（含推导式/Lambda/def/class）；If/For/While/With 语句只降
# .body（Try 单独同样只降 .body）；Match 不指名，经 ast.iter_child_nodes 泛下降。
_SCOPE_NODES = (FunctionDef, AsyncFunctionDef, ClassDef, Lambda,
                ListComp, SetComp, GeneratorExp, DictComp)
_STMT_BODY_ONLY = (If, For, While, With)
_MAIN = "main"
_GUARD_LITERAL = "__main__"
_ENTRY_GUARD_REASON = (
    "拼接源码缺少可执行入口守卫：顶层必须存在 if __name__ == \"__main__\": "
    "语句，且其代码体（不含 else 分支）内调用 main()；守卫不得嵌套在函数或"
    "类定义内，比较式必须为 __name__ 在左、字符串字面量 \"__main__\" 在右的"
    " == 比较（反序或其他字面量均不接受）。当前拼接体不满足上述条件，python "
    "直接运行时 main() 不会执行（与 V1 入口结构不一致），请按该形态补齐守卫"
    "后整轮重发。")
# run6 §15.7-H4（D2）：rc=0 但约定产物缺失时注入修补轮的机器核验真值句。
# 全句无 ASCII 双引号（永不匹配 _FRAME_RE）；relpath 只许传 spec 的产物相对路径。
_RC0_MISSING_ARTIFACT_FMT = (
    "机器核验（非模型输出）：进程退出码 rc=0，但成功判据约定产物 {relpath} "
    "在工作目录中不存在。本轮机器成功判据为「退出码 rc=0 且工作目录下存在 "
    "{relpath}」，两条同时满足才算成功；stderr/traceback 为空，无运行时异常"
    "信号。以上仅为机器核验事实，不含原因诊断；请自行核对脚本执行与产物写出"
    "环节。")

# run7b（2026-10-23 用户裁决，L505② 协议口径修正，见 PatchAnchorError
# docstring）：锚点 !=1 整轮拒绝后给模型的机器核验反馈。
# v1.0 锁内适配（critic stage-1 MAJOR-1，2026-10-24 亲验复现）：FMT 必须
# 显式多物理行（行间显式 \n），{reason}+{hint} 独占一条物理行——
# redact_for_model 按物理行整行丢弃，draft 的相邻字面量隐式拼接成一行，
# rel 含六禁词时全部指令被连带静默丢弃。现形态：redact 至多丢原因行。
# 戒律（MINOR-9：适用范围仅常量与两 hint）：纯 str、无 ASCII 双引号
# （不匹配 runner.py:315 _FRAME_RE 帧形）、无六禁词、无 prelude 路径、
# 「机器核验（非模型输出）」口吻。渲染后 reason 为模型可控文本，不承诺
# 字符集，只承诺换行中和（见 _ANCHOR_LINE_SEPS）。
_ANCHOR_REJECT_FMT = (
    "机器核验（非模型输出）：上一轮返回的补丁已被整体拒绝，本轮没有\n"
    "应用任何补丁，全部目标文件字节保持不变，连新建目录也未创建。\n"
    "机器判定原因：{reason}。{hint}\n"
    "请在轮帽内重发修正后的补丁：old（或 unified diff 的 hunk 上下文）\n"
    "必须是当前文件中恰好出现一次的连续原文（含缩进与空白，逐字复制）；\n"
    "同一响应内多个补丁按给出顺序应用；多处同形位点必须扩大上下文使其\n"
    "唯一，或拆成多条补丁让每条 old 各自只命中一个位点；禁止复用同一\n"
    "条歧义 old，禁止凭行号或记忆猜测原文。")
_ANCHOR_HINT_MULTI = (
    "该锚点在当前文件中出现了多次，属于非唯一锚点。")
_ANCHOR_HINT_ZERO = (
    "该锚点在当前文件中一次都未出现，old 与当前文件实际内容不符。")

# v1.0（MAJOR-1）：str.splitlines 全分隔符集（LEAD 2026-10-24 在
# Python 3.10.12 亲验恰 10 个：\n \r \v \f \x1c \x1d \x1e \x85
# U+2028 U+2029）。用 chr 码表构造（源码不出现不可见字面量，防抄损）。
_ANCHOR_LINE_SEPS = (
    "\n", "\r", "\v", "\f",
    *(chr(c) for c in (0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029)))


class CodingLimitError(GatewayPausedError):
    """5 轮用尽/失败 run 被再次进入：等价硬停，不发网（计划 L505 ③）。

    父类 GatewayPausedError.__init__ 绑定 router 长熔断的 pause-state dict；
    这里只复用「可被同一硬停子句捕获、绝不发网」的语义，故越过它直接走
    RuntimeError 初始化，isinstance(x, GatewayPausedError) 仍然成立。
    """

    def __init__(self, message: str):
        RuntimeError.__init__(self, message)


class PatchSafetyError(RuntimeError):
    """补丁越界或无法安全应用：拒绝且不重试（计划 L505 ②）。"""


class PatchAnchorError(PatchSafetyError):
    """锚点计数 !=1 的本地拒绝：整轮不应用、零落盘，反馈后续跑（受轮帽）。

    锁内适配（Phase_1 L505② 协议口径修正，用户 2026-10-23 逐字裁决
    「把 L505② 非唯一锚从立即结案改为计为一轮失败、把歧义反馈给模型
    令其给唯一锚点、在 5 轮帽内继续」）：L505② 原文只钉「补丁越界」不
    重试；锚点非唯一/未命中是模型可自修的响应缺陷，run7 首跑（old 块
    命中 2 次）暴露旧实现把它归并到越界同判立即结案，使预注册 5 轮帽
    （L502）只花了 1 轮。本子类把该条件切到 ChunkBirthError 同构的
    「失败→机器反馈→下一 coding 轮」通道；越界等策略违例仍由父类
    PatchSafetyError 立即结案。这是协议口径修正，不是调参翻案：
    RUN_CODING_CAP/MAX_CODING_ROUNDS 均=5 不动，再败仍诚实 human_failure。

    hits 为机器计数（0=未命中，>1=非唯一），进 timeline 整数键与
    模型面 hint 选择；str(exc) 仍是人侧/模型面机器原因原文。

    加宽授权链留痕（critic stage-1 ACCEPT-WIDENING 条件 1）：用户
    2026-10-23 逐字原话只点名「非唯一锚（>1）」；hits==0（未命中）
    纳入本子类系同一 !=1 机器谓词的同缺陷类，经用户指定的「另开
    评审+三签」协议评审通道（fresh critic 2026-10-24 APPROVE 边界）
    接受，非用户原话字面授权；两 hint 分述。切换站点严格限
    unified hunk 与 structured count 两个计数判定，不外延。

    行为代差登记（MINOR-2）：已存在非空文件 old="" 旧实现经
    text.count("")==len+1（>1）走父类 PatchSafetyError 立即结案，
    本子类生效后改走反馈续跑（消息串不变、hits=len+1、零落盘）；
    出生（文件不存在）old="" 仍恰好 1 次、正常写出，行为不变。
    """

    def __init__(self, message: str, *, hits: int):
        super().__init__(message)
        self.hits = hits


class PatchTransportError(PatchSafetyError):
    """transport 层失败（v1.4 代差登记，设计稿 §3）：帽内反馈续跑，不 instant-death。

    锁内适配（Task6 修复环 v1.4「IDE 式多次小合成」协议，
    .omc/handoffs/task6-ide-repair-protocol-design.md，与 PatchAnchorError
    同体例）：设计 §头部授权链——用户 2026-10-24 逐字原话「下面我们重点猛攻
    Task6，务必要将Task6的Gate1至Gate2之间的代码生成、仿真执行的通路成功跑通！
    否则的话，像Task7/Task8这样的新任务，或者我们未来将遇到的其他螺旋升级老
    任务还是会遇到Task6这样的问题。像你刚才提到的，继续解决Task6时，我们可以
    参考 IDE/Agent会话交互里的"多次小合成"模式，而且要提高Task6方法的鲁棒性：
    未来像今天这样"切换到mimo-v2.6-*"的模型切换动作肯定还会有。」
    与 2026-10-23 L505② 同类的协议口径修正（非调参翻案）：把「无法安全应用
    且模型可自修」的补丁失败从 instant-death 收窄为帽内反馈轮；
    **唯一保留 instant-death 的是路径越界**（_validate_target 族，P1）。

    行为代差登记（设计 §3 逐站点穷举，仿 :231-234 体例；切换面=决策表中
    全部「新行为=反馈」行）——JSON 通道 10 站（instant-death → transport
    反馈）：J1 缺 path 字段、J2 old/new 非字符串、J3 unified diff 缺 +++ 头、
    J4 unified diff 无 hunk、J5 返回非 JSON 对象、J6 unified_diff 空串/
    非字符串、J7 patches 空列表（run5/run7c 实杀形态）/非列表、J8 键缺失
    （既无 unified_diff 也无 patches）、J9 JSONDecodeError（零花括号散文/
    截断无闭合/截断有闭合内层重 parse 三形态，_json_object 调用点前置捕获）、
    J10 patches 元素非对象；围栏通道 7 形（新增面）：F1 围栏结构违例（半打/
    全角/CR/正文含 end 行/围栏外正文）、F2 func 锚 0 命中（经锚点通道）、
    F3 锚 ≥2 无 class=（经锚点通道）、F4 class 不存在/类内无该 func（经
    锚点通道）、F5 目标文件 SyntaxError（指引 JSON 通道）、F6 投影重
    parse 失败（携行号、零落盘）、F7 非 .py path 围栏（指引 JSON）——均
    帽内反馈；F2/F3/F4 仍抛平级兄弟 PatchAnchorError（不入本类）。
    唯一保留 instant-death：P1 路径越界（_validate_target 族）——父类裸
    PatchSafetyError 直捕，test_02 no-retry 语义不变。与 PatchAnchorError
    平级（绝不子类化锚点类——test_run7b_a6 ⑤ 断 not isinstance 依赖此形）。
    """


class ChunkBirthError(RuntimeError):
    """拆块出生轮本地校验失败：整轮作废、不半成品落盘，主循环进入下一轮。

    锁内适配（计划 v1.2 外，seq42-47 拆块出生，用户 2026-10-20 裁决）：
    与 CodingLimitError 同属 RuntimeError 族的「不触网失败信号」——失败发生
    在本地围栏/ast 校验（run4 前为 JSON/ast），不产生新的网关调用。区别在生命周期：本异常在 run
    循环内被捕获后，走既有「失败→再执行→下一 coding 轮」机制，仍受
    RunSpec.max_coding_rounds 约束；它不是 CodingLimitError 那种结案硬停。
    消息必须含块号与机器原因（下一轮 prompt 的失败反馈段原样带上）。

    run5（规格 §15.3-H2）：keyword-only detail 是内部证据标签（schema
    simpraxis.chunk_split_reject.v1，仅 class/window 等整数与英文枚举），
    只在长度分类两条新路径上随异常携带，经 decision/timeline 的条件键落证据，
    永不进 str(exc)→chunk_feedback→模型面；既有全部 raise 点不传 detail
    （默认 None），str() 与全链路字节行为不变。
    """

    def __init__(self, message: str, *, detail: "dict | None" = None):
        super().__init__(message)
        self.detail = detail


@dataclass(frozen=True)
class ChunkSpec:
    """拆块出生规格。锁内适配（计划 v1.2 外，seq42-47 拆块出生，
    用户 2026-10-20 裁决）：chunk_count 固定来自工厂（Task6 run3 前为 4；
    run4 起 n=6 是工厂规格变更，见 task6.py 与
    .omc/handoffs/task6-sentinel-seam-design.md §3），禁止运行时由模型自报
    块数（防模型逃单块）。

    chunk_boundaries 是工单四字段外的最小增补（同条锁内适配）：runner 在
    底层（不得模块级反向 import orchestrator.graph；Option 2 起唯一例外＝
    flag 守卫的函数级 lazy import scan_shim_topology_gate），渲染「本块
    边界/行数预算」所需文本只能经规格从工厂流入，故与 chunk_titles 同形
    并列，构造即校长度。
    """

    target_path: str
    chunk_count: int                                  # n 固定来自工厂（Task6 run4 起 6）
    chunk_titles: tuple[str, ...]                     # 长度=chunk_count
    per_call_sections: tuple[tuple[str, str], ...]    # 合同/提案/骨架，每 call 都带
    chunk_boundaries: tuple[str, ...]                 # 每块允许章节+行数预算文案

    def __post_init__(self) -> None:
        if not isinstance(self.target_path, str) or not self.target_path.strip():
            raise ValueError("ChunkSpec.target_path 必须是非空字符串")
        if not isinstance(self.chunk_count, int) \
                or isinstance(self.chunk_count, bool) or self.chunk_count < 1:
            raise ValueError(f"ChunkSpec.chunk_count 非法：{self.chunk_count!r}")
        for name in ("chunk_titles", "chunk_boundaries"):
            val = getattr(self, name)
            if (not isinstance(val, tuple) or len(val) != self.chunk_count
                    or not all(isinstance(x, str) and x.strip() for x in val)):
                raise ValueError(
                    f"ChunkSpec.{name} 必须是 {self.chunk_count} 个非空字符串")
        if not isinstance(self.per_call_sections, tuple) \
                or not all(
                    isinstance(s, tuple) and len(s) == 2
                    and all(isinstance(x, str) for x in s)
                    for s in self.per_call_sections):
            raise ValueError("ChunkSpec.per_call_sections 必须是 (标题, 正文) 元组")


@dataclass(frozen=True)
class ChunkPromptContext:
    """单次拆块调用的渲染输入（runner 循环内按块构造，不进任何持久状态）。"""

    index: int
    total: int
    target_path: str
    titles: tuple[str, ...]
    boundaries: tuple[str, ...]
    previous_code: str = ""
    round_feedback: str = ""


@dataclass(frozen=True)
class CodingRecallSpec:
    """攢批94 锁内适配（Task8 Genesis 流体两战役实测墙，2026-10-27）：
    coding 站增强召回。默认不开（RunSpec.coding_recall=None 时所有任务检索
    query/namespace/top_k 与 prompt 逐字节旧行为，Task6/7 路径零变化）。

    - ``clean_troubleshooting_hint``：True 时 troubleshooting 主路查询串由
      ``"仿真失败 " + tail[-500:]`` 改为错误签名清洗串（剥离 Genesis/torch/
      litellm 警告噪声与 Traceback 帧行；实测 run1–5 真实 tail 下 Q17
      「Scene is already built」被警告挤出 top-6，清洗后回到 #0）。
    - ``extra_kb``：额外 KB 检索路 ``(namespace, top_k, query)`` 元组序列。
      coding 站历史只查 troubleshooting；Genesis SPH API 金块在 capabilities
      的 code_knowledge §3.5，旧路径永不可达。查询串必须在活库实测冻结
      （见 task8_fluid.FLUID_CAPABILITY_QUERY 取证注释），禁止凭感觉填。
    """
    clean_troubleshooting_hint: bool = False
    extra_kb: tuple[tuple[str, int, str], ...] = ()


@dataclass
class RunSpec:
    thread_id: str
    workdir: Path
    cmd: list[str]
    telemetry_relpath: str                 # 成功产物（相对 workdir）
    batch_id: str
    # L-1：本 run 在 thread 内的稳定序号（与证据文件名 r{run_idx} 同一约定，
    # 工厂用 len(agent_runs)+1 预占、接缝用 len(agent_runs) 复核，二者同号）。
    # 仅当显式给出时启用调用级重放缓存；None（如 Task4 直连）不查不写缓存。
    run_idx: Optional[int] = None
    billing_task: int = 4                  # Task7/8 联调时传 7/8（锁内适配 L506）
    gate2_notes: Optional[str] = None      # 图侧 gate2_rejected 事件注入
    env_extra: dict[str, str] = field(default_factory=dict)
    run_timeout_s: int = RUN_TIMEOUT_S
    task_title: str = ""
    # Task7：真实 rollout 视频产物（相对 workdir）。存在才回填视频四字段；
    # duration_ms 优先读场景 manifest（n_frames×frame_dt_s），缺失再 imageio 解码。
    video_relpath: Optional[str] = None
    cover_relpath: Optional[str] = None
    manifest_relpath: Optional[str] = None
    # Phase 2 Task2：每 run coding_debug 上限可配置；默认 MAX_CODING_ROUNDS(=5)
    # 与 Phase 1 行为逐值一致。Task6 升阶 run 工厂将显式传 3（run 级收窄，
    # 见计划 L193），本任务不改 Task7/8 既有工厂。
    max_coding_rounds: int = MAX_CODING_ROUNDS
    # 锁内适配（计划 v1.2 外，seq42-47 拆块出生，用户 2026-10-20 裁决）：
    # 以下四字段默认值保证 Task7/8 与既有流程字节级旧行为——coding 调用仍
    # 8192 tokens/120s（120 与 router.call 默认值同值，旧路径只是隐式吃默认），
    # 无额外 prompt 段、无拆块。Task6 V2 出生轮显式传 32768/600 + ChunkSpec。
    coding_max_tokens: int = MAX_TOKENS
    coding_timeout_s: int = DEFAULT_CODING_TIMEOUT_S
    extra_prompt_sections: tuple[tuple[str, str], ...] = ()
    chunked_birth: "ChunkSpec | None" = None
    # run7b 提交 B（rc0 批次结构闸，用户 2026-10-23 评审改闸授权）：默认 False
    # 保持逐字节旧行为（缓存键/指纹/成功判据不变）；仅 Task6 run 工厂显式 True。
    batch_schema_check: bool = False
    # 锁内适配（计划 v1.2 外，用户 2026-10-25 裁决 Option 2）：默认 False 保持
    # 逐字节旧行为（task7/8 不传）；仅 Task6 run 工厂显式 True。True 时 rc0+遥测
    # +结构闸全绿后仍跑 Gate2 构卡前同款 host-shim 双闸机验
    # （task6.scan_shim_topology_gate＝_require_shim_evidence 同一闸，窗口/正则/
    # 9-2 契约单真值），违例跳过 ok 短路并注入编码修复轮——run25 结构性停机
    #（拓扑墙反馈永不进模型环 → 重挂 delta=0）的反馈通路；帽仍 max_coding_rounds。
    shim_topology_check: bool = False
    # 攢批94 锁内适配（2026-10-27 Task8 流体 run1–7 实测墙）：None 时检索与
    # prompt 与 Phase 1 以来逐字节一致；仅 task8_fluid 工厂显式给增强召回
    # （troubleshooting 错误签名清洗 + capabilities 额外检索路）。
    coding_recall: "CodingRecallSpec | None" = None


@dataclass
class _ExecResult:
    rc: int
    stdout: str
    stderr: str


def _default_evidence_root(thread_id: str) -> Path:
    d = phase1_root() / "task4_runner" / thread_id
    d.mkdir(parents=True, exist_ok=True)
    return d


SubprocessRunner = Callable[[list[str], Path, float, dict], _ExecResult]


def _default_subprocess(cmd, workdir, timeout_s, env_extra) -> _ExecResult:
    env = dict(os.environ)
    env.update(env_extra)
    proc = subprocess.run(
        cmd, cwd=str(workdir), env=env, capture_output=True, text=True,
        timeout=timeout_s)
    return _ExecResult(proc.returncode, proc.stdout or "", proc.stderr or "")


# ---------------------------------------------------------------------------
# traceback 取证（只准读 workdir 内的帧文件）
# ---------------------------------------------------------------------------

_FRAME_RE = re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+)')


def _collect_snippets(workdir: Path, text: str) -> list[dict]:
    workdir_r = workdir.resolve()
    seen: set[tuple[str, int]] = set()
    out: list[dict] = []
    total = 0
    for m in _FRAME_RE.finditer(text):
        fp = Path(m.group("path"))
        try:
            resolved = fp.resolve(strict=False)
            resolved.relative_to(workdir_r)
        except (OSError, ValueError):
            continue  # stdlib / site-packages / 仓库外：禁读
        key = (str(resolved), int(m.group("line")))
        if key in seen:
            continue
        seen.add(key)
        try:
            lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        ln = int(m.group("line"))
        lo, hi = max(0, ln - 1 - SNIPPET_CTX_LINES), ln + SNIPPET_CTX_LINES
        block = "\n".join(f"{i+1:5d}| {lines[i]}" for i in range(lo, min(hi, len(lines))))
        if total + len(block) > SNIPPET_TOTAL_CHARS:
            break
        total += len(block)
        out.append({"file": str(resolved.relative_to(workdir_r)),
                    "line": ln, "snippet": block})
    return out


# ---------------------------------------------------------------------------
# 补丁解析与安全应用
# ---------------------------------------------------------------------------

def _validate_target(path: Path, workdir: Path) -> Path:
    name = path.name
    if name in FORBIDDEN_BASENAMES:
        raise PatchSafetyError(f"补丁企图修改禁改文件：{name}")
    if "access" in path.parts:
        raise PatchSafetyError(f"补丁企图进入 ~/access 区域：{path}")
    workdir_r = workdir.resolve()
    if path.is_absolute():
        target = path.resolve(strict=False)
    else:
        target = (workdir_r / path).resolve(strict=False)
    try:
        target.relative_to(workdir_r)
    except ValueError:
        raise PatchSafetyError(
            f"补丁目标越出 workdir：{path} -> {target}（workdir={workdir_r}）")
    # 拒绝 symlink 逃逸（路径上任何一环是指向 workdir 外的链接）
    cur = workdir_r
    for part in target.relative_to(workdir_r).parts:
        cur = cur / part
        if cur.is_symlink():
            real = cur.resolve(strict=False)
            try:
                real.relative_to(workdir_r)
            except ValueError:
                raise PatchSafetyError(f"补丁经符号链接逃逸 workdir：{cur} -> {real}")
    return target


_HUNK_RE = re.compile(
    r"^@@ -(?P<o>\d+)(?:,\d+)? \+(?P<n>\d+)(?:,\d+)? @@", re.M)


def _stage_structured(staged: dict, workdir: Path, patch: dict) -> str:
    """run7b §3.2：单 structured 补丁的内存投影（只读盘、只写 staged；同文件后续锚点落前序投影）。"""
    rel = patch.get("path")
    if not isinstance(rel, str) or not rel:
        # v1.4 代差登记 J1（设计 §3 行为代差登记节）：缺 path → transport 反馈。
        raise PatchTransportError("补丁缺 path 字段")
    target = _validate_target(Path(rel), workdir)
    old, new = patch.get("old"), patch.get("new")
    if not isinstance(old, str) or not isinstance(new, str):
        # v1.4 代差登记 J2（设计 §3 行为代差登记节）：old/new 非字符串 → transport 反馈。
        raise PatchTransportError("补丁 old/new 必须为字符串")
    key = str(target)
    if key not in staged:
        staged[key] = target.read_text(encoding="utf-8") if target.exists() else ""
    text = staged[key]
    hits = text.count(old)
    if hits != 1:
        # run7b 锁内适配（L505② 协议口径修正，§10.1 标记 2）：锚点 !=1 改道子类反馈通道（MINOR-2 old="" 见类 docstring）。
        raise PatchAnchorError(f"old 块命中 {hits} 次（需恰好 1 次）：{rel}", hits=hits)
    staged[key] = text.replace(old, new, 1)
    return str(target.relative_to(workdir.resolve()))


def _commit_staged(staged: dict, workdir: Path) -> None:
    """run7b §3.2 MINOR-3：全部锚点通过后按首触顺序落盘，每文件恰三件 rename-free 原语。"""
    for key, text in staged.items():
        target = Path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        # L-11：0700/0755 可执行脚本补丁后保留属主执行位（幂等，跟随链接）。
        chmod_keep_user_exec(target)


def _stage_unified_section(staged: dict, workdir: Path, section: str) -> "Optional[str]":
    """run7b（冻结规格 §3.2）：单 unified section 的内存投影；空段返回 None。"""
    if not section.strip():
        return None
    m_new = re.search(r"^\+\+\+ (?:b/)?(.+)$", section, re.M)
    if not m_new:
        # v1.4 代差登记 J3（设计 §3 行为代差登记节）：缺 +++ 头 → transport 反馈。
        raise PatchTransportError("unified diff 缺 +++ 头")
    rel = m_new.group(1).strip()
    target = _validate_target(Path(rel), workdir)
    body_start = section.find("\n@@")
    body = section[body_start + 1:] if body_start >= 0 else ""
    key = str(target)
    if key in staged:
        old_lines = staged[key].splitlines(keepends=True)  # MINOR-1：第二 section 从第一 section 投影取行
    else:
        old_lines = (target.read_text(encoding="utf-8").splitlines(keepends=True)
                     if target.exists() else [])
    # 按 hunk 顺序定位替换（cursor 在投影行上推进）
    result: list[str] = []
    cursor = 0
    hunks = list(_HUNK_RE.finditer(body))
    if not hunks:
        # v1.4 代差登记 J4（设计 §3 行为代差登记节）：无 hunk → transport 反馈。
        raise PatchTransportError("unified diff 无 hunk")
    for hi, hm in enumerate(hunks):
        hunk_end = hunks[hi + 1].start() if hi + 1 < len(hunks) else len(body)
        hunk_lines = body[hm.start():hunk_end].splitlines()
        old_block, new_block = [], []
        for hl in hunk_lines[1:]:
            if hl.startswith("\\"):
                continue
            if hl.startswith("-"):
                old_block.append(hl[1:])
            elif hl.startswith("+"):
                new_block.append(hl[1:])
            else:
                ctx = hl[1:] if hl[:1] in (" ", "") else hl
                old_block.append(ctx)
                new_block.append(ctx)
        window = [l.rstrip("\n") for l in old_lines[cursor:]]
        old_s = [l.rstrip("\n") for l in old_block]
        hits_idx = [i for i in range(len(window) - len(old_s) + 1)
                    if window[i:i + len(old_s)] == old_s]
        if len(hits_idx) != 1:
            # run7b 锁内适配（L505② 协议口径修正，§10.1 标记 2）：hunk 锚点 !=1 改道子类反馈通道。
            raise PatchAnchorError(f"hunk 上下文命中 {len(hits_idx)} 次（需恰好 1 次）：{rel}", hits=len(hits_idx))
        at = cursor + hits_idx[0]
        result.extend(old_lines[cursor:at])
        result.extend("\n".join(new_block).splitlines(keepends=True))
        cursor = at + len(old_s)
    result.extend(old_lines[cursor:])
    staged[key] = "".join(result)
    return str(target.relative_to(workdir.resolve()))


def _apply_unified_diff(workdir: Path, diff_text: str) -> list[str]:
    """最小 unified diff 应用器（git 风格头）；每段旧块必须唯一命中。run7b §3.2：投影后统一 commit，同文件两 section applied=[rel,rel] 不塌缩。"""
    files = re.split(r"^diff --git .+$", diff_text, flags=re.M)
    staged: dict[str, str] = {}
    applied: list[str] = []
    for section in files:
        rel = _stage_unified_section(staged, workdir, section)
        if rel is not None:
            applied.append(rel)
    _commit_staged(staged, workdir)
    return applied


def _apply_structured_patch(workdir: Path, patch: dict) -> str:
    """run7b §3.2：单补丁薄包装（三直调 old="" 出生语义不变："".count("")==1 后 commit）。"""
    staged: dict[str, str] = {}
    rel = _stage_structured(staged, workdir, patch)
    _commit_staged(staged, workdir)
    return rel


def _parse_and_apply(workdir: Path, raw: str) -> list[str]:
    try:
        obj = _json_object(raw)
    except json.JSONDecodeError as exc:
        # v1.4 代差登记 J9（设计 §3 行为代差登记节）：JSONDecodeError 前置捕获
        # 转 transport 反馈（旧实现图层逃逸不经 decision/不耗帽）；只报行列，
        # 不附 str(exc)（内含文档片段）。
        raise PatchTransportError(
            f"响应不是合法 JSON 对象（JSONDecodeError：行 {exc.lineno} 列 {exc.colno}）") from None
    if not isinstance(obj, dict):
        # v1.4 代差登记 J5（设计 §3 行为代差登记节）：非 JSON 对象 → transport 反馈。
        raise PatchTransportError("coding_debug 返回非 JSON 对象")
    if "unified_diff" in obj:
        diff = obj["unified_diff"]
        if not isinstance(diff, str) or not diff.strip():
            # v1.4 代差登记 J6（设计 §3 行为代差登记节）：空串/非字符串同站兼拒 → transport 反馈。
            raise PatchTransportError("unified_diff 为空")
        return _apply_unified_diff(workdir, diff)
    patches = obj.get("patches")
    if not isinstance(patches, list) or not patches:
        # v1.4 代差登记 J7/J8（设计 §3 行为代差登记节，run5/run7c 实杀形态）：
        # 空列表/非列表/键缺失同站兼拒 → transport 反馈（旧实现 instant-death
        # 废掉剩余轮帽）。
        raise PatchTransportError("返回中既无 unified_diff 也无 patches[]")
    # run7b §3.2：先逐件投影（任一锚点失败即整轮零落盘），全过后统一 commit。
    staged: dict[str, str] = {}
    applied = []
    for p in patches:
        if not isinstance(p, dict):
            # v1.4 代差登记 J10（设计 §3 行为代差登记节）：元素非对象 → transport 反馈。
            raise PatchTransportError("patches 元素必须是对象")
        applied.append(_stage_structured(staged, workdir, p))
    _commit_staged(staged, workdir)
    return applied


def _json_object(raw: str) -> Any:
    """容忍 ```json 围栏（与 Task2 parse_json_object 同风格的最小提取）。"""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


# ---------------------------------------------------------------------------
# v1.4 修复传输双通道（设计稿 §2.1/§2.2/§2.3
# .omc/handoffs/task6-ide-repair-protocol-design.md；授权链见
# PatchTransportError docstring）：replace 围栏整替 + JSON 兜底混出决策。
# 围栏语法纪律与出生围栏同族（整行 begin/end、token 字段、CRLF 逐行归一、
# 全角不命中→结构反馈、正文含 end 行→违例、半打→结构反馈）。本块零网关
# 调用、零账本触碰；F2/F3/F4 抛 PatchAnchorError（既有键族），其余违例
# 抛 PatchTransportError。
# ---------------------------------------------------------------------------

REPLACE_FENCE_BEGIN = "<<<<<<< simpraxis.replace.v1"
REPLACE_FENCE_END = ">>>>>>> simpraxis.replace.v1.end"
REPLACE_FENCE_NUCLEUS = "simpraxis.replace.v1"
# §2.3 函数源码注入字符预算：整函数注入禁止截断；单函数超预算跳过并注记
# 「函数过大未注入，请用 JSON 通道」；两函数合计超预算只注入较小者。
FUNCTION_SOURCE_TOTAL_CHARS = 8192

# 字段序钉死 path→func→[class]；token 口径 [^ \t\r]+ 同出生；$ 前只许空白。
_REPLACE_BEGIN_RE = re.compile(
    r"^[ \t]*" + re.escape(REPLACE_FENCE_BEGIN)
    + r"[ \t]+path=([^ \t\r]+)[ \t]+func=([^ \t\r]+)"
    r"(?:[ \t]+class=([^ \t\r]+))?[ \t]*$")
_REPLACE_END_RE = re.compile(
    r"^[ \t]*" + re.escape(REPLACE_FENCE_END) + r"[ \t]*$")


@dataclass(frozen=True)
class _ReplaceBlock:
    """v1.4 单个合法 replace 围栏（begin RE 命中 + end 配对 + 正文捕获）。"""

    path: str
    func: str
    cls: "Optional[str]"
    body: str
    line: int  # begin 行号（1-based，机器原因引用用）
    end_line: int  # end 行号（1-based；三签 MAJOR-1 JSON 候选剥离用）


def _scan_replace_fences(raw: str) -> "list[_ReplaceBlock]":
    """v1.4 §2.1 围栏扫描（出生同族纪律）：违例一律 PatchTransportError（F1）。"""
    if REPLACE_FENCE_NUCLEUS not in raw:
        return []
    # CRLF 逐行归一（剥恰好一粒行尾 \r，同出生扫描器口径）；后续 body 为 LF 文本，
    # 与 read_text 通用换行模式的内存口径一致。
    lines = [ln[:-1] if ln.endswith("\r") else ln
             for ln in raw.split("\n")]
    blocks: "list[_ReplaceBlock]" = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        bm = _REPLACE_BEGIN_RE.match(line)
        if bm is None:
            # 围栏外非核记内容（M1 同轮 JSON 件/短散文）容忍——M1 语义要求
            # 围栏与 JSON 同响应共存；仅含核记的迷走行判结构违例（全角 begin
            # ＜＜＜ 等/字段序错/迷走 end 行，机器原因不回显模型行文本）。
            if REPLACE_FENCE_NUCLEUS in line:
                if _REPLACE_END_RE.match(line):
                    raise PatchTransportError(
                        f"replace 围栏结构违例：end 行 {i + 1} 无配对 begin")
                raise PatchTransportError(
                    f"replace 围栏结构违例：begin/end 行不合规（行 {i + 1}）")
            i += 1
            continue
        j = i + 1
        body_lines: "list[str]" = []
        while True:
            if j >= n:
                raise PatchTransportError(
                    f"replace 围栏结构违例：begin 行 {i + 1} 无配对 end（半打）")
            cand = lines[j]
            if _REPLACE_END_RE.match(cand):
                # 可选 ======= 分隔行：仅当紧贴 end 行之前才剥（前瞻一次）。
                if body_lines and body_lines[-1].strip() == "=======":
                    body_lines = body_lines[:-1]
                break
            if REPLACE_FENCE_NUCLEUS in cand:
                # 正文中出现围栏核记（含 end 行同形）即违例（同出生纪律）。
                raise PatchTransportError(
                    f"replace 围栏结构违例：正文第 {j - i} 行含围栏核记（begin 行 {i + 1}）")
            body_lines.append(cand)
            j += 1
        if not "".join(body_lines).strip():
            # 格式契约底线：空/纯空白正文不是「函数整替」（解释申报件，见交付报告）。
            raise PatchTransportError(
                f"replace 围栏结构违例：正文为空（begin 行 {i + 1}）")
        blocks.append(_ReplaceBlock(
            path=bm.group(1), func=bm.group(2), cls=bm.group(3),
            body="\n".join(body_lines), line=i + 1, end_line=j + 1))
        i = j + 1
    return blocks


def _strip_replace_fence_spans(
        raw: str, blocks: "list[_ReplaceBlock]") -> str:
    """v1.4 三签 MAJOR-1：剥除围栏行段，得 JSON 侧候选文本。

    仅在 `_scan_replace_fences` 成功后调用（结构已验：围栏外无核记行，
    begin/end 行段与块一一对应）。按 begin..end 行号闭区间整段删除，
    其余行逐字保留（行尾 CRLF 随行保留，剥行号不受影响）。围栏体含
    dict 字面量（`{'sum': a + b}`）时，first-`{`/last-`}` 切片不再切
    进围栏内容——M1 判定与 M2 JSON 回退都走该候选（先扫成功后剥离）。
    """
    if not blocks:
        return raw
    lines = raw.split("\n")
    drop: "set[int]" = set()
    for b in blocks:
        # b.line / b.end_line 均 1-based 闭区间 → 0-based 半开 [begin-1, end)。
        drop.update(range(b.line - 1, b.end_line))
    return "\n".join(ln for idx, ln in enumerate(lines) if idx not in drop)


def _byte_col_to_char(line: str, byte_off: int) -> int:
    """ast 列偏移是 UTF-8 字节口径（py3.10.12 亲验）；换算该行字符列。"""
    return len(line.encode("utf-8")[:byte_off].decode("utf-8", "ignore"))


def _function_span(text: str, node) -> "tuple[int, int]":
    """函数节点的字符级 [start, end) 跨度：起点含装饰器行（回退 def 行首列），终点=end 列。"""
    lines = text.split("\n")
    line_starts: "list[int]" = []
    acc = 0
    for ln in lines:
        line_starts.append(acc)
        acc += len(ln) + 1
    if node.decorator_list:
        dec = node.decorator_list[0]
        dline = lines[dec.lineno - 1]
        # decorator 的 col_offset 指表达式起点（@ 之后）；前缀里 rfind('@') 取 @ 位。
        prefix = dline.encode("utf-8")[:dec.col_offset].decode("utf-8", "ignore")
        at = prefix.rfind("@")
        col = at if at >= 0 else _byte_col_to_char(dline, dec.col_offset)
        start = line_starts[dec.lineno - 1] + col
    else:
        start = (line_starts[node.lineno - 1]
                 + _byte_col_to_char(lines[node.lineno - 1], node.col_offset))
    end = (line_starts[node.end_lineno - 1]
           + _byte_col_to_char(lines[node.end_lineno - 1], node.end_col_offset))
    return start, end


def _find_replace_anchor(tree, block: "_ReplaceBlock"):
    """v1.4 §2.1 AST 定位：模块级 def + ClassDef 直属方法（嵌套 def 不在域）。

    class= 省略：func 在域内恰 1 处；≥2 → 锚点反馈列 AST 候选（可加 class=
    消歧或改 JSON）；class= 给定：类不存在 class_not_found / 类内无该直属
    func func_not_in_class（F2/F3/F4 均抛 PatchAnchorError 走既有键族）。
    """
    func, cls = block.func, block.cls
    module_hits: "list[tuple[str, Any]]" = []
    class_hits: "list[tuple[str, Any]]" = []
    class_def_exists = False
    for node in tree.body:
        if isinstance(node, (FunctionDef, AsyncFunctionDef)) and node.name == func:
            module_hits.append((func, node))
        elif isinstance(node, ClassDef):
            if node.name == cls:
                class_def_exists = True
            for m in node.body:
                if isinstance(m, (FunctionDef, AsyncFunctionDef)) and m.name == func:
                    if cls is None or node.name == cls:
                        # 候选标签是文件内容（AST 派生），可回显模型面。
                        class_hits.append((f"{node.name}.{func}", m))
    if cls is not None:
        if not class_def_exists:
            raise PatchAnchorError(
                f"replace 围栏锚点失败（begin 行 {block.line}，机器原因："
                "class_not_found）：文件中无该类定义", hits=0)
        if not class_hits:
            raise PatchAnchorError(
                f"replace 围栏锚点失败（begin 行 {block.line}，机器原因："
                "func_not_in_class）：类内无该函数的直属方法", hits=0)
        cands = class_hits  # class= 给定：只认该类直属，模块级同名不参与消歧
    else:
        cands = module_hits + class_hits
        if not cands:
            raise PatchAnchorError(
                f"replace 围栏 func 锚 0 命中（begin 行 {block.line}）："
                "目标文件中无该函数的模块级/类直属定义", hits=0)
    if len(cands) > 1:
        labels = "、".join(label for label, _ in cands)
        raise PatchAnchorError(
            f"replace 围栏锚点 {len(cands)} 命中（需恰 1；begin 行 "
            f"{block.line}）：候选 {labels}；可加 class= 消歧或改用 JSON 通道",
            hits=len(cands))
    return cands[0]


def _rebase_body_indent(body: str, prefix: str) -> str:
    """v1.4：正文缩进归一——先剥最长公共前导空白（接受齐左/文件摘录两种约定），再给首行外的非空行加目标缩进 prefix。

    span 起点在 @/def 首字符（缩进留在 prefix 里），类直属方法的多行正文
    必须随 prefix 重定基，否则第二行起丢失类缩进（IndentationError→F6）。
    空行不加前缀（避免行尾空白）；多行字符串内容会随缩进平移，语法无损。
    """
    lines = body.split("\n")
    common: "Optional[str]" = None
    for ln in lines:
        if not ln.strip():
            continue
        ws = ln[:len(ln) - len(ln.lstrip(" \t"))]
        if common is None:
            common = ws
        else:
            i = 0
            while i < min(len(common), len(ws)) and common[i] == ws[i]:
                i += 1
            common = common[:i]
    if common:
        lines = [ln[len(common):] if ln.strip() else ln for ln in lines]
    if prefix:
        # 首行不加：text[:start] 已以 prefix 收尾（span 起点在 @/def 首字符）。
        lines = [lines[0]] + [prefix + ln if ln.strip() else ln
                              for ln in lines[1:]]
    return "\n".join(lines)


def _apply_replace_staged(workdir: Path, blocks: "list[_ReplaceBlock]") -> list[str]:
    """v1.4 §2.2 三闸：定位闸 → 投影闸（内存 staging）→ 重 parse 闸 → commit。"""
    staged: dict[str, str] = {}
    applied: list[str] = []
    for b in blocks:
        target = _validate_target(Path(b.path), workdir)  # P1：越界仍 instant-death
        if target.suffix != ".py":
            # F7：非 .py 围栏 → transport 反馈指引 JSON 通道（不落盘）。
            raise PatchTransportError(
                f"replace 围栏 path 非 .py 文件（begin 行 {b.line}；"
                "不落盘，请改用 JSON 通道）")
        key = str(target)
        if key not in staged:
            staged[key] = target.read_text(encoding="utf-8") if target.exists() else ""
        text = staged[key]
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            # F5：目标文件语法不可解析 → 不走 AST 通道，transport 反馈指引
            # JSON（from None 断链：不附源码行，同出生体例）。
            raise PatchTransportError(
                f"replace 定位闸失败：文件当前语法不可解析（行 {exc.lineno} 列 "
                f"{exc.offset}）；请用 JSON old/new 通道整替，或先修复语法") from None
        _label, node = _find_replace_anchor(tree, b)
        start, end = _function_span(text, node)
        body = b.body
        if body.startswith("\n"):
            # 出生同纪律：剥恰好一个前导 LF（span 起点在 @/def 首字符）。
            body = body[1:]
        # 缩进重定基：span 行首缩进留在 prefix，正文非空行统一贴回该缩进
        # （齐左与文件摘录两种正文约定都成立，见 _rebase_body_indent）。
        prefix = text[text.rfind("\n", 0, start) + 1:start]
        staged[key] = text[:start] + _rebase_body_indent(body, prefix) + text[end:]
        applied.append(str(target.relative_to(workdir.resolve())))
    # 重 parse 闸（F6）：投影全文 ast.parse 通过才 commit；失败零落盘——
    # 封死「坏拼接落盘→AST 通道永久失效→无通道可用」自毁环。
    for key, text in staged.items():
        try:
            ast.parse(text)
        except SyntaxError as exc:
            rel = str(Path(key).relative_to(workdir.resolve()))
            raise PatchTransportError(
                f"replace 重 parse 闸失败：投影全文语法不可解析（文件 {rel} 行 "
                f"{exc.lineno}）；本轮零落盘；请改用 JSON old/new 通道或修正围栏源码") from None
    _commit_staged(staged, workdir)
    return applied


def _parse_replace_and_apply(workdir: Path, raw: str) -> "tuple[list[str], dict]":
    """v1.4 混出决策（设计 §2.1/§3 判定序）：路径闸 → 围栏探测 → JSON 尝试 → 反馈。

    返回 (applied, decision_extras)：extras 为 decision 证据登记件——M1 键
    json_part_ignored、M2 键 fence_invalid_json_applied（机器原因）。无围栏
    核记的响应直走既有 JSON 通道（extras 恒空，字节行为不变）。
    """
    if REPLACE_FENCE_NUCLEUS not in raw:
        return _parse_and_apply(workdir, raw), {}
    fence_exc: "Optional[PatchSafetyError]" = None
    # v1.4 三签 MAJOR-1：JSON 侧（M1 判定与 M2 回退）一律走剥除围栏行段
    # 后的候选文本——围栏体含 dict 字面量时，raw 整段 first-`{`/last-`}`
    # 切片会切进围栏内容成垃圾。仅在围栏扫描成功后剥离（结构已验）；
    # 扫描失败（F1）无 span 可用，保持 raw（既有口径，申报件）。
    json_candidate = raw
    try:
        blocks = _scan_replace_fences(raw)
        # 先扫围栏成功、后剥离（顺序钉死：strip 依赖扫描已验的行段对应）。
        json_candidate = _strip_replace_fence_spans(raw, blocks)
        applied = _apply_replace_staged(workdir, blocks)
        extras: dict = {}
        # M1：围栏全部合法且应用成功 → 应用，同响应另有 JSON 补丁则忽略并
        # 登记（半份修复不许无声蒸发）。M1 判定 = _json_object 成功且对象
        # 含 patches/unified_diff 键（try 包裹：失败即无 JSON 件可登记）。
        try:
            obj = _json_object(json_candidate)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and ("patches" in obj or "unified_diff" in obj):
            extras["json_part_ignored"] = True
        return applied, extras
    except PatchAnchorError as exc:
        fence_exc = exc
    except PatchTransportError as exc:
        fence_exc = exc
    except PatchSafetyError:
        # P1 路径越界：唯一 instant-death，不回退 JSON。
        raise
    # 围栏整轮作废零落盘，回退尝试 JSON（M2 / 组合反馈）。MAJOR-1：候选
    # 文本同样取剥除 span 后版本（F2–F7 时扫描已成功、json_candidate 已
    # 剥离；F1 扫描失败保持 raw）。
    try:
        applied = _parse_and_apply(workdir, json_candidate)
    except PatchSafetyError as json_exc:
        # v1.4 三签 MAJOR-2：JSON 侧 P1 路径越界仍是唯一 instant-death——
        # type-is 精确匹配裸 PatchSafetyError 族直接透传（不误放 transport/
        # anchor 子类），不得洗成围栏+JSON 组合反馈轮（§3 P1「围栏或
        # JSON…不变」）。
        if type(json_exc) is PatchSafetyError:
            raise
        # JSON 也失败 → transport 反馈，机器原因取围栏错误优先、JSON 错误附后；
        # 围栏错误本身是锚点通道（F2/F3/F4）时保持锚点键族（hits 保留）。
        if isinstance(fence_exc, PatchAnchorError):
            raise PatchAnchorError(
                f"{fence_exc}；同轮 JSON 通道同时失败：{json_exc}",
                hits=fence_exc.hits) from None
        raise PatchTransportError(
            f"{fence_exc}；同轮 JSON 通道同时失败：{json_exc}") from None
    return applied, {"fence_invalid_json_applied": str(fence_exc)}


# ---------------------------------------------------------------------------
# 拆块出生：sentinel 围栏纯文本机器校验 + 拼接整体校验
# （run4 2026-10-21 起替代逐块手写 JSON：run1-3 九发两中非法 JSON（22%），
# 失败轴是转义纪律而非输出预算；协议见 .omc/handoffs/task6-sentinel-seam-design.md
# §2。锁内适配，见 ChunkSpec）
# ---------------------------------------------------------------------------

# begin/end 整行正则（允许行首 [SP TAB]*、行尾仅 [SP TAB]*；段间单空白
# 仅 ASCII SP/TAB——全角＝/＜/引号包裹值一律不命中→情形 2）。行尾 CR 不放进
# 正则：扫描已每行剥过恰好一个尾 CR（CRLF 归一），残留第二个 CR 必须不命中
# （三签 LOW-1）。字段 token 容错捕获（任何非 SP/TAB/CR 字符），合规性由
# pathv/intv 在策略层判：path 真闸是与 target 逐字比对，字符类只决定机器
# 原因能否安全回显。
_FENCE_BEGIN_LINE_RE = re.compile(
    r"^[ \t]*" + re.escape(CHUNK_FENCE_BEGIN)
    + r"[ \t]+path=([^ \t\r]+)[ \t]+index=([^ \t\r]+)[ \t]+total=([^ \t\r]+)"
    + r"[ \t]*$")
_FENCE_END_LINE_RE = re.compile(
    r"^[ \t]*" + re.escape(CHUNK_FENCE_END) + r"[ \t]*$")
# intv："0" | [1-9][0-9]{0,2}——禁前导零/符号；bool 无入口（无 JSON 类型面）。
_FENCE_INT_RE = re.compile(r"(?:0|[1-9][0-9]{0,2})")
# 裸控制字符（情形 12）：U+0000-0008、U+000D 残留、U+000B/000C、U+000E-001F；
# TAB(U+0009) 允许；LF(U+000A) 是行分隔不进判定。
_FENCE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0d\x0e-\x1f]")
# 零宽字符（情形 16）：U+200B/200C/200D；U+FEFF 在非首位置（首位置落情形 15）。
_FENCE_ZEROWIDTH_RE = re.compile(r"[​‌‍﻿]")


def _strip_one_trailing_cr(line: str) -> str:
    """逐行剥至多一个行尾 CR（CRLF 唯一合法 CR 形态；EBNF end of §2.2）。"""
    return line[:-1] if line.endswith("\r") else line


def _scan_fence_lines(raw: str):
    """纯结构扫描（设计 §2.3 扫描算法步骤 2-3，不含块号策略）。

    禁 str.splitlines()：它对 VT(U+000B)/FF(U+000C)/NEL 的切分语义与本规范
    不同（矩阵②钉死）；统一 split("\\n")，逐行剥至多一个行尾 CR 后做整行判定。
    返回 (lines, begins, ends, nucleus, begin_fields)：
    lines=剥尾 CR 后的行列表；begins/ends/nucleus 为命中行号（0-based）；
    begin_fields 为 begin 行号→(path,index,total) 原始 token。
    """
    lines = [_strip_one_trailing_cr(part) for part in raw.split("\n")]
    begins: list[int] = []
    ends: list[int] = []
    nucleus: list[int] = []
    begin_fields: dict[int, tuple[str, str, str]] = {}
    for ln, line in enumerate(lines):
        m = _FENCE_BEGIN_LINE_RE.match(line)
        if m:
            begins.append(ln)
            begin_fields[ln] = (m.group(1), m.group(2), m.group(3))
        if _FENCE_END_LINE_RE.match(line):
            ends.append(ln)
        if CHUNK_FENCE_NUCLEUS in line:  # 区分大小写；end 行含同核记，计数含之
            nucleus.append(ln)
    return lines, begins, ends, nucleus, begin_fields


def _extract_chunk(raw: str, *, target: str, index: int, total: int) -> str:
    """解析单块围栏响应并机器校验；任何不符抛 ChunkBirthError（块 i+机器原因）。

    严格按设计 §2.3 的 5 步扫描算法与 16 形原因表。机器原因总则：只允许
    计数/行号/字符类/模板固定字面值，严禁摘录任何模型输出原文（防注入下一
    轮块 0 prompt）；path 仅在过白名单 fullmatch 时逐字回显，index/total
    仅纯数字 intv 形回显数值。
    """
    # 步骤 1：BOM 先判（情形 15）。
    if raw.startswith("\ufeff"):
        raise ChunkBirthError(
            f"块 {index} 响应以 U+FEFF BOM 开头（模板为 ASCII，禁止 BOM）")
    # 步骤 2-3：split("\n") + 剥尾 CR + 整行三判定。
    lines, begins, ends, nucleus, begin_fields = _scan_fence_lines(raw)

    def fmt_ls(seq: list[int]) -> str:
        return ",".join(str(ln + 1) for ln in seq)  # 行号一律响应相对、1-based

    # 步骤 4：先全局计数再切 body（评审 M1 钉死；双 begin 单 end 必拒）。
    if not begins:
        if not nucleus:
            raise ChunkBirthError(
                f"块 {index} 缺起始围栏行（0 个 {CHUNK_FENCE_NUCLEUS} 核记行）")
        raise ChunkBirthError(
            f"块 {index} 起始围栏行不合规（核记行 {len(nucleus)} 行，"
            "合规 begin 0 行；检查字段顺序/引号/全角字符）")
    if len(begins) >= 2:  # 情形 3
        raise ChunkBirthError(
            f"块 {index} 起始围栏出现 {len(begins)} 次（需恰好 1 次，"
            f"行号 {fmt_ls(begins)}）")
    b_idx = begins[0]
    ends_after = [ln for ln in ends if ln > b_idx]
    if not ends_after:  # 情形 4（end 在 begin 之前同样落本形：其后 0 个 end）
        raise ChunkBirthError(
            f"块 {index} 缺结束围栏行（begin 行号 {b_idx + 1}，"
            "其后 0 个 end 行，按输出截断记录）")
    if len(ends) >= 2:  # 情形 5：全局 end 计数（body 内 end 已含在内）
        raise ChunkBirthError(
            f"块 {index} 结束围栏出现 {len(ends)} 次（需恰好 1 次，"
            f"行号 {fmt_ls(ends)}）")
    e_idx = ends_after[0]
    body_lines = lines[b_idx + 1:e_idx]
    for off, line in enumerate(body_lines):  # 情形 6
        if (_FENCE_BEGIN_LINE_RE.match(line) or _FENCE_END_LINE_RE.match(line)
                or CHUNK_FENCE_NUCLEUS in line):
            resp_line = b_idx + 1 + off + 1
            raise ChunkBirthError(
                f"块 {index} 代码体内第 {resp_line} 行出现围栏核记"
                "（围栏核记全响应只允许 2 行）")
    # 步骤 5：围栏外区域走 ASCII 专用空白谓词（禁用 str.strip()——
    # 它把 U+3000 当空白 fail-open；U+200B 同样在此被拦）。情形 7。
    bad_before = sum(
        1 for line in lines[:b_idx] if any(ch not in " \t" for ch in line))
    bad_after = sum(
        1 for line in lines[e_idx + 1:] if any(ch not in " \t" for ch in line))
    if bad_before or bad_after:
        raise ChunkBirthError(
            f"块 {index} 围栏外存在非空白正文（起前 {bad_before} 非空行/"
            f"尾后 {bad_after} 非空行，不摘录内容）")
    # 字段定序 path→index→total（情形 8/9）。
    path_v, index_v, total_v = begin_fields[b_idx]
    if CHUNK_FENCE_PATH_RE.fullmatch(path_v):
        if path_v != target:
            raise ChunkBirthError(
                f"块 {index} path 不符：收到 {path_v}，应为 {target}")
    else:
        raise ChunkBirthError(
            f"块 {index} path 含非白名单字符（长度 {len(path_v)}，不回显），"
            f"应为 {target}")
    for name, v, expected in (("index", index_v, index), ("total", total_v, total)):
        if not _FENCE_INT_RE.fullmatch(v):
            raise ChunkBirthError(
                f"块 {index} {name} 不合规整数字段（不回显），应为 {expected}")
        if int(v) != expected:
            raise ChunkBirthError(
                f"块 {index} {name} 不符：收到 {int(v)}，应为 {expected}")
    # body 定界（EBNF「取走恰好一个首部 LF / 尾部在 end_line 前恰好剥一个
    # LF」）：两个剥除都是语法分隔 LF——首部为 begin 行终止 LF、尾部为 end
    # 行前的分隔 LF，split("\n") 切行时结构性消费，join 后即代码原文，故尾部
    # 不得再剥（否则吞掉代码自身的末行换行）；仅容忍 begin 后恰好多一空行
    # （join 首部多出的一个 LF，至多一个，对齐 EBNF 空白容错边界）。
    code = "\n".join(body_lines)
    if code.startswith("\n"):
        code = code[1:]
    # 情形 16：正文零宽字符（响应相对行号）。围栏行上的零宽已打断整行
    # 正则，落情形 2/4；此处只可能命中正文行。
    for off, line in enumerate(body_lines):
        mw = _FENCE_ZEROWIDTH_RE.search(line)
        if mw:
            resp_line = b_idx + 1 + off + 1
            raise ChunkBirthError(
                f"块 {index} 第 {resp_line} 行含零宽字符 "
                f"U+{ord(mw.group(0)):04X}（模板为 ASCII）")
    # 情形 12：正文裸控制字符（正文相对行号；TAB 允许，CR 只允许行尾 CRLF）。
    for off, line in enumerate(body_lines):
        mc = _FENCE_CONTROL_RE.search(line)
        if mc:
            raise ChunkBirthError(
                f"块 {index} code 第 {off + 1} 行含裸控制字符 "
                f"U+{ord(mc.group(0)):04X}（TAB 允许；CR 只允许行尾 CRLF）")
    if not code or not code.strip():  # 情形 10
        raise ChunkBirthError(f"块 {index} code 为空或纯空白")
    if code.strip().startswith("```"):  # 情形 11（沿用现式判定）
        raise ChunkBirthError(
            f"块 {index} code 以 markdown 围栏开头（禁止 markdown 包裹）")
    if len(code) < CHUNK_MIN_CODE_CHARS:  # 情形 13
        raise ChunkBirthError(
            f"块 {index} code 长度 {len(code)} < {CHUNK_MIN_CODE_CHARS} chars")
    # run5（规格 §15.1-C/§15.3）：>18000 硬拒分支删除——围栏语法/空/800 下限/
    # CR/BOM/零宽/核记等 15+ 形全部不动；长度唯一分类在调用点之后的模块级
    # 纯函数 _classify_closed_body（18001–36000 可行 LF→record 原样接受，
    # C 空/L>36000→ChunkBirthError 真值外串）。
    return code


def _validate_assembled_chunks(code: str) -> None:
    """拼接整体校验：ast 可解析、顶层无重复 def/class 名、总长 ≥ 下限。"""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        # run4：只取异常类型与行/列，不附 str(exc)——其文本含模型源码行，
        # 会经块 0 反馈段回流下一轮 prompt（设计 §2.3 情形 14/MAJOR-1）。
        raise ChunkBirthError(
            f"拼接源码 ast.parse 失败（行 {exc.lineno} 列 {exc.offset}）："
            f"{type(exc).__name__}") from None
    seen: set[str] = set()
    dups: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in seen and node.name not in dups:
                dups.append(node.name)
            seen.add(node.name)
    if dups:
        # run4 三签 MAJOR-1：重名是模型自造 XID 标识符（CJK 合法、可藏整句
        # 注入），str(exc) 会经块 0 反馈段回流下一轮 prompt（设计 §2.3），
        # 故只报计数、逐字标识符一律不回显。
        raise ChunkBirthError(
            f"拼接源码顶层重复 def/class 名（硬失败，{len(dups)} 个重名；"
            "整轮重生时顶层名须唯一，机器原因不回显标识符）")
    if len(code) < CHUNK_MIN_ASSEMBLED_CHARS:
        raise ChunkBirthError(
            f"拼接总长 {len(code)} < {CHUNK_MIN_ASSEMBLED_CHARS} chars"
            "（若行数≥600 请核对字符密度后整轮重生）")
    # run6（§15.8-H2/H5）：末位闸——可执行入口守卫；tree 复用上方 parse。
    _validate_entry_guard(tree)


def _validate_entry_guard(tree):
    # run6 §15.8.2-H5：仅枚举顶层 If（禁整树遍历，否则穿透 def/class）；
    # 候选 test 必须恰为规范 Compare，守卫 body 经助手一命中即接受。
    for node in tree.body:
        if not isinstance(node, If):
            continue
        test = node.test
        if not (isinstance(test, Compare)
                and isinstance(test.left, Name)
                and test.left.id == "__name__"
                and isinstance(test.left.ctx, Load)
                and len(test.ops) == 1
                and isinstance(test.ops[0], Eq)
                and len(test.comparators) == 1):
            continue
        comp = test.comparators[0]
        if not (isinstance(comp, Constant) and type(comp.value) is str
                and comp.value == _GUARD_LITERAL):
            continue
        if _entry_guard_body_calls_main(node.body):
            return
    raise ChunkBirthError(_ENTRY_GUARD_REASON)


def _entry_guard_body_calls_main(stmts):
    # 守卫体语句层受控遍历（0 raise；禁手工栈，只许 for+递归）。
    for stmt in stmts:
        if isinstance(stmt, _SCOPE_NODES):
            continue  # 八类作用域整枝剪（def/class/lambda/推导式）
        if isinstance(stmt, BoolOp):
            continue  # BoolOp 死/活支静态不可判，整枝不认
        if (isinstance(stmt, Call) and isinstance(stmt.func, Name)
                and stmt.func.id == _MAIN
                and isinstance(stmt.func.ctx, Load)):
            return True
        if isinstance(stmt, _STMT_BODY_ONLY):
            if _entry_guard_body_calls_main(stmt.body):
                return True
            continue  # 永不进 orelse/.iter/.items
        if isinstance(stmt, Try):
            if _entry_guard_body_calls_main(stmt.body):
                return True
            continue  # handlers/orelse/finalbody 均不进
        if isinstance(stmt, IfExp):
            # 三元只认真分支 .body；.test/.orelse 不读
            if _entry_guard_contains(stmt.body):
                return True
            continue
        if _entry_guard_contains(stmt):
            return True
    return False


def _entry_guard_contains(node):
    # 表达式层受控泛下降（0 raise）：入口先对 node 自身做叶子判定（变异探针⑧）。
    if (isinstance(node, Call) and isinstance(node.func, Name)
            and node.func.id == _MAIN
            and isinstance(node.func.ctx, Load)):
        return True
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _SCOPE_NODES) or isinstance(child, BoolOp):
            continue
        if (isinstance(child, Call) and isinstance(child.func, Name)
                and child.func.id == _MAIN
                and isinstance(child.func.ctx, Load)):
            return True
        if isinstance(child, _STMT_BODY_ONLY):
            if _entry_guard_body_calls_main(child.body):
                return True
            continue
        if isinstance(child, Try):
            if _entry_guard_body_calls_main(child.body):
                return True
            continue
        if isinstance(child, IfExp):
            if _entry_guard_contains(child.body):
                return True
            continue
        if _entry_guard_contains(child):
            return True
    return False


def _classify_closed_body(code: str, index: int) -> "dict | None":
    """闭合围栏 code 体长度三分类（run5，规格 §15.1-A/B/E、§15.3-H2；纯函数）。

    L≤18000：现状，返回 None。18000<L≤36000：在可行 LF 切点集合
    C={c | code[c]=="\\n", max(800,L−18001)≤c≤min(18000,L−801)} 中取
    c*=argmin|c−(L−1)/2|（等距取小）；C 非空返回固定白名单 record，code
    原样接受——**碎片字符串从不离开本函数**（不构造、不返回 p0/p1），恒等式
    code[:c]+"\\n"+code[c+1:]==code 使切点对两个既有 "\\n".join 不可观测，
    codes 恒为 6 个逻辑块；C 空抛 ChunkBirthError（真值外串，细节进 detail）。
    L>36000：over_split_max 抛 ChunkBirthError。只允许抛 ChunkBirthError。

    确定性纪律：禁 datetime/random/任何 IO/任何持久态（kill-9 重放从调用
    缓存 raw 在本函数确定性重算，0 重付，§15.1-C）。AST 红线（§15.2-m5/
    §15.6，测试 ast.walk 钉死）：函数体无 import 节点、无 Attribute 节点
    （连 str.find/rfind/.count 也禁）、无 self；调用白名单
    len/min/abs/enumerate/range/int/sum（+lambda）；ast.Raise 恰 2 枚
    （C 空、类3 各一），落窗/恒等式用 assert。
    """
    length = len(code)
    if length <= CHUNK_MAX_CODE_CHARS:
        return None
    if length > CHUNK_CLOSED_SPLIT_MAX_CHARS:
        raise ChunkBirthError(
            f"块 {index} code 长度 {length} > 36000 chars（机器窗上限）",
            detail={
                "schema": "simpraxis.chunk_split_reject.v1",
                "chunk_index": index,
                "closed_chars": length,
                "class": "over_split_max",
                "window": None,
            })
    # lo=max(800, L-18001) 展开为 IfExp：AST 调用白名单（§15.2-m5）无 max。
    raw_lo = length - CHUNK_MAX_CODE_CHARS - 1
    lo = raw_lo if raw_lo > CHUNK_MIN_CODE_CHARS else CHUNK_MIN_CODE_CHARS
    hi = min(CHUNK_MAX_CODE_CHARS, length - CHUNK_MIN_CODE_CHARS - 1)
    cands = [j for j in range(lo, hi + 1) if code[j] == "\n"]
    if not cands:
        raise ChunkBirthError(
            f"块 {index} code 长度 {length} > 18000 chars，请将该块整体缩短至"
            " 18000 字符以内后整轮重发（本机逐字符计数）",
            detail={
                "schema": "simpraxis.chunk_split_reject.v1",
                "chunk_index": index,
                "closed_chars": length,
                "class": "no_line_boundary",
                "window": [lo, hi],
            })
    # 整数键避免浮点：|2c−(L−1)| 与 |c−(L−1)/2| 同序；等距对在第二键取小 c。
    c = min(cands, key=lambda j: (abs(2 * j - (length - 1)), j))
    frag0 = c
    frag1 = length - c - 1
    cut_line = sum(1 for j, ch in enumerate(code) if j < c and ch == "\n")
    assert CHUNK_MIN_CODE_CHARS <= frag0 <= CHUNK_MAX_CODE_CHARS
    assert CHUNK_MIN_CODE_CHARS <= frag1 <= CHUNK_MAX_CODE_CHARS
    assert code[:c] + "\n" + code[c + 1:] == code
    return {
        "schema": "simpraxis.chunk_split.v1",
        "chunk_index": index,
        "closed_chars": length,
        "cut_offset": c,
        "cut_line": cut_line,
        "frag0_chars": frag0,
        "frag1_chars": frag1,
        "boundary": "line",
    }


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------

def _kb_block(hits: list[dict], *, label: str = "S",
              empty_ns: str = "troubleshooting") -> str:
    # 攢批94：label/empty_ns 参数化（额外 capabilities 路用 C 前缀，避免与
    # troubleshooting 的 S1..S6 编号混淆）；默认值与旧模板逐字节一致。
    if not hits:
        return f"（{empty_ns} 检索 0 行；不得据此编造 KB 引用）"
    lines = []
    for i, h in enumerate(hits, 1):
        src = f"{h.get('kb_relpath') or '?'}#{h.get('section_path') or ''}"
        refs = h.get("evidence_refs") or []
        ref_str = (f" | evidence_refs: {', '.join(map(str, refs))}"
                   if refs else "")
        lines.append(f"[{label}{i} 来源: {src}{ref_str}]\n"
                     f"{(h.get('text') or '').strip()}")
    return "\n\n".join(lines)


# 攢批94 锁内适配：coding 站检索串错误签名清洗。run1–7 实测 tail[-500:] 被
# Genesis/torch 版本警告、litellm 注册噪声与 Traceback 帧行主导，相关 KB
# （如 Q17 Scene is already built）掉出 troubleshooting top-6；剥离噪声后
# 以异常签名行做 embedding 查询，活库实测 Q17 回到 #0（取证：
# phase2/task8_diag/recall_probe/probe94.py，0 付费）。
_CODING_HINT_NOISE_RE = re.compile(
    r"(litellm|provider list|register_model|cost map|cost fields|"
    r"pendingdeprecation|deprecation|future version|warnings?\.warn\(|"
    r"upgrade pytorch|torch<[0-9]|tqdm|[0-9]+%\|)", re.I)


def _clean_coding_hint(tail: str) -> str:
    """从 exec tail 提取错误签名拼 ``仿真失败 <sig>``（≤300 字符）。

    纯函数、确定性：丢弃空行、``File ``/``Traceback`` 帧行与警告噪声行；
    取最后一条含 Error/Exception/Fatal/Failure/assert 的行；该行把真实
    错误消息包在引号内时（GenesisException 元组形）取最像错误签名的引号
    串。无任何可用行时退回旧形态 tail 切片（fail-safe，不改检索语义）。
    """
    kept: list[str] = []
    for raw in (tail or "").splitlines():
        ln = raw.strip()
        if not ln or ln.startswith(("File ", "Traceback", "During handling")):
            continue
        if _CODING_HINT_NOISE_RE.search(ln):
            continue
        kept.append(ln)
    sig = ""
    for ln in reversed(kept):
        if re.search(r"Error|Exception|Fatal|Failure|assert", ln):
            sig = ln
            break
    if not sig:
        if kept:
            sig = kept[-1]
        else:
            return "仿真失败 " + (tail or "")[-200:]
    quotes = re.findall(r"['\"]([^'\"]{8,200})['\"]", sig)
    if quotes:
        sig = next((s for s in quotes if re.search(
            r"already|missing|not |no |failed|error|invalid|unexpected|must|"
            r"cannot", s, re.I)), quotes[0])
    return ("仿真失败 " + sig)[:300]


def _learning_block(learning_hits: "list[dict] | None") -> str:
    """【实战演化避坑】段（Phase 2 Task1）：只渲染学习表真实行，逐字引用。

    空/None → ""（调用方整段不拼，0 命中时 prompt 不出现避坑标题；
    learning_hits=None 时消息与 Phase 1 一致，旧测试零改动）。补丁片段只能
    来自这些真实行的 text（含修复动作/人工意见原文），不得合成 KB 引用。
    """
    if not learning_hits:
        return ""
    lines = ["以下条目逐字摘自历史真实运行学习表 phase1_learnings，必须作为"
             "避坑约束对照；补丁只能引用这些真实条目，不得编造其中不存在的"
             "修复或 KB 引用："]
    for i, h in enumerate(learning_hits, 1):
        lines.append(
            f"[L{i} 来源 learning_id={h.get('id', '?')} "
            f"run_id={h.get('run_id', '?')} "
            f"gate_source={h.get('gate_source', '?')} "
            f"rung={h.get('rung', '?')}]\n{str(h.get('text', ''))}")
    return "\n\n".join(lines)


def _chunk_section(ctx: "ChunkPromptContext") -> str:
    """渲染《拆块交付（第 i+1/n 块）》段（锁内适配，见 ChunkSpec 注释）。

    锁内适配（计划 v1.2 外，seq42-47 拆块出生，用户 2026-10-20 裁决）：
    清单（编号+标题+行数预算/边界）、本块边界、前序块逐字定稿围栏、上一轮
    机器失败反馈（仅该轮块 0）、交付格式五部分。run4（2026-10-21）起交付格式
    由严格 chunk JSON 改为 sentinel 围栏纯文本（设计 §5.1），前四部分不变。
    """
    i, n = ctx.index, ctx.total
    lines = [f"## 拆块交付（第 {i + 1}/{n} 块）", "",
             f"目标文件分 {n} 块顺序出生，你正在交付第 {i + 1}/{n} 块；"
             "本次只允许产出这一块。", "",
             "### 交付块清单（编号、标题、行数预算与边界）"]
    for j, title in enumerate(ctx.titles):
        lines.append(f"{j + 1}. 《{title}》")
        lines.append(ctx.boundaries[j])
    lines += [
        "",
        "### 本块边界（只允许写的章节）",
        f"你本次只能产出第 {i + 1}/{n} 块《{ctx.titles[i]}》："
        f"{ctx.boundaries[i]}",
        "严禁在本块 code 中写入任何其他块的章节，也不得重复前序已定稿内容。",
    ]
    if ctx.previous_code:
        lines += [
            "",
            "### 前序已定稿块（逐字拼接文本，已最终定稿，禁止重复/改动）",
            f"以下是前 {i} 块拼接后的逐字定稿，你的 code 会被直接拼在其后；"
            "拼接结果必须是合法 Python：不得重复其中 import/常量/函数，"
            "不得改动或重排其中任何字符。",
            "```python",
            ctx.previous_code,
            "```",
        ]
    if ctx.round_feedback:
        lines += [
            "",
            "### 上一轮拆块交付失败反馈（机器原因，本轮必须避免）",
            ctx.round_feedback,
        ]
    lines += [
        "",
        "### 交付格式（围栏纯文本，禁止 JSON）",
        "本块 Python 原文直接书写，**不要**放入 JSON、不要做任何转义；"
        "反斜杠、引号、三引号、中文与换行都按 Python 源码原样写。"
        "你的整条响应必须且只能是下面三个部分：首行为起始围栏（独占一行）、"
        "中间为本块 Python 原文（若干行）、末行为结束围栏（独占一行）；"
        "字段逐字照抄、顺序固定、ASCII 字符：",
        f"<<<<<<< simpraxis.chunk.v1 path={ctx.target_path} "
        f"index={i} total={n}",
        "<本块 Python 原文，逐字；不要 markdown 围栏、不要解释>",
        ">>>>>>> simpraxis.chunk.v1.end",
        f"- path 逐字等于 {ctx.target_path}；index 逐字等于 {i}；"
        f"total 逐字等于 {n}；三个字段顺序不得调换、不加引号；",
        "- 起始围栏之前、结束围栏之后不允许任何字符（空白与换行除外）；"
        "代码体内不允许出现围栏行或 simpraxis.chunk.v1 字样；",
        "- 若响应在结束围栏之前结束（例如输出被截断），本机记录为"
        "「缺结束围栏」，本块机器校验失败；",
        # 锁内适配：模型面一行，用户裁决②（2026-10-22 run5 定向二拆）。
        # 只陈述真实硬闸（<800/>36000/缺围栏）；18001-36000 多行件机器
        # 二拆为内部机制，不披露（规格 §15.3-H1）。
        f"- code 只含本块 Python 原文，请控制在 {CHUNK_MAX_CODE_CHARS} 字符以内"
        "（本机逐字符计数）；少于 "
        f"{CHUNK_MIN_CODE_CHARS} 字符、超过 {CHUNK_CLOSED_SPLIT_MAX_CHARS} 字符"
        "或缺结束围栏，仍机器校验失败；",
        f"- 全部 {n} 块拼接后须为完整可运行文件（总长不少于 "
        f"{CHUNK_MIN_ASSEMBLED_CHARS} 字符），块边界不得重叠。",
        "",
    ]
    return "\n".join(lines)


def _build_messages(*, gate2_notes: Optional[str], traceback_tail: str,
                    snippets: list[dict], hits: list[dict],
                    cmd: list[str],
                    learning_hits: "list[dict] | None" = None,
                    # 锁内适配（计划 v1.2 外，seq42-47 拆块出生，用户
                    # 2026-10-20 裁决）：两新参 None/空时输出与旧版逐字一致。
                    extra_sections: "tuple[tuple[str, str], ...] | None" = None,
                    chunk_context: "ChunkPromptContext | None" = None,
                    # 攢批94 锁内适配（Task8 流体，2026-10-27）：额外 KB
                    # 检索路段 ``(namespace, top_k, hits)``；None 时 prompt
                    # 与旧版逐字一致（字符级快照测试锁定）。
                    extra_kb_sections: "tuple[tuple[str, int, list[dict]], ...] | None" = None,
                    ) -> list[dict[str, str]]:
    # v1.4 §2.5 条件拼装：出生轮（chunk_context 非 None）system 逐字保持
    # 现行原文（出生环连续四跑成功路径零变化，快照测试锁逐字不变）；修复轮
    # （None）双通道文案——JSON 通道保留原文语义，追加 replace 围栏格式
    # （含示例）与「输出前先确认选了哪种通道」句。
    if chunk_context is None:
        system = (
            "你是具身智能仿真工具链的排障 coding agent。输出前先确认选了哪种"
            "通道。通道一（JSON，只允许输出严格 JSON）："
            '{"patches":[{"path":"相对运行工作目录的路径","old":"原文（须唯一）",'
            '"new":"替换文"}]}，或 {"unified_diff":"git 风格 diff"}。'
            "通道二（simpraxis.replace.v1 围栏整替函数，优先）示例：\n"
            "<<<<<<< simpraxis.replace.v1 path=脚本文件.py func=函数名 "
            "[class=类名]\n"
            "<该函数含装饰器行的完整新源码>\n"
            "=======\n"
            ">>>>>>> simpraxis.replace.v1.end\n"
            "首行 path/func 逐字给相对路径与函数名，class= 可省略（函数名在"
            "文件中多处定义时用它消歧）；中间是完整新源码；======="
            "是可选分隔行；末行结束围栏；三行各独占一行。"
            "补丁只能改本次运行工作目录内的文件；禁止改配置、凭据、仓库源码。"
            "不要输出解释、不要输出 image。")
    else:
        system = (
            "你是具身智能仿真工具链的排障 coding agent。只允许输出严格 JSON："
            '{"patches":[{"path":"相对运行工作目录的路径","old":"原文（须唯一）",'
            '"new":"替换文"}]}，或 {"unified_diff":"git 风格 diff"}。'
            "补丁只能改本次运行工作目录内的文件；禁止改配置、凭据、仓库源码。"
            "不要输出解释、不要输出 image。")
    notes_block = ""
    if gate2_notes and gate2_notes.strip():
        notes_block = (
            "## 人工评审意见（必须逐条响应，最高优先级）\n"
            f"{gate2_notes.strip()}\n")
    snippet_block = "\n\n".join(
        f"### {s['file']}:{s['line']}\n{s['snippet']}" for s in snippets) \
        or "（traceback 帧均在工作目录外，无源片段可读）"
    # Phase 2 Task1：避坑段仅在 learning 路有真实命中时出现，置于 kb 段之后；
    # 0 命中不拼标题（禁止据此编造 fix/KB 引用）。
    learning_section = ""
    learning_text = _learning_block(learning_hits)
    if learning_text:
        learning_section = "\n\n## 实战演化避坑（phase1_learnings 真实行，逐字引用）\n" \
            + learning_text
    # 锁内适配（计划 v1.2 外，seq42-47 拆块出生，用户 2026-10-20 裁决）：
    # extra 段位于 notes 之后、失败命令之前；chunk 段紧随其后。两参数为空时
    # 两个 block 都是 ""，拼接结果与旧模板逐字一致（字符级回归测试锁定）。
    extra_block = ""
    if extra_sections:
        for title, body in extra_sections:
            extra_block += f"## {title}\n{body}\n\n"
    chunk_block = _chunk_section(chunk_context) + "\n" if chunk_context else ""
    # 攢批94：额外 KB 路（capabilities API 参考）紧随 troubleshooting 段、
    # 避坑段之前；None 时整段为 ""，与旧模板逐字节一致。
    extra_kb_section = ""
    if extra_kb_sections:
        rendered = []
        for ns, ns_top_k, ns_hits in extra_kb_sections:
            rendered.append(
                f"## {ns} 知识库命中（API 参考；namespace={ns}, "
                f"top_k={ns_top_k}）\n"
                + _kb_block(ns_hits, label="C", empty_ns=ns))
        extra_kb_section = "\n\n" + "\n\n".join(rendered)
    user = (
        f"{notes_block}{extra_block}{chunk_block}"
        "## 失败命令\n" + " ".join(cmd) + "\n\n"
        "## traceback / 日志尾部\n" + traceback_tail + "\n\n"
        "## 相关源文件片段（仅限运行工作目录）\n" + snippet_block + "\n\n"
        "## troubleshooting 知识库命中（namespaces=troubleshooting, top_k=6）\n"
        + _kb_block(hits) + extra_kb_section + learning_section)
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def _tb_tail(exec_result: _ExecResult) -> str:
    blob = (exec_result.stderr or "") + "\n" + (exec_result.stdout or "")
    return blob[-TB_TAIL_CHARS:]


def _append_rc0_missing_artifact_note(tail: str, relpath: str) -> str:
    # run6 §15.7-H4（D2）：rc=0 缺约定产物时的机器核验真值句。纯 str、幂等
    # （节点重放不重复注入）；relpath 只由调用方传 spec.telemetry_relpath。
    note = _RC0_MISSING_ARTIFACT_FMT.format(relpath=relpath)
    if note in tail:
        return tail
    return tail + "\n\n" + note


def _neutralize_for_model(reason: str) -> str:
    """v1.0 MAJOR-1：仅用于模型面 reason；全 splitlines 分隔符折叠单空格。

    人侧 str(exc) 永不走此中和（decision/timeline/FAILURE RAW）。
    """
    for sep in _ANCHOR_LINE_SEPS:
        reason = reason.replace(sep, " ")
    return re.sub(r" {2,}", " ", reason)


def _anchor_note(exc: "PatchAnchorError") -> str:
    """run7b：锚点拒绝的机器核验反馈（模型面；reason 经换行中和）。"""
    hint = _ANCHOR_HINT_ZERO if exc.hits == 0 else _ANCHOR_HINT_MULTI
    return _ANCHOR_REJECT_FMT.format(reason=_neutralize_for_model(str(exc)), hint=hint)


def _append_anchor_feedback(tail: str, note: str) -> str:
    # run7b：幂等同 rc0 note（节点重放不重复注入）；标记句取注记首句。
    _MARK = "机器核验（非模型输出）：上一轮返回的补丁已被整体拒绝"
    if _MARK in tail:
        return tail
    return tail + "\n\n" + note


# v1.4（设计 §2.4）：transport 拒绝的机器核验反馈。{reason} 独占一条物理行
# （redact_for_model 行级丢弃纪律同 _ANCHOR_REJECT_FMT；MINOR-9 戒律范围
# 仅锁 _ANCHOR_REJECT_FMT 与两 hint——本 FMT 受设计 §2.4 逐字文本约束，
# 保留 {"patches":[...]} 形的 ASCII 引号：不匹配 _FRAME_RE 帧形、无六禁词）。
_TRANSPORT_REJECT_FMT = (
    "机器核验（非模型输出）：上一轮未产出可应用补丁（机器原因：{reason}）。\n"
    "允许且优先：simpraxis.replace.v1 围栏整替函数（格式见 system）；\n"
    "或 JSON {\"patches\":[...]} / {\"unified_diff\":...}。")


def _transport_note(exc: "PatchSafetyError") -> str:
    """v1.4：transport 拒绝的模型面注记（reason 经换行中和；同锚点族）。

    用定向 replace 而非 str.format：FMT 尾行含设计逐字的
    {"patches":[...]} 字面花括号，format 会把它当格式场报 KeyError。
    """
    return _TRANSPORT_REJECT_FMT.replace(
        "{reason}", _neutralize_for_model(str(exc)))


def _append_transport_feedback(tail: str, note: str) -> str:
    # v1.4：幂等同锚点/rc0 note（节点重放不重复注入）；标记句取注记首句。
    _MARK = "机器核验（非模型输出）：上一轮未产出可应用补丁"
    if _MARK in tail:
        return tail
    return tail + "\n\n" + note


# ---------------------------------------------------------------------------
# run7b 提交 B：rc0 批次结构闸模型面（锁内适配（批次结构闸，用户 2026-10-23
# 评审改闸授权））。人侧 timeline RAW 全量/不截断/不去重/不脱敏，绝不走本通道。
# 本块零 raise/零 try/零新顶层 import；re 复用顶部既有导入。
# ---------------------------------------------------------------------------

MODEL_SCHEMA_FACE_CAP = 8

# 两形归一化模板（溯源 gait_rungs.validate_telemetry_structure :181 与
# :183-184 的实际发射；runner 不跨模块导入 ENGINE 等常量，期望值一律用捕获
# 组从校验器自己发射的常量位置回填，禁止手写 1.3.3 等字面）。两形均须
# flags=re.S：engine 形「实际」尾部模型值未经 repr，可含真实换行/tab。
_SCHEMA_FACE_SCHEMA_RE = re.compile(
    r"^(?P<run>normal|ice_nominal|ice_dr)_s\d+: schema=.*?，要求 "
    r"(?P<want>\S+)$", flags=re.S)
_SCHEMA_FACE_ENGINE_RE = re.compile(
    r"^(?P<run>normal|ice_nominal|ice_dr)_s\d+: engine/version 必须是 "
    r"(?P<eng>\S+) (?P<ver>\S+)，实际.*$", flags=re.S)


def _schema_face_normalize(line: str) -> str:
    """模型面专用：仅两形模型可控值模板做固定改写，模型值整段丢弃；其余原样。"""
    m = _SCHEMA_FACE_SCHEMA_RE.match(line)
    if m:
        return f"{m.group('run')}: schema 不等于约定值（要求 {m.group('want')}）"
    m = _SCHEMA_FACE_ENGINE_RE.match(line)
    if m:
        return (f"{m.group('run')}: engine/version 必须是 {m.group('eng')} "
                f"{m.group('ver')}")
    return line


def _sanitize_schema_face(line: str) -> str:
    """通用净化：ASCII 双引号→单引号；剔除其余 ASCII 控制符（\\x00-\\x1f）。"""
    line = line.replace('"', "'")
    return "".join(c for c in line if ord(c) >= 32)


def _scrub_workdir(line: str, workdir) -> str:
    """仅模型面 bullet：绝对 workdir 前缀折叠（带斜杠形先行，避免 '.' 尾巴）。"""
    return line.replace(str(workdir) + "/", "").replace(str(workdir), ".")


def _dedup_schema_face(lines: list[str]) -> tuple[list[str], int]:
    """对归一化+净化+scrub 后的面文保序去重；键取首个 ': ' 之后的消息模板。

    run_id 不含 ': '，全角「：」不参与；缺件/缺列/seeds 文案各自成类。
    """
    seen: set[str] = set()
    uniq: list[str] = []
    for line in lines:
        key = line.split(": ", 1)[1] if ": " in line else line
        if key not in seen:
            seen.add(key)
            uniq.append(line)
    return uniq, len(uniq)


def _build_schema_face(raw: list[str], *, workdir) -> tuple[list[str], int]:
    """RAW → 模型面：逐条归一化→净化→scrub，再保序去重，返回 (各类首例, M)。"""
    faces = [
        _scrub_workdir(_sanitize_schema_face(_schema_face_normalize(line)),
                       workdir)
        for line in raw
    ]
    return _dedup_schema_face(faces)


_SCHEMA_NOTE_HEAD_FMT = (
    "机器核验（非模型输出）：上一轮脚本 rc=0，但批次结构闸未通过"
    "（共 {n} 条，去重 {m} 类；下为各类首例）：")
_SCHEMA_OMIT_FMT = (
    "- ……（其余 {omitted} 类略，完整机器证据见 timeline/FAILURE）")
_SCHEMA_REPAIR_HEAD = (
    "修复要求（仅结构修正，不得改阈值、随机种子、通过判据或帧数设置）：")
_SCHEMA_REPAIR_BULLETS = (
    "- outcomes 中的左右脚量（slip_dist_m、slip_v_max_mps、"
    "slip_v_max_postpush_mps、contact_force_peak_N、contact_frames）"
    "必须是嵌套对象，即 R 与 L 两个键的对象，与同目录 V1 种子 "
    "genesis_humanoid_dr.py 的 outcomes 块形状一致。",
    "- 扁平后缀键（如 slip_dist_m_R、contact_frames_L）只允许出现在 "
    "CSV 行里；telemetry JSON 必须嵌套。",
    "- 只修正输出结构后重跑 batch，不要改动实验语义。",
)
_READFAULT_REPAIR_HEAD = (
    "修复要求（仅产物修复，不得改阈值、随机种子、通过判据或帧数设置）：")
_READFAULT_REPAIR_BULLET = (
    "- 请重新完整产出该批次产物（summary.json、telemetry/*.json、"
    "dr_controls_vs_experimental.csv），保证文件存在且为可解析的约定格式"
    "后重跑 batch。"
)


def _append_batch_schema_note(tail: str, raw: list[str], n_true: int, *,
                              read_fault: bool, workdir) -> str:
    """把批次结构闸结果装配成模型面注记（两形只看 read_fault bool 分流）。

    有意不做标记式幂等检查：tail 含不可信模型 stdout，标记检查可被回声
    抑制；每轮恰好一次由控制流保证（迭代内重建）。字句见设计件 §5 快照。
    """
    faces, m_total = _build_schema_face(list(raw), workdir=workdir)
    parts = [_SCHEMA_NOTE_HEAD_FMT.format(n=n_true, m=m_total)]
    parts.extend("- " + face for face in faces[:MODEL_SCHEMA_FACE_CAP])
    if m_total > MODEL_SCHEMA_FACE_CAP:
        parts.append(_SCHEMA_OMIT_FMT.format(
            omitted=m_total - MODEL_SCHEMA_FACE_CAP))
    if read_fault:
        parts.append(_READFAULT_REPAIR_HEAD)
        parts.append(_READFAULT_REPAIR_BULLET)
    else:
        parts.append(_SCHEMA_REPAIR_HEAD)
        parts.extend(_SCHEMA_REPAIR_BULLETS)
    return tail + "\n\n" + "\n".join(parts)


_SHIM_NOTE_HEAD = (
    "机器核验（非模型输出）：上一轮脚本 rc=0，但 host-shim 证据双闸未通过"
    "（与 Gate2 构卡前同款机验；违例原文如下）：")
_SHIM_REPAIR_HEAD = (
    "修复要求（仅进程拓扑收敛，不得改阈值、随机种子、通过判据、帧数设置、"
    "host-shim 本体或 9/2 契约数字，不得过滤/吞掉 stderr）：")
_SHIM_REPAIR_BULLETS = (
    "- 契约（Gate2 构卡前双闸单真值）：末次 rc0 批次的 stderr 窗口内恰 9 条 "
    "site=torch atexit 行（9 个 rollout 子进程）+ 恰 2 条 site=idle 行"
    "（V2 batch 父 + gpu_sampler）。",
    "- 只收敛 V2 自增的包装/守护进程使 site=idle 回到 2；禁止改契约数字、"
    "禁止加大窗口、禁止在 V2 内屏蔽或稀释 host-shim 输出。",
    "- 修完重跑 batch；拓扑对齐后同款双闸自然放行至 Gate2 构卡。",
)


def _append_shim_topology_note(tail: str, errors: "list[str]") -> str:
    """把 host-shim 双闸违例装配成模型面注记（锁内适配，用户 2026-10-25）。

    违例文本＝Gate2 GraphStateError 原文（人侧 timeline/模型侧注记同源机器
    证据）。有意不做标记式幂等检查（同 _append_batch_schema_note：tail 含
    不可信模型 stdout，标记检查可被回声抑制）；每轮恰一次由控制流保证。
    """
    parts = [_SHIM_NOTE_HEAD]
    parts.extend("- " + err for err in errors)
    parts.append(_SHIM_REPAIR_HEAD)
    parts.extend(_SHIM_REPAIR_BULLETS)
    return tail + "\n\n" + "\n".join(parts)


def _coding_cap_reason(spec: RunSpec, anchor_exc: "Optional[PatchAnchorError]" = None,
                       *, transport_exc: "Optional[PatchSafetyError]" = None) -> str:
    """run7b：轮帽文案唯一收口；基串逐字同旧文案，锚点后缀与 chunk 后缀互斥。

    v1.4（设计 §2.4）：transport_exc 为 keyword-only 新参；anchor_exc 给定
    时锚点后缀优先（互斥，与既有调用字节一致），否则 transport 后缀
    「；末次补丁传输机器原因：{exc}」。
    """
    reason = f"{spec.max_coding_rounds} 轮 coding_debug 后仍失败（RunSpec 上限）"
    if anchor_exc is not None:
        reason += f"；末次锚点机器原因：{anchor_exc}"
    elif transport_exc is not None:
        reason += f"；末次补丁传输机器原因：{transport_exc}"
    return reason


# ---------------------------------------------------------------------------
# v1.4 §2.3 IDE 式源码上下文注入（设计稿 §2.3）：修复轮反馈段附涉案函数
# 当前完整源码（含装饰器行）。纯本地计算（ast/read_text），0 网关触碰；
# 注入位置 rc0→anchor→schema→transport→函数源码→redact（G10 扩展序）由
# run() 装配点钉住；本函数只产段文本。
# ---------------------------------------------------------------------------

_FIELD_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")  # len>=3 ASCII 标识符
_FRAME_FUNC_RE = re.compile(r",\s+in\s+([A-Za-z_][A-Za-z0-9_]*)")


def _schema_field_names(schema_lines) -> set:
    """schema 失败行 → 字段名词干（首个 ': ' 之后的 ASCII 标识符 token，len>=3 过滤噪声）。"""
    names: set = set()
    for line in schema_lines:
        body = line.split(": ", 1)[1] if ": " in line else line
        names.update(_FIELD_TOKEN_RE.findall(body))
    return names


def _frame_func_names(traceback_tail: str) -> set:
    """traceback 尾 → 帧函数名集合（`File \"x\", line N, in f` 形；<module> 不匹配）。"""
    return set(_FRAME_FUNC_RE.findall(traceback_tail or ""))


def _iter_workdir_functions(workdir):
    """workdir 树内 *.py 的模块级 def + ClassDef 直属方法（嵌套 def 不在域；排序稳定）。"""
    wd = Path(workdir).resolve()
    for dirpath, dirnames, filenames in os.walk(wd, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for fname in sorted(filenames):
            if not fname.endswith(".py"):
                continue
            p = Path(dirpath) / fname
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            for node in tree.body:
                if isinstance(node, (FunctionDef, AsyncFunctionDef)):
                    yield str(p.relative_to(wd)), node.name, node.name, node, text
                elif isinstance(node, ClassDef):
                    for m in node.body:
                        if isinstance(m, (FunctionDef, AsyncFunctionDef)):
                            yield (str(p.relative_to(wd)), m.name,
                                   f"{node.name}.{m.name}", m, text)


def collect_repair_functions(workdir, *, schema_lines=(),
                             traceback_tail: str = "") -> str:
    """v1.4 §2.3：涉案函数当前完整源码注入段（0 命中返回空串，不新增报错面）。

    命中启发（设计 §2.3 校正版）：函数名 ∈ traceback 帧函数名，或函数子树
    内 ast.Constant 字符串值与 schema 失败字段名互为 substring（任一方向，
    len>=3 双端过滤——slip_dist_m 命中 slip_dist_m_R 键即此形）。按行数
    升序取前 2 个函数；整函数注入禁止截断，FUNCTION_SOURCE_TOTAL_CHARS
    是硬预算：单函数超预算跳过并注记「函数过大未注入，请用 JSON 通道」；
    两函数合计超预算只注入较小者（较小者自身超预算 → 同跳过注记）。
    """
    fields = _schema_field_names(list(schema_lines or ()))
    frames = _frame_func_names(traceback_tail or "")
    if not fields and not frames:
        return ""
    hits: "list[tuple[int, str, str, str]]" = []  # (nlines, rel, label, source)
    for rel, base, label, node, text in _iter_workdir_functions(workdir):
        hit = base in frames
        if not hit and fields:
            for sub in ast.walk(node):
                if not isinstance(sub, Constant) or not isinstance(sub.value, str):
                    continue
                cv = sub.value
                if len(cv) >= 3 and any(cv in f or f in cv for f in fields):
                    hit = True
                    break
        if not hit:
            continue
        start, end = _function_span(text, node)
        nstart = (node.decorator_list[0].lineno
                  if node.decorator_list else node.lineno)
        hits.append((node.end_lineno - nstart + 1, rel, label, text[start:end]))
    if not hits:
        return ""
    hits.sort(key=lambda h: (h[0], h[1], h[2]))  # 行数升序，小者优先
    selected = hits[:2]
    notes: "list[str]" = []
    to_inject: "list[tuple[str, str, str]]" = []
    if len(selected) == 1:
        _nl, rel, label, source = selected[0]
        if len(source) > FUNCTION_SOURCE_TOTAL_CHARS:
            notes.append("函数过大未注入，请用 JSON 通道")
        else:
            to_inject.append((rel, label, source))
    else:
        _n0, r0, l0, s0 = selected[0]
        _n1, r1, l1, s1 = selected[1]
        if len(s0) + len(s1) <= FUNCTION_SOURCE_TOTAL_CHARS:
            to_inject.append((r0, l0, s0))
            to_inject.append((r1, l1, s1))
        elif len(s0) <= FUNCTION_SOURCE_TOTAL_CHARS:
            to_inject.append((r0, l0, s0))  # 只注入较小者
        else:
            notes.append("函数过大未注入，请用 JSON 通道")
    parts = [
        "## 涉案函数当前源码（机器注入，供整替参考）",
        "以下为机器从当前工作目录源码提取的完整函数定义（含装饰器行），"
        "优先用 simpraxis.replace.v1 围栏整替（class= 用法见 system）："]
    for rel, label, source in to_inject:
        parts.append(f"### {rel} :: {label}")
        parts.append("```python")
        parts.append(source)
        parts.append("```")
    parts.extend(notes)
    return "\n".join(parts)


def _detect_workdir_hook_pollution(workdir) -> "list[str] | None":
    """run7 §3.6 卫生预检：workdir 顶层非递归三件同名件 + pycache pyc。

    MINOR 卫生项（非安全门——真实激活形经 §2.3 d1 实验证明不可劫持）：命中
    返回排序后的命中名（pyc 为相对路径），无命中返回 None。不递归子目录。
    """
    workdir = Path(workdir)
    hits: list[str] = []
    try:
        top_names = set(os.listdir(workdir))
    except OSError:
        return None
    hits.extend(sorted(n for n in _WORKDIR_HOOK_BASENAMES if n in top_names))
    pycache = workdir / "__pycache__"
    if pycache.is_dir():
        for stem in _WORKDIR_HOOK_STEMS:
            for pyc in sorted(pycache.glob(f"{stem}*.pyc")):
                hits.append(str(pyc.relative_to(workdir)))
    return hits or None


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class AgentRunner:
    def __init__(self, gateway: ModelGateway, vector_engine: VectorEngine, *,
                 evidence_root: "Path | None" = None,
                 subprocess_runner: "SubprocessRunner | None" = None,
                 evidence_writer: "Callable | None" = None,
                 call_cache: "CallReplay | None" = None,
                 recall: "ProductionRecall | None" = None):
        self.gateway = gateway
        self.vector_engine = vector_engine
        self._evidence_root = evidence_root
        self._run = subprocess_runner or _default_subprocess
        self._write_evidence = evidence_writer  # 测试注入：(root, stem, kind, payload)->ref
        # Phase 2 Task1：None=Phase 1 行为（revert 单文件回滚）；注入后启用
        # learning 双路召回 + 采纳遥测（coding 站，site="coding"）。
        self.recall = recall
        # L-1：None=调用方未注入；默认目录 phase1_root()/.call_cache 延迟到
        # 首用（且 spec.run_idx 非 None）才建，避免 Task4 直连在 /mnt 留件。
        self._call_cache: "Optional[CallReplay]" = call_cache

    def _replay_cache(self, spec: "RunSpec") -> "Optional[CallReplay]":
        """L-1：仅 run_idx 显式给出时启用（稳定 key coding_{t}_r{idx}_round{n}
        的前提）；默认缓存目录延迟创建。run_idx=None 一律不查不写。"""
        if spec.run_idx is None:
            return None
        if self._call_cache is None:
            self._call_cache = CallReplay(phase1_root() / ".call_cache")
        return self._call_cache

    @staticmethod
    def _usage_ints(usage: Any) -> "Optional[tuple[int, int]]":
        """真实网关返回两个非负 int；fake/无计量网关可能给 (None, None)——
        此类结果不入重放缓存（CallReplay.put 不接受非 int）。"""
        if (isinstance(usage, (list, tuple)) and len(usage) == 2
                and all(isinstance(v, int) and not isinstance(v, bool)
                        and v >= 0 for v in usage)):
            return int(usage[0]), int(usage[1])
        return None

    def _root(self, spec: RunSpec) -> Path:
        if self._evidence_root is not None:
            d = self._evidence_root / spec.thread_id
        else:
            d = _default_evidence_root(spec.thread_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _evidence(self, root: Path, stem: str, kind: "str | None",
                  payload: Any) -> str:
        if self._write_evidence is not None:
            return self._write_evidence(root, stem, kind, payload)
        # 失败镜像件名为单后缀 failure.json（kind=None；历史卷 failure.json.json
        # 为冻结证据不改名，本修复只影响今后新写件）。
        mid = f".{kind}" if kind is not None else ""
        p = root / f"{stem}{mid}.json"
        # S-6：write→chmod 收口 fsutil 原子写（mkstemp 0600→fsync→replace）。
        atomic_write_text(p, json.dumps(payload, ensure_ascii=False, indent=2))
        return f"phase1/task4_runner/{root.name}/{p.name}"

    @staticmethod
    def _count_round_fragment_files(root: Path, coding_round: int) -> int:
        """数某出生轮已写盘 fragments 件（observe-only，§15.3-H2）。

        record 非 None 即写一件，件名 coding_round{r}.chunk{i}.fragments.json；
        正则锚定轮号（防 round5 前缀误配 round15）。仅供时间线计数器，成功
        判据永不引用。
        """
        name_re = re.compile(
            rf"^coding_round{coding_round}\.chunk\d+\.fragments\.json$")
        return sum(1 for p in root.glob(
            f"coding_round{coding_round}.chunk*.fragments.json")
                   if name_re.fullmatch(p.name))

    @staticmethod
    def _video_artifacts(workdir: Path, spec: "RunSpec") \
            -> "tuple[Optional[str], Optional[int], Optional[str]]":
        """Task7：从 workdir 真实产物回填视频四字段（不存在即 None，不合成）。

        duration_ms 优先取场景 manifest 的 n_frames×frame_dt_s（免解码）；
        manifest 缺失再用 imageio 读 fps 并逐帧计数（venv pin 2.36.1）。
        intranet_url 恒 None：Gate2 先走原生视频 ≤28MB 直传，全失败才由
        feishu video 阶梯自行降级，runner 不编造内网地址。
        """
        if not spec.video_relpath:
            return None, None, None
        video = (workdir / spec.video_relpath)
        if not video.is_file():
            return None, None, None
        duration_ms: "Optional[int]" = None
        if spec.manifest_relpath:
            mf = workdir / spec.manifest_relpath
            if mf.is_file():
                try:
                    meta = json.loads(mf.read_text(encoding="utf-8"))
                    n_frames = meta.get("n_frames")
                    frame_dt = meta.get("frame_dt_s")
                    if isinstance(n_frames, int) and n_frames > 0 \
                            and isinstance(frame_dt, (int, float)) and frame_dt > 0:
                        duration_ms = int(round(n_frames * float(frame_dt) * 1000))
                except (OSError, ValueError):
                    duration_ms = None
        if duration_ms is None:
            try:
                import imageio.v3 as iio  # type: ignore
                meta_i = iio.immeta(video)
                fps = float(meta_i.get("fps") or 0.0)
                if fps > 0:
                    duration_ms = int(round(
                        sum(1 for _ in iio.imiter(video)) / fps * 1000))
            except Exception:  # noqa: BLE001 - 解码失败不伪造，交 Gate2 降级
                duration_ms = None
        cover = None
        if spec.cover_relpath and (workdir / spec.cover_relpath).is_file():
            cover = str(workdir / spec.cover_relpath)
        return str(video), duration_ms, cover

    def run(self, spec: RunSpec) -> dict:
        """执行一次 run：失败 → 至多 5 轮 coding 补救 → 重跑。"""
        workdir = Path(spec.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        failure_marker = workdir / FAILURE_JSON
        if failure_marker.is_file():
            # 计划 L505 ③：已结案 run 再进入，计数处硬停，零网络
            raise CodingLimitError(
                f"{spec.thread_id} 已有 {FAILURE_JSON}，禁止重启失败 run")

        root = self._root(spec)
        telemetry = (workdir / spec.telemetry_relpath).resolve(strict=False)
        timeline: list[dict] = []
        coding_calls = 0
        last_result: Optional[_ExecResult] = None
        # 锁内适配（计划 v1.2 外，seq42-47 拆块出生，用户 2026-10-20 裁决）：
        # 上一拆块轮的机器原因（含块号），原样进下一轮块 0 的失败反馈段。
        chunk_feedback = ""
        anchor_feedback = ""  # run7b §5.2 点 1；成功应用即清零（点 4，见下）
        # v1.4（设计 §2.4）：transport 注记与锚点注记互斥（同轮只保留一种机器
        # 注记）；成功应用两者一起清零。
        transport_feedback = ""

        while True:
            exec_no = len(timeline) + 1
            # run7 §3.6（MINOR 卫生预检，非安全门；仅 marker=1 形态）：exec 前
            # 非递归扫 workdir 顶层三件同名件 + __pycache__ 同 stem pyc，命中
            # 即本轮不 exec（0 付费/0 模型/0 触网），机器诊断直接 _fail，真实
            # 文件名只进人侧 timeline/FAILURE。
            hook_hits = (_detect_workdir_hook_pollution(workdir)
                         if spec.env_extra.get(_HOST_SHIM_MARKER) == "1"
                         else None)
            if hook_hits is not None:
                diag = ("工作目录顶层检出宿主引导同名件（卫生预检）："
                        + ", ".join(hook_hits)
                        + "。本轮未执行（0 运行/0 模型调用），请人工核查文件"
                          "来源后再跑。")
                timeline.append({
                    "exec": exec_no, "rc": None,
                    "stderr_tail": diag[-500:],
                    "stderr_only_tail": diag[-STDERR_ONLY_TAIL_CHARS:],
                    "stdout_chars": 0, "stderr_chars": len(diag)})
                return self._fail(root, spec, timeline, None, reason=diag)
            try:
                result = self._run(list(spec.cmd), workdir,
                                   spec.run_timeout_s, dict(spec.env_extra))
            except subprocess.TimeoutExpired as exc:
                result = _ExecResult(124, exc.stdout or "",
                                     (exc.stderr or "") + f"\nTIMEOUT after "
                                     f"{spec.run_timeout_s}s")
            timeline.append({
                "exec": exec_no, "rc": result.rc,
                "stderr_tail": _tb_tail(result)[-500:],  # 旧 merged 键字节不动（D4 撤销）
                "stderr_only_tail":
                    (result.stderr or "")[-STDERR_ONLY_TAIL_CHARS:],
                "stdout_chars": len(result.stdout or ""),
                "stderr_chars": len(result.stderr or "")})
            last_result = result
            # run6 §15.7-H4（D2）：telemetry_ok 在成功判定处一次性绑定，
            # rc0 缺产物注入分支共用同一布尔，禁止二次 is_file。
            telemetry_ok = result.rc == 0 and telemetry.is_file()
            # run7b 提交 B（rc0 批次结构闸）：仅在 rc0+产物在且 Task6 opt-in
            # 时只读扫描。函数内模块导入 + 属性访问（测试 monkeypatch 模块
            # 属性）；引擎类异常（含 ImportError）无 try 自然上冒至图 __error__。
            if telemetry_ok and spec.batch_schema_check:
                from orchestrator.judge import gait_rungs
                schema_raw, schema_n, schema_read_fault = (
                    gait_rungs.scan_batch_schema_gate(workdir))
            else:
                schema_raw, schema_n, schema_read_fault = [], 0, False
            # 人侧 RAW：全量/不截断/不去重/不脱敏（含绝对路径），立即挂当前
            # 失败 exec 条目；干净/rc!=0 轮键不存在（非空 list）。
            if schema_raw:
                timeline[-1]["batch_schema_errors"] = schema_raw
            # 锁内适配（计划 v1.2 外，用户 2026-10-25 裁决 Option 2）：run25
            # 结构性停机——rc0+遥测+结构闸全绿即 ok 短路复用批次，Gate2 构卡前
            # host-shim 双闸 GraphStateError 节点外抛，拓扑漂移反馈永不进编码
            # 环。Task6 opt-in：短路前跑 Gate2 同款机验（task6.scan_shim_topology_
            # gate＝_require_shim_evidence 同一闸，窗口/正则/9-2 契约单真值），
            # 违例跳过短路并强制模型修复轮（帽仍 max_coding_rounds）；任务侧闸
            # 本体一字不改，双闸纵深防御保留。人侧 RAW：违例原文（Gate2 逐字
            # 文案）立即挂当前 exec 条目；干净/rc!=0 轮键不存在（非空 list）。
            if telemetry_ok and spec.shim_topology_check:
                from orchestrator.graph.task6 import scan_shim_topology_gate
                shim_errors = scan_shim_topology_gate(timeline)
            else:
                shim_errors = []
            if shim_errors:
                timeline[-1]["shim_topology_errors"] = shim_errors
            if telemetry_ok and not schema_raw and not shim_errors:
                vp, dur_ms, cover = self._video_artifacts(workdir, spec)
                return {"status": "ok", "telemetry_uri": str(telemetry),
                        "coding_round": coding_calls, "executions": exec_no,
                        "timeline": timeline,
                        "video_path": vp, "duration_ms": dur_ms,
                        "cover_png": cover, "intranet_url": None}

            # —— 失败路径：coding 调用用尽即结案，结构上不存在多一次调用 ——
            if coding_calls >= spec.max_coding_rounds:
                reason = _coding_cap_reason(spec)  # run7b：普通帽尽点 anchor_exc=None；chunk 后缀逻辑不动
                if chunk_feedback:
                    reason += f"；末次拆块出生机器原因：{chunk_feedback}"
                # run7b 提交 B：结构闸未过走模型反馈轮；帽尽时 reason 追加
                # N/M（M 现算）。锚点帽尽路径在下方 PatchAnchorError 分支，
                # 有意不拼此句（G13b）。
                if schema_raw:
                    _sf, _schema_m = _build_schema_face(
                        schema_raw, workdir=workdir)
                    reason += (f"；末次批次结构闸未通过：共 {schema_n} 条，"
                               f"去重 {_schema_m} 类（机器证据见 timeline）")
                if shim_errors:
                    reason += (f"；末次 host-shim 证据双闸未通过（与 Gate2 "
                               f"构卡前同款机验）：{shim_errors[0]}")
                return self._fail(root, spec, timeline, last_result, reason=reason)

            tail = _tb_tail(result)
            snippets = _collect_snippets(workdir, tail)
            # Phase 2 Task1：recall 注入时 kb 路由 ProductionRecall 同参查询
            # （query_kb(namespace=troubleshooting, top_k=TOP_K)），
            # ret["kb_hits"] 与原 hits 同源同语义；learning 路只查真实学习表。
            # 攢批94 锁内适配（Task8 流体，2026-10-27）：spec.coding_recall
            # 非 None 时 troubleshooting 主路改用错误签名清洗查询串，并按
            # 工厂冻结的 (namespace, top_k, query) 增补 KB 检索路；None 时
            # 查询串/namespace/top_k 与旧行为逐字节一致。
            recall_cfg = spec.coding_recall
            if recall_cfg is not None and recall_cfg.clean_troubleshooting_hint:
                coding_hint = _clean_coding_hint(tail)
            else:
                coding_hint = "仿真失败 " + tail[-500:]
            learning_hits: list[dict] = []
            if self.recall is not None:
                ret = self.recall.retrieve(coding_hint, site="coding")
                hits = ret["kb_hits"]
                learning_hits = ret["learning_hits"]
            else:
                hits = self.vector_engine.query_kb(
                    coding_hint, namespace="troubleshooting",
                    top_k=TOP_K)
            extra_kb_sections: "tuple[tuple[str, int, list[dict]], ...]" = ()
            if recall_cfg is not None:
                secs = []
                for ns, ns_top_k, ns_query in recall_cfg.extra_kb:
                    secs.append((ns, ns_top_k, self.vector_engine.query_kb(
                        ns_query, namespace=ns, top_k=ns_top_k)))
                extra_kb_sections = tuple(secs)
            # run6 §15.7-H4（D2）：rc=0 且缺约定产物时把机器核验真值句追加进
            # 模型侧 tail；严格在 snippets/recall 收集之后、_build_messages 之前。
            # recall 查询串/snippets/timeline/_fail 继续用未注入 tail。
            repair_tail = tail
            rc0_missing = result.rc == 0 and not telemetry_ok
            if rc0_missing:
                repair_tail = _append_rc0_missing_artifact_note(
                    tail, spec.telemetry_relpath)
            # run7b 锁内适配（L505②，§5.2 点 3/§10.1 标记 5）：锚点注记固定 rc0 之后、redact_for_model 之前（A9 钉次序）；人侧 RAW。
            if anchor_feedback:
                repair_tail = _append_anchor_feedback(repair_tail, anchor_feedback)
            # run7b 提交 B：批次结构闸注记固定锚点之后、redact 之前
            # （次序 rc0→anchor→schema→redact，G10 钉）。
            if schema_raw:
                repair_tail = _append_batch_schema_note(
                    repair_tail, schema_raw, schema_n,
                    read_fault=schema_read_fault, workdir=workdir)
            # 锁内适配（用户 2026-10-25 裁决 Option 2）：host-shim 双闸注记固定
            # schema 之后、transport 之前（次序 rc0→anchor→schema→[shim]→
            # transport→函数源码→redact；[shim] 仅 Task6 opt-in 在场，其余任务
            # 注记序列逐字节不变，G10 既有钉点保持）。
            if shim_errors:
                repair_tail = _append_shim_topology_note(
                    repair_tail, shim_errors)
            # v1.4（设计 §2.3 G10 扩展序）：transport 注记固定 schema 之后、
            # 函数源码之前（次序 rc0→anchor→schema→transport→函数源码→redact）。
            if transport_feedback:
                repair_tail = _append_transport_feedback(
                    repair_tail, transport_feedback)
            # v1.4 §2.3（Option B 注入闸）：修复轮反馈段附涉案函数源码——
            # 仅当本尾带任一机器反馈件（rc0 缺产物注记/锚点/schema/transport/
            # shim 双闸）时注入。锚点/schema/transport/shim 为修复环专属通道，
            # 拆块出生轮尾不产；rc0 缺产物注记则出生轮亦可能出现（结构闸出生轮
            # 同效），非恒 False（三签 P3-2 措辞改准）。0 命中 collect 返回
            # 空串，不新增报错面。
            if (rc0_missing or anchor_feedback or schema_raw or transport_feedback
                    or shim_errors):
                func_section = collect_repair_functions(
                    workdir, schema_lines=schema_raw, traceback_tail=tail)
                if func_section:
                    repair_tail = repair_tail + "\n\n" + func_section
            # run7 A1（规格 §3.4，脱敏单点）：note 注入之后只求一次模型面
            # tail；普通修补轮与拆块出生轮两通道统一吃 redact 值。人侧
            # timeline/FAILURE/snippets/recall 继续 RAW（见上各采集点）。
            model_tail = redact_for_model(repair_tail)
            messages = _build_messages(
                gate2_notes=spec.gate2_notes, traceback_tail=model_tail,
                snippets=snippets, hits=hits, cmd=spec.cmd,
                learning_hits=learning_hits or None,
                extra_sections=spec.extra_prompt_sections or None,
                extra_kb_sections=extra_kb_sections or None)
            # 硬规则1：runner 永不构造 image block（双保险，网关入口亦拒）
            for mm in messages:
                if not isinstance(mm.get("content"), str):
                    raise PatchSafetyError("runner 消息出现非文本块（硬规则1）")

            coding_calls += 1
            stem = f"coding_round{coding_calls}"
            model = self.gateway.model_id(STAGE)
            cs = spec.chunked_birth
            # 锁内适配（计划 v1.2 外，seq42-47 拆块出生，用户 2026-10-20 裁决）
            # run loop 分支：ChunkSpec 仅在目标文件**出生前**的轮次生效；
            # 同 run 内文件已出生后的修补轮自动回到既有单次补丁路径（规格不
            # 变，runner 按磁盘现状选路，绝不重复出生已存在文件）。
            # 已知键漂移（评审 Minor 3，多付不错放）：出生占 round1 的 _chunk<i>
            # 键，紧接的修补轮用无后缀 round2 键；若 kill-9 后整节点重放，工厂
            # 见文件已存在→不挂 spec，重放按无后缀键查缓存 miss 会多重付一次，
            # 但请求指纹含前序补丁串，跨规格绝不错放旧补丁（miss 安全方向）。
            chunked_birth = cs is not None and not _validate_target(
                Path(cs.target_path), workdir).is_file()
            try:
                if chunked_birth:
                    codes, records = self._run_chunked_birth_round(
                        spec=spec, root=root, model=model,
                        coding_round=coding_calls, tail=model_tail,
                        snippets=snippets,
                        hits=hits, learning_hits=learning_hits,
                        round_feedback=chunk_feedback,
                        extra_kb_sections=extra_kb_sections)
                    # 设计 §2.2 EBNF：begin 行后/end 行前的 LF 是语法分隔符，
                    # _extract_chunk 逐字保留 body（自然上线形态每块 code 不带
                    # 尾换行），拼接必须补回恰好一个块间 LF；"".join 会让上块
                    # 末行与下块首行粘连（IndentationError）。body 自带尾换行时
                    # 此处多一个空行即模型原文，语义无损。
                    assembled = "\n".join(codes)
                    # 拼接整体 ast/重名/总长机器校验通过后，才合成单个 old=""
                    # 出生补丁；补丁应用路径一字不改（仍走 _parse_and_apply）。
                    _validate_assembled_chunks(assembled)
                    raw = json.dumps(
                        {"patches": [{"path": cs.target_path, "old": "",
                                      "new": assembled}]}, ensure_ascii=False)
                else:
                    # L-1 调用级幂等：kill-9 落在实呼返回后、节点返回前，
                    # LangGraph 0.2.28 会整节点重放。稳定 key + 请求指纹命中
                    # 即复用上次 raw，不发网、不进 billing_context、不写
                    # ledger，req/resp 证据不动；补丁仍要重放应用。
                    raw = self._cached_coding_call(
                        spec=spec, root=root, stem=stem, model=model,
                        messages=messages, coding_round=coding_calls,
                        learning_hits=learning_hits,
                        cache_key=(f"coding_{spec.thread_id}_r{spec.run_idx}"
                                   f"_round{coding_calls}"),
                        fp_extra={"max_tokens": spec.coding_max_tokens,
                                  "temperature": TEMPERATURE,
                                  "thinking": CODING_THINKING},
                        max_tokens=spec.coding_max_tokens,
                        timeout=spec.coding_timeout_s,
                        thinking=CODING_THINKING)
            except ChunkBirthError as exc:
                # 任一块不符 → 整轮失败、不半成品落盘；机器原因进 decision
                # 证据/时间线/下一轮块 0 反馈段，继续既有失败轮机制
                # （RUN_CODING_CAP 不变），无任何 patch 应用、无文件出生。
                # run5（§15.3-H2）：仅长度分类两条新路径携带 detail，条件写
                # rejected_detail（timeline 同义键）；15+ 旧形 detail 为 None，
                # 键缺席。rejected/chunk_birth_rejected 仍是 str(exc) 原文。
                decision_doc = {"applied": [], "rejected": str(exc)}
                if exc.detail is not None:
                    decision_doc["rejected_detail"] = exc.detail
                self._evidence(root, stem, "decision", decision_doc)
                timeline[-1]["coding_round"] = coding_calls
                timeline[-1]["chunk_birth_rejected"] = str(exc)
                if exc.detail is not None:
                    timeline[-1]["chunk_birth_rejected_detail"] = exc.detail
                if chunked_birth:
                    # observe-only 计数器（§15.1-D/§15.3-H2）：出生成败判据
                    # 仍是预注册 3 轮闭包，任何成功/阈值判定禁止引用本三项。
                    # 短路失败时 accepted 以已存活 fragments 件计数
                    # （record 非 None 数 == fragments 件数）。
                    accepted = self._count_round_fragment_files(
                        root, coding_calls)
                    rejected_n = 1 if exc.detail is not None else 0
                    timeline[-1]["split_accepted"] = accepted
                    timeline[-1]["split_rejected"] = rejected_n
                    timeline[-1]["closed_oversize_blocks"] = (
                        accepted + rejected_n)
                chunk_feedback = str(exc)
                continue
            try:
                # v1.4（设计 §2.1/§3）：修复传输双通道——replace 围栏优先、
                # JSON 兜底混出；extras 携 M1/M2 decision 证据登记件。
                applied, decision_extras = _parse_replace_and_apply(workdir, raw)
            except PatchAnchorError as exc:
                # run7b 锁内适配（L505② 协议口径修正，用户 2026-10-23 逐字裁决；§2.2/§4.2，§10.1 标记 3）：
                # 锚点 !=1 模型可自修（旧实现归并越界立即结案，5 轮帽只花 1 轮），故反馈续跑；越界/结构违例走下句父类硬停。
                # 第 5 轮拒绝在此直接 _fail——被拒轮字节未变，再跑 sidecar 确定失败（executions=5，不白跑第 6 次）；1-4 轮 continue 必先再 exec。
                # v1.4：F2/F3/F4 围栏锚点也走此通道（既有 patch_anchor_* 键族）；
                # 注记互斥——本分支清 transport_feedback。
                self._evidence(root, stem, "decision", {"applied": [], "rejected": str(exc)})
                timeline[-1]["coding_round"] = coding_calls
                timeline[-1]["patch_anchor_rejected"] = str(exc)
                timeline[-1]["patch_anchor_hits"] = exc.hits
                if coding_calls >= spec.max_coding_rounds:
                    return self._fail(root, spec, timeline, last_result,
                                      reason=_coding_cap_reason(spec, exc))
                anchor_feedback = _anchor_note(exc)
                transport_feedback = ""
                continue
            except PatchTransportError as exc:
                # v1.4 代差登记（设计 §3 行为代差登记节）：J1-J10/F1/F5/F6/F7
                # transport 失败由 instant-death 收窄为帽内反馈续跑（与锚点
                # 通道同构 continue）；P1 路径越界仍走下方父类硬停。
                # 第 5 轮拒绝在此直接 _fail（同锚点分支：被拒轮字节未变，
                # 再跑 sidecar 确定失败；1-4 轮 continue 必先再 exec）。
                decision_doc = {"applied": [], "rejected": str(exc), "transport": True}
                self._evidence(root, stem, "decision", decision_doc)
                timeline[-1]["coding_round"] = coding_calls  # v1.4 MINOR-2：与锚点通道同位落键
                timeline[-1]["patch_transport_rejected"] = _neutralize_for_model(str(exc))
                if coding_calls >= spec.max_coding_rounds:
                    return self._fail(root, spec, timeline, last_result,
                                      reason=_coding_cap_reason(spec, transport_exc=exc))
                transport_feedback = _transport_note(exc)
                anchor_feedback = ""  # 互斥：下一轮只带一种机器注记
                continue
            except PatchSafetyError as exc:
                # v1.4 收窄后仅剩 _validate_target 路径越界族（P1，唯一保留项）：
                # 拒绝且不发起重试（L505 ② 原文只钉越界），直接结案。
                self._evidence(root, stem, "decision",
                               {"applied": [], "rejected": str(exc)})
                return self._fail(
                    root, spec, timeline, last_result,
                    reason=f"补丁安全拒绝（不重试）：{exc}")
            # run5 m3（§15.2-m3）：chunk_split_records 只在 chunked 出生**成功**
            # decision 出现（长度恰 6、None 位为 null）；单补丁轮不带该键。
            # v1.4：decision_extras（M1 json_part_ignored / M2
            # fence_invalid_json_applied）并入成功 decision（设计 §2.1）。
            decision_doc = {"applied": applied, "rejected": None, **decision_extras}
            if chunked_birth:
                decision_doc["chunk_split_records"] = records
                accepted = sum(1 for rec in records if rec is not None)
                timeline[-1]["split_accepted"] = accepted
                timeline[-1]["split_rejected"] = 0
                timeline[-1]["closed_oversize_blocks"] = accepted
            self._evidence(root, stem, "decision", decision_doc)
            timeline[-1]["coding_round"] = coding_calls
            timeline[-1]["applied"] = applied
            anchor_feedback = ""  # run7b §5.2 点 4：成功应用即清零，下轮不带陈旧注记（A7）
            transport_feedback = ""  # v1.4 §2.4：成功应用同清零，下轮不带陈旧注记

    # -- 锁内适配（计划 v1.2 外，seq42-47 拆块出生，用户 2026-10-20 裁决）--

    def _cached_coding_call(self, *, spec: RunSpec, root: Path, stem: str,
                            model: str, messages: list[dict],
                            coding_round: int, learning_hits: list[dict],
                            cache_key: str, fp_extra: dict,
                            max_tokens: int, timeout: int,
                            thinking: "dict | None" = None,
                            req_extra: "dict | None" = None,
                            resp_payload: "Callable[[str], dict] | None" = None
                            ) -> str:
        """单次 coding 网关调用的缓存/证据/计费/采纳遥测收口（单次与拆块共用）。

        命中调用级重放缓存：直接返回 raw，不发网、不进 billing_context、
        不写 ledger、req/resp 证据不动。未命中：req 证据 → billing_context
        内 set_current_evidence 后 call（锁内适配 L501：证据必须在上下文进入
        之后挂接）→ resp 证据 → 成功返回后才入缓存 → 采纳遥测。
        ``resp_payload`` 为拆块路径的校验器：返回 resp 证据 dict；它在本地
        校验失败时可自写 reject resp 证据并抛 ChunkBirthError（不 put 缓存，
        安全方向：重放按 miss 重新计费）。
        """
        cache = self._replay_cache(spec)
        # S-8：指纹并入 stage/model；Task1：prompt JSON 规范串同时是采纳遥测
        # prompt_sha256 的规范串——两 sha 同源可交叉对账。
        prompt_json = json.dumps(messages, ensure_ascii=False, sort_keys=True)
        fp = request_fingerprint(prompt_json, None, fp_extra,
                                 stage=STAGE, model=model)
        cached = cache.get(cache_key, fp) if cache is not None else None
        if cached is not None:
            # Task1：重放不重复记采纳行——首呼已记，节点重放属同一逻辑调用。
            return cached["raw"]
        req_doc = {
            # P2（Task0）：Phase 2 新证据行时间戳统一严格 UTC Z。
            "ts_iso": utc_now_iso_z(),
            "stage": STAGE, "model": model,
            "thread_id": spec.thread_id, "batch_id": spec.batch_id,
            "coding_round": coding_round, "gate2_notes_present":
                bool(spec.gate2_notes and spec.gate2_notes.strip()),
            # 攢批92：思考契约随证据落盘（温度/thinking 成对，见常量注释）。
            "temperature": TEMPERATURE,
            "thinking": thinking,
            "messages": json.loads(redact(json.dumps(
                messages, ensure_ascii=False))),
        }
        if req_extra:
            req_doc.update(req_extra)
        req_ref = self._evidence(root, stem, "req", req_doc)
        # 锁内适配（计划 L501）：evidence 必须在 billing_context 进入之后
        # 挂接——该上下文管理器每次创建新 BillingContext，提前 set 会被重置。
        with billing_context(task=spec.billing_task, batch_id=spec.batch_id,
                             thread_id=spec.thread_id):
            set_current_evidence(req_ref)
            raw, _usage = self.gateway.call(
                STAGE, messages, max_tokens=max_tokens,
                temperature=TEMPERATURE, timeout=timeout,
                thinking=thinking)
        # Task1 采纳遥测（QA-1 M1）：唯一条件=call 成功返回；replay 不记，
        # 发网前失败 0 采纳行。必须先于 resp_payload 本地校验——拆块路径上
        # 网关已成功返回但本块围栏/ast 本地校验失败时，采纳语义与旧单次路径
        # （坏响应亦记）保持一致；崩溃只漏记不重记（安全方向）。
        # 锁内适配评审 Minor 1（2026-10-20 全新 opus reviewer）。
        if self.recall is not None:
            for hit in learning_hits:
                self.recall.record_adoption(
                    thread_id=spec.thread_id, site="coding",
                    learning_id=hit["id"], prompt_text=prompt_json,
                    learning_text=prompt_embedded_text(
                        str(hit.get("text", ""))))
        if resp_payload is not None:
            resp_doc = resp_payload(raw)
        else:
            resp_doc = {"ok": True, "text": redact(raw)}
        self._evidence(root, stem, "resp", resp_doc)
        # 成功返回后才入缓存（异常路径不 put，按 miss 安全重付）。
        usage_ints = self._usage_ints(_usage)
        if cache is not None and usage_ints is not None:
            cache.put(cache_key, kind=STAGE, model=model, raw=raw,
                      usage=usage_ints, fingerprint=fp)
        return raw

    def _chunk_resp_payload(self, root: Path, stem: str, raw: str, *,
                            target: str, index: int, total: int) -> dict:
        """拆块 resp 证据构造兼机器校验：成功存元信息+code 全文；失败自写
        reject resp 证据（raw head/tail 各 400，与探针证据口径一致）后抛。"""
        try:
            code = _extract_chunk(raw, target=target, index=index, total=total)
        except ChunkBirthError as exc:
            self._evidence(root, stem, "resp", {
                "ok": False, "chunk_index": index, "chunk_total": total,
                "parse_rejected": str(exc),
                "raw_head": redact(raw[:400]), "raw_tail": redact(raw[-400:])})
            raise
        return {
            "ok": True,
            "chunk": {"path": target, "index": index, "total": total},
            # code 全文按密钥口径 redact 后落证据；拼接用未脱敏原文。
            "code": redact(code),
            "raw_head": redact(raw[:400]), "raw_tail": redact(raw[-400:])}

    def _run_chunked_birth_round(self, *, spec: RunSpec, root: Path,
                                 model: str, coding_round: int,
                                 tail: str, snippets: list[dict],
                                 hits: list[dict], learning_hits: list[dict],
                                 round_feedback: str,
                                 extra_kb_sections:
                                 "tuple[tuple[str, int, list[dict]], ...]" = ()
                                 ) -> "tuple[list[str], list[dict | None]]":
        """一个 coding 轮内顺序完成 n 次出生调用，返回 (各块原文, 各块 record)。

        每块：构造带合同/提案/骨架 + chunk_context 的消息（硬规则1逐块文本
        守卫）→ 幂等键 coding_<t>_r<run>_round<r>_chunk<i>，指纹含
        chunk_index/total → 调用（max_tokens/timeout 取 RunSpec）→ 逐块
        sentinel 围栏机器校验 + 长度三分类。任何一块不符即 ChunkBirthError
        整轮失败（不继续为后续块付费），由 run() 捕获进入下一轮；全过程不写
        任何目标文件。

        run5（规格 §15.1-A/C、§15.3-H2）：codes 元素只能是逻辑块 b 本身
        （碎片字符串只在 _classify_closed_body 内以两个 int 长度存在，
        len(codes)==chunk_count==6 不变）；records 逐块对齐（None 或白名单
        record）。record 非 None 时**立即**写独立 fragments 证据件，先于
        append 与拼接三闸——后续闸失败/下一轮反馈/整 run human_failure 均
        不影响该件存活；正常块不写件。
        """
        cs = spec.chunked_birth
        codes: list[str] = []
        records: "list[dict | None]" = []
        for i in range(cs.chunk_count):
            ctx = ChunkPromptContext(
                index=i, total=cs.chunk_count, target_path=cs.target_path,
                titles=cs.chunk_titles, boundaries=cs.chunk_boundaries,
                # 同出生拼接语义（设计 §2.2 EBNF）：end 行前 LF 是分隔符、
                # 提取器逐字保留 body，前序上下文块间必须补恰好一个 LF，否则
                # 前序块末行与下一块首行粘连，模型看到被污染的参考码。
                previous_code="\n".join(codes),
                round_feedback=round_feedback if i == 0 else "")
            messages = _build_messages(
                gate2_notes=spec.gate2_notes, traceback_tail=tail,
                snippets=snippets, hits=hits, cmd=spec.cmd,
                learning_hits=learning_hits or None,
                extra_sections=cs.per_call_sections, chunk_context=ctx,
                extra_kb_sections=extra_kb_sections or None)
            # 硬规则1：逐块消息同样只允许纯文本（网关入口另有递归守卫）。
            for mm in messages:
                if not isinstance(mm.get("content"), str):
                    raise PatchSafetyError("runner 消息出现非文本块（硬规则1）")
            stem = f"coding_round{coding_round}.chunk{i}"
            raw = self._cached_coding_call(
                spec=spec, root=root, stem=stem, model=model,
                messages=messages, coding_round=coding_round,
                learning_hits=learning_hits,
                cache_key=(f"coding_{spec.thread_id}_r{spec.run_idx}"
                           f"_round{coding_round}_chunk{i}"),
                fp_extra={"max_tokens": spec.coding_max_tokens,
                          "temperature": TEMPERATURE,
                          "thinking": CODING_THINKING,
                          "chunk_index": i, "chunk_total": cs.chunk_count},
                max_tokens=spec.coding_max_tokens,
                timeout=spec.coding_timeout_s,
                thinking=CODING_THINKING,
                req_extra={"chunk_index": i, "chunk_total": cs.chunk_count},
                resp_payload=partial(
                    self._chunk_resp_payload, root, stem,
                    target=cs.target_path, index=i, total=cs.chunk_count))
            # 拼接用未脱敏原文（resp 证据内是 redact 后的口径）。
            b = _extract_chunk(
                raw, target=cs.target_path, index=i, total=cs.chunk_count)
            # run5（§15.1-A/§15.3-H2）：长度三分类。逻辑块 b 原样入 codes；
            # 切点只用于可行性判定与 record，碎片字符串不离开纯函数。
            rec = _classify_closed_body(b, i)
            if rec is not None:
                # 先于 codes.append 与拼接三闸写盘：后续 assembled ast/重名/
                # 30000 地板拒绝时，「接受过几块二拆」仍有完整机器证据。
                self._evidence(root, stem, "fragments", rec)
            codes.append(b)
            records.append(rec)
        return codes, records

    def _fail(self, root: Path, spec: RunSpec, timeline: list[dict],
              result: Optional[_ExecResult], *, reason: str) -> dict:
        # L-4：失败结案被重放（kill 落在 FAILURE.json 落盘后、节点返回前）时，
        # 保留首次写入的 ts_iso，时间线以首次失败时刻为准，不滚动成新时间戳。
        # P2（Task0）：首写时间戳改严格 UTC Z；旧件 +0000/+0800 串原样保留。
        ts_iso = utc_now_iso_z()
        existing_p = Path(spec.workdir) / FAILURE_JSON
        if existing_p.is_file():
            try:
                old_ts = json.loads(existing_p.read_text(
                    encoding="utf-8")).get("ts_iso")
                if isinstance(old_ts, str) and old_ts.strip():
                    ts_iso = old_ts
            except (ValueError, OSError):
                pass
        payload = {
            "status": "failed",
            "thread_id": spec.thread_id,
            "reason": reason,
            "coding_round": len([t for t in timeline if "coding_round" in t]),
            "executions": len(timeline),
            "last_rc": result.rc if result else None,
            "last_stderr_tail": _tb_tail(result)[-2000:] if result else "",
            # run7 A3'：纯 stderr 新键；旧 last_stderr_tail[-2000:] 不动。
            "last_stderr_only_tail":
                ((result.stderr or "")[-STDERR_ONLY_TAIL_CHARS:]
                 if result else ""),
            "timeline": timeline,
            "ts_iso": ts_iso,
        }
        p = Path(spec.workdir) / FAILURE_JSON
        # S-6：原子写 0600；L-4 首写 ts 保留语义在上方不变。
        atomic_write_text(p, json.dumps(payload, ensure_ascii=False, indent=2))
        self._evidence(root, "failure", None, payload)
        return {"status": "failed", "failure_json": str(p),
                "coding_round": payload["coding_round"],
                "executions": len(timeline), "timeline": timeline,
                "reason": reason}


def agent_run_seam(runner: AgentRunner, spec_factory: "Callable[[dict], RunSpec]"):
    """图接缝工厂：agent_run 节点每次按当前 state 构造 RunSpec 后执行。

    Gate2 打回意见由 spec_factory 从 state 的 gate2_rejected 事件提取。
    """
    def seam(state: dict) -> dict:
        spec = spec_factory(state)
        record = runner.run(spec)
        if record["status"] == "ok":
            return {"telemetry_uri": record["telemetry_uri"],
                    "coding_round": record["coding_round"],
                    "executions": record.get("executions"),
                    "timeline": record.get("timeline", []),
                    "status": "ok",
                    "video_path": record.get("video_path"),
                    "duration_ms": record.get("duration_ms"),
                    "cover_png": record.get("cover_png"),
                    "intranet_url": record.get("intranet_url")}
        return {"telemetry_uri": "", "status": "failed",
                "coding_round": record["coding_round"],
                "failure_json": record["failure_json"],
                "reason": record["reason"],
                # 攢批94：失败 run 同样回灌 timeline。旧实现只在 ok 记录带，
                # learning._fix_actions 读 run["timeline"].applied，失败结案
                # 学习行恒为「修复动作：（无）」，五轮真实补丁无证据；与 ok
                # 路径同源（runner._fail 返回值含 timeline）。
                "timeline": record.get("timeline", []),
                "executions": record.get("executions")}
    return seam
