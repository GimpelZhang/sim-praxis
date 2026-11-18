"""S0-3：八守卫 8 正 8 负（负控名称＝历史事故形状）。"""
from __future__ import annotations

import hashlib

import pytest

from orchestrator.hk import contracts as C
from orchestrator.hk.guards import (GUARDS, GuardCtx, GuardViolation, enforce,
                                    g_budget, g_bytes, g_claim, g_freeze,
                                    g_noprog, g_prereg, g_send_once, g_unit)

GOOD_SHA = hashlib.sha256(b"spec").hexdigest()


def _expect(func, ctx, code: str) -> GuardViolation:
    with pytest.raises(GuardViolation) as excinfo:
        func(ctx)
    assert excinfo.value.code == code, excinfo.value
    return excinfo.value


# —— G-PREREG ——
def test_g_prereg_pass():
    g_prereg(GuardCtx(stage="K1", registered_spec_sha=GOOD_SHA))


def test_g_prereg_rejects_missing_or_malformed_sha():
    _expect(g_prereg, GuardCtx(stage="K1", registered_spec_sha=None), "SPEC_UNREGISTERED")
    _expect(g_prereg, GuardCtx(stage="K1", registered_spec_sha="abc"), "SPEC_UNREGISTERED")


# —— G-BUDGET ——
def test_g_budget_pass_when_enough_remaining():
    g_budget(GuardCtx(stage="K3", remaining_budget=3, required_actions=3))


def test_g_budget_unreachable_blocks_send():
    _expect(g_budget, GuardCtx(stage="K3", remaining_budget=1, required_actions=3),
            "BUDGET_UNREACHABLE")


# —— G-SEND-ONCE ——
def test_g_send_once_pass_first_send():
    g_send_once(GuardCtx(slot="slot-1", sent_slots={"slot-0"}))


def test_g_send_once_rejects_second_send_and_unknown_slot():
    _expect(g_send_once, GuardCtx(slot="slot-1", sent_slots={"slot-1"}),
            "SLOT_ALREADY_SENT")
    _expect(g_send_once, GuardCtx(slot=None, sent_slots=set()), "UNKNOWN_SEND")


# —— G-FREEZE ——
def test_g_freeze_pass_without_mutation():
    g_freeze(GuardCtx(stage="K7", frozen=True, mutation=None))


def test_g_freeze_rejects_post_freeze_mutation():
    _expect(g_freeze, GuardCtx(stage="K7", frozen=True, mutation="edit analysis.py"),
            "FROZEN_MUTATION")


# —— G-BYTES ——
def test_g_bytes_pass_when_hash_matches_and_no_sentinel():
    body = b'{"contrast": "A"}'
    g_bytes(GuardCtx(stage="K4", request_bytes=body,
                     registered_request_sha256=hashlib.sha256(body).hexdigest(),
                     private_sentinels=(b"SECRET-ANSWER",)))


def test_g_bytes_rejects_tampered_bytes():
    body = b'{"contrast": "A"}'
    _expect(g_bytes, GuardCtx(stage="K4", request_bytes=body,
                              registered_request_sha256=hashlib.sha256(b"other").hexdigest()),
            "BYTES_MISMATCH")


def test_g_bytes_rejects_private_sentinel_leak():
    body = b'{"hint": "SECRET-ANSWER"}'
    _expect(g_bytes, GuardCtx(stage="K4", request_bytes=body,
                              registered_request_sha256=hashlib.sha256(body).hexdigest(),
                              private_sentinels=(b"SECRET-ANSWER",)), "PRIVATE_LEAK")


# —— G-CLAIM ——
def test_g_claim_pass_on_identical_documents():
    host = {"effect": 0.040867, "lower": 0.01, "upper": 0.07,
            "claim": "SUPPORTED", "nested": {"n": 32}}
    g_claim(GuardCtx(claim_doc=dict(host), host_recomputed=dict(host)))


def test_g_claim_rejects_transcription_error():
    # R1.3 事故形状：自报 0.0407 vs host 0.040867
    host = {"effect": 0.040867, "claim": "SUPPORTED"}
    exc = _expect(g_claim, GuardCtx(claim_doc={"effect": 0.0407, "claim": "SUPPORTED"},
                                    host_recomputed=host), "CLAIM_INVALID")
    assert "0.0407" in exc.detail


# —— G-UNIT ——
def test_g_unit_pass_on_compatible_contracts_and_strings():
    g_unit(GuardCtx(unit_pairs=[(C.RUN_MANIFEST, C.RUN_MANIFEST), ("m", "m")]))


def test_g_unit_rejects_unit_mismatch():
    _expect(g_unit, GuardCtx(unit_pairs=[(C.RUN_MANIFEST,
                                          C.Contract(name="run_manifest", unit="s"))]),
            "UNIT_MISMATCH")
    _expect(g_unit, GuardCtx(unit_pairs=[("m", "s")]), "UNIT_MISMATCH")


# —— G-NOPROG ——
def test_g_noprog_pass_below_threshold():
    g_noprog(GuardCtx(round_no_new_info=1, noprog_rounds=2))


def test_g_noprog_rejects_stalled_rounds():
    _expect(g_noprog, GuardCtx(round_no_new_info=2, noprog_rounds=2), "NO_PROGRESS")


def test_all_eight_guards_registered_and_enforce_order():
    assert len(GUARDS) == 8
    with pytest.raises(GuardViolation) as excinfo:
        enforce(["G-PREREG", "G-NOPROG"],
                GuardCtx(registered_spec_sha=None, round_no_new_info=0))
    assert excinfo.value.guard == "g_prereg"  # 首个违规即停
    with pytest.raises(GuardViolation) as excinfo:
        enforce(["G-NOT-A-GUARD"], GuardCtx())
    assert excinfo.value.code == "UNKNOWN_GUARD"
