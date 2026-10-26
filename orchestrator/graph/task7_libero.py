#!/usr/bin/env python3
"""Phase 2 Task7 B 线图接缝：LIBERO 第五平台 track（Stage0 闸 + Stage1/2 骨架
+ Stage3 WJ-Lite 报告契约）。

计划锚点：Phase_2_Plan_detailed.md Task7（L573-590）、D-4（L119）、
WJ-Lite 定义（§1.3，L198-200：统一指标契约 ``simpraxis.wj_report.v1`` +
Rung1–3 适配 + Rung4 永不单独放行）、附录 A 四测（L954-957）。

与 Phase1 ``orchestrator/graph/task7.py``（Track1 MuJoCo/ManiSkill）的关系：
不改它一字；本模块是第五平台的独立接缝（CLI 经 ``--task 7 --variant libero``
路由到本模块工厂/Gate1/Gate2 接缝）。

零网络、零付费、零 tour 执行（硬规则 10）：
  * Stage0 兼容事实由容器探针 sandbox/probes/probe_libero_compat.py 先产出，
    本模块只做 fail-closed 装载/机判；隔离 venv 路径常量从探针模块单点导入；
  * Stage1/2 解题脚本（stage1_minimal.py/stage2_suite.py）按三层分离生于
    coding workdir、Gate2 后回收进内容层 solutions/track1_tools/03_libero/，
    引擎仓携带零题目脚本——内容层尚无该方案时工厂走**出生态**（cmd 指向
    出生时不存在的入口 + 出生合同注入 coding 环，task6 V2 家法、单次合同），
    回收后自动切 resolve+stage 逐件 sha256 态（清单损坏 fail-closed，不回退出生）；
  * Rung1–3 单 rollout 评估直接复用 orchestrator.judge.rungs.evaluate_rollout
    契约（LIBERO 原生 per-task success 不翻译成 telemetry，只在 WJ 报表聚合）。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from orchestrator.agent_runner.runner import RunSpec
from orchestrator.common.fsutil import atomic_write_text
from orchestrator.graph import task7_libero_prompts as prompts
from orchestrator.graph.nodes import GraphStateError
from orchestrator.judge.rungs import evaluate_rollout
from orchestrator.solutions import SolutionError, resolve_solution, solutions_root

TASK = 7
PLATFORM = "libero"

# ---------------------------------------------------------------------------
# 探针模块单点接入（sandbox/ 不是 Python 包——无 __init__.py，且本任务约束
# 不得新增文件；故按文件路径装载，零 sys.path 污染、零额外依赖）。
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROBE_PATH = _REPO_ROOT / "sandbox" / "probes" / "probe_libero_compat.py"


def _load_probe_module():  # pragma: no cover - 装载失败即环境损坏
    spec = importlib.util.spec_from_file_location(
        "simpraxis_probe_libero_compat", _PROBE_PATH)
    if spec is None or spec.loader is None:
        raise GraphStateError(f"无法装载 LIBERO 探针模块：{_PROBE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_probe = _load_probe_module()

# 隔离 venv 路径/探针 schema 以探针为单一来源（测试钉死同对象）。
VENV_PATH: str = _probe.VENV_PATH
VENV_PY: str = _probe.venv_python(_probe.VENV_PATH)
STAGE0_SCHEMA: str = _probe.REPORT_SCHEMA
DEGRADATION: str = _probe.DEGRADATION

STAGE0_REPORT_NAME = "stage0_compat_report.json"
WJ_REPORT_NAME = "wj_report.json"
TELEMETRY_NAME = "telemetry.json"
VIDEO_NAME = "video.mp4"
COVER_NAME = "cover.png"
MANIFEST_NAME = "manifest.json"

# state/chosen 里 Stage0 报告路径的固定键（direction→Gate1 卡片读取同键）。
STAGE0_REPORT_STATE_KEY = "stage0_report_path"
WJ_REPORT_STATE_KEY = "wj_report_path"

# Stage1 libero_spatial 单任务闭环；Stage2 套件（Gate1 卡可登记缩减范围）。
STAGE1_SPATIAL = "stage1_spatial"
STAGE2_SUITE = "stage2_suite"
STAGES = (STAGE1_SPATIAL, STAGE2_SUITE)
STAGE_TIMEOUT_S = {STAGE1_SPATIAL: 1200, STAGE2_SUITE: 2400}
# 计划 L585：方案脚本 Gate2 后回收进内容层的目标 solution_id（今天尚不存在）。
LIBERO_SOLUTION_ID = "track1_tools/03_libero"
STAGE_SCRIPT = {
    STAGE1_SPATIAL: "stage1_minimal.py",
    STAGE2_SUITE: "stage2_suite.py",
}
# 每任务局数（Gate1 卡与出生合同同一来源；validator 要求逐任务 == 此值）。
STAGE_N_EPISODES = {STAGE1_SPATIAL: 2, STAGE2_SUITE: 2}

# 出生合同的 coding 单次上限（task6 V2 CODING_* 同值家法：32768/600）。
CODING_MAX_TOKENS = 32768
CODING_TIMEOUT_S = 600

# 出生脚本唯一必须自报的原始计数产物（聚合由引擎组装器现算）。
RESULTS_NAME = "results.json"


def default_stage0_report_path() -> Path:
    """CLI --stage0-report 缺省落点（与 Stage0 探针产物目录同口径）。"""
    return _runs_root() / "phase2" / "task7" / "stage0" / STAGE0_REPORT_NAME

# ---------------------------------------------------------------------------
# Stage0 兼容报告：fail-closed 装载 + Gate1 提案机判
# ---------------------------------------------------------------------------


def load_stage0_report(path: "str | Path") -> dict:
    """装载并校验 Stage0 报告。缺件/坏 JSON/手工伪造/枚举违规一律 GraphStateError。"""
    try:
        return _probe.load_report(Path(path))
    except _probe.Stage0ReportError as exc:
        raise GraphStateError(f"Task7 LIBERO Stage0 报告 fail-closed：{exc}") from exc


def stage0_report_from_state(state: dict) -> dict:
    """从 state 解析 Stage0 报告路径并装载（chosen 优先，兼容顶层键）。"""
    chosen = state.get("chosen") or {}
    path = chosen.get(STAGE0_REPORT_STATE_KEY) or state.get(
        STAGE0_REPORT_STATE_KEY)
    if not isinstance(path, str) or not path:
        raise GraphStateError(
            "Task7 LIBERO 缺 Stage0 兼容报告路径"
            f"（chosen.{STAGE0_REPORT_STATE_KEY}）：未跑 Stage0 禁止提 Gate1 运行案")
    return load_stage0_report(path)


def gate1_proposal(state: dict) -> dict:
    """Stage0 事实 → Gate1 提案机判（确定性，零付费）。

    compatible：可提 Stage1 运行案（mujoco 漂移作为事实/备注随卡呈示，不拦）；
    conflict_isolated_failed：propose_run=False，按 D-4 降级为「结构化巡礼 +
    兼容性白皮书」，绝不生成 Gate1 运行案，更不伪装跑通。
    """
    report = stage0_report_from_state(state)
    base = {
        "platform": PLATFORM,
        "stage0_schema": report["schema"],
        "outcome": report["outcome"],
        "venv_path": report["venv_path"],
        "pin_facts": report["pin_facts"],
        "reasons": report["reasons"],
        "notes": report["notes"],
        "tour_dir": report["tour_dir"],
    }
    if report["outcome"] == "compatible":
        return {"propose_run": True, **base}
    return {"propose_run": False, "degradation": DEGRADATION, **base}


def _runs_root() -> Path:
    return Path(os.environ.get("SIMPRAXIS_RUN_DIR",
                               "/mnt/artifacts/simpraxis/runs"))


def _libero_run_dir(thread_id: str, stage: str, run_idx: int) -> Path:
    d = _runs_root() / "phase2" / "task7_libero" / stage \
        / f"run_{thread_id}_{run_idx}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _last_event(state: dict, kind: str) -> "dict | None":
    for ev in reversed(state.get("events", [])):
        if ev.get("type") == kind:
            return ev
    return None


def make_spec_factory(thread_id: str, coding_batch: str, *,
                      stage: str = STAGE1_SPATIAL,
                      task_title: str = "") -> Callable[[dict], RunSpec]:
    """Stage1/2 agent_run 工厂（出生/回收双态，task6 V2 家法、单次合同）。

    三层分离闸：内容层尚无 ``track1_tools/03_libero``（Gate2 后才回收）时走
    **出生态**——cmd 指向出生时不存在的入口文件，首次 exec 必然「文件不存在」
    失败，coding 环按 ``stage_birth_contract`` 以单 JSON 补丁出生脚本；
    回收后自动切 resolve+stage 逐件 sha256 态（清单损坏 SolutionError 包成
    GraphStateError fail-closed，**绝不**回退出生把损坏当不存在）。

    被调用即表示已过 Gate1——Stage0 不兼容在这里是机器硬闸（D-4，不是卡片
    文案问题）。
    """
    if stage not in STAGES:
        raise GraphStateError(
            f"Task7 LIBERO 未知 stage={stage!r}；合法 {STAGES}")

    def factory(state: dict) -> RunSpec:
        proposal = gate1_proposal(state)
        if not proposal["propose_run"]:
            raise GraphStateError(
                "Task7 LIBERO Stage0 结局 "
                f"{proposal['outcome']}：按 D-4 降级 {DEGRADATION}，"
                "不得构造容器运行 RunSpec（No Fake Approval）；"
                f"reasons={proposal['reasons']}")

        run_idx = len(state.get("agent_runs", [])) + 1
        workdir = _libero_run_dir(thread_id, stage, run_idx)

        birth = False
        if (solutions_root() / LIBERO_SOLUTION_ID).is_dir():
            # 三层分离：已回收 → 题目脚本只来自内容层 manifest（sha256/C4 全量
            # 校验）；任何损坏 fail-closed，不静默回退出生。
            try:
                staged = resolve_solution(LIBERO_SOLUTION_ID).stage(workdir)
            except SolutionError as exc:
                raise GraphStateError(
                    f"Task7 LIBERO 方案 stage 失败（{LIBERO_SOLUTION_ID}，"
                    f"内容层已回收但清单/落件不合规）：{exc}"
                ) from exc
            target = staged.entrypoint
            if target.name != STAGE_SCRIPT[stage]:
                raise GraphStateError(
                    f"Task7 LIBERO {stage} 入口应为 {STAGE_SCRIPT[stage]}，"
                    f"manifest 给的是 {target.name}")
        else:
            # 出生态：工厂只登记目标路径，既不创建也不删除（同 run 重入时已
            # 出生的文件保留——runner 失败重试/coding 补丁在自己那层语义）。
            birth = True
            target = workdir / STAGE_SCRIPT[stage]

        # libero/__init__.py 首跑缺 config.yaml 会交互式 input()（容器无 TTY
        # 即 EOFError）：按 Stage0 探针同款脚手架预置非交互配置，bddl/assets
        # 指向 editable 安装的 A 线 tour 包内目录。
        config_dir = workdir / ".libero"
        config_dir.mkdir(parents=True, exist_ok=True)
        inner_pkg = Path(proposal["tour_dir"]) / "libero" / "libero"
        (config_dir / "config.yaml").write_text(
            _probe.libero_config_text(inner_pkg, workdir / "datasets"),
            encoding="utf-8")

        ev = _last_event(state, "gate2_rejected")
        notes = ev["human_notes"] if ev else None
        run_id = f"{thread_id}-r{run_idx}"
        env_extra = {
            # robosuite/mujoco 无头渲染走 EGL（与 Phase1 mujoco 场景同口径）。
            "MUJOCO_GL": "egl",
            "PYOPENGL_PLATFORM": "egl",
            "LIBERO_CONFIG_PATH": str(config_dir),
            "HOME": "/mnt/caches/home",
            "HF_HOME": "/mnt/caches/hf",
            "HF_ENDPOINT": "https://hf-mirror.com",
            "SIMPRAXIS_USE_PROXY": "0",
        }
        extra: tuple[tuple[str, str], ...] = ()
        if birth and not target.exists():
            # 仅首次出生注入合同；同 run 重入（文件已出生）不再注入，避免
            # coding 环拿到「再建一遍」的误导指令（old="" 补丁对已存在文件
            # 会 mismatch 失败）。
            extra = (
                (f"Task7 LIBERO {stage} 出生合同",
                 prompts.stage_birth_contract(
                     stage=stage, script_name=STAGE_SCRIPT[stage],
                     n_episodes=STAGE_N_EPISODES[stage],
                     stage0_facts_text=prompts.stage0_facts_text(proposal))),
                ("已批准 macro 提案",
                 json.dumps(state["chosen"], ensure_ascii=False, indent=2)),
            )
        return RunSpec(
            thread_id=thread_id,
            workdir=workdir,
            cmd=[VENV_PY, str(target), "--out-dir", str(workdir),
                 "--run-id", run_id],
            telemetry_relpath=TELEMETRY_NAME,
            run_idx=run_idx,
            batch_id=coding_batch,
            billing_task=TASK,
            gate2_notes=notes,
            env_extra=env_extra,
            run_timeout_s=STAGE_TIMEOUT_S[stage],
            task_title=task_title or f"Task7 LIBERO {stage} 真实运行（隔离 venv）",
            video_relpath=VIDEO_NAME,
            cover_relpath=COVER_NAME,
            manifest_relpath=MANIFEST_NAME,
            # 出生合同是单次长生成（完整脚本一次补丁）：与 task6 V2 CODING_* 同值。
            coding_max_tokens=CODING_MAX_TOKENS,
            coding_timeout_s=CODING_TIMEOUT_S,
            extra_prompt_sections=extra,
        )

    return factory


# ---------------------------------------------------------------------------
# Rung1–3 适配（WJ-Lite §1.3：单 rollout 直接复用 rungs 契约）
# ---------------------------------------------------------------------------

def evaluate_single_rollout(telemetry: dict) -> dict:
    """LIBERO Stage1 单 rollout 若产出共享 telemetry 契约，Rung1–3 与
    MuJoCo/ManiSkill 同一判定器（分级向量，无总分）；LIBERO 原生 success flag
    不进此函数，它在 WJ 报告 tasks[].success 聚合层处理。"""
    return evaluate_rollout(telemetry)


# ---------------------------------------------------------------------------
# Stage3 WJ-Lite 报告：simpraxis.wj_report.v1（版本化常量 + 手写 fail-closed
# 校验；不引第三方 jsonschema）。
#
# 字段对齐蓝图 Stage3 出口（templates/tour_blueprint.json repo_requirements）：
# 报告每个数字可回指结果文件（metrics_ref.path+keys+sha256），判分器来源显式
# 标注（官方/自补，自补分数不外推是蓝图对自补替身的硬要求），Gate2 媒体齐备。
# 形态选择：机器消费契约放代码常量（同 rungs._REQUIRED/探针 schema 家法），
# 不放 templates/*.example.json——后者是人读模板位（v1_index/manifest/蓝图）。
# ---------------------------------------------------------------------------

WJ_REPORT_SCHEMA = "simpraxis.wj_report.v1"
WJ_REPORT_VERSION = 1
WJ_STAGES = (STAGE1_SPATIAL, STAGE2_SUITE)
JUDGE_SOURCES = ("官方", "自补")
MEDIA_KINDS = ("video", "image", "log")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_WJ_REQUIRED = ["schema", "version", "task", "platform", "thread_id", "stage",
                "ts_utc", "suite", "stage0_compat_ref", "tasks", "aggregate",
                "judge_sources_used", "media", "rung4", "gate2_ready"]


class WjReportError(ValueError):
    """WJ 报告缺失/结构违规/数字不可溯源（图接缝据此 fail-closed）。"""


def _sha_or_null(v: Any) -> bool:
    return v is None or (isinstance(v, str) and bool(_SHA256_RE.match(v)))


def validate_wj_report(obj: Any) -> list[str]:
    """返回错误列表（空 = 合法）。缺溯源/坏判分器来源标签/媒体错配均拒。"""
    errs: list[str] = []
    if not isinstance(obj, dict):
        return ["WJ 报告顶层必须是对象"]
    for k in _WJ_REQUIRED:
        if k not in obj:
            errs.append(f"缺必填字段：{k}")
    if errs:
        return errs

    if obj["schema"] != WJ_REPORT_SCHEMA:
        errs.append(f"schema 必须是 {WJ_REPORT_SCHEMA}")
    if obj["version"] != WJ_REPORT_VERSION:
        errs.append(f"version 必须是 {WJ_REPORT_VERSION}")
    if obj["task"] != TASK:
        errs.append("task 必须是 7")
    if obj["platform"] != PLATFORM:
        errs.append("platform 必须是 libero")
    if not isinstance(obj["thread_id"], str) or not obj["thread_id"]:
        errs.append("thread_id 必须是非空字符串")
    if obj["stage"] not in WJ_STAGES:
        errs.append(f"stage 必须是 {WJ_STAGES} 之一")
    if not (isinstance(obj["ts_utc"], str) and _TS_RE.match(obj["ts_utc"])):
        errs.append("ts_utc 必须是严格 UTC Z")
    if not (isinstance(obj["stage0_compat_ref"], str)
            and obj["stage0_compat_ref"]):
        errs.append("stage0_compat_ref 必须非空（Stage3 数字须可回链 Stage0 事实）")
    if not isinstance(obj["gate2_ready"], bool):
        errs.append("gate2_ready 必须是布尔")
    if obj["rung4"] is not None and not isinstance(obj["rung4"], dict):
        errs.append("rung4 必须是对象或 null")

    suite = obj["suite"]
    if not isinstance(suite, dict):
        errs.append("suite 必须是对象")
    elif not (isinstance(suite.get("name"), str) and suite["name"]
              and isinstance(suite.get("n_tasks"), int)
              and not isinstance(suite.get("n_tasks"), bool)
              and suite["n_tasks"] > 0
              and isinstance(suite.get("n_episodes_per_task"), int)
              and not isinstance(suite.get("n_episodes_per_task"), bool)
              and suite["n_episodes_per_task"] > 0):
        errs.append("suite 需含非空 name 与正整数 n_tasks/n_episodes_per_task")

    media = obj["media"]
    media_paths: set[str] = set()
    if not isinstance(media, list) or not media:
        errs.append("media 必须是非空数组")
    else:
        for i, md in enumerate(media):
            if not isinstance(md, dict):
                errs.append(f"media[{i}] 必须是对象")
                continue
            if md.get("kind") not in MEDIA_KINDS:
                errs.append(f"media[{i}].kind 必须是 {MEDIA_KINDS} 之一")
            if not isinstance(md.get("path"), str) or not md["path"]:
                errs.append(f"media[{i}].path 必须非空")
            elif md["path"] in media_paths:
                errs.append(f"media[{i}].path 重复：{md['path']}")
            else:
                media_paths.add(md["path"])
            if not _sha_or_null(md.get("sha256")):
                errs.append(f"media[{i}].sha256 必须是 64 位小写 hex 或 null")

    tasks = obj["tasks"]
    if not isinstance(tasks, list) or not tasks:
        errs.append("tasks 必须是非空数组")
        return errs  # 后续项级检查无意义

    if isinstance(suite, dict) and isinstance(suite.get("n_tasks"), int) \
            and suite["n_tasks"] != len(tasks):
        errs.append(f"suite.n_tasks={suite['n_tasks']} 与 tasks 实际 "
                    f"{len(tasks)} 不一致")

    seen_ids: set[str] = set()
    sum_success = sum_episodes = 0
    sources: set[str] = set()
    for i, t in enumerate(tasks):
        p = f"tasks[{i}]"
        if not isinstance(t, dict):
            errs.append(f"{p} 必须是对象")
            continue
        tid = t.get("task_id")
        if not isinstance(tid, str) or not tid:
            errs.append(f"{p}.task_id 必须非空")
        elif tid in seen_ids:
            errs.append(f"{p}.task_id 重复：{tid}")
        seen_ids.add(tid)
        if not isinstance(t.get("bddl"), str) or not t["bddl"]:
            errs.append(f"{p}.bddl 必须非空（任务定义须可溯源到 BDDL 文件）")
        succ, eps = t.get("success"), t.get("episodes")
        if not (isinstance(succ, int) and not isinstance(succ, bool)
                and isinstance(eps, int) and not isinstance(eps, bool)
                and eps >= 1 and 0 <= succ <= eps):
            errs.append(f"{p} 需 0 ≤ success(int) ≤ episodes(int≥1)")
        else:
            sum_success += succ
            sum_episodes += eps
            # LIBERO Stage1/2 协议：每任务固定打 suite.n_episodes_per_task 局；
            # 缩减套件只减 n_tasks（少跑任务），不允许逐任务不同局数——手构
            # 5 vs 实 2 的分母失配在此 fail-closed（变更口径须先改 Gate1 卡）。
            if isinstance(suite, dict) \
                    and isinstance(suite.get("n_episodes_per_task"), int) \
                    and not isinstance(suite["n_episodes_per_task"], bool) \
                    and eps != suite["n_episodes_per_task"]:
                errs.append(
                    f"{p}.episodes={eps} 必须 == "
                    f"suite.n_episodes_per_task="
                    f"{suite['n_episodes_per_task']}")
            rate = t.get("success_rate")
            if not isinstance(rate, (int, float)) or isinstance(rate, bool) \
                    or abs(float(rate) - succ / eps) > 1e-9:
                errs.append(f"{p}.success_rate 必须 == success/episodes")
        # 数字溯源：每个判分必须回指结果文件 + 字段键（+可选 sha256）。
        ref = t.get("metrics_ref")
        if not isinstance(ref, dict):
            errs.append(f"{p}.metrics_ref 必须是对象（数字不可溯源）")
        else:
            if not isinstance(ref.get("path"), str) or not ref["path"]:
                errs.append(f"{p}.metrics_ref.path 必须非空（缺结果文件溯源）")
            keys = ref.get("keys")
            if not (isinstance(keys, list) and keys
                    and all(isinstance(k, str) and k for k in keys)):
                errs.append(f"{p}.metrics_ref.keys 必须是非空字符串数组")
            if not _sha_or_null(ref.get("sha256")):
                errs.append(f"{p}.metrics_ref.sha256 必须是 hex 或 null")
        label = t.get("judge_source")
        if label not in JUDGE_SOURCES:
            errs.append(f"{p}.judge_source 必须是 {JUDGE_SOURCES} 之一")
        else:
            sources.add(label)
        rungs = t.get("rungs")
        if rungs is not None:
            if not (isinstance(rungs, dict)
                    and all(isinstance(rungs.get(k), bool)
                            for k in ("rung1", "rung2", "rung3"))):
                errs.append(f"{p}.rungs 必须含 rung1/rung2/rung3 布尔或为 null")
        tmedia = t.get("media")
        if not isinstance(tmedia, list) or not tmedia \
                or not all(isinstance(x, str) and x for x in tmedia):
            errs.append(f"{p}.media 必须是非空路径字符串数组")
        elif media_paths and not set(tmedia) <= media_paths:
            errs.append(
                f"{p}.media 引用了顶层 media 未声明的路径（媒体错配）："
                f"{sorted(set(tmedia) - media_paths)}")

    agg = obj["aggregate"]
    if not isinstance(agg, dict):
        errs.append("aggregate 必须是对象")
    else:
        eps_total = agg.get("episodes")
        succ_total = agg.get("success")
        rate = agg.get("success_rate")
        eps_ok = (isinstance(eps_total, int) and not isinstance(eps_total, bool)
                  and eps_total == sum_episodes and eps_total >= 1)
        succ_ok = (
            isinstance(succ_total, int) and not isinstance(succ_total, bool)
            and succ_total == sum_success
            and isinstance(eps_total, int) and not isinstance(eps_total, bool)
            and 0 <= succ_total <= eps_total)
        if not eps_ok:
            errs.append("aggregate.episodes 必须 == Σ tasks.episodes 且 ≥1")
        if not succ_ok:
            errs.append("aggregate.success 必须 == Σ tasks.success 且在界内")
        # 分母合法才比率一致性检查：手构 JSON 给 episodes=0/坏类型时，上面已有
        # 错误条目；校验器契约是「只返回错误字符串、永不抛」，故此处不得触发
        # ZeroDivisionError/TypeError（tasks 级有 eps≥1 守卫，aggregate 级同款）。
        if eps_ok and not (isinstance(rate, (int, float))
                           and not isinstance(rate, bool)
                           and abs(float(rate) - succ_total / eps_total) < 1e-9):
            errs.append("aggregate.success_rate 必须 == Σsuccess/Σepisodes")

    used = obj["judge_sources_used"]
    expect_sources = sorted(sources)
    if not (isinstance(used, list) and sorted(used) == expect_sources
            and all(isinstance(x, str) for x in used)):
        errs.append("judge_sources_used 必须恰为 tasks 中标注来源的去重排序集")

    if obj["gate2_ready"]:
        if not any(isinstance(md, dict) and md.get("kind") == "video"
                   for md in media):
            errs.append("gate2_ready=true 时顶层 media 至少含一条 video")
        if not sources:
            errs.append("gate2_ready=true 时必须有判分器来源标注")
    return errs


def load_wj_report(path: "str | Path") -> dict:
    """图接缝入口：缺件/坏 JSON/契约违规统一 WjReportError。"""
    p = Path(path)
    if not p.is_file():
        raise WjReportError(f"WJ 报告不存在：{p}")
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise WjReportError(f"WJ 报告无法解析：{p}: {exc}") from exc
    errs = validate_wj_report(obj)
    if errs:
        raise WjReportError(
            f"WJ 报告契约违规（fail-closed）：{p}: " + "; ".join(errs[:5]))
    return obj


def build_wj_report(*, thread_id: str, stage: str, suite_name: str,
                    n_episodes_per_task: int, tasks: list[dict],
                    media: list[dict], stage0_compat_ref: str,
                    gate2_ready: bool = False, rung4: "dict | None" = None,
                    ts_utc: "str | None" = None) -> dict:
    """由 Stage3 评估产物组装 wj_report v1（自动算成功率/聚合/来源集），
    组装后立即自校验；非法输入抛 WjReportError——不让非法件出引擎。

    tasks 每项：{task_id, bddl, success, episodes, metrics_ref, judge_source,
                 media=[路径...], rungs=None|{rung1,rung2,rung3}}
    media 每项：{kind, path, sha256=None}
    """
    from datetime import datetime, timezone
    if ts_utc is None:
        ts_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        norm_tasks = []
        for t in tasks:
            # int() 非数值 / tasks=[] 的除零等一律包成 WjReportError——
            # docstring 承诺「非法输入抛 WjReportError」，不裸抛内置异常。
            eps, succ = int(t["episodes"]), int(t["success"])
            norm_tasks.append({
                "task_id": t["task_id"],
                "bddl": t["bddl"],
                "success": succ,
                "episodes": eps,
                "success_rate": succ / eps,
                "metrics_ref": t["metrics_ref"],
                "judge_source": t["judge_source"],
                "rungs": t.get("rungs"),
                "media": list(t["media"]),
            })
        total_succ = sum(t["success"] for t in norm_tasks)
        total_eps = sum(t["episodes"] for t in norm_tasks)
        report = {
            "schema": WJ_REPORT_SCHEMA,
            "version": WJ_REPORT_VERSION,
            "task": TASK,
            "platform": PLATFORM,
            "thread_id": thread_id,
            "stage": stage,
            "ts_utc": ts_utc,
            "suite": {"name": suite_name, "n_tasks": len(norm_tasks),
                      "n_episodes_per_task": int(n_episodes_per_task)},
            "stage0_compat_ref": stage0_compat_ref,
            "tasks": norm_tasks,
            "aggregate": {"success": total_succ, "episodes": total_eps,
                          "success_rate": total_succ / total_eps},
            "judge_sources_used": sorted(
                {t["judge_source"] for t in norm_tasks}),
            "media": media,
            "rung4": rung4,
            "gate2_ready": bool(gate2_ready),
        }
    except (ValueError, TypeError, ZeroDivisionError, KeyError) as exc:
        raise WjReportError(
            f"WJ 报告组装输入非法：{type(exc).__name__}: {exc}") from exc
    errs = validate_wj_report(report)
    if errs:
        raise WjReportError("WJ 报告组装自校验失败：" + "; ".join(errs[:5]))
    return report


# ---------------------------------------------------------------------------
# evaluate 接缝（Stage3）：wj_report 路径推导 + 两级装入（已有报表直接读；
# 只有 results.json 原始计数时引擎现算组装并落盘）。组装数字全部引擎计算，
# 不采信脚本自报的衍生比率（WJ 报表契约见 build_wj_report docstring）。
# ---------------------------------------------------------------------------

def _safe_media_rel(workdir: Path, rel: str, *, what: str) -> str:
    """C4 路径守卫：相对、无 ..、resolve 后仍在 workdir 内且文件存在。"""
    if not isinstance(rel, str) or not rel:
        raise WjReportError(f"{what} 路径必须是非空字符串")
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        raise WjReportError(f"{what} 路径越界（绝对/..）：{rel}")
    target = workdir / p
    try:
        contained = target.resolve().is_relative_to(workdir.resolve())
    except OSError:
        contained = False
    if not contained or not target.is_file():
        raise WjReportError(f"{what} 文件不存在或越界：{rel}")
    return p.as_posix()


def _assemble_wj_from_results(results_path: Path, workdir: Path, *,
                              thread_id: str, stage0_compat_ref: str) -> dict:
    """results.json 原始计数 → wj_report v1（引擎现算聚合/比率/来源集）。

    每个字段界与 validate_wj_report 同口径 fail-closed；media 逐件 C4 + 存在性
    复验；metrics_ref 钉 results.json 的 sha256（数字可回指到字节）。Rung1–3
    仅在单任务 run 且 telemetry 过共享契约时附判定向量——LIBERO 原生 success
    不翻译成 telemetry（计划 §1.3），多任务套件不把 whole-run 判定外推。
    """
    try:
        raw = json.loads(results_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WjReportError(
            f"results.json 无法解析：{results_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise WjReportError("results.json 顶层必须是对象")
    if raw.get("schema") != prompts.RESULTS_SCHEMA:
        raise WjReportError(f"results.schema 必须是 {prompts.RESULTS_SCHEMA}")
    stage = raw.get("stage")
    if stage not in WJ_STAGES:
        raise WjReportError(f"results.stage 必须是 {WJ_STAGES} 之一")
    suite_name = raw.get("suite_name")
    if not isinstance(suite_name, str) or not suite_name:
        raise WjReportError("results.suite_name 必须是非空字符串")
    n_eps = raw.get("n_episodes_per_task")
    if not (isinstance(n_eps, int) and not isinstance(n_eps, bool)
            and n_eps >= 1):
        raise WjReportError("results.n_episodes_per_task 必须是 ≥1 整数")
    judge_map = {prompts.JUDGE_OFFICIAL: "官方", prompts.JUDGE_SELF: "自补"}
    judge_raw = raw.get("judge")
    if judge_raw not in judge_map:
        raise WjReportError(
            f"results.judge 必须是 {sorted(judge_map)} 之一（禁止冒充官方）")
    judge_label = judge_map[judge_raw]

    # 顶层 media：脚本声明（可选，逐件 C4）+ 引擎发现的已知产物；去重。
    media: list[dict] = []
    seen: set[str] = set()

    def _add(kind: str, rel: str) -> str:
        norm = _safe_media_rel(workdir, rel, what=f"media({kind})")
        if norm not in seen:
            seen.add(norm)
            media.append({"kind": kind, "path": norm, "sha256": None})
        return norm

    declared = raw.get("media")
    if declared is not None:
        if not isinstance(declared, list):
            raise WjReportError("results.media 若给出必须是数组")
        for i, md in enumerate(declared):
            if not isinstance(md, dict) or md.get("kind") not in MEDIA_KINDS \
                    or not isinstance(md.get("path"), str):
                raise WjReportError(f"results.media[{i}] 需 {{kind,path}} 合法")
            _add(md["kind"], md["path"])
    for kind, rel in (("video", VIDEO_NAME), ("image", COVER_NAME),
                      ("log", RESULTS_NAME)):
        if (workdir / rel).is_file():
            _add(kind, rel)
    video_norm = VIDEO_NAME if (workdir / VIDEO_NAME).is_file() else None

    results_sha = hashlib.sha256(results_path.read_bytes()).hexdigest()
    raw_tasks = raw.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise WjReportError("results.tasks 必须是非空数组")
    tasks: list[dict] = []
    seen_ids: set[str] = set()
    for i, t in enumerate(raw_tasks):
        p = f"tasks[{i}]"
        if not isinstance(t, dict):
            raise WjReportError(f"results.{p} 必须是对象")
        tid = t.get("task_id")
        if not isinstance(tid, str) or not tid:
            raise WjReportError(f"results.{p}.task_id 必须非空")
        if tid in seen_ids:
            raise WjReportError(f"results.{p}.task_id 重复：{tid}")
        seen_ids.add(tid)
        bddl = t.get("bddl")
        if not isinstance(bddl, str) or not bddl:
            raise WjReportError(f"results.{p}.bddl 必须非空（BDDL 可溯源）")
        succ, eps = t.get("success"), t.get("episodes")
        if not (isinstance(succ, int) and not isinstance(succ, bool)
                and isinstance(eps, int) and not isinstance(eps, bool)
                and eps == n_eps and 0 <= succ <= eps):
            raise WjReportError(
                f"results.{p} 需 0 ≤ success ≤ episodes 且 episodes == "
                f"n_episodes_per_task({n_eps})")
        for derived in ("success_rate", "aggregate"):
            if derived in t:
                raise WjReportError(
                    f"results.{p} 禁止自报衍生字段 {derived}"
                    "（引擎只认原始计数）")
        tmedia = t.get("media")
        if tmedia is None:
            tmedia = [r for r in (video_norm, RESULTS_NAME) if r]
        if not isinstance(tmedia, list) or not tmedia:
            raise WjReportError(f"results.{p}.media 若给出必须是非空数组")
        norm_media = [
            _safe_media_rel(workdir, r, what=f"results.{p}.media")
            for r in tmedia]
        tasks.append({
            "task_id": tid,
            "bddl": bddl,
            "success": succ,
            "episodes": eps,
            "metrics_ref": {
                "path": RESULTS_NAME,
                "keys": ["tasks", "success", "episodes"],
                "sha256": results_sha,
            },
            "judge_source": judge_label,
            "rungs": None,
            "media": norm_media,
        })

    if len(tasks) == 1:
        tele_path = workdir / TELEMETRY_NAME
        if tele_path.is_file():
            try:
                judged = evaluate_single_rollout(
                    json.loads(tele_path.read_text(encoding="utf-8")))
                if judged.get("ok"):
                    tasks[0]["rungs"] = judged["vector"]
            except (ValueError, OSError):
                pass  # telemetry 坏/缺 → rungs 保持 null（诚实不判，不算失败）

    return build_wj_report(
        thread_id=thread_id, stage=stage, suite_name=suite_name,
        n_episodes_per_task=n_eps, tasks=tasks, media=media,
        stage0_compat_ref=stage0_compat_ref,
        gate2_ready=(video_norm is not None))


def _derive_wj_path(state: dict, rec: dict) -> "Path | None":
    """wj_report 路径三级推导：state 键 → rec 键 → telemetry 同目录缺省名。

    state 键一旦给出（即使文件缺）即采用——缺件由 load_wj_report fail-closed
    响亮报错，不静默降级改道。"""
    path = state.get(WJ_REPORT_STATE_KEY) or rec.get(WJ_REPORT_STATE_KEY)
    if isinstance(path, str) and path:
        return Path(path)
    tele = rec.get("telemetry_uri") or state.get("telemetry_uri")
    if isinstance(tele, str) and tele:
        cand = Path(tele).parent / WJ_REPORT_NAME
        if cand.is_file():
            return cand
    return None


def make_evaluate_seam(thread_id: str) -> Callable[[dict], dict]:
    """evaluate 节点接缝（Stage3）：不构造任何 LIBERO 环境，只读产物并
    fail-closed 校验。两级装入——
    ① 推导出的 wj_report.json 已存在（state/rec/telemetry 同目录）→ 严格读；
    ② 只有 results.json 原始计数 → 引擎组装（_assemble_wj_from_results）并
       原子落盘 0600 到 workdir/wj_report.json 再读（节点可重放：重入时 ① 先命中）；
    ③ 两者皆无 → GraphStateError（不静默编报告）。
    gate2_ready=False 是合法人判输入（不抛），契约违规才抛。"""
    def seam(state: dict) -> dict:
        runs = state.get("agent_runs", [])
        rec = runs[-1] if runs else {}
        path = _derive_wj_path(state, rec)
        if path is None:
            tele = rec.get("telemetry_uri") or state.get("telemetry_uri")
            if not isinstance(tele, str) or not tele:
                raise GraphStateError(
                    "task7_libero evaluate：state 缺 wj_report_path "
                    "且无 telemetry_uri（无法定位产物）")
            workdir = Path(tele).parent
            stage0_ref = state.get(STAGE0_REPORT_STATE_KEY) or (
                (state.get("chosen") or {}).get(STAGE0_REPORT_STATE_KEY))
            if not isinstance(stage0_ref, str) or not stage0_ref:
                raise GraphStateError(
                    "task7_libero evaluate：state 缺 stage0_report_path"
                    "（WJ stage0_compat_ref 溯源链要求）")
            try:
                report = _assemble_wj_from_results(
                    workdir / RESULTS_NAME, workdir, thread_id=thread_id,
                    stage0_compat_ref=stage0_ref)
            except WjReportError as exc:
                raise GraphStateError(f"task7_libero evaluate：{exc}") from exc
            out = workdir / WJ_REPORT_NAME
            atomic_write_text(
                out, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            path = out
        try:
            report = load_wj_report(path)
        except WjReportError as exc:
            raise GraphStateError(f"task7_libero evaluate：{exc}") from exc
        return {
            "ok": True,
            "thread_id": thread_id,
            "wj_report_path": str(path),
            "schema": report["schema"],
            "stage": report["stage"],
            "aggregate": report["aggregate"],
            "judge_sources_used": report["judge_sources_used"],
            "gate2_ready": report["gate2_ready"],
            "provenance": f"file:{WJ_REPORT_STATE_KEY}",
        }

    return seam


# ---------------------------------------------------------------------------
# Gate1 卡接缝 + Gate2 三问组装（真人均由 feishu_gateway 阻塞等待；本模块
# 零假批准——D-4 降级时连卡都不发，直接 GraphStateError 结案）
# ---------------------------------------------------------------------------

def make_gate1_seam(feishu: Any, thread_id: str, task_title: str):
    """Gate1 接缝：Stage0 兼容事实机判 + 巡礼方案卡（真人批准才放行）。

    propose_run=False（D-4 降级）时**不发卡**、不生成运行案，直接
    GraphStateError 结案——绝不把降级伪装成运行案等一个「本不该存在的批准」。
    """
    def seam(state: dict) -> dict:
        proposal = gate1_proposal(state)
        if not proposal["propose_run"]:
            raise GraphStateError(
                "Task7 LIBERO Stage0 不兼容（D-4 降级 "
                f"{DEGRADATION}）：禁止提 Gate1 运行案"
                f"（reasons={proposal['reasons']}）")
        return feishu.request_gate1_libero_approval(
            thread_id, task_title, state["proposals"],
            stage0_facts=proposal)

    return seam


def build_gate2_explanation(state: dict,
                            rec: dict) -> "dict[str, str] | None":
    """Gate2 卡三问（libero 变体，键必须是 task/problem/video 家法）。

    数字从已验证的 wj_report（evaluate 已落 state.metrics.wj_report_path）
    与 rec（duration/manifest）现读，不编引擎叙事——缺报表返回 None，卡片
    退回纯指标（同 task7 家法：不编故事）。
    """
    metrics = state.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    wj_path = metrics.get("wj_report_path")
    report = None
    if isinstance(wj_path, str) and wj_path:
        try:
            report = load_wj_report(wj_path)
        except WjReportError:
            report = None

    chosen = state.get("chosen") or {}
    scheme = str(chosen.get("name") or "（未命名方案）").strip()
    task = (
        f"Gate1 批准方案：{scheme}\n"
        "本次是 LIBERO 第五平台 track 的 Stage1/2 实跑——最小操作闭环"
        "（libero_spatial）官方判分逐任务计数，数字经 WJ 契约可溯源。")
    lines = [
        "它要回答的不是「脚本有没有跑通」，而是「这次 LIBERO 实跑能否作为"
        "第五平台的可信证据」：",
        "· 官方判分：每任务 success 按 LIBERO check_success 计，聚合成功率"
        " = Σsuccess/Σepisodes（引擎现算，不信脚本自报比率）；",
        "· 数字可溯源：每个计数回指 results.json（路径 + 字段键 + sha256）；",
        "· WJ 契约：simpraxis.wj_report.v1 机检（判分来源标注官方/自补，"
        "自补分数不外推）；Rung4 视觉永不单独放行。",
    ]
    if report is not None:
        agg = report.get("aggregate") or {}
        rate = agg.get("success_rate")
        rate_s = f"{rate:.3f}" if isinstance(rate, (int, float)) \
            and not isinstance(rate, bool) else "?"
        lines.append(
            f"本次聚合：{agg.get('success')}/{agg.get('episodes')} 局成功"
            f"（成功率 {rate_s}，stage={report.get('stage')}，判分来源 "
            f"{'/'.join(report.get('judge_sources_used') or [])}）。")
    problem = "\n".join(lines)

    parts: list[str] = []
    tele = rec.get("telemetry_uri") or state.get("telemetry_uri")
    if isinstance(tele, str) and tele:
        mp = Path(tele).parent / MANIFEST_NAME
        if mp.is_file():
            try:
                loaded = json.loads(mp.read_text(encoding="utf-8"))
                n_frames = loaded.get("n_frames") if isinstance(
                    loaded, dict) else None
                if isinstance(n_frames, int) and n_frames > 0:
                    parts.append(f"{n_frames} 帧")
            except (ValueError, OSError):
                pass
    dur_ms = rec.get("duration_ms")
    if isinstance(dur_ms, (int, float)) and not isinstance(dur_ms, bool) \
            and dur_ms > 0:
        parts.append(f"{dur_ms / 1000:.1f} s")
    length = ("本段约 " + "、".join(parts)) if parts else ""
    video = (
        "请重点看：任务执行是否真实连贯（抓取/移动/放置无穿透、瞬移、悬空），"
        "成功局与失败局是否与上方聚合计数一致。"
        + (f"（{length}）" if length else ""))
    return {"task": task, "problem": problem, "video": video}
