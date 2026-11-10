"""RSI-R0.2 事实收集器（计划 §8/§10，AC20/AC31）。

**只从真实事件、槽账、状态与观测产物重算**，不填默认、不读「结果自述」：

* trial 状态/分母来自 ``state.json``（``closeout.read_facts`` 同一路径）；
* run 索引来自 ``observations/trusted/<instance>/run_*.json(+manifest)`` 的逐件 sha256；
* 主张（claim）来自 journal 里 ``judge`` 事件的原样裁决，逐条带 verdict sha256；
* 未开跑的 trial 一律 ``NOT_RUN_WITH_REASON``，绝不写成成功/失败。

T3 的 closeout 预演与 T8 的打包都读这里的产物；LOCK 冻结其实现（改语义即 LOCK 失效）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.rsi_r02 import campaign as C
from orchestrator.rsi_r02 import closeout as CO
from orchestrator.rsi_r02 import state as ST

SCHEMA_FACTS = "simpraxis.rsi_r02.gather.v1"
SCHEMA_RUNS = "simpraxis.rsi_r02.gather_runs.v1"
SCHEMA_CLAIMS = "simpraxis.rsi_r02.gather_claims.v1"
NOT_RUN = "NOT_RUN_WITH_REASON"


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def runs_index(ctx: C.CampaignContext,
               problems: "list[str] | None" = None
               ) -> "list[dict[str, Any]]":
    """观测产物索引：每 run 一件观测 + 一件 manifest，逐件 sha256。

    评审 m4：只有 manifest 没有观测原件的 run **不入索引**，改记
    ``MISSING_OBSERVATION``——带 ``observation_sha256: null`` 入表等于把缺件
    洗成「有这件 run」，下游按条数算分母时凭空多一个。
    """
    root = ctx.relative("observations/trusted")
    out: "list[dict[str, Any]]" = []
    if not root.is_dir():
        return out
    for inst_dir in sorted(root.iterdir()):
        if inst_dir.is_symlink() or not inst_dir.is_dir():
            continue
        for manifest in sorted(inst_dir.glob("run_*.manifest.json")):
            obs = inst_dir / manifest.name.replace(".manifest.json", ".json")
            if not obs.is_file():
                if problems is None:      # 没有收集器就必须炸，不能静默丢件
                    raise FileNotFoundError(
                        f"观测原件缺失：{obs.relative_to(ctx.root)}")
                problems.append(
                    f"MISSING_OBSERVATION: {obs.relative_to(ctx.root)}")
                continue
            doc = json.loads(manifest.read_text(encoding="utf-8"))
            out.append({
                "instance_id": inst_dir.name,
                "run_id": obs.stem,
                "observation_path": str(obs.relative_to(ctx.root)),
                "observation_sha256": _sha_file(obs),
                "manifest_path": str(manifest.relative_to(ctx.root)),
                "manifest_sha256": _sha_file(manifest),
                "trial_id": doc.get("trial_id"),
                "cell": doc.get("cell"),
            })
    return out


def claim_index(ctx: C.CampaignContext) -> "list[dict[str, Any]]":
    """judge 事件里的原样裁决（不交叉解释、不合并）。"""
    out: "list[dict[str, Any]]" = []
    for evt in ST.Journal(ctx).events:
        if evt.get("action") != "judge":
            continue
        verdict = (evt.get("payload") or {}).get("verdict")
        if not isinstance(verdict, Mapping):
            continue
        claim = verdict.get("verdict") or {}
        out.append({
            "trial_id": evt.get("trial_id"),
            "family": verdict.get("family"),
            "layer": verdict.get("layer"),
            "pass": bool(verdict.get("pass")),
            "claim_status": claim.get("claim_status"),
            "claim_value": claim.get("claim_value"),
            "verdict_sha256": hashlib.sha256(_canonical(dict(verdict))).hexdigest(),
        })
    return out


def collect(ctx: C.CampaignContext) -> "dict[str, Any]":
    facts = CO.read_facts(ctx)
    trials: "list[dict[str, Any]]" = []
    for t in facts["trials"]:
        row = dict(t)
        if not t["started"]:
            row["effective_status"] = NOT_RUN
            row["reason"] = "trial 未开跑（无 state.json）"
        else:
            row["effective_status"] = t["status"]
            row["reason"] = t["reason_code"]
        trials.append(row)
    assigned: "dict[str, int]" = {}
    success: "dict[str, int]" = {}
    for row in trials:
        # 协议链 trial 也列进 trials（它花过 POST、槽账认它），但**不进能力分母**：
        # 协议准入不是能力样本，混进 E 族分母会把覆盖度说得更好看。
        if row.get("phase") == "protocol":
            continue
        assigned[row["family"]] = assigned.get(row["family"], 0) + 1
        if (row["effective_status"] == ST.TRIAL_SUBMITTED
                and row.get("independent_success") is True
                and not row.get("assisted")):
            success[row["family"]] = success.get(row["family"], 0) + 1
    run_problems: "list[str]" = []
    runs = runs_index(ctx, run_problems)
    return {
        "schema": SCHEMA_FACTS,
        "recorded_at_utc": utc_now_iso_z(),
        "campaign_id": ctx.campaign_id,
        "facts": facts,
        "trials": trials,
        "denominators": {"assigned_by_family": assigned,
                         "independent_success_by_family": success},
        "runs": runs,
        "claims": claim_index(ctx),
        "problems": run_problems,
        "note": ("所有字段来自事件/状态/观测产物；未开跑记 NOT_RUN_WITH_REASON，"
                 "不填默认、不把缺失写成成功或失败。"),
    }


def write(ctx: C.CampaignContext, doc: Mapping[str, Any] | None = None
          ) -> "dict[str, Any]":
    doc = dict(doc or collect(ctx))
    runs = doc.pop("runs", [])
    claims = doc.pop("claims", [])
    C.write_json(ctx, "gather/facts.json", doc)
    ctx.write_text("gather/runs.jsonl", "".join(
        json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in runs))
    C.write_json(ctx, "gather/claims.json", {"schema": SCHEMA_CLAIMS,
                                             "claims": claims})
    C.write_json(ctx, "gather/manifest.json", {
        "schema": SCHEMA_RUNS, "recorded_at_utc": utc_now_iso_z(),
        "files": {rel: _sha_file(ctx.relative(rel)) for rel in
                  ("gather/facts.json", "gather/runs.jsonl",
                   "gather/claims.json")},
    })
    return doc


def main(argv: "list[str] | None" = None) -> int:
    import argparse

    from orchestrator.common.runtime_paths import rsi_r02_campaign_id, rsi_r02_root

    ap = argparse.ArgumentParser(description="RSI-R0.2 事实收集（零 POST）")
    ap.add_argument("--campaign-id", default=None)
    ap.add_argument("--root", default=None)
    args = ap.parse_args(list(argv) if argv is not None else None)
    cid = args.campaign_id or rsi_r02_campaign_id()
    base = Path(args.root) if args.root else rsi_r02_root()
    ctx = C.load_campaign(campaign_id=cid, root=base)
    # 评审 m1：先收集再写盘——``write`` 在副本上摘掉 runs/claims，若写后从
    # 返回值取长度，摘要会恒报 0/0（假绿）。
    doc = collect(ctx)
    write(ctx, doc)
    print(json.dumps({"campaign_id": doc["campaign_id"],
                      "trials": len(doc["trials"]),
                      "runs": len(doc["runs"]),
                      "claims": len(doc["claims"]),
                      "problems": len(doc.get("problems") or [])},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
