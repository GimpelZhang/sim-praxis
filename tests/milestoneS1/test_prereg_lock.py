"""S1-0：预注册件与双签 LOCK（AC-01 的机器半边）。

覆盖：build 后单签不合格、双签互异通过、改件即 bundle 失效（LOCK 拒绝）、
manifest 逐件 sha 复算。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_MODULE = Path(__file__).resolve().parents[2] / "scripts" / "s1" / "prereg_studies.py"


@pytest.fixture()
def prereg(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("s1_prereg", _MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "prereg"
    manifest = module.build_prereg(root)
    return module, root, manifest


def test_build_writes_four_files_and_manifest(prereg):
    module, root, manifest = prereg
    for name in ("study_A_N.json", "study_B_N.json", "protocol.json",
                 "budgets.json", "manifest.json"):
        assert (root / name).is_file(), name
    assert manifest["bundle_sha256"]
    a = json.loads((root / "study_A_N.json").read_text())
    b = json.loads((root / "study_B_N.json").read_text())
    assert a["n_confirm_runs"] == 5 * 32 == 160
    assert b["n_confirm_runs"] == 4 * 32 == 128
    assert a["n_block"] == 32
    assert {c["id"] for c in a["contrasts"]} == {"friction_x_mass",
                                                 "negative_torsional"}
    budgets = json.loads((root / "budgets.json").read_text())
    assert budgets["gateway"]["hard_cap_posts"] == 40
    assert budgets["session"]["hard_cap_sessions"] == 20


def test_single_signature_rejected(prereg):
    module, root, _ = prereg
    module.sign(signer="lane-a", role="executor", root=root)
    with pytest.raises(SystemExit) as excinfo:
        module.verify_prereg_lock(root)
    assert "SINGLE-SIGN" in str(excinfo.value)


def test_two_distinct_signatures_verify(prereg):
    module, root, _ = prereg
    module.sign(signer="lane-a", role="executor", root=root)
    module.sign(signer="lane-b", role="reviewer", root=root)
    report = module.verify_prereg_lock(root)
    assert report["signers"] == ["lane-a", "lane-b"]


def test_same_signer_twice_rejected(prereg):
    module, root, _ = prereg
    module.sign(signer="lane-a", role="executor", root=root)
    with pytest.raises(SystemExit):
        module.sign(signer="lane-a", role="reviewer", root=root)


def test_tampered_file_breaks_lock(prereg):
    module, root, manifest = prereg
    module.sign(signer="lane-a", role="executor", root=root)
    module.sign(signer="lane-b", role="reviewer", root=root)
    doc = json.loads((root / "study_A_N.json").read_text())
    doc["n_confirm_runs"] = 159  # 改件即新 bundle
    (root / "study_A_N.json").write_text(json.dumps(doc, sort_keys=True) + "\n")
    with pytest.raises(SystemExit) as excinfo:
        module.verify_prereg_lock(root)
    assert "PRE-REG-CHANGED" in str(excinfo.value)


def test_rebuild_drops_old_lock(prereg):
    module, root, _ = prereg
    module.sign(signer="lane-a", role="executor", root=root)
    module.sign(signer="lane-b", role="reviewer", root=root)
    module.build_prereg(root)  # 新 bundle → LOCK 移除
    with pytest.raises(SystemExit):
        module.verify_prereg_lock(root)
