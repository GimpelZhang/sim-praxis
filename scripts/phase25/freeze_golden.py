#!/usr/bin/env python3
"""Phase 2.5 Task0 AC-T0-6：旧路径出生 prompt 黄金快照冻结器（0 付费/0 联网）。

对 task6（Genesis V2 升阶，n=6 拆块出生）、task7_libero（单补丁出生）、
task8_fluid（n=6 拆块出生）三个生产工厂，在纯 hermetic 环境下构造 run_idx=1
的出生 RunSpec，再调生产函数 ``orchestrator.agent_runner.runner._build_messages``
逐字构造出生轮请求消息，冻结：

  1. 规范 prompt JSON（json.dumps(messages, ensure_ascii=False, sort_keys=True)，
     与 callcache 采纳遥测/请求指纹同一规范串）的全文与 sha256；
  2. callcache 键 ``coding_<thread>_r<run_idx>_round1[_chunk<i>]``；
  3. 生产 request_fingerprint(stage/model/prompt/extra)（model 固定字面量
     "golden-oldpath-model"，不读 stage_models.yaml，永不触方舟）。

用途（Task 4 字节回归）：agentic 层接入后重跑本冻结器到临时目录，与入库快照
逐字节比对；prompt 构造、合同段、拆块上下文、键形状、指纹管线任一漂移即红。
本脚本【不向 RunSpec 引入任何 agentic 字段】（Task0 红线）。

确定性来源：工厂 workdir/合同内嵌的绝对路径全部固定到常量
GOLDEN_BUILD_ROOT（generator 与回归测试同一字面量）；Stage0/标定用合成夹具。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts" / "phase25")):
    if p not in sys.path:
        sys.path.insert(0, p)

from orchestrator.common.clock import utc_now_iso_z  # noqa: E402
from orchestrator.common.callcache import request_fingerprint  # noqa: E402
from orchestrator.agent_runner import runner as runner_mod  # noqa: E402

# 固定构造根：出生合同/cmd 内嵌绝对路径，generator 与回归测试必须用同一字面量，
# 否则同一份生产 prompt 会因 tmp 路径不同而哈希失配。
GOLDEN_BUILD_ROOT = "/tmp/simpraxis_phase25_golden_build"
GOLDEN_MODEL = "golden-oldpath-model"
GOLDEN_TAIL = ("[golden-freeze] 出生前首轮 exec 失败（确定性冻结输入）："
               "入口脚本尚不存在，等待 coding_debug 出生。")
GOLDEN_SCHEMA = "simpraxis.phase25.t0_golden.v1"
DEFAULT_OUT_DIR = REPO_ROOT / "tests" / "fixtures" / "phase25_golden"

T6_THREAD = "t6-golden-ascent-20261029a"
T7_THREAD = "t7l-golden-20261029a"
T8_THREAD = "t8f-golden-20261029a"
T6_BATCH = "P25-GOLDEN-T6"
T7_BATCH = "P25-GOLDEN-T7"
T8_BATCH = "P25-GOLDEN-T8"


def _reset_dir(path: Path, *, expected_name: str) -> None:
    """删除守卫：只允许清空 basename 固定的自有字面目录（硬规则 6）。

    L5（2026-10-29 评审修单）：先拒符号链接（lexists/is_symlink，防止 rmtree
    穿透到链接目标），重建后显式 chmod 700 收敛属主私有权限。
    """
    if path.name != expected_name:
        raise RuntimeError(
            f"拒绝清空非预期目录：{path}（期望 basename {expected_name}）")
    if path.is_symlink():
        raise RuntimeError(f"拒绝清空符号链接：{path} -> {os.readlink(path)}")
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _hermetic_env(build_root: Path) -> dict[str, str]:
    """进程内 env 覆盖（保存原值，build_golden 退出时恢复）。"""
    overrides = {
        "SIMPRAXIS_PHASE1_ROOT": str(build_root / "phase1"),
        "SIMPRAXIS_PHASE2_ROOT": str(build_root / "phase2"),
        "SIMPRAXIS_PHASE25_ROOT": str(build_root / "phase25"),
        "SIMPRAXIS_GATEWAY_STATE": str(build_root / "gwstate"),
        "SIMPRAXIS_RUN_DIR": str(build_root / "runs"),
        "SIMPRAXIS_USE_PROXY": "0",
        "HF_ENDPOINT": "https://hf-mirror.com",
    }
    saved: dict[str, str | None] = {}
    for k, v in overrides.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v
    return saved


def _restore_env(saved: dict[str, str | None]) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _task6_spec(build_root: Path):
    """task6 Genesis V2 出生 RunSpec（n=6 拆块）；合成 V1 方案 + 合成标定。"""
    from orchestrator.graph import task6 as task6_mod
    from orchestrator.graph import task8

    from tests.milestone1.test_task8_seam import (
        FIXTURE_SOLUTIONS_ROOT,
        _calibration_v2,
    )

    os.environ["SIMPRAXIS_SOLUTIONS_ROOT"] = str(FIXTURE_SOLUTIONS_ROOT)
    cal_src = build_root / "cal_src.json"
    cal_src.parent.mkdir(parents=True, exist_ok=True)
    cal_src.write_text(json.dumps(_calibration_v2()), encoding="utf-8")
    orig_cal = task8.CALIBRATION_SOURCE
    task8.CALIBRATION_SOURCE = cal_src
    try:
        state = {
            "chosen": {"sim_engine": "genesis", "name": "V2 阻抗+DR（golden 合成）"},
            "v1_archive": {"thread_id": "t8-golden-20261016a",
                           "host_workdir": str(build_root / "v1arch")},
            "agent_runs": [], "events": [], "gate1_rejects": 0,
            "proposals": []}
        factory = task6_mod.make_spec_factory(T6_THREAD, T6_BATCH)
        return task6_mod, factory(state), orig_cal
    finally:
        task8.CALIBRATION_SOURCE = orig_cal
        os.environ.pop("SIMPRAXIS_SOLUTIONS_ROOT", None)


def _synthetic_report(module, builder_name: str) -> dict:
    return getattr(module, builder_name)()


def _task7_spec(build_root: Path):
    """task7 LIBERO stage1_spatial 出生 RunSpec（单补丁路径）。"""
    from orchestrator.graph import task7_libero as t7
    from tests.milestone2.test_task7_libero_cli_wiring import _ok_report

    empty_kb = build_root / "kb_solutions_t7"
    empty_kb.mkdir(parents=True, exist_ok=True)
    s0_dir = build_root / "stage0_t7"
    s0_dir.mkdir(parents=True, exist_ok=True)
    s0 = s0_dir / t7.STAGE0_REPORT_NAME
    s0.write_text(json.dumps(_ok_report(), ensure_ascii=False), encoding="utf-8")
    orig_root = t7.solutions_root
    t7.solutions_root = lambda: empty_kb  # noqa: E731 —— 临时替身，finally 还原
    try:
        state = {
            "thread_id": T7_THREAD, "track": "track1",
            "proposals": [{"name": "libero_spatial 最小闭环（golden 合成）",
                           "sim_engine": "LIBERO/robosuite",
                           "first_principles": "官方 check_success",
                           "artifacts": ["video.mp4"]}],
            "chosen": {"name": "libero_spatial 最小闭环（golden 合成）",
                       t7.STAGE0_REPORT_STATE_KEY: str(s0)},
            "events": []}
        factory = t7.make_spec_factory(T7_THREAD, T7_BATCH,
                                       stage=t7.STAGE1_SPATIAL)
        return t7, factory(state), orig_root
    finally:
        t7.solutions_root = orig_root


def _task8_spec(build_root: Path):
    """task8 Genesis 流体 stage1_pour 出生 RunSpec（n=6 拆块）。"""
    from orchestrator.graph import task8_fluid as t8f
    from tests.milestone2.test_task8_fluid_cli_wiring import _good_s0

    empty_kb = build_root / "kb_solutions_t8"
    empty_kb.mkdir(parents=True, exist_ok=True)
    s0_dir = build_root / "stage0_t8"
    s0_dir.mkdir(parents=True, exist_ok=True)
    s0 = s0_dir / t8f.STAGE0_REPORT_NAME
    s0.write_text(json.dumps(_good_s0(), ensure_ascii=False), encoding="utf-8")
    orig_root = t8f.solutions_root
    t8f.solutions_root = lambda: empty_kb  # noqa: E731 —— 临时替身，finally 还原
    try:
        state = {
            "thread_id": T8_THREAD, "track": "track2",
            "proposals": [{"name": "Q5 厨房倒水最小闭环（golden 合成）",
                           "sim_engine": "Genesis 粒子流体",
                           "first_principles": "严格几何入杯 + 守恒三查",
                           "artifacts": ["video.mp4"]}],
            "chosen": {"name": "Q5 厨房倒水最小闭环（golden 合成）",
                       t8f.STAGE0_REPORT_STATE_KEY: str(s0)},
            "events": []}
        factory = t8f.make_spec_factory(T8_THREAD, T8_BATCH,
                                        task_title="Task8 Q5（golden）")
        return t8f, factory(state), orig_root
    finally:
        t8f.solutions_root = orig_root


def _record(factory_name: str, spec, chunk_index, messages, out_dir: Path,
            rel_prompt: Path) -> dict:
    prompt_json = json.dumps(messages, ensure_ascii=False, sort_keys=True)
    prompt_path = out_dir / rel_prompt
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt_json, encoding="utf-8")
    # M-A：键形单一源在 runner.coding_call_cache_key，冻结器不得自拼后缀
    # （单侧漂移不会红 golden）。
    cache_key = runner_mod.coding_call_cache_key(
        spec.thread_id, spec.run_idx, 1, chunk_index=chunk_index)
    fp_extra = {"max_tokens": spec.coding_max_tokens,
                "temperature": runner_mod.TEMPERATURE,
                "thinking": runner_mod.CODING_THINKING}
    if chunk_index is not None:
        fp_extra["chunk_index"] = chunk_index
        fp_extra["chunk_total"] = spec.chunked_birth.chunk_count
    return {
        "factory": factory_name,
        "thread_id": spec.thread_id,
        "batch_id": spec.batch_id,
        "run_idx": spec.run_idx,
        "round": 1,
        "chunk_index": chunk_index,
        "cache_key": cache_key,
        "stage": runner_mod.STAGE,
        "model": GOLDEN_MODEL,
        "temperature": runner_mod.TEMPERATURE,
        "thinking": runner_mod.CODING_THINKING,
        "max_tokens": spec.coding_max_tokens,
        "prompt_canonical_json": str(rel_prompt),
        "prompt_chars": len(prompt_json),
        "prompt_sha256": hashlib.sha256(
            prompt_json.encode("utf-8")).hexdigest(),
        "request_fingerprint": request_fingerprint(
            prompt_json, None, fp_extra,
            stage=runner_mod.STAGE, model=GOLDEN_MODEL),
        "fp_extra": fp_extra,
    }


def _records_for_factory(name: str, spec, out_dir: Path) -> list[dict]:
    records: list[dict] = []
    cs = spec.chunked_birth
    build = runner_mod._build_messages
    if cs is None:
        # task7：生产单补丁出生路径（extra_sections=spec.extra_prompt_sections）。
        messages = build(
            gate2_notes=spec.gate2_notes, traceback_tail=GOLDEN_TAIL,
            snippets=[], hits=[], cmd=spec.cmd, learning_hits=None,
            extra_sections=spec.extra_prompt_sections or None)
        records.append(_record(name, spec, None, messages, out_dir,
                               Path(name) / "prompts" / "round1.prompt.json"))
        return records
    # task6/task8：生产拆块出生路径（_run_chunked_birth_round 逐块同形构造）。
    codes: list[str] = []
    for i in range(cs.chunk_count):
        ctx = runner_mod.ChunkPromptContext(
            index=i, total=cs.chunk_count, target_path=cs.target_path,
            titles=cs.chunk_titles, boundaries=cs.chunk_boundaries,
            previous_code="\n".join(codes), round_feedback="" if i == 0 else "")
        messages = build(
            gate2_notes=spec.gate2_notes, traceback_tail=GOLDEN_TAIL,
            snippets=[], hits=[], cmd=spec.cmd, learning_hits=None,
            extra_sections=cs.per_call_sections, chunk_context=ctx)
        records.append(_record(name, spec, i, messages, out_dir,
                               Path(name) / "prompts"
                               / f"round1_chunk{i}.prompt.json"))
        # 块间反馈的前序码是提取后逻辑块；golden 只冻结 prompt 构造，后续块的
        # previous_code 用确定性占位（生产路径上同轮前块模型输出不可在出生前获得，
        # 逐字节回归比对的是构造管线；占位事实写入 manifest 说明）。
        codes.append(f"# [golden-freeze] previous_code placeholder chunk {i}\n")
    return records


def build_golden(out_dir: Path | str | None = None) -> dict:
    """构造黄金快照到 out_dir（默认入库夹具目录）；返回 manifest dict。"""
    out = Path(out_dir) if out_dir is not None else DEFAULT_OUT_DIR
    build_root = Path(GOLDEN_BUILD_ROOT)
    _reset_dir(out, expected_name="phase25_golden")
    _reset_dir(build_root, expected_name=Path(GOLDEN_BUILD_ROOT).name)

    saved_env = _hermetic_env(build_root)
    saved_solutions = os.environ.get("SIMPRAXIS_SOLUTIONS_ROOT")
    try:
        factories: dict[str, dict] = {}
        for name, builder in (
                ("task6_ascent", _task6_spec),
                ("task7_libero", _task7_spec),
                ("task8_fluid", _task8_spec)):
            _mod, spec, _orig = builder(build_root)
            if spec.run_idx != 1:
                raise AssertionError(f"{name}: 出生 RunSpec.run_idx 必须为 1")
            records = _records_for_factory(name, spec, out)
            cs = spec.chunked_birth
            factories[name] = {
                "thread_id": spec.thread_id,
                "billing_task": spec.billing_task,
                "run_idx": spec.run_idx,
                "birth_mode": "chunked" if cs is not None else "single_patch",
                "chunk_count": cs.chunk_count if cs is not None else 1,
                "target_entrypoint": (cs.target_path if cs is not None
                                      else Path(spec.cmd[1]).name),
                "coding_max_tokens": spec.coding_max_tokens,
                "records": records,
            }
    finally:
        if saved_solutions is None:
            os.environ.pop("SIMPRAXIS_SOLUTIONS_ROOT", None)
        else:
            os.environ["SIMPRAXIS_SOLUTIONS_ROOT"] = saved_solutions
        _restore_env(saved_env)

    manifest = {
        "schema": GOLDEN_SCHEMA,
        "generated_at_iso": utc_now_iso_z(),
        "build_root": GOLDEN_BUILD_ROOT,
        "fixed_inputs": {
            "model_literal": GOLDEN_MODEL,
            "stage": runner_mod.STAGE,
            "temperature": runner_mod.TEMPERATURE,
            "thinking": runner_mod.CODING_THINKING,
            "traceback_tail": GOLDEN_TAIL,
            "snippets": [], "hits": [], "learning_hits": None,
            "gate2_notes": None,
            "note": ("出生轮无前序模型输出；chunk i>0 的 previous_code 用确定性"
                     "占位（生产为同轮前块提取码，出生前不可得）；冻结对象是"
                     "prompt 构造管线与键/指纹形状，不是模型响应。"),
        },
        "task4_usage": (
            "Task 4 agentic 层接入后，以 out_dir=临时目录重跑 build_golden()，"
            "与入库快照逐字节比对 manifest.factories[*].records 与 prompts/ 全文；"
            "只允许在出生合同有意变更时经评审重新入库。"),
        "factories": factories,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="冻结 task6/7/8 旧路径出生 prompt 黄金快照")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    args = ap.parse_args(argv)
    manifest = build_golden(args.out_dir)
    n = sum(len(f["records"]) for f in manifest["factories"].values())
    print(json.dumps({"out": args.out_dir, "factories": list(manifest["factories"]),
                      "records": n}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
