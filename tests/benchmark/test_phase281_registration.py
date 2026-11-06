"""P281-5 预注册 / LOCK 测试（AC-P281-5-01 … -13，计划 §9.6）。

这一组断言守护的是「**付费之前那一刻的设计不可事后修改**」：

* 齐备（01）—— 8 件预注册件存在且可解析；缺一件即红。
* 顺序（02）—— 账本已有 ``event=="call"`` 行时 ``write_lock`` 必须**拒绝**。
  先花钱再补 LOCK，预注册就变成了事后叙事。
* 可破（04/05）—— 改任一预注册件的 1 个字节、或改 ``trial_order.json``，
  ``verify_lock`` 必须**指名道姓**地报出来（STOP-13）。
* 不漂移（06/07/10/11/12）—— 臂参数与 ``ARMS`` 双向等值、运行时要求是派生件、
  预算三数与常量逐值相等、统计面没有禁用方法、17 条 STOP 一条不多一条不少。
* 自锚（09）—— ``lock_sha256`` 可复算（置空 → 规范化 → sha256）。
* 批准件（13）—— 「用户批准 LOCK」必须有可机判的落盘件，缺件/篡改一律拒。

**改动面**：测试一律在 ``tmp_path`` 里造合成预注册树（复制仓内真实 8 件 +
现算 criteria + 现算 order），只有「本件确实在场且未被改动」类断言直接读仓内，
因此跑测试不会写脏预注册目录。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from orchestrator.benchmark.phase281 import arm_adapter, constants, registration

BENCH = (Path(registration._repo_root())
         / "benchmarks/research_engineering_v1")
PREREG = registration.prereg_root()
#: 数据根只读镜像（不在场时跳过镜像层断言——CI/新机器上它本就不存在）
_MIRROR = Path("/mnt/artifacts/simpraxis/phase281/pre_registration")


# --------------------------------------------------------------------------
# 合成预注册树（复制仓内真件；criteria / manifest / order 现算）
# --------------------------------------------------------------------------

def _make_tree(tmp_path: Path) -> Path:
    bench = tmp_path / "bench"
    (bench / "criteria").mkdir(parents=True)
    for task_id in constants.TASK_IDS:
        src = BENCH / "criteria" / f"{task_id}.criteria.json"
        (bench / "criteria" / src.name).write_bytes(src.read_bytes())
    prereg = bench / "experiments" / "roadmap_phase1"
    prereg.mkdir(parents=True)
    for name in registration.PREREG_FILES:
        (prereg / name).write_bytes((PREREG / name).read_bytes())
    (prereg / registration.ORDER_FILE).write_bytes(registration.order_bytes())
    locks = registration.criteria_locks(prereg)
    aggregate = registration.ts.criteria_aggregate_sha256(locks)
    manifest = {
        "schema": "simpraxis.benchmark_manifest.v1",
        "version": 1,
        "tasks": sorted(constants.TASK_IDS),
        "criteria_lock": aggregate,
    }
    (bench / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    return prereg


def _write_ledger(path: Path, rows: "list[dict]") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False,
                                       sort_keys=True) + "\n" for r in rows),
                    encoding="utf-8")
    return path


def _call_row(seq: int = 1) -> dict:
    return {"seq": seq, "event": "call", "ok": True, "stage": "coding_debug",
            "thread_id": "icy_ground_dr_v1__legacy_v1__rep0", "model": "m"}


def _selfcheck_row() -> dict:
    return {"seq": 1, "event": "selfcheck", "ok": True, "model": None}


def _lock(tmp_path: Path) -> "tuple[Path, dict]":
    """在合成树上写 LOCK（账本空），返回 (prereg 根, LOCK 文档)。"""
    prereg = _make_tree(tmp_path)
    ledger = _write_ledger(tmp_path / "quota" / "ledger.jsonl", [])
    doc = registration.write_lock(prereg / registration.LOCK_FILE,
                                  root=prereg, ledger=ledger)
    return prereg, doc


# --------------------------------------------------------------------------
# AC-P281-5-01：8 件齐备且可解析
# --------------------------------------------------------------------------

def test_ac_p281_5_01_prereg_complete(tmp_path):
    hashes = registration.collect_prereg_hashes()
    assert set(hashes) == set(registration.PREREG_FILES)
    assert len(hashes) == 8
    assert all(len(h) == 64 for h in hashes.values())
    # 逐件真的在仓内、真的可解析（collect 内部已 _load_doc，这里再核一次存在性）
    for name in registration.PREREG_FILES:
        assert (PREREG / name).is_file(), name
    # 负例①：缺件必须抛，且报出缺的是哪一件
    tree = _make_tree(tmp_path)
    (tree / "stopping.yaml").unlink()
    with pytest.raises(registration.RegistrationError, match="stopping.yaml"):
        registration.collect_prereg_hashes(tree)
    # 负例②：坏件必须抛（不可解析 = 不可预注册）
    tree2 = _make_tree(tmp_path / "second")
    (tree2 / "arms.yaml").write_text("arms: [{\n", encoding="utf-8")
    with pytest.raises(registration.RegistrationError, match="解析失败"):
        registration.collect_prereg_hashes(tree2)


# --------------------------------------------------------------------------
# AC-P281-5-02：账本有 call 行 → write_lock 拒绝
# --------------------------------------------------------------------------

def test_ac_p281_5_02_refuses_after_call(tmp_path):
    prereg = _make_tree(tmp_path)
    ledger = _write_ledger(tmp_path / "quota" / "ledger.jsonl", [_call_row()])
    target = prereg / registration.LOCK_FILE
    with pytest.raises(registration.LockRefused) as caught:
        registration.write_lock(target, root=prereg, ledger=ledger)
    assert "event=call" in str(caught.value)
    assert not target.exists(), "拒绝之后不许留下半个 LOCK"

    # 正例对照：只有开账 selfcheck 行（占 seq 不计实呼）→ 允许写，且如实记快照
    ledger2 = _write_ledger(tmp_path / "quota2" / "ledger.jsonl",
                            [_selfcheck_row()])
    doc = registration.write_lock(target, root=prereg, ledger=ledger2)
    assert doc["ledger_rows_at_lock"] == {"calls": 0, "total": 1}
    assert registration.ledger_counts(ledger2)["calls"] == 0


# --------------------------------------------------------------------------
# AC-P281-5-03：未改动件 → verify_lock 返回 []
# --------------------------------------------------------------------------

def test_ac_p281_5_03_verify_clean(tmp_path):
    prereg, doc = _lock(tmp_path)
    assert registration.verify_lock(prereg / registration.LOCK_FILE,
                                    root=prereg) == []
    # 幂等：再验一次仍是空表（verify 不写盘）
    before = (prereg / registration.LOCK_FILE).read_bytes()
    assert registration.verify_lock(prereg / registration.LOCK_FILE,
                                    root=prereg) == []
    assert (prereg / registration.LOCK_FILE).read_bytes() == before
    registration.verify_or_raise(prereg / registration.LOCK_FILE, root=prereg)
    # 镜像对账：逐字复制之后仍然空表
    mirror = tmp_path / "mirror"
    registration._write_mirror(mirror, before, root=prereg)
    assert registration.verify_lock(prereg / registration.LOCK_FILE,
                                    root=prereg, mirror_dir=mirror) == []


def test_ac_p281_5_03b_repo_lock_verifies():
    """仓内真值必须自洽：`verify_lock()` 对**入 git 的那份** LOCK 返回 `[]`。

    AC-03 用合成树证明「未改动 → 空表」，这条把同一断言钉在真实件上。
    没有它，改完预注册件却忘了重写 LOCK（或反过来）时，13 条 AC 全绿而
    `--verify` 在悄悄报 STOP-13——机判与人看的不是同一面。
    """
    assert registration.verify_lock(root=PREREG) == []
    # 镜像必须与仓内逐字节相等（§9.3 第一层），否则运行期读到的是另一份设计
    assert registration.verify_lock(root=PREREG,
                                    mirror_dir=_MIRROR) == [] or not _MIRROR.is_dir()


# --------------------------------------------------------------------------
# AC-P281-5-04：改任一预注册件 1 字节 → 报出该件名
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", registration.PREREG_FILES)
def test_ac_p281_5_04_tamper_detected(tmp_path, name):
    prereg, _doc = _lock(tmp_path)
    target = prereg / name
    original = target.read_bytes()
    target.write_bytes(original + b"\n")      # 1 字节：只加一个换行
    problems = registration.verify_lock(prereg / registration.LOCK_FILE,
                                        root=prereg)
    assert any(name in p for p in problems), problems
    # 还原后必须回到空表（证明报的是「被改」而不是「恒红」）
    target.write_bytes(original)
    assert registration.verify_lock(prereg / registration.LOCK_FILE,
                                    root=prereg) == []
    # verify_or_raise 在同样情形下抛 LockBroken（CLI 的 fail-closed 面）
    target.write_bytes(original + b"\n")
    with pytest.raises(registration.LockBroken):
        registration.verify_or_raise(prereg / registration.LOCK_FILE, root=prereg)


def test_ac_p281_5_04b_mirror_drift_detected(tmp_path):
    prereg, _doc = _lock(tmp_path)
    lock_bytes = (prereg / registration.LOCK_FILE).read_bytes()
    mirror = tmp_path / "mirror"
    registration._write_mirror(mirror, lock_bytes, root=prereg)
    assert registration.verify_lock(prereg / registration.LOCK_FILE,
                                    root=prereg, mirror_dir=mirror) == []
    # 直接编辑镜像 = 伪造预注册（§9.3）
    (mirror / "budget.yaml").write_bytes(
        (prereg / "budget.yaml").read_bytes() + "# 手改\n".encode("utf-8"))
    (mirror / registration.ORDER_FILE).unlink()
    problems = registration.verify_lock(prereg / registration.LOCK_FILE,
                                        root=prereg, mirror_dir=mirror)
    assert any("镜像漂移：budget.yaml" in p for p in problems), problems
    assert any("镜像缺件：trial_order.json" in p for p in problems), problems


# --------------------------------------------------------------------------
# AC-P281-5-05：改 trial_order.json → 报 trial_order_sha256 不符
# --------------------------------------------------------------------------

def test_ac_p281_5_05_order_tamper(tmp_path):
    prereg, _doc = _lock(tmp_path)
    order_path = prereg / registration.ORDER_FILE
    order = json.loads(order_path.read_text(encoding="utf-8"))
    key = registration.pair_key(constants.TASK_IDS[0], 0)
    order[key] = list(reversed(order[key]))          # 换先后顺序
    order_path.write_text(json.dumps(order, ensure_ascii=False, sort_keys=True,
                                     indent=2) + "\n", encoding="utf-8")
    problems = registration.verify_lock(prereg / registration.LOCK_FILE,
                                        root=prereg)
    assert any("trial_order_sha256" in p for p in problems), problems
    # 负例对照：只把「重新生成」跑一遍就回到空表（证明原文件是复算产物）
    registration.write_order(root=prereg)
    assert registration.verify_lock(prereg / registration.LOCK_FILE,
                                    root=prereg) == []


# --------------------------------------------------------------------------
# AC-P281-5-06：arms.yaml 与 ARMS 双向等值
# --------------------------------------------------------------------------

def test_ac_p281_5_06_arms_mirror():
    doc = yaml.safe_load((PREREG / "arms.yaml").read_text(encoding="utf-8"))
    arms = doc["arms"]
    assert set(arms) == set(constants.ARMS)
    for arm_id in constants.ARMS:
        live = arm_adapter.arm_params(arm_id)
        frozen = arms[arm_id]
        # 双向：键集合相等且逐值相等（少键、多键、值不等都红）
        assert set(frozen) == set(live), (arm_id, set(frozen) ^ set(live))
        for key in live:
            assert frozen[key] == live[key], (arm_id, key, frozen[key], live[key])
    # 负例：改一个帽值即不等
    broken = yaml.safe_load((PREREG / "arms.yaml").read_text(encoding="utf-8"))
    broken["arms"]["agentic_v1"]["max_coding_rounds"] = 12
    assert broken["arms"]["agentic_v1"]["max_coding_rounds"] != \
        constants.MAX_CODING_ROUNDS


# --------------------------------------------------------------------------
# AC-P281-5-07：runtime_requirement.yaml 是派生件（逐键相等，不漂移）
# --------------------------------------------------------------------------

def test_ac_p281_5_07_requirements_derived(tmp_path):
    from orchestrator.benchmark.phase281 import candidate_birth

    on_disk = (PREREG / "runtime_requirement.yaml").read_bytes()
    assert on_disk == registration.requirements_bytes(), (
        "runtime_requirement.yaml 与派生输出不一致：手写或未重跑 "
        "--derive-requirements")
    doc = yaml.safe_load(on_disk)
    assert set(doc["tasks"]) == set(constants.TASK_IDS)
    for task_id in constants.TASK_IDS:
        live = registration._plain(candidate_birth.load_task(task_id).runtime)
        frozen = doc["tasks"][task_id]["runtime"]
        assert set(frozen) == set(live), (task_id, set(frozen) ^ set(live))
        for key in live:
            assert frozen[key] == live[key], (task_id, key)
        # task_no 与 constants 逐值一致（账本归因用）
        assert doc["tasks"][task_id]["harness"]["task_no"] == \
            constants.TASK_NO_BY_ID[task_id]
        assert doc["tasks"][task_id]["harness"]["form"] == \
            constants.RUNTIME_BY_ID[task_id]
    # 负例：手写偏移（改 python 路径）必须被抓
    drifted = yaml.safe_load(on_disk)
    drifted["tasks"]["libero_env_adapter_v1"]["runtime"]["python"] = \
        "/opt/venv/bin/python"
    assert drifted["tasks"]["libero_env_adapter_v1"]["runtime"] != \
        registration._plain(candidate_birth.load_task(
            "libero_env_adapter_v1").runtime)
    # 派生函数本身不读盘上文件：它必须每次现算
    assert registration.derive_requirements()["tasks"] == doc["tasks"]


# --------------------------------------------------------------------------
# AC-P281-5-08：trial_order 由种子可复算（逐字节）
# --------------------------------------------------------------------------

def test_ac_p281_5_08_order_deterministic():
    first = registration.compute_trial_order()
    second = registration.compute_trial_order()
    assert first == second
    assert registration.order_bytes(first) == registration.order_bytes()
    assert (PREREG / registration.ORDER_FILE).read_bytes() == \
        registration.order_bytes()
    # 形态：9 对，每对恰两臂、且顺序确实是两臂的全排列之一
    assert len(first) == len(constants.TASK_IDS) * constants.REPS_TOTAL
    for key, arms in first.items():
        assert sorted(arms) == sorted(constants.ARMS), key
    # 逐字复算 §9.4：不调用被测函数，另写一遍公式
    import hashlib

    task_id, rep = constants.TASK_IDS[0], 0
    seed = hashlib.sha256(
        f"{constants.EXPERIMENT_ID}|{task_id}|{rep}".encode()).hexdigest()
    expected = sorted(constants.ARMS,
                      key=lambda a: hashlib.sha256(
                          f"{seed}|{a}".encode()).hexdigest())
    assert first[registration.pair_key(task_id, rep)] == expected


# --------------------------------------------------------------------------
# AC-P281-5-09：lock_sha256 自锚可复算
# --------------------------------------------------------------------------

def test_ac_p281_5_09_self_anchor(tmp_path):
    _prereg, doc = _lock(tmp_path)
    anchor = doc["lock_sha256"]
    assert len(anchor) == 64 and anchor == registration.lock_anchor(doc)
    # 置空 → 规范化 → sha256 的逐步复算（不借 lock_anchor 自己证明自己）
    import hashlib

    body = dict(doc)
    body["lock_sha256"] = ""
    manual = hashlib.sha256(registration.canonical_json(body).encode("utf-8")
                            ).hexdigest()
    assert manual == anchor
    # 任意一个被覆盖的字段改动 → 锚变
    for key in ("trial_order_sha256", "manifest_criteria_lock", "git_commit"):
        tampered = dict(doc)
        tampered[key] = "0" * 40
        assert registration.lock_anchor(tampered) != anchor, key
    # 落盘件与内存文档逐字一致（自锚吃的是同一串字节）
    on_disk = json.loads((tmp_path / "bench" / "experiments" / "roadmap_phase1"
                          / registration.LOCK_FILE).read_text(encoding="utf-8"))
    assert on_disk["lock_sha256"] == anchor
    assert registration.lock_bytes(on_disk) == registration.lock_bytes(doc)


# --------------------------------------------------------------------------
# AC-P281-5-10：预算三数与 constants 逐值相等
# --------------------------------------------------------------------------

def test_ac_p281_5_10_budget_matches_constants(tmp_path):
    doc = yaml.safe_load((PREREG / "budget.yaml").read_text(encoding="utf-8"))
    budgets = doc["budgets"]
    assert budgets["pilot"] == constants.PILOT_BUDGET == 60
    assert budgets["confirmatory"] == constants.CONFIRMATORY_BUDGET == 120
    assert budgets["hard_cap"] == constants.HARD_CAP == 180
    assert budgets["per_task_cap"] == constants.TASK_CAP == 60
    assert budgets["call_cap_per_trial"] == constants.CALL_CAP_PER_TRIAL == 10
    assert doc["max_attempts"] == 1
    # LOCK 里的预算块与 constants 同源（不是第二份手写值）
    _prereg, lock = _lock(tmp_path)
    assert lock["budget"] == {
        "pilot": constants.PILOT_BUDGET,
        "confirmatory": constants.CONFIRMATORY_BUDGET,
        "hard_cap": constants.HARD_CAP,
        "per_task_cap": constants.TASK_CAP,
        "call_cap_per_trial": constants.CALL_CAP_PER_TRIAL,
    }
    # 等式自洽：3 任务 × 2 臂 × 1 rep × 10 = pilot；× 2 rep = confirmatory
    assert (len(constants.TASK_ORDER) * len(constants.ARMS)
            * constants.REPS_PILOT * constants.CALL_CAP_PER_TRIAL) == 60
    assert (len(constants.TASK_ORDER) * len(constants.ARMS)
            * constants.REPS_CONFIRMATORY * constants.CALL_CAP_PER_TRIAL) == 120


# --------------------------------------------------------------------------
# AC-P281-5-11：statistics.yaml 没有禁用方法
# --------------------------------------------------------------------------

def _strings(value) -> "list[str]":
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: "list[str]" = []
        for k, v in value.items():
            out.extend(_strings(str(k)))
            out.extend(_strings(v))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(_strings(item))
        return out
    return []


def test_ac_p281_5_11_forbidden_stats_absent():
    doc = yaml.safe_load((PREREG / "statistics.yaml").read_text(encoding="utf-8"))
    forbidden = doc["forbidden"]
    keywords = [str(entry["keyword"]) for entry in forbidden]
    assert len(keywords) == len(set(keywords)), "禁令清单里有重复关键词"
    assert len(keywords) >= 8
    # ① 禁令清单之外**任何字符串**都不得含禁用词
    outside = _strings({k: v for k, v in doc.items() if k != "forbidden"})
    hits = [(kw, s) for s in outside for kw in keywords if kw in s]
    assert hits == [], f"禁用统计方法出现在允许面：{hits}"
    # ② 每个禁令必须在 forbidden 块里真的有出处（清单本身不许漏项）
    inside = " ".join(_strings(forbidden))
    missing = [kw for kw in keywords if kw not in inside]
    assert missing == []
    # 负例：把「显著」搬进允许面必须被抓
    bad = yaml.safe_load((PREREG / "statistics.yaml").read_text(encoding="utf-8"))
    bad["allowed"].append({"id": "STAT-BAD", "name": "差异显著",
                           "content": "两臂差异显著"})
    bad_outside = _strings({k: v for k, v in bad.items() if k != "forbidden"})
    assert [(kw, s) for s in bad_outside for kw in keywords if kw in s]


# --------------------------------------------------------------------------
# AC-P281-5-12：stopping.yaml 恰 17 条且每条有 detector
# --------------------------------------------------------------------------

def test_ac_p281_5_12_stop_map_complete():
    doc = yaml.safe_load((PREREG / "stopping.yaml").read_text(encoding="utf-8"))
    rows = doc["stops"]
    ids = [str(row["id"]) for row in rows]
    assert len(ids) == 17
    assert set(ids) == set(registration.STOP_IDS), (
        f"多/漏：{sorted(set(ids) ^ set(registration.STOP_IDS))}")
    assert ids == list(registration.STOP_IDS), "顺序必须与 §15.2 一致"
    for row in rows:
        detector = str(row.get("detector") or "").strip()
        assert detector, f"{row['id']} 没有 detector（只是祈祷，不会响）"
        assert str(row.get("trigger") or "").strip(), row["id"]
        assert str(row.get("action") or "").strip(), row["id"]
    # 负例①：漏一条 → 集合不等
    assert set(ids[:-1]) != set(registration.STOP_IDS)
    # 负例②：多一条 → 集合不等
    assert set(ids + ["STOP-18"]) != set(registration.STOP_IDS)
    # 负例③：空 detector → 逐条判红
    trimmed = [dict(row) for row in rows]
    trimmed[3]["detector"] = "   "
    assert [r["id"] for r in trimmed
            if not str(r.get("detector") or "").strip()] == [trimmed[3]["id"]]


# --------------------------------------------------------------------------
# AC-P281-5-13：用户批准 LOCK 的可机判落盘件
# --------------------------------------------------------------------------

def test_ac_p281_5_13_lock_consent_artifact(tmp_path):
    prereg, _doc = _lock(tmp_path)
    lock = prereg / registration.LOCK_FILE
    consent = tmp_path / "closeout" / "lock_consent.json"

    # ① 缺件即问题（且理由里说明「不得出终判」）
    problems = registration.verify_consent(consent, lock)
    assert len(problems) == 1 and "不得出终判" in problems[0], problems

    # ② 合规件 → 空表
    doc = registration.write_consent(
        consent, lock_path=lock,
        user_statement="用户逐字：批准 LOCK，按此预注册执行。",
        approved_at="2026-11-07T02:00:00Z")
    assert registration.verify_consent(consent, lock) == []
    assert doc["approved_by"] == "user"
    assert doc["lock_sha256"] == registration.file_sha256(lock)
    assert consent.stat().st_mode & 0o777 == 0o600

    # ③ sha 与 LOCK 不符 → 红（只写一句「用户已同意」而无 sha 也算不符）
    base = json.loads(consent.read_text(encoding="utf-8"))
    for mutate, needle in (
        (lambda d: d.update(lock_sha256="0" * 64), "lock_sha256 不符"),
        (lambda d: d.pop("lock_sha256", None), "lock_sha256 不符"),
        (lambda d: d.update(approved_by="agent"), "approved_by 必须是 user"),
        (lambda d: d.update(approved_at="2026-11-07T02:00:00+08:00"),
         "approved_at 必须是严格 Z"),
        (lambda d: d.update(approved_at="2026-11-07 02:00"), "严格 Z"),
        (lambda d: d.update(user_statement="  "), "user_statement 为空"),
    ):
        broken = json.loads(json.dumps(base))
        mutate(broken)
        consent.write_text(json.dumps(broken, ensure_ascii=False,
                                      sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(consent, 0o600)
        got = registration.verify_consent(consent, lock)
        assert any(needle in item for item in got), (needle, got)

    # ④ 权限不是 0600 → 红
    consent.write_text(json.dumps(base, ensure_ascii=False, sort_keys=True) + "\n",
                       encoding="utf-8")
    os.chmod(consent, 0o644)
    assert any("0600" in item for item in registration.verify_consent(consent, lock))

    # ⑤ 写件时没有用户原文 → 直接拒绝（No Fake Approval）
    with pytest.raises(registration.ConsentError):
        registration.write_consent(consent, lock_path=lock, user_statement="")

    # ⑥ LOCK 本身被改之后旧批准件自动失效（sha 绑定）。
    #    注意绑定面是 LOCK 的字节：预注册件漂移由 verify_lock 报（STOP-13），
    #    两条断言分工不同，互不替代。
    os.chmod(consent, 0o600)
    registration.write_consent(consent, lock_path=lock,
                               user_statement="用户逐字：批准 LOCK。",
                               approved_at="2026-11-07T02:00:00Z")
    assert registration.verify_consent(consent, lock) == []
    lock_payload = json.loads(lock.read_text(encoding="utf-8"))
    lock_payload["budget"]["hard_cap"] = 1800
    lock.write_text(json.dumps(lock_payload, ensure_ascii=False,
                               sort_keys=True) + "\n", encoding="utf-8")
    assert any("lock_sha256 不符" in item
               for item in registration.verify_consent(consent, lock))
