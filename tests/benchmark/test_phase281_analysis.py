"""Phase 2.8.1 P281-9 终局分析 —— AC-P281-9-01..07（计划 §13.6）。

两层，与 P281-7 同一条纪律（**不许 skip 放水**）：

* **合成层**（默认两排除套就跑）：在 tmp 根上造 6 个 / 18 个 Trial，逐条驱动
  ``build_final_report`` 与 ``verdict`` 的纯函数。生产账一个字节不碰。
* **活产物层**（``kb_integration`` 标记）：读**真的** ``final_report.json``，
  断言 18 行的形状、五条假设的「未跑」事由码与报告文本的扫描结果。文件不在
  即失败——收尾车道上这个文件必须已经在场。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
for _p in (str(_REPO), str(_REPO / "scripts" / "verification"),
           str(_REPO / "scripts" / "benchmark")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import check_phase281_closure as judge  # noqa: E402
from orchestrator.benchmark.phase281 import analysis, constants  # noqa: E402
from orchestrator.benchmark.phase281 import verdict as v  # noqa: E402
from tests.benchmark import phase281_closeout_fixtures as fx  # noqa: E402
from tests.kb_support import ENV_FLAG as KB_INTEGRATION_FLAG  # noqa: E402

LIVE_GATE = KB_INTEGRATION_FLAG
live = pytest.mark.skipif(
    not __import__("os").environ.get(LIVE_GATE),
    reason=f"{LIVE_GATE} 未开（活产物车道只在收尾显式开闸时跑）")


def write_final_report(root: Path, **kwargs) -> dict:
    """按 ``check_final_report`` 的重算口径落盘（同 indent / sort_keys / 尾巴）。"""
    report = analysis.build_final_report(root, **kwargs)
    path = Path(root) / "analysis" / "final_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                               sort_keys=True) + "\n", encoding="utf-8")
    return report


# ---------------------------------------------------------------------------
# AC-P281-9-01：18 个 Trial 无遗漏无重复
# ---------------------------------------------------------------------------

def test_ac_p281_9_01_all_trials(tmp_path):
    root = fx.make_root(tmp_path / "root", reps=(0,))
    report = write_final_report(root)
    ids = [r["trial_id"] for r in report["trials"]]
    expected = [constants.trial_id(t, a, r) for t in constants.TASK_IDS
                for a in constants.ARMS for r in range(constants.REPS_TOTAL)]
    # 顺序是「重复在外、任务/臂在内」（``collect_phase_trials`` 的 rep 循环），
    # 故按集合比；顺序本身不是判据，覆盖与去重才是。
    assert sorted(ids) == sorted(expected)
    assert len(set(ids)) == 18
    assert report["pre_registered_trials"] == 18
    assert report["trial_counts"] == {"pre_registered": 18, "present": 6,
                                      "not_run": 12, "pilot_present": 6,
                                      "confirmatory_present": 0}
    problems, _ = judge.check_final_report(root)
    assert problems == [], problems


def test_ac_p281_9_01_negative_missing_and_duplicate(tmp_path):
    """负例：少一行 / 多一行重复，机判都必须当场判红。"""
    root = fx.make_root(tmp_path / "root", reps=(0,))
    report = write_final_report(root)

    dropped = dict(report, trials=report["trials"][:-1])
    (root / "analysis" / "final_report.json").write_text(
        json.dumps(dropped, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    problems, _ = judge.check_final_report(root)
    assert any("缺 Trial" in p for p in problems), problems

    duped = dict(report, trials=report["trials"] + [report["trials"][0]])
    (root / "analysis" / "final_report.json").write_text(
        json.dumps(duped, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    problems, _ = judge.check_final_report(root)
    assert any("Trial 重复" in p for p in problems), problems


# ---------------------------------------------------------------------------
# AC-P281-9-02：三态枚举 + 语义车道自洽
# ---------------------------------------------------------------------------

def test_ac_p281_9_02_verdicts_enum_enabled(tmp_path):
    root = fx.make_root(tmp_path / "root", reps=(0,))
    report = write_final_report(root, semantic_lane="enabled",
                                semantic_lane_reviewers=("BigG", "BigC"))
    assert set(report["hypotheses"]) == set(v.HYPOTHESIS_IDS)
    for hid, doc in report["hypotheses"].items():
        v.assert_partition(hid, doc["state"])
        assert doc["state"] in v.HYPOTHESIS_STATES
    assert report["semantic_lane"] == "enabled"
    assert report["semantic_lane_reviewers"] == ["BigG", "BigC"]
    problems, _ = judge.check_final_report(root)
    assert problems == [], problems


def test_ac_p281_9_02_verdicts_enum_disabled(tmp_path):
    """disabled 分支：H4 不得为 CD，且限制节逐字含「R1 仅得结构性结论」。"""
    root = fx.make_root(tmp_path / "root", reps=(0,))
    report = write_final_report(root, semantic_lane="disabled",
                                semantic_lane_reviewers=())
    assert report["hypotheses"]["H4"]["state"] != "CAPABILITY_DEMONSTRATED"
    assert any("R1 仅得结构性结论" in line for line in report["limitations"])
    problems, _ = judge.check_final_report(root)
    assert problems == [], problems


def test_ac_p281_9_02_negative_self_inconsistent(tmp_path):
    root = fx.make_root(tmp_path / "root", reps=(0,))
    report = write_final_report(root, semantic_lane="enabled",
                                semantic_lane_reviewers=("BigG", "BigC"))

    # ① disabled 却判 H4 为 CD
    bad = json.loads(json.dumps(report))
    bad["semantic_lane"] = "disabled"
    bad["hypotheses"]["H4"]["state"] = "CAPABILITY_DEMONSTRATED"
    (root / "analysis" / "final_report.json").write_text(
        json.dumps(bad, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    problems, _ = judge.check_final_report(root)
    assert any("disabled 却把 H4 判为" in p for p in problems), problems

    # ② enabled 却没有两名复核人
    bad = json.loads(json.dumps(report))
    bad["semantic_lane_reviewers"] = []
    (root / "analysis" / "final_report.json").write_text(
        json.dumps(bad, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    problems, _ = judge.check_final_report(root)
    assert any("两名复核人" in p for p in problems), problems

    # ③ 三态之外的枚举值
    bad = json.loads(json.dumps(report))
    bad["hypotheses"]["H1"]["state"] = "PASS"
    (root / "analysis" / "final_report.json").write_text(
        json.dumps(bad, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    problems, _ = judge.check_final_report(root)
    assert any("不在三态值域" in p for p in problems), problems


# ---------------------------------------------------------------------------
# AC-P281-9-03：可复算
# ---------------------------------------------------------------------------

def test_ac_p281_9_03_deterministic(tmp_path):
    root = fx.make_root(tmp_path / "root", reps=(0,))
    first = json.dumps(analysis.build_final_report(root), ensure_ascii=False,
                       indent=2, sort_keys=True)
    second = json.dumps(analysis.build_final_report(root), ensure_ascii=False,
                        indent=2, sort_keys=True)
    assert first == second
    write_final_report(root)
    problems, detail = judge.check_final_report(root)
    assert detail["deterministic"] is True
    assert problems == [], problems


def test_ac_p281_9_03_negative_not_recomputable(tmp_path):
    """负例：磁盘件被改一个空格 ⇒ 不复算 ⇒ 红。"""
    root = fx.make_root(tmp_path / "root", reps=(0,))
    write_final_report(root)
    path = root / "analysis" / "final_report.json"
    text = path.read_text(encoding="utf-8")
    assert '"go_no_go": "GO"' in text, "合成根 6 个 Trial 全在场且全 BORN ⇒ GO"
    path.write_text(text.replace('"go_no_go": "GO"', '"go_no_go":  "GO"'),
                    encoding="utf-8")
    problems, detail = judge.check_final_report(root)
    assert detail["deterministic"] is False
    assert any("不可复算" in p for p in problems), problems


# ---------------------------------------------------------------------------
# AC-P281-9-04：敏感性（改坏一个 Trial 的判决 → 假设判决按规则改变）
# ---------------------------------------------------------------------------

def test_ac_p281_9_04_sensitivity(tmp_path):
    root = fx.make_confirmatory_root(tmp_path / "clean")
    base = analysis.build_final_report(root)
    assert base["hypotheses"]["H1"]["state"] == "INCONCLUSIVE", \
        "两臂在 L1 上各 2 个 PASS ⇒ 打平 ⇒ INCONCLUSIVE（不是 CD）"

    flipped = constants.trial_id("libero_env_adapter_v1", "legacy_v1", 1)
    root2 = fx.make_confirmatory_root(
        tmp_path / "flipped", verdicts={flipped: "FAIL"})
    after = analysis.build_final_report(root2)
    assert after["hypotheses"]["H1"]["state"] == "CAPABILITY_DEMONSTRATED"
    assert after["hypotheses"]["H1"]["reason_code"] == "AGENTIC_GREATER"
    assert after["hypotheses"]["H1"] != base["hypotheses"]["H1"]


def test_ac_p281_9_04_real_root_stays_honest():
    """真根没跑确认性 ⇒ 五条一律 CONFIRMATORY_NOT_RUN，不许被敏感性测试带跑。"""
    root = Path("/mnt/artifacts/simpraxis/phase281")
    path = root / "analysis" / "final_report.json"
    if not path.is_file():  # 合成环境（无付产物）不在这里判红
        pytest.skip("活产物不在场：本用例只在收尾车道读真根")
    report = json.loads(path.read_text(encoding="utf-8"))
    if report["trial_counts"]["confirmatory_present"]:
        pytest.skip("确认性已跑：本阶段的诚实负例不再适用")
    for hid, doc in report["hypotheses"].items():
        assert doc["reason_code"] == "CONFIRMATORY_NOT_RUN", hid


# ---------------------------------------------------------------------------
# AC-P281-9-05：NOT_ESTABLISHED 与 INCONCLUSIVE 互斥穷尽（逐分支）
# ---------------------------------------------------------------------------

def test_ac_p281_9_05_partition():
    # H1：三分支
    assert v.decide_h1(2, 1)["state"] == "CAPABILITY_DEMONSTRATED"
    assert v.decide_h1(1, 1)["state"] == "INCONCLUSIVE"
    assert v.decide_h1(1, 2)["state"] == "NOT_ESTABLISHED"
    # H2：含 +∞（None）的全部组合
    assert v.decide_h2(2, 3)["state"] == "CAPABILITY_DEMONSTRATED"
    assert v.decide_h2(3, 3)["state"] == "INCONCLUSIVE"
    assert v.decide_h2(4, 3)["state"] == "NOT_ESTABLISHED"
    assert v.decide_h2(None, None)["state"] == "INCONCLUSIVE"
    assert v.decide_h2(None, 3)["state"] == "NOT_ESTABLISHED"
    assert v.decide_h2(3, None)["state"] == "CAPABILITY_DEMONSTRATED"
    # H3：绝对值比较
    assert v.decide_h3(0, 2)["state"] == "CAPABILITY_DEMONSTRATED"
    assert v.decide_h3(2, 2)["state"] == "INCONCLUSIVE"
    assert v.decide_h3(3, 2)["state"] == "NOT_ESTABLISHED"
    assert v.decide_h3(-1, 2)["state"] == "CAPABILITY_DEMONSTRATED"
    # H5：两分支（≤0 一律 NOT_ESTABLISHED，没有「打平」这一态）
    assert v.decide_h5(10.0, 9.0)["state"] == "CAPABILITY_DEMONSTRATED"
    assert v.decide_h5(9.0, 9.0)["state"] == "NOT_ESTABLISHED"
    assert v.decide_h5(8.0, 9.0)["state"] == "NOT_ESTABLISHED"


def test_ac_p281_9_05_h4_branches():
    common = dict(structure_agentic_median=1.0, structure_legacy_median=1.0)
    assert v.decide_h4(structure_worse_reps=(1,), semantic_lane="enabled",
                       **common)["state"] == "NOT_ESTABLISHED"
    assert v.decide_h4(structure_worse_reps=(), structure_present=False,
                       semantic_lane="enabled", **common)["state"] == "INCONCLUSIVE"
    assert v.decide_h4(structure_worse_reps=(), semantic_lane="disabled",
                       **common)["state"] == "INCONCLUSIVE"
    assert v.decide_h4(structure_worse_reps=(), semantic_lane="enabled",
                       semantic_present=False, **common)["state"] == "INCONCLUSIVE"
    assert v.decide_h4(structure_worse_reps=(), semantic_lane="enabled",
                       semantic_present=True, semantic_agentic_median=0.9,
                       semantic_legacy_median=0.8, **common)["state"] == \
        "CAPABILITY_DEMONSTRATED"
    assert v.decide_h4(structure_worse_reps=(), semantic_lane="enabled",
                       semantic_present=True, semantic_agentic_median=0.7,
                       semantic_legacy_median=0.8, **common)["state"] == \
        "NOT_ESTABLISHED"
    assert v.decide_h4(structure_worse_reps=(), semantic_lane="enabled",
                       semantic_present=True, semantic_agentic_median=0.9,
                       semantic_legacy_median=0.8, structure_agentic_median=0.5,
                       structure_legacy_median=0.9)["state"] == "NOT_ESTABLISHED"
    with pytest.raises(ValueError):
        v.decide_h4(structure_worse_reps=(), semantic_lane="maybe", **common)


def test_ac_p281_9_05_reason_codes_are_disjoint():
    """同一假设里，一个事由码只能对应一个状态（不会两条路径给同一个判决）。"""
    codes = []
    for fn, args in (
        (v.decide_h1, [(2, 1), (1, 1), (1, 2)]),
        (v.decide_h2, [(1, 2), (1, 1), (2, 1), (None, None), (None, 1), (1, None)]),
        (v.decide_h3, [(0, 1), (1, 1), (2, 1)]),
        (v.decide_h5, [(2.0, 1.0), (1.0, 1.0)]),
    ):
        for arg in args:
            doc = fn(*arg)
            v.assert_partition(doc["id"], doc["state"])
            codes.append((doc["id"], doc["reason_code"], doc["state"]))
    by_code: "dict[tuple[str, str], set[str]]" = {}
    for hid, code, state in codes:
        by_code.setdefault((hid, code), set()).add(state)
    ambiguous = {k: sorted(s) for k, s in by_code.items() if len(s) > 1}
    assert not ambiguous, f"同一事由码对应多个状态：{ambiguous}"


# ---------------------------------------------------------------------------
# verdict 纯函数：失败四分类 / 首出生轮次 / 中位数
# ---------------------------------------------------------------------------

def test_classify_failure_table():
    assert v.classify_failure("BLOCKED_INFRA", None) == "BLOCKED_INFRA"
    assert v.classify_failure("INVALID_ARTIFACT", "B4") == "INVALID_ARTIFACT"
    assert v.classify_failure("PASS", "B4") == "NONE"
    assert v.classify_failure("FAIL", "B4") == "MODEL_FAILURE"
    assert v.classify_failure("FAIL", "B3") == "TASK_FAILURE"
    assert v.classify_failure("FAIL", None) == "TASK_FAILURE"
    with pytest.raises(ValueError):
        v.classify_failure("MAYBE", None)


def test_first_birth_round_zero_is_never_born():
    """运行器用 0 表示「从未可解析」；直接拿 0 参与中位数会把方向反过来。"""
    assert v.first_birth_round("BORN", 0) is None
    assert v.first_birth_round("BORN", None) is None
    assert v.first_birth_round("NOT_BORN", 3) is None
    assert v.first_birth_round("BORN", 3) == 3
    assert v.first_birth_round("BORN", True) is None


def test_median_rounds_treats_none_as_infinity():
    assert v.median_rounds([None, 2, 3]) == 3
    assert v.median_rounds([2, 3]) == 2          # 下中位，整数轮次
    assert v.median_rounds([None, None]) is None
    assert v.median_rounds([]) is None
    assert v.median_rounds([1, 1, None, None]) == 1


# ---------------------------------------------------------------------------
# AC-P281-9-06 / 07：禁用措辞与首页声明
# ---------------------------------------------------------------------------

def _render(tmp_path: Path) -> str:
    root = fx.make_root(tmp_path / "root", reps=(0,))
    report = write_final_report(root)
    return analysis.render_report_markdown(report)


def test_ac_p281_9_07_sample_size_disclaimer(tmp_path):
    text = _render(tmp_path)
    # 「首页」= 报告最上头的那几行（标题之下）：不埋在附录里即算。
    head = "\n".join(text.splitlines()[:8])
    assert v.SAMPLE_SIZE_DISCLAIMER in head
    assert text.index(v.SAMPLE_SIZE_DISCLAIMER) < 400
    assert v.disclaimer_sha256() in text
    assert "首页声明块 sha256" in text


def test_ac_p281_9_06_forbidden_phrasing(tmp_path):
    text = _render(tmp_path)
    assert v.scan_forbidden(
        text, registered_disclaimer_sha256=v.disclaimer_sha256()) == []


def test_ac_p281_9_06_negative_outside_block_still_red(tmp_path):
    """剔除块**之外**的同一个词仍判红——豁免不等于整篇放行。"""
    text = _render(tmp_path) + "\n本实验证明了 agentic 更强。\n"
    hits = v.scan_forbidden(text, registered_disclaimer_sha256=v.disclaimer_sha256())
    assert [h["pattern"] for h in hits] == ["证明了"]


def test_ac_p281_9_06_negative_tampered_block_not_exempted(tmp_path):
    """把首页声明改一个字 ⇒ 哈希对不上 ⇒ 块内的词立刻判红（防篡改）。"""
    text = _render(tmp_path).replace("不足以做统计显著性检验", "不足以做检验")
    hits = v.scan_forbidden(text, registered_disclaimer_sha256=v.disclaimer_sha256())
    patterns = {h["pattern"] for h in hits}
    assert {"显著性"} <= patterns, hits


def test_ac_p281_9_06_negative_blanket_exemption_is_red(tmp_path):
    """负例：把整句豁免写进扫描器就会被这组断言抓住（这里验 strip 的边界）。"""
    text = _render(tmp_path)
    # 只剔除声明块，块外一个字的差别都要保留
    stripped = v.strip_disclaimer(text, v.disclaimer_sha256())
    assert v.SAMPLE_SIZE_DISCLAIMER not in stripped
    assert len(stripped) == len(text) - len(v.SAMPLE_SIZE_DISCLAIMER)
    # 错误的 sha256 ⇒ 不剔除（不给「改个说法再豁免」留口子）
    assert v.strip_disclaimer(text, "0" * 64) == text


# ---------------------------------------------------------------------------
# 活产物层
# ---------------------------------------------------------------------------

@live
def test_ac_p281_9_01_live_final_report():
    root = Path("/mnt/artifacts/simpraxis/phase281")
    report = json.loads((root / "analysis" / "final_report.json")
                        .read_text(encoding="utf-8"))
    assert frozenset(report) == analysis.FINAL_REPORT_FIELDS
    ids = [r["trial_id"] for r in report["trials"]]
    assert len(ids) == 18 and len(set(ids)) == 18
    assert report["trial_counts"]["present"] == 6
    problems, detail = judge.check_final_report(root)
    assert problems == [], problems
    assert detail["deterministic"] is True


@live
def test_ac_p281_9_06_live_report_text():
    root = Path("/mnt/artifacts/simpraxis/phase281")
    text = (root / "reports" / "final_report.md").read_text(encoding="utf-8")
    problems, detail = judge.check_report_text(root, text=text)
    assert problems == [], problems
    assert detail["forbidden_hits"] == 0
    assert detail["disclaimer_sha256"] == v.disclaimer_sha256()


# ---------------------------------------------------------------------------
# 评审 CR-31：空 token 样本不得折成 0.0（H5 的缺失数据 → INCONCLUSIVE）
# ---------------------------------------------------------------------------

def test_f5_cr31_token_median_empty_is_none_and_h5_inconclusive():
    """无账行 ⇒ ``_token_median=None`` ⇒ H5 ``INCONCLUSIVE``/``TOKEN_LEDGER_MISSING``。

    缺陷形态：空样本返回 0.0，H5 把「该臂没有账」读成「agentic 不更贵」，
    给出 ``NOT_ESTABLISHED`` 的方向性结论。
    """
    assert analysis._token_median([], "agentic_v1", {}) is None
    rows = [{"arm_id": "agentic_v1", "trial_id": "t1"},
            {"arm_id": "agentic_v1", "trial_id": "t2"}]
    # 偶数样本取中间两值均值（token 是连续量；与轮次的「下中位」不同）
    tokens = {"t1": {"tokens_in": 10, "tokens_out": 0},
              "t2": {"tokens_in": 100, "tokens_out": 10}}
    assert analysis._token_median(rows, "agentic_v1", tokens) == 60.0
    for args in ((None, 5.0), (5.0, None), (None, None)):
        doc = v.decide_h5(*args)
        assert doc["state"] == "INCONCLUSIVE", doc
        assert doc["reason_code"] == "TOKEN_LEDGER_MISSING", doc
        v.assert_partition(doc["id"], doc["state"])
    # 数值路径不变：≤ 即 NOT_ESTABLISHED，> 即 CAPABILITY_DEMONSTRATED
    assert v.decide_h5(10.0, 9.0)["state"] == "CAPABILITY_DEMONSTRATED"
    assert v.decide_h5(9.0, 9.0)["state"] == "NOT_ESTABLISHED"


# ---------------------------------------------------------------------------
# 评审 CR-33：limitations 的「触帽/预算」声明必须由数据推出
# ---------------------------------------------------------------------------

def test_f5_cr33_limitations_derive_cap_and_budget_from_ledger():
    """账本调用数 ≥ HARD_CAP 时不得写「未触」；确认性 Trial 在场时不得写「未用」。"""
    pilot_only = [{"present": True, "status": "FINISHED", "verdict": "PASS",
                   "trial_id": "pilot", "phase": "pilot"}]
    confirmatory = pilot_only + [
        {"present": True, "status": "FINISHED", "verdict": "PASS",
         "trial_id": f"c{i}", "phase": "confirmatory"} for i in range(2)]

    def _render(rows, calls):
        return " ".join(analysis._limitations(
            rows, go_no_go="HOLD", go_reasons=("hash_freeze",),
            ledger={"calls": calls}, semantic_lane="disabled"))

    unused = _render(pilot_only, 63)
    assert "硬帽 180 未触" in unused, unused
    assert "预算未被使用" in unused, unused

    hit = _render(pilot_only, constants.HARD_CAP + 20)
    assert "硬帽 180 已触" in hit, hit
    assert "未触" not in hit.split("账本合计", 1)[1].split("。", 1)[0], hit

    used = _render(confirmatory, 63)
    assert "确认性阶段预算已使用" in used, used
    assert "预算未被使用" not in used, used
