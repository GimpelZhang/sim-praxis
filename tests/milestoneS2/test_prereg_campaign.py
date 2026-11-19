"""S2 双预注册 LOCK（AC-01）：规则面先冻结、两锁分签、改件即失效。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def campaign(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_RSI_S2_ROOT", str(tmp_path / "rsi_s2"))
    path = _REPO / "scripts" / "s2" / "prereg_campaign.py"
    spec = importlib.util.spec_from_file_location("s2_prereg_campaign", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, tmp_path / "rsi_s2"


def test_rules_build_sign_verify(campaign):
    module, root = campaign
    manifest = module.build_rules()
    assert manifest["bundle_sha256"]
    rules_doc = json.loads((root / "prereg" / "rules" / "rules.json").read_text())
    assert rules_doc["verdict_vocabulary"]["values"] == [
        "SUPPORTED", "REFUTED", "NOT_DETECTED", "EQUIVALENT", "INCONCLUSIVE"]
    assert rules_doc["report_contract"]["sections"]
    assert rules_doc["nc_registration"]["criteria"]
    assert "orchestrator/hk/verdicts.py" in rules_doc["rule_anchors_sha256"]

    module.sign(which="rules", signer="s2-spec", role="executor")
    with pytest.raises(SystemExit) as excinfo:
        module.verify_lock("rules")
    assert "SINGLE-SIGN" in str(excinfo.value)
    module.sign(which="rules", signer="s2-audit", role="reviewer")
    report = module.verify_lock("rules")
    assert report["signers"] == ["s2-spec", "s2-audit"]


def test_same_signer_twice_rejected(campaign):
    module, _ = campaign
    module.build_rules()
    module.sign(which="rules", signer="s2-spec", role="executor")
    with pytest.raises(SystemExit):
        module.sign(which="rules", signer="s2-spec", role="reviewer")


def test_tampered_rules_break_lock(campaign):
    module, root = campaign
    module.build_rules()
    module.sign(which="rules", signer="a", role="executor")
    module.sign(which="rules", signer="b", role="reviewer")
    path = root / "prereg" / "rules" / "rules.json"
    doc = json.loads(path.read_text())
    doc["version"] = 99  # 改件即新 bundle
    path.write_text(json.dumps(doc, sort_keys=True) + "\n")
    with pytest.raises(SystemExit) as excinfo:
        module.verify_lock("rules")
    assert "PRE-REG-CHANGED" in str(excinfo.value)


def test_campaign_requires_rules_lock(campaign):
    module, _ = campaign
    with pytest.raises(SystemExit) as excinfo:
        module.build_campaign()
    assert "RULES-UNLOCKED" in str(excinfo.value)


def test_campaign_build_sign_verify_binds_rules(campaign):
    module, root = campaign
    rules_manifest = module.build_rules()
    module.sign(which="rules", signer="s2-spec", role="executor")
    module.sign(which="rules", signer="s2-audit", role="reviewer")
    manifest = module.build_campaign()
    assert manifest["rules_bundle_sha256"] == rules_manifest["bundle_sha256"]
    design = json.loads((root / "prereg" / "campaign" / "design.json").read_text())
    assert design["n_instances"] == 6
    assert design["statistics"]["n_sign_patterns"] == 64
    assert design["statistics"]["min_p"] == "0.015625"
    assert design["paired_unit"] == "instance"
    assert design["arms"] == ["v0", "v1"]
    assert len(design["sample_reduction_order"]) == 6
    assert design["non_inferiority"]["rerun_n_diffs"] == 0

    module.sign(which="campaign", signer="s2-camp-spec", role="executor")
    with pytest.raises(SystemExit):
        module.verify_lock("campaign")
    module.sign(which="campaign", signer="s2-camp-audit", role="reviewer")
    report = module.verify_lock("campaign")
    assert report["which"] == "campaign"
    assert report["signers"] == ["s2-camp-spec", "s2-camp-audit"]


def test_rebuild_drops_lock(campaign):
    module, root = campaign
    module.build_rules()
    module.sign(which="rules", signer="a", role="executor")
    module.sign(which="rules", signer="b", role="reviewer")
    module.build_rules()
    with pytest.raises(SystemExit):
        module.verify_lock("rules")
