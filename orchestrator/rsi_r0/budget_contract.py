"""R0 资源与报价合同（G-P 的"资源/报价合同"条件，零付费、纯算术）。

计划 §6.2 要求付费前把**最坏请求成本上界**写死：不能上界就不发网。本模块把那部分
做成可机判的产物 ``protocol/pricing_contract.json``：

* 单请求输入/输出 token 帽（§6.2：20,000 / 8,192）；
* 每百万 token 的规划单价与据此算出的单请求最坏美元；
* 整批请求帽（222）与据此算出的阶段最坏美元，必须 ≤ 货币帽 100 USD；
* **口径诚实**：``pricing_basis`` 明写这是保守规划界、不是供应商报价；真实花费以账本
  记录的 token 为准，本阶段不做发票对账。

单价取"高到不可能低估"的保守值（正文见 PRICE_* 注释）：若真实单价高于此界，合同
本身即失效，必须重签而不是悄悄沿用。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orchestrator.rsi_r0.contracts import sha256_of

SCHEMA = "rsi_r0.pricing_contract.v1"

#: 计划 §6.2：每个 R0 请求的输入/输出帽。
INPUT_TOKENS_CAP = 20_000
OUTPUT_TOKENS_CAP = 8_192

#: 规划单价（USD / 1M token）。这是**保守上界**而不是报价：取到高于任何在用供应商
#: 公开发布的同类模型单价，保证真实花费不被低估。单价上调即合同失效，必须重签。
PRICE_PER_1M_INPUT_USD = 1.00
PRICE_PER_1M_OUTPUT_USD = 4.00

#: 计划 §6.1：R0 全部批次的真实请求上限（= 6 + 72 + 144）。
REQUESTS_TOTAL_CAP = 222

#: 计划 §6.2：全批次货币帽（含条件重验仍须 ≤ 此值）。
USD_CEILING = 100.0


def worst_case_per_request_usd(input_cap: int = INPUT_TOKENS_CAP,
                               output_cap: int = OUTPUT_TOKENS_CAP) -> float:
    return round(input_cap / 1e6 * PRICE_PER_1M_INPUT_USD
                 + output_cap / 1e6 * PRICE_PER_1M_OUTPUT_USD, 6)


def build_contract(**overrides: Any) -> dict[str, Any]:
    per_request = worst_case_per_request_usd()
    total = round(per_request * REQUESTS_TOTAL_CAP, 6)
    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "currency": "USD",
        "fx_basis": "计价与结算币种同为美元；本阶段无换汇（若改由人民币计价须重签）",
        "pricing_basis": "conservative_ceiling_not_vendor_quote",
        "note": ("单请求最坏成本 = 输入帽×输入单价 + 输出帽×输出单价；真实花费以账本"
                 "token 记录为准，本阶段不做发票对账。单价上调即本件失效。"),
        "input_tokens_cap": INPUT_TOKENS_CAP,
        "output_tokens_cap": OUTPUT_TOKENS_CAP,
        "price_per_1m_input_usd": PRICE_PER_1M_INPUT_USD,
        "price_per_1m_output_usd": PRICE_PER_1M_OUTPUT_USD,
        "worst_case_per_request_usd": per_request,
        "requests_total_cap": REQUESTS_TOTAL_CAP,
        "worst_case_total_usd": total,
        "usd_ceiling": USD_CEILING,
    }
    doc.update(overrides)
    doc["contract_sha256"] = sha256_of({k: v for k, v in doc.items()
                                        if k != "contract_sha256"})
    return doc


def write_contract(path: Path, **overrides: Any) -> dict[str, Any]:
    doc = build_contract(**overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return doc


def read_contract(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def contract_ok(doc: dict[str, Any] | None) -> tuple[bool, str]:
    """G-M 的 ``cost_upper_bound_known`` 事实：齐、自洽、且最坏总量不越货币帽。"""
    if not doc:
        return False, "缺 pricing_contract.json"
    if doc.get("schema") != SCHEMA:
        return False, f"schema 不符：{doc.get('schema')!r}"
    body = {k: v for k, v in doc.items() if k != "contract_sha256"}
    if sha256_of(body) != doc.get("contract_sha256"):
        return False, "自哈希不符（被改过）"
    per = doc.get("worst_case_per_request_usd")
    total = doc.get("worst_case_total_usd")
    if not isinstance(per, (int, float)) or per <= 0:
        return False, "单请求最坏成本未上界"
    if not isinstance(total, (int, float)) or total <= 0:
        return False, "整批最坏成本未上界"
    if float(total) > float(doc.get("usd_ceiling") or 0):
        return False, (f"最坏总量 {total} 越过货币帽 {doc.get('usd_ceiling')}")
    if doc.get("requests_total_cap") != REQUESTS_TOTAL_CAP:
        return False, f"请求总量帽不是 {REQUESTS_TOTAL_CAP}"
    # 评审 MINOR-2：只验自哈希等于"自洽"，把单价写成 0.0001 再重算自哈希照样过。
    # 这里按合同自己的字段**重算算术**，不信任任何预先写死的金额。
    try:
        recomputed_per = (float(doc["input_tokens_cap"]) / 1e6
                          * float(doc["price_per_1m_input_usd"])
                          + float(doc["output_tokens_cap"]) / 1e6
                          * float(doc["price_per_1m_output_usd"]))
        recomputed_total = recomputed_per * int(doc["requests_total_cap"])
    except (KeyError, TypeError, ValueError) as exc:
        return False, f"合同字段不足以重算最坏成本：{exc}"
    if abs(recomputed_per - float(per)) > 1e-9:
        return False, f"单请求最坏成本与算术不符：记 {per}，重算 {recomputed_per:.9f}"
    if abs(recomputed_total - float(total)) > 1e-6:
        return False, f"整批最坏成本与算术不符：记 {total}，重算 {recomputed_total:.9f}"
    return True, "ok"
