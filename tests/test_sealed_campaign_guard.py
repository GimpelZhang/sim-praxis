"""CR-11 回归：RSI 家族「已结案 campaign」的账本必须拒绝写打开。

封存判据是 campaign 根下 ``closeout/`` 的结案产物件（各家族统一写在那里），
而不是把不可枚举的 campaign 路径塞进 ``sealed_ledger_paths()``。
"""

from pathlib import Path

import pytest

from orchestrator.common.runtime_paths import (
    SealedLedgerError,
    assert_not_sealed_ledger,
    closed_campaign_root,
    open_ledger_append,
)


def _campaign(tmp_path: Path, *, readout: str | None) -> Path:
    root = tmp_path / "rsi_r12" / "r12-test"
    (root / "quota").mkdir(parents=True)
    (root / "quota" / "ledger.jsonl").write_text("", encoding="utf-8")
    if readout is not None:
        (root / "closeout").mkdir()
        (root / "closeout" / readout).write_text("{}", encoding="utf-8")
    return root


@pytest.mark.parametrize("readout", ["r12_readout.json", "r1_readout.json",
                                    "closeout_zero_paid.json", "closeout_report.json"])
def test_closed_campaign_ledger_is_sealed(tmp_path: Path, readout: str) -> None:
    root = _campaign(tmp_path, readout=readout)
    ledger = root / "quota" / "ledger.jsonl"

    assert closed_campaign_root(ledger) == root
    with pytest.raises(SealedLedgerError):
        assert_not_sealed_ledger(ledger)
    with pytest.raises(SealedLedgerError):
        open_ledger_append(ledger)


def test_open_campaign_ledger_stays_writable(tmp_path: Path) -> None:
    root = _campaign(tmp_path, readout=None)
    ledger = root / "quota" / "ledger.jsonl"

    assert closed_campaign_root(ledger) is None
    assert assert_not_sealed_ledger(ledger) == ledger
    fh = open_ledger_append(ledger)
    fh.close()
    assert ledger.read_text(encoding="utf-8") == ""


def test_marker_only_applies_to_quota_ledger_shape(tmp_path: Path) -> None:
    root = _campaign(tmp_path, readout="r12_readout.json")
    # 同名文件但不在 campaign/quota 形状下：不当作账本
    other = root / "quota" / "other.jsonl"
    other.write_text("", encoding="utf-8")
    assert closed_campaign_root(other) is None
    assert closed_campaign_root(root / "ledger.jsonl") is None
