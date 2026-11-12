"""RSI-R1.2 T4：独立 replay 的候选分析（AC53）。

证明 ``replay_driver._r12_replay_analysis``：

* 只从**本批复跑**观测目录构造 fresh 视图（旧 confirm raw 不参与）；
* 载入**冻结源码字节**（登记 sha 与磁盘 sha 不等即拒）；
* 在独立子进程真实执行候选分析（``LocalRunner`` 跑生产沙盒视图/输出/回收）；
* 生成**新 receipt**（execution_id 来自本次容器），旧 receipt/output 不能替代；
* 宿主从复跑 raw 独立重算，与候选输出逐字段比较后才算 analysis_ok。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import assembly as A
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import judge as J
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import replay_driver as RPD
from orchestrator.rsi_r1 import state as S
from tests import rsi_r1_helpers as H
from tests.rsi_r1_rehearsal import REHEARSAL_IMAGE, LocalRunner

KEEPER = "b" * 64
BATCH = "replay_A_20261112"

ANALYSIS_TEMPLATE = '''\
import json, pathlib, sys

view = pathlib.Path(sys.argv[1])
out = pathlib.Path(sys.argv[2])
CONTRASTS = __CONTRASTS__

# 公开合同承诺视图里有且只有一份计划（宿主写入，候选不可覆盖）。生产 consumer
# 的分析模板就是这样取对比定义的；夹具同构断言，缺宿主输入即 exit≠0。
if len(sorted(view.glob("plan-*.json"))) != 1:
    raise SystemExit("expected exactly one plan")

recs = {}
cond = {}
for path in sorted(view.rglob("*.jsonl")):
    rec = json.loads(path.read_text(encoding="utf-8"))
    rid = str(rec["run_id"])
    recs[rid] = rec
for path in sorted(view.rglob("*.meta.json")):
    meta = json.loads(path.read_text(encoding="utf-8"))
    cond[str(meta["run_id"])] = str(meta["condition_id"])


def disp(run_id):
    q = recs[run_id]["qpos"]
    return float(q[-1][0]) - float(q[0][0])


def classify(prop, delta, lower, upper):
    if prop == "decrease_gt":
        if upper < -delta:
            return "SUPPORTED"
        return "REFUTED" if lower >= -delta else "INCONCLUSIVE"
    if prop == "increase_gt":
        if lower > delta:
            return "SUPPORTED"
        return "REFUTED" if upper <= delta else "INCONCLUSIVE"
    if lower > -delta and upper < delta:
        return "SUPPORTED"
    if upper <= -delta or lower >= delta:
        return "REFUTED"
    return "INCONCLUSIVE"


contrasts = []
for spec in CONTRASTS:
    blocks = {}
    for run_id, condition in cond.items():
        if condition not in (spec["treatment"], spec["control"]):
            continue
        block = recs[run_id]["block_id"]
        blocks.setdefault(block, {})[condition] = disp(run_id)
    diffs = [blocks[b][spec["treatment"]] - blocks[b][spec["control"]]
             for b in sorted(blocks)
             if spec["treatment"] in blocks[b] and spec["control"] in blocks[b]]
    est = sum(diffs) / len(diffs)
    contrasts.append({
        "contrast_id": spec["id"], "metric_id": spec["metric_id"],
        "proposition": spec["proposition"], "estimate": est,
        "lower": est, "upper": est,          # 常数配对差 → 区间塌缩为一点
        "status": classify(spec["proposition"], spec["delta"], est, est),
        "n_blocks": len(diffs),
    })

doc = {"schema": "simpraxis.rsi_r1.analysis_output.v1",
       "run_ids": sorted(recs),
       "contrasts": contrasts}
(out / "analysis.json").write_text(
    json.dumps(doc, allow_nan=False), encoding="utf-8")
'''


def _record(spec: E.ExperimentSpec, *, displacement: float) -> dict:
    times = [i * 0.001 for i in range(6)]
    return {
        "time_s": times,
        "qpos": [[displacement if i == len(times) - 1 else 0.0, 0.0]
                 for i in range(len(times))],
        "qvel": [[0.5, 0.0] for _ in times],
        "run_id": spec.run_id, "spec_hash": spec.spec_hash,
        "block_id": spec.block_id,
    }


def _campaign(tmp_path: Path):
    root = tmp_path / "campaign"
    H.make_dirs(root)
    ctx = H.make_ctx(root)
    plan = H.doc_of(H.plan_doc("A"))
    contrasts = []
    for c in J._contrasts(plan):
        t_cond, c_cond = c.condition_ids
        contrasts.append({"id": c.id, "metric_id": c.metric,
                          "proposition": c.proposition,
                          "delta": float(c.delta),
                          "treatment": t_cond, "control": c_cond})
    text = ANALYSIS_TEMPLATE.replace(
        "__CONTRASTS__", json.dumps(contrasts, sort_keys=True))
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    plan = H.doc_of(H.plan_doc("A", analysis_source_sha=sha))
    # 源码只含条件名（与 plan sha 无关），故终版 plan 定下后再分配 manifest：
    # run_id 嵌 plan sha，manifest.plan_sha256 必须绑终版 plan。
    manifest = E.allocate_confirm(plan, KEEPER)
    specs = J.build_specs(plan, manifest)
    design = ctx.relative(f"designs/{sha}.py")
    design.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    design.write_text(text, encoding="utf-8")
    # 锁内适配（2026-11-12 T5）：journal 的 artifact 事件写的是**注册表词汇**
    # （state.PRODUCER_CANDIDATE == "candidate_model"）；凭据词汇 "candidate"
    # 只出现在 receipt 里。夹具曾直接用凭据词汇，掩盖了 replay 路径漏做映射
    # 的缺陷，现按真实写入口径构造。
    S.ResearchState(ctx).journal.append("artifact", {
        "source_sha256": sha, "producer": S.PRODUCER_CANDIDATE,
        "response_id": "reply-42", "action_sha256": "act-42"})
    artifacts = {}
    batch_dir = ctx.subdir("observations") / BATCH
    batch_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for rid, spec in specs.items():
        disp = -0.01 if spec.condition_id == "low_mu" else 0.0
        rec = _record(spec, displacement=disp)
        artifacts[rid] = {
            "record": rec,
            "meta": {"run_id": rid, "spec_hash": spec.spec_hash,
                     "condition_id": spec.condition_id, "initial_x_m": 0.0}}
        (batch_dir / f"{rid}.jsonl").write_text(
            json.dumps(rec), encoding="utf-8")
        (batch_dir / f"{rid}.meta.json").write_text(
            json.dumps(artifacts[rid]["meta"]), encoding="utf-8")
    # 冻结 claim（confirm 阶段产物）：replay 判决会拿它在复跑数值上重判
    per_run = J.recompute_metrics(plan=plan, artifacts=artifacts, specs=specs)
    by_cid = {c.id: c for c in J._contrasts(plan)}
    refs = {}
    for contrast in by_cid.values():
        ref = J._judge_contrast(plan, per_run, contrast, [])
        refs[contrast.id] = {**ref, "estimate": ref.get("effect")}
    claims = []
    for hid, h in sorted({h.id: h for h in plan.hypotheses}.items()):
        ref = refs[h.contrast_id]
        claims.append({
            "hypothesis_id": hid, "proposition": ref["proposition"],
            "metric_id": ref["metric_id"],
            "delta": str(J.R.metric_delta(ref["metric_id"])),
            "estimate": ref["effect"], "lower": ref["lower"],
            "upper": ref["upper"]})
    claim_dir = ctx.subdir("claim")
    claim_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # 锁内适配（2026-11-12 T5）：生产 campaign 在 freeze 前已发布公开视图，
    # replay 的分析视图与生产**同构**（plan-<sha12>.json + public_contract.json，
    # 见 assembly.analysis_host_inputs）。夹具此前不建这两件，掩盖了 replay
    # 视图缺宿主输入、任何合规分析模板都会 ``expected exactly one plan`` 的缺陷。
    pub = ctx.relative("public/public_contract.json")
    pub.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pub.write_text(json.dumps({"schema": "simpraxis.rsi_r1.public_contract.v1",
                               "schema_version": 1}), encoding="utf-8")
    (claim_dir / "submission_A.json").write_text(json.dumps({
        "schema": J.SUBMISSION_SCHEMA, "study_id": "A",
        "plan_sha256": plan.sha256,
        "confirm_manifest_sha256": manifest.sha256,
        "primary_contrast_id": plan.primary.id, "claims": claims},
        sort_keys=True), encoding="utf-8")
    return ctx, plan, manifest, artifacts, sha


def _run(ctx, plan, manifest, artifacts, *, runner=None, batch: str = BATCH):
    profile = P.profile_by_id(P.PROFILE_R12)
    return RPD._r12_replay_analysis(
        ctx, study="A", plan=plan, manifest=manifest, batch_id=batch,
        artifacts=artifacts, runner=runner or LocalRunner(),
        image=REHEARSAL_IMAGE, profile=profile)


def test_replay_analysis_runs_frozen_source_and_passes(tmp_path) -> None:
    ctx, plan, manifest, artifacts, sha = _campaign(tmp_path)
    runner = LocalRunner()
    result = _run(ctx, plan, manifest, artifacts, runner=runner)
    assert result["analysis_ok"] is True, result["reason_codes"]
    assert result["judge_verdict"] == "JUDGE_OK"
    assert result["receipt_valid"] is True
    assert result["source_sha256"] == sha
    assert result["n_runs"] == len(artifacts)
    assert len(runner.calls) == 1              # 恰一次真实分析执行
    # 新 receipt 落在私有目录：绑定本次执行与源码 sha
    rel = RPD.analysis_receipt_relpath("A")
    assert ctx.exists(rel)
    receipt = ctx.read_json(rel)
    assert receipt["phase"] == "replay"
    assert receipt["source_sha256"] == sha
    assert receipt["execution_id"] == result["execution_id"]
    assert receipt["producer"] == "candidate"
    assert receipt["registered_reply_id"] == "reply-42"
    assert {d["relative_path"] for d in receipt["inputs"]} == {
        f"{BATCH}/{rid}.jsonl" for rid in artifacts} | {
        f"{BATCH}/{rid}.meta.json" for rid in artifacts}
    assert result["receipt_sha256"] == \
        hashlib.sha256(ctx.relative(rel).read_bytes()).hexdigest()


def test_old_receipt_and_output_cannot_substitute(tmp_path) -> None:
    ctx, plan, manifest, artifacts, sha = _campaign(tmp_path)
    rel = RPD.analysis_receipt_relpath("A")
    planted = ctx.relative(rel)
    planted.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    planted.write_text('{"schema": "stale", "validity": true}\n',
                       encoding="utf-8")
    old = json.loads(planted.read_text())
    result = _run(ctx, plan, manifest, artifacts)
    assert result["analysis_ok"] is True
    fresh = json.loads(planted.read_text())
    assert fresh["schema"] == "simpraxis.rsi_r1.analysis_receipt.v1"
    assert fresh != old
    assert fresh["output_sha256"] != old.get("output_sha256")


def test_source_drift_is_rejected(tmp_path) -> None:
    ctx, plan, manifest, artifacts, sha = _campaign(tmp_path)
    design = ctx.relative(f"designs/{sha}.py")
    design.write_text(design.read_text(encoding="utf-8") + "\n# drift\n",
                      encoding="utf-8")
    with pytest.raises(RPD.ReplayDriverError) as exc:
        _run(ctx, plan, manifest, artifacts)
    assert exc.value.code == "SOURCE_FILE_DRIFT"


def test_unregistered_source_is_rejected(tmp_path) -> None:
    ctx, plan, manifest, artifacts, sha = _campaign(tmp_path)
    # 另建一个合法但不在 journal 登记的源码
    text = "print('noop')\n"
    other = hashlib.sha256(text.encode("utf-8")).hexdigest()
    ctx.relative(f"designs/{other}.py").write_text(text, encoding="utf-8")
    plan2 = H.doc_of(H.plan_doc("A", analysis_source_sha=other))
    with pytest.raises(RPD.ReplayDriverError) as exc:
        _run(ctx, plan2, manifest, artifacts)
    assert exc.value.code == "UNBOUND_SOURCE"


def test_missing_design_file_is_rejected(tmp_path) -> None:
    ctx, plan, manifest, artifacts, sha = _campaign(tmp_path)
    ctx.relative(f"designs/{sha}.py").unlink()
    with pytest.raises(RPD.ReplayDriverError) as exc:
        _run(ctx, plan, manifest, artifacts)
    assert exc.value.code == "SOURCE_FILE_MISSING"


def test_old_confirm_raw_does_not_enter_view(tmp_path) -> None:
    ctx, plan, manifest, artifacts, sha = _campaign(tmp_path)
    # 在另一目录放同名旧 raw：视图只认本批目录
    stale = ctx.subdir("observations") / "confirm_old_A"
    stale.mkdir(parents=True, exist_ok=True, mode=0o700)
    for rid in artifacts:
        (stale / f"{rid}.jsonl").write_text('{"run_id": "x"}\n',
                                            encoding="utf-8")
    result = _run(ctx, plan, manifest, artifacts)
    receipt = ctx.read_json(RPD.analysis_receipt_relpath("A"))
    assert {d["relative_path"] for d in receipt["inputs"]} == {
        f"{BATCH}/{rid}.jsonl" for rid in artifacts} | {
        f"{BATCH}/{rid}.meta.json" for rid in artifacts}
    assert result["analysis_ok"] is True
