#!/usr/bin/env python3
"""transport probe（设计 §2.6）：Task6 修复环 run 前的独立传输预检探针。

锁内适配（Task6 修复环 v1.4「IDE 式多次小合成」协议，
.omc/handoffs/task6-ide-repair-protocol-design.md §2.6；授权链见
runner.PatchTransportError docstring）。命名避让 task6.py 既有
「Gate1 preflight」——概念名 transport probe，不入 task6.py 工厂
（run 前独立步骤，不产生图内付费 surprise）。

契约（设计 §2.6 逐条）：
- 1 次付费调用：10 行玩具函数 + 1 处 bug，要求 replace 围栏整替修复
  （含 class= 用法——两同名方法使 class= 结构性必需）；判定：围栏解析 →
  class= 齐备 → AST 锚命中（生产同一 _apply_replace_staged 三闸）→
  替换后 toy 断言过。
- 账道：ModelGateway.call("coding_debug", ...) 正规账道，逐次 ledger 记账
  （硬规则 1：业务代码不直填模型 ID）；探针 FAIL 时该 seq 保留在台账作
  模型能力证据，append-only 不撤销。
- 缓存：.call_cache 键 transport_probe_{model}_{sha256 前缀}，模型未变
  重跑 0 触网；命中仍查请求指纹（S-8 口径）。
- 预测力诚实声明（防升格引用）：PASS 只说明「玩具契约服从」，不构成
  任务级传输就绪证明（历史反例：同款模型小任务可收敛、18KB 规格任务
  thinking 吃满 32768 全败，seq43-47/87-89）。FAIL → 不起 run、证据落盘、
  按硬规则 1 报用户（模型能力证据，不自换模型）；run8 失败仍走硬规则 1
  报告路径，不因探针 PASS 降级报告义务。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path

from orchestrator.agent_runner.runner import (
    PatchSafetyError,
    _apply_replace_staged,
    _scan_replace_fences,
)
from orchestrator.common.callcache import CallReplay, request_fingerprint
from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import atomic_write_text
from orchestrator.gateway.quota_ledger import (
    QuotaLedger,
    billing_context,
    phase1_root,
)
from orchestrator.gateway.router import ModelGateway

TOY_FILENAME = "toy_calc.py"
# 玩具契约：两 class 同名方法 add——不带 class= 的 func=add 必命中 F3 候选
# 反馈，class= 结构性必需；Calc.add 返回差值是唯一 bug（check() 断言求和）。
TOY_SOURCE = '''class Calc:
    @staticmethod
    def add(a, b):
        return a - b


class Calc2:
    @staticmethod
    def add(a, b):
        return a + b


def check():
    assert Calc.add(2, 3) == 5, "Calc.add must sum"
    assert Calc2.add(2, 3) == 5, "Calc2.add must stay sum"
'''

# 仪器保真修复（2026-10-25，seq=121 FAIL 后）：原 PROBE_SYSTEM 只说
# 「整行 begin/end；首行 path= …」却从未给出字面前缀 `<<<<<<< `——生产
# 修复轮 system（runner._build_messages chunk_context=None 分支）带完整
# 示例逐字含前缀。首探输出（证据 probe_transport-probe-t6-v14_
# 2026-10-24T171044Z.json）恰按歧义措辞拆行：核记独占行 1、字段行 2，
# judge fence_parse 拒——仪器缺陷非模型能力证据（n=1 混杂归因，修复后
# 是否真能过由重探实呼裁决，不双向断言）。修法=镜像生产示例（begin 行
# 前缀+字段同行逐字），判定三闸与玩具契约零改动；相对生产示例的唯一
# 有意分歧：class= 探针契约恒必须给（生产文案可省略；两同名方法结构性
# 必需，非本次变更）。
PROBE_SYSTEM = (
    "你是具身智能仿真工具链的传输预检 agent。只输出 simpraxis.replace.v1 "
    "围栏整替（格式示例，begin/end 整行各独占一行）：\n"
    "<<<<<<< simpraxis.replace.v1 path=相对路径.py func=函数名 class=类名\n"
    "<该函数含装饰器行的完整新源码>\n"
    "=======\n"
    ">>>>>>> simpraxis.replace.v1.end\n"
    "首行 path/func 逐字给相对路径与函数名，class= 必须给（本次两同名方法"
    "必须消歧）；中间是完整新源码；======= 是可选分隔行；末行结束围栏；"
    "三行各独占一行。补丁只能改本次工作目录内的文件；不要输出解释、"
    "不要输出 image。")

PROBE_HONESTY = (
    "预测力诚实声明（设计 §2.6）：PASS 只说明玩具契约服从，不构成任务级"
    "传输就绪证明（历史反例：同款模型小任务可收敛、18KB 规格任务 thinking "
    "吃满 32768 全败，seq43-47/87-89）；FAIL 时不起 run、证据落盘、按硬"
    "规则 1 报用户（模型能力证据，不自换模型）。")


def build_probe_messages() -> "list[dict[str, str]]":
    """探针请求两件套（纯函数，0 触网）：玩具契约 + class= 强制要求。"""
    user = (
        f"目标文件 {TOY_FILENAME}（工作目录根下）当前内容：\n"
        "```python\n" + TOY_SOURCE + "```\n"
        "其中 Calc.add 有且仅有 1 处 bug（返回差值而非和）。请用 "
        "simpraxis.replace.v1 围栏整替修复该 bug：因为 Calc 与 Calc2 有同名"
        "方法 add，begin 行必须带 class= 消歧；只输出围栏，不要输出 JSON、"
        "解释或 image。")
    return [{"role": "system", "content": PROBE_SYSTEM},
            {"role": "user", "content": user}]


def judge_probe(response: str, workdir: "str | Path") -> dict:
    """探针判定（纯函数侧；workdir 为放了 TOY_SOURCE 的临时目录）。

    阶段：fence_parse（围栏扫描）→ class_usage（全部块带 class=）→
    anchor_or_apply（生产同一三闸应用；含 F2/F3/F4 锚与 F5/F6 重 parse）→
    toy_check（exec + check() 断言）。任一阶段失败返回 pass=False 与机器
    原因；全部通过 pass=True。不触网、不计费。
    """
    wd = Path(workdir)
    try:
        blocks = _scan_replace_fences(response)
    except PatchSafetyError as exc:
        return {"pass": False, "stage": "fence_parse", "detail": str(exc)}
    if not blocks:
        return {"pass": False, "stage": "fence_parse",
                "detail": "响应不含 simpraxis.replace.v1 围栏"}
    if not all(b.cls for b in blocks):
        return {"pass": False, "stage": "class_usage",
                "detail": "围栏未全部使用 class=（玩具契约强制 class= 消歧）"}
    try:
        _apply_replace_staged(wd, blocks)
    except PatchSafetyError as exc:
        return {"pass": False, "stage": "anchor_or_apply", "detail": str(exc)}
    text = (wd / TOY_FILENAME).read_text(encoding="utf-8")
    ns: dict = {}
    try:
        exec(compile(text, TOY_FILENAME, "exec"), ns)  # noqa: S102 - 探针自玩具源码
        ns["check"]()
    except Exception as exc:  # noqa: BLE001 - 判定收口，类型即机器原因
        return {"pass": False, "stage": "toy_check",
                "detail": f"{type(exc).__name__}: {exc}"}
    return {"pass": True, "stage": "ok", "detail": ""}


def _cache_key(model: str, prompt_json: str) -> str:
    """设计 §2.6：键 (transport_probe, model_id, sha256(probe_text))。

    model 逐字符白名单化（CallReplay 键正则 ^[A-Za-z0-9_.-]{1,160}$）；
    摘要取 sha256 前 40 位（请求指纹另查全量完整性，S-8）。
    """
    digest = hashlib.sha256(prompt_json.encode("utf-8")).hexdigest()
    model_part = re.sub(r"[^A-Za-z0-9_.-]", "_", model)[:64]
    return f"transport_probe_{model_part}_{digest[:40]}"


def run_probe(*, gateway: ModelGateway, batch_id: str,
              thread_id: str = "transport-probe",
              evidence_dir: "str | Path | None" = None,
              workdir: "str | Path | None" = None,
              call_cache: "CallReplay | None" = None) -> dict:
    """探针主流程（CLI 与测试共用；gateway 注入 Fake 即 0 付费 hermetic）。

    计费走 billing_context(task=6)；缓存命中 0 触网 0 记账。返回 dict 至少
    含 status pass/fail、judge 阶段件、cache_hit、honesty 声明。
    """
    messages = build_probe_messages()
    prompt_json = json.dumps(messages, ensure_ascii=False, sort_keys=True)
    model = gateway.model_id("coding_debug")
    if not model:
        raise RuntimeError("coding_debug stage model_id 不可得（stage_models.yaml）")
    key = _cache_key(model, prompt_json)
    fp = request_fingerprint(prompt_json, None,
                             {"max_tokens": 4096, "temperature": 0.2},
                             stage="coding_debug", model=model)
    cache = call_cache if call_cache is not None else CallReplay(
        phase1_root() / ".call_cache")
    hit = cache.get(key, fp)
    if hit is not None:
        raw, usage, cache_hit = hit["raw"], hit["usage"], True
    else:
        with billing_context(task=6, batch_id=batch_id, thread_id=thread_id):
            raw, usage = gateway.call(
                "coding_debug", messages,
                max_tokens=4096, temperature=0.2, timeout=120)
        usage_ints = (int(usage[0]), int(usage[1])) if (
            isinstance(usage, (list, tuple)) and len(usage) == 2
            and all(isinstance(v, int) and not isinstance(v, bool)
                    and v >= 0 for v in usage)) else None
        if usage_ints is not None:
            cache.put(key, kind="transport_probe", model=model, raw=raw,
                      usage=usage_ints, fingerprint=fp)
        cache_hit = False
    if workdir is None:
        wd = Path(tempfile.mkdtemp(prefix="simpraxis_transport_probe_"))
    else:
        wd = Path(workdir)
        wd.mkdir(parents=True, exist_ok=True)
    (wd / TOY_FILENAME).write_text(TOY_SOURCE, encoding="utf-8")
    verdict = judge_probe(raw, wd)
    result = {
        "schema": "simpraxis.transport_probe.v1",
        "status": "pass" if verdict["pass"] else "fail",
        "judge": verdict,
        "model": model,
        "batch_id": batch_id,
        "thread_id": thread_id,
        "cache_hit": cache_hit,
        "replay_key": key,
        "usage": list(usage) if usage is not None else None,
        "response_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "response_raw": raw,
        "ts_iso": utc_now_iso_z(),
        "honesty": PROBE_HONESTY,
    }
    ev_dir = Path(evidence_dir) if evidence_dir is not None else (
        phase1_root() / "transport_probe")
    ev_dir.mkdir(parents=True, exist_ok=True)
    # 三签 P3-1：thread_id 过文件名安全字符集再拼（证据件路径不得含 / 等）。
    safe_tid = re.sub(r"[^A-Za-z0-9_.-]", "_", thread_id)
    ev = ev_dir / f"probe_{safe_tid}_{result['ts_iso'].replace(':', '')}.json"
    atomic_write_text(ev, json.dumps(result, ensure_ascii=False, indent=2))
    result["evidence"] = str(ev)
    return result


def main(argv: "list[str] | None" = None) -> int:
    """CLI：python -m orchestrator.agent_runner.transport_probe。

    0 付费纪律：本入口是唯一付费路径（1 实呼或缓存命中）；测试一律注入
    FakeGateway 直调 run_probe，永不走 main。
    """
    p = argparse.ArgumentParser(
        description="Task6 修复环 transport probe（设计 §2.6；PASS≠任务级就绪）")
    p.add_argument("--batch-id", required=True)
    p.add_argument("--thread-id", default="transport-probe")
    p.add_argument("--evidence-dir", default=None)
    p.add_argument("--workdir", default=None)
    args = p.parse_args(argv)
    gateway = ModelGateway(ledger=QuotaLedger())
    result = run_probe(gateway=gateway, batch_id=args.batch_id,
                       thread_id=args.thread_id,
                       evidence_dir=args.evidence_dir,
                       workdir=args.workdir)
    out = {k: result[k] for k in
           ("status", "judge", "model", "cache_hit", "evidence", "honesty")}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if result["status"] != "pass":
        print("PROBE_FAIL：不起 run；模型能力证据已落盘；按硬规则 1 报用户"
              "（不自换模型）。", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
