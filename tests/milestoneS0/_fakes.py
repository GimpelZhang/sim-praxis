"""S0 测试共用假件（不做网络/引擎；只回协议形状）。"""
from __future__ import annotations

from orchestrator.hk.driver import GateDecision


def fake_actor(log: list | None = None):
    def actor(name, contract, value):
        if log is not None:
            log.append({"name": name, "contract": contract,
                        "notes": (value or {}).get("human_notes", "")})
        if name == "propose_plan":
            return {"conditions": ["c1", "c2"], "n_block": 4,
                    "proposition": "decrease_gt", "delta": "0.01",
                    "plan_hint": (value or {}).get("human_notes", "")}
        if name == "write_analysis":
            return {"source": "raise SystemExit('frozen analysis program')\n"}
        if name == "submit_claim":
            return {"verdict": "INCONCLUSIVE", "statement": "s0 skeleton claim"}
        if name == "diagnose_fail":  # S1 新增第 4 触点：打回意见注入诊断（stub）
            return {"diagnosis": "stub", "notes": (value or {}).get("human_notes", "")}
        raise AssertionError(f"未知触点 {name}")

    return actor


def fake_batch_runner(plan, context=None):
    runs = []
    for i, condition in enumerate(plan["conditions"]):
        for block in range(plan["n_block"]):
            runs.append({"block_id": f"b{block:02d}", "condition_id": condition,
                         "metric_id": "m1", "value": float(block) + 0.1 * i})
    return {
        "runs": runs,
        "contrasts": [{"id": "c1v2", "metric_id": "m1",
                       "treatment_condition": "c2", "control_condition": "c1",
                       "proposition": "decrease_gt", "delta": "0.01"}],
    }


def fake_analyzer(run_manifest, context=None):
    return {
        "contrasts": [{"id": "c1v2", "metric_id": "m1", "estimate": 0.1,
                       "ci_low": 0.05, "ci_high": 0.15, "claim": "INCONCLUSIVE",
                       "delta": "0.01", "proposition": "decrease_gt"}],
        "confirm_plan": {"pairs": {"c1v2": [0.1, 0.12, 0.08, 0.11]}},
    }


def fake_confirmer(confirm_plan, context=None):
    # S2b F1.1：confirm_result 必须含 negative_control + 全字段 results 行。
    return {
        "contrasts": [{"id": "c1v2", "metric_id": "m1",
                       "proposition": "decrease_gt", "delta": "0.01",
                       "level": "0.95", "estimate": 0.1025,
                       "ci_low": 0.05, "ci_high": 0.15, "n_blocks": 4,
                       "claim": "INCONCLUSIVE"}],
        "negative_control": {"registered": True, "base": "c1", "sham": "c1_sham",
                             "non_degenerate": True, "sub_threshold": True,
                             "sensitivity_calibrated": True,
                             "estimate": 0.0, "ci_low": -0.001, "ci_high": 0.001},
    }


def spec():
    return {"hypothesis": "friction reduces slide distance",
            "delta_m": 0.01, "stop_condition": "two rejects"}


class GateScript:
    """按序返回门判据；用尽后始终 approve。"""

    def __init__(self, decisions, default_approved: bool = True):
        self.decisions = list(decisions)
        self.default_approved = default_approved
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if self.decisions:
            return self.decisions.pop(0)
        return GateDecision(approved=self.default_approved)


def approve() -> "GateDecision":
    from orchestrator.hk.driver import GateDecision
    return GateDecision(approved=True)


def reject(notes: str = "请补充对照") -> "GateDecision":
    from orchestrator.hk.driver import GateDecision
    return GateDecision(approved=False, human_notes=notes)
