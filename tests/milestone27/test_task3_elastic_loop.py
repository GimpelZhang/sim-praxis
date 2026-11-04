"""Phase 2.7 Task 3（D3-1..D3-10）：弹性调试内环机判 —— AC-T3-1..13（0 付费）。

对应计划 §T3.6 的十三条验收准则，逐条一个用例（名字与机判命令逐字一致）：

``test_ac_t3_1_prompt_budget`` / ``test_ac_t3_2_preview_probe``（container）/
``test_ac_t3_3_twenty_round_soak`` / ``test_ac_t3_4_no_blind_search`` /
``test_ac_t3_5_round_accounting`` / ``test_ac_t3_6_cache_epoch_v4`` /
``test_ac_t3_7_no_image_to_coding`` / ``test_ac_t3_8_security_boundary_intact`` /
``test_ac_t3_9_privileged_state_gate`` / ``test_ac_t3_10_visual_critic_accounting`` /
``test_ac_t3_11_spatial_relation_gate`` / ``test_ac_t3_12_scratchpad_safety_belt`` /
``test_ac_t3_13_contracts_moved_to_gate2``。

纪律（硬规则 2/7）：

- 全部 mock 网关 + 本地/桩后端，**0 实呼、0 联网**；唯一真容器是带 ``container``
  marker 的两条（裸跑收集期 deselect，不是 skip —— 与 Task 1 同构）；
- 断言不放水：阈值只从常量读取（``PROMPT_BASELINE_BYTES`` / ``PREVIEW_COLD_TIMEOUT_S``
  / ``VISUAL_TEXT_MAX`` …），不抄字面量；冷/热耗时**实测**，超限即红；
- 分支 (a)（``spout_defined=True``）由 ``tests/fixtures/milestone27/
  manifest_spout_true.json`` 合成夹具驱动（四件真实资产全为轴对称件，这是本
  Phase 的确定事实，见 AC-T3-11⑤）——不 skip、不空转。

计数口径（AC-T3-3「trace 行数 == 20」）：trace 里除了 ``__model_round__``（一次
付费网关轮）还有 ``__condense__`` / ``__answer__`` / ``__env_spec__`` 等引擎控制行
（``seq=null``、``step=0``，见 ``forensics`` 的 ``*_TOOL`` 常量）。「轮」只按模型轮
行计——把控制行数进「轮」会把「20 轮」说成 37 轮（实测 20 轮 soak 产 17 条
``__condense__``）。本文件因此断言：模型轮行恰 20 条，且**工具行** 0 条（soak 是
纯模型轮，不带工具副作用那一支另有 AC-T3-3 的工具混合支）。

证据落 ``$SIMPRAXIS_PHASE27_ROOT/task3_elastic_loop/``（0700 目录 / 0600 文件，
§T3.7 九件 + ``env_spec.json`` 环境卡 + 本 Task 的两份附加对照件）。
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import itertools
import json
import os
import re
import stat
import subprocess
import sys
import types
import zlib
from pathlib import Path

import pytest
import yaml

import orchestrator.gateway.router as R
from orchestrator.agentic.caps import BudgetTracker
from orchestrator.agentic.config import ALL_OFF, AgenticConfig
from orchestrator.agentic.env_spec import (
    ENV_SPEC_NAME,
    ENV_SPEC_PATH_ENV,
    ENV_SPEC_SCHEMA,
    EnvSpecError,
    EnvSpec,
    PrivilegedState,
    load_env_spec,
    static_env_spec,
)
from orchestrator.agentic.executor import HardenedLocalPythonExecutor
from orchestrator.agentic.forensics import (
    CACHE_VERSION,
    CACHE_VERSION_LEGACY,
    CACHE_VERSION_V4,
    CONDENSE_TOOL,
    ENV_SPEC_TOOL,
    MODEL_ROUND_TOOL,
    TraceStore,
    cache_epoch_for,
    cache_prefixes,
    sha256_text,
)
from orchestrator.agentic.image_pin import (
    AGENTIC_IMAGE,
    SCENE_BUILD_IMAGE,
    SCENE_BUILD_PYTHON,
)
from orchestrator.agentic.loop import (
    SEED_MAX_CHARS,
    SEED_TOOLS,
    _preview_permit,
    run_agentic_inner_loop,
)
from orchestrator.agentic.model import flatten_text_messages
from orchestrator.agentic.policy import (
    POLICY_DENY_MARKER,
    GrantTable,
    PreviewPermit,
    ToolContext,
)
from orchestrator.agentic.prompt import (
    ENV_SPEC_MARKER,
    PROMPT_BASELINE_BYTES,
    PROMPT_BUDGET_BYTES,
    PROMPT_SOFT_BYTES,
    SCRATCHPAD_RULE,
    SCRATCHPAD_RULE_BYTES,
    SKILLS_MARKER,
    build_system_prompt,
    prompt_bytes,
)
from orchestrator.agentic.tools.render_preview import (
    PREVIEW_COLD_TIMEOUT_S,
    PREVIEW_MAX_PX,
    PREVIEW_WARM_TIMEOUT_S,
    VISUAL_TEXT_MAX,
    CAMERAS,
    compute_spatial_relation,
    render_quick_preview,
    resolve_from_manifest,
)
from orchestrator.agentic.tools.run import (
    _MODEL_ENV_ALLOWLIST,
    _validate_env,
    check_backend,
)
from orchestrator.agentic.tools.scratchpad import SCRATCHPAD_REL
from orchestrator.assets import manifest as mf
from orchestrator.assets.scene_spec import Body, SceneSpec
from orchestrator.common import runtime_paths as rp
from orchestrator.common.callcache import CallReplay
from orchestrator.gateway import quota_ledger as ql
from orchestrator.gateway.quota_ledger import (
    QuotaLedger,
    billing_context,
    ledger_summary,
)
from orchestrator.graph import task8_fluid_contracts as contracts
from orchestrator.skills import registry as skills_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "milestone27"
SPOUT_FIXTURE = FIXTURES / "manifest_spout_true.json"
PROBES_DIR = REPO_ROOT / "sandbox" / "probes"
ENV_SCAN_PROBE = PROBES_DIR / "probe_env_scan.py"
ZOO_ROOT = mf.zoo_root()

# 生产 phase27 根在**导入时点**取一次（monkeypatch 的 tmp 账根不影响证据落点）。
PROD_P27_ROOT = rp.phase27_root()
EVIDENCE = PROD_P27_ROOT / "task3_elastic_loop"

_FENCE = "```python"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_B64_MARKERS = ("iVBORw0KGgo", "data:image")
# AC-T3-11④ 的关键点/空间关系词形（模式 R 一律不许出现）。
# ``pitch(?!er)`` 的排除项是**必要的口径澄清**而非放水：资产名
# ``vessel/pitcher_standard`` 里的 "pitch" 是名词词干、不是 `pitcher_pitch_angle_deg`
# 那个空间关系键；AC 原文要求扫的是「关键点/空间关系**数字**形态」，而
# `..._angle_deg` / 独立的 `pitch` 仍被本式命中（`pitcher_pitch_angle_deg`
# 的第二个 "pitch" 后面跟 `_`，照样中）。真值几何一旦泄漏必被抓住。
_RELATION_WORDS = re.compile(r"rim_center|spout|tilt_axis|pitch(?!er)")


# ---------------------------------------------------------------------------
# 通用脚手架
# ---------------------------------------------------------------------------

class ScriptedGateway:
    """按调用序号回放代码块；``calls`` 数即「模型轮」代理（0 付费）。"""

    def __init__(self, plays):
        self.plays = list(plays)
        self.calls = []

    def model_id(self, stage):
        return "unit-fake-model"

    def call(self, stage, messages, **kwargs):
        i = len(self.calls)
        self.calls.append({"stage": stage,
                           "messages": [_norm_message(m) for m in messages]})
        text = self.plays[min(i, len(self.plays) - 1)]
        return text, (10 + i, 20 + i)


class HybridGateway(ScriptedGateway):
    """模型轮走脚本，``call_visual`` 转发到真网关（只为 AC-T3-10 的账务）。"""

    def __init__(self, plays, *, visual):
        super().__init__(plays)
        self._visual = visual
        self.visual_calls = []

    def call_visual(self, prompt, image_b64, **kwargs):
        self.visual_calls.append({"prompt": prompt, "image_b64": image_b64})
        return self._visual.call_visual(prompt, image_b64, **kwargs)


def _norm_message(m):
    if isinstance(m, dict):
        return {"role": m.get("role"), "content": m.get("content")}
    return {"role": getattr(m, "role", None),
            "content": getattr(m, "content", None)}


def _play(code: str) -> str:
    return f"{_FENCE}\n{code}\n```"


def _rows(trace_path) -> list[dict]:
    path = Path(trace_path)
    return [json.loads(line) for line in path.read_text(encoding="utf-8")
            .splitlines() if line.strip()]


def _model_rows(rows) -> list[dict]:
    return [r for r in rows if r.get("tool") == MODEL_ROUND_TOOL]


def _tool_rows(rows) -> list[dict]:
    """非模型轮、非控制行（控制行名以 ``__`` 开头，见 forensics 常量）。"""
    return [r for r in rows
            if not str(r.get("tool", "")).startswith("__")]


def _msg_text(msg) -> str:
    content = msg.get("content") if isinstance(msg, dict) \
        else getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(getattr(block, "text", "")))
        return "".join(parts)
    return str(content)


def _first_system_text(messages) -> str:
    for msg in messages:
        role = msg.get("role") if isinstance(msg, dict) \
            else getattr(msg, "role", None)
        if role == "system":
            return _msg_text(msg)
    raise AssertionError("payload 中不存在 system 消息")


def _write_evidence(name: str, text: str) -> Path:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    os.chmod(EVIDENCE, 0o700)
    path = EVIDENCE / name
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(EVIDENCE.stat().st_mode) == 0o700
    return path


def _run_loop(tmp_path: Path, plays, *, cfg=None, round_cap: int = 12,
              thread_id: str = "thr-t3", call_cache=None,
              files: "dict[str, str] | None" = None,
              grants: "GrantTable | None" = None, backend=None,
              gateway=None, seq_provider=None, nonprod: bool = False,
              probe_registry=None):
    """跑一次 agentic 内环（fake gateway）；返回 (wd, budget, gateway, outcome)。"""
    wd = tmp_path / "wd"
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "a.txt").write_text("alpha\n", encoding="utf-8")
    for name, content in (files or {}).items():
        path = wd / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    budget = BudgetTracker(round_cap=round_cap)
    tracer = TraceStore(tmp_path / "traces", thread_id)
    gw = gateway if gateway is not None else ScriptedGateway(plays)
    outcome = run_agentic_inner_loop(
        workdir=wd, task="task3 acceptance",
        grants=grants or GrantTable(
            tools=frozenset({"read_file", "list_dir", "grep"}),
            write_roots=("",)),
        gateway=gw, budget=budget, tracer=tracer, thread_id=thread_id,
        call_cache=call_cache, run_backend=backend, nonprod=nonprod,
        probe_registry=probe_registry, seq_provider=seq_provider,
        agentic_config=cfg)
    return wd, budget, gw, outcome


class StubRunBackend:
    """兄弟容器形态的桩后端：零 IO、零进程，回 rc=0（写类工具因此可放行）。

    ``extra_ro_mounts`` 必须带 ``/assets``：``render_quick_preview`` 对兄弟容器
    形态硬校验资产库挂载（缺则 deny，见 tools/render_preview.py 的挂载门）。
    """

    form = "sibling_container"
    nonprod = False

    def __init__(self, probes_path=None, extra_ro_mounts=None):
        self.probes_path = str(probes_path) if probes_path else None
        self.extra_ro_mounts = (
            ((str(ZOO_ROOT), "/assets"),) if extra_ro_mounts is None
            else tuple(extra_ro_mounts))
        self.calls: list[dict] = []

    def run(self, argv, *, cwd, env=None, timeout_s=60):
        from orchestrator.agentic.tools.run import CommandResult

        self.calls.append({"argv": list(argv), "cwd": str(cwd),
                           "env": dict(env or {}), "timeout_s": timeout_s})
        return CommandResult(rc=0, stdout=b"ok\n", stderr=b"", timed_out=False,
                             argv=list(argv), form=self.form)


def _png_256() -> bytes:
    """最小的 256×256 真 PNG（IHDR 头逐字节正确；AC 只验尺寸与魔数）。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (len(data).to_bytes(4, "big") + tag + data
                + zlib.crc32(tag + data).to_bytes(4, "big"))

    ihdr = (PREVIEW_MAX_PX.to_bytes(4, "big") + PREVIEW_MAX_PX.to_bytes(4, "big")
            + bytes((8, 2, 0, 0, 0)))
    return _PNG_MAGIC + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


class PreviewStubBackend(StubRunBackend):
    """预览探针的桩后端：按 ``--out`` 写 PNG + 旁侧回执，再回一行 JSON。

    argv/挂载门控仍由真 policy/jail 走一遍（``render_quick_preview`` 内部），
    本桩只替换「容器真的渲一帧」这一步——AC-T3-7/10/11 验的是回执与门控，
    真渲染由 container marker 的 AC-T3-2 负责。
    """

    def __init__(self, probes_path, *, wall_s=3.0, warm_s=0.5,
                 unique_colors=3, extra_rows=None):
        super().__init__(probes_path=probes_path)
        self.wall_s = wall_s
        self.warm_s = warm_s
        self.unique_colors = unique_colors
        self.extra_rows = extra_rows

    def run(self, argv, *, cwd, env=None, timeout_s=60):
        from orchestrator.agentic.tools.run import CommandResult

        self.calls.append({"argv": list(argv), "cwd": str(cwd)})
        cwd = Path(cwd)
        assert "--out" in argv, argv
        out_rel = argv[argv.index("--out") + 1]
        camera = argv[argv.index("--camera") + 1] if "--camera" in argv else "iso"
        out_path = cwd / out_rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(_png_256())
        rows = self.extra_rows or [
            {"asset": "vessel/cup_standard",
             "min": [0.28, 0.21, 1.0], "max": [0.36, 0.29, 1.08],
             "center": [0.32, 0.25, 1.04], "extents": [0.08, 0.08, 0.08]},
            {"asset": "vessel/pitcher_standard",
             "min": [0.14, 0.19, 1.0], "max": [0.26, 0.31, 1.12],
             "center": [0.20, 0.25, 1.06], "extents": [0.12, 0.12, 0.12]},
        ]
        receipt = {
            "schema": "simpraxis.preview_probe.v1", "ok": True, "camera": camera,
            "png": out_rel, "size": [PREVIEW_MAX_PX, PREVIEW_MAX_PX],
            "bbox_table": rows,
            "render_seconds_first": round(self.wall_s / 4.0, 3),
            "render_seconds_second": self.warm_s,
            "wall_s": self.wall_s, "unique_colors": self.unique_colors,
            "std": 0.42, "light_added": True,
        }
        out_path.with_suffix(".json").write_text(
            json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        return CommandResult(
            rc=0, stdout=(json.dumps(receipt, ensure_ascii=False) + "\n").encode(),
            stderr=b"", timed_out=False, argv=list(argv), form=self.form)


def _scene_spec_json() -> str:
    spec = SceneSpec(engine="genesis", bodies=(
        Body(asset="vessel/pitcher_standard", pos=(0.20, 0.25, 1.00), fixed=True),
        Body(asset="vessel/cup_standard", pos=(0.32, 0.25, 1.00), fixed=True),
    ))
    return spec.to_json()


def _scene_spec_file(workdir: Path, *, name: str = "scene_spec.json") -> Path:
    path = workdir / name
    path.write_text(_scene_spec_json(), encoding="utf-8")
    return path


def _preview_ctx(workdir: Path, *, preview, probes_path=PROBES_DIR,
                 tools=("render_quick_preview",), backend=None) -> ToolContext:
    return ToolContext(
        workdir=workdir,
        grants=GrantTable(tools=frozenset(tools), write_roots=("",)),
        thread_id="thr-t3", run_backend=backend or PreviewStubBackend(probes_path),
        preview=preview)


# ---------------------------------------------------------------------------
# AC-T3-1：单一 prompt 入口与字节预算（+ 20 轮 sha256 恒定）
# ---------------------------------------------------------------------------

# 2026-11-03 实测：SCRATCHPAD_RULE 的 sha256（逐字钉死，改一个字就红）。
SCRATCHPAD_RULE_SHA256 = (
    "751e2dfea542e551f207badbe3c95e248ad5756ab79fd7bab78143bd91b87bbb")


def test_ac_t3_1_prompt_budget(tmp_path, monkeypatch):
    """无 ENV_SPEC ⇒ 基线 ±0；含 ENV_SPEC+技能块 ⇒ ≤ 软上限；硬上限必抛。"""
    cfg = AgenticConfig(verify_repair_tail=True, run_expr=True,
                        tolerant_patch=True, scratchpad=True)

    # ① 无 ENV_SPEC 时 == 基线口径（3,057 + SCRATCHPAD_RULE_BYTES），±0。
    baseline = prompt_bytes(cfg)
    assert SCRATCHPAD_RULE_BYTES == 223
    assert PROMPT_BASELINE_BYTES + SCRATCHPAD_RULE_BYTES == 3280
    assert baseline == PROMPT_BASELINE_BYTES + SCRATCHPAD_RULE_BYTES, baseline
    plain = build_system_prompt(cfg=cfg)
    assert ENV_SPEC_MARKER not in plain
    assert SKILLS_MARKER not in plain

    # ② 含 ENV_SPEC + 技能块（真实技能清单）⇒ ≤ 6,000 B，且标记/解释器在场。
    names = tuple(e.name for e in skills_registry.list_skills())
    assert names, "技能清单为空：Task 2 的 manifest 未登记任何技能"
    env_spec = static_env_spec(skills=names)
    env_block = env_spec.render_block(mode="R")
    skills_block = skills_registry.env_spec_skills_block()
    full = build_system_prompt(cfg=cfg, env_spec_block=env_block,
                               skills_block=skills_block)
    full_bytes = len(full.encode("utf-8"))
    assert full_bytes <= PROMPT_SOFT_BYTES, f"{full_bytes} B > 软上限"
    assert full_bytes <= PROMPT_BUDGET_BYTES
    assert ENV_SPEC_MARKER in full and SKILLS_MARKER in full
    assert env_spec.python in full, "环境卡缺解释器路径"

    # ③ 任何情况 ≤ 12,000 B：超过即抛错，不截断。
    with pytest.raises(ValueError) as ei:
        build_system_prompt(cfg=cfg, env_spec_block=env_block,
                            extra="x" * (PROMPT_BUDGET_BYTES + 1))
    assert str(PROMPT_BUDGET_BYTES) in str(ei.value)

    # ④ 20 轮 soak：system prompt sha256 恒定（D3-9，prompt 不随历史增长）。
    # ENV_SPEC 卡片缺失 ⇒ 静态段兜底（走真实降级路径，不伪造一张卡）。
    missing = tmp_path / "no_such_env_spec.json"
    monkeypatch.setenv(ENV_SPEC_PATH_ENV, str(missing))
    soak_cfg = AgenticConfig(env_spec=True)
    plays = [_play('read_file(path="a.txt")') for _ in range(20)]
    wd, _budget, gateway, outcome = _run_loop(
        tmp_path, plays, cfg=soak_cfg, round_cap=20)
    assert outcome.rounds_used == 20 and len(gateway.calls) == 20
    first = _first_system_text(gateway.calls[0]["messages"])
    last = _first_system_text(gateway.calls[19]["messages"])
    assert sha256_text(first) == sha256_text(last)
    assert ENV_SPEC_MARKER in first
    rows = _rows(outcome.trace_path)
    env_rows = [r for r in rows if r.get("tool") == ENV_SPEC_TOOL]
    assert [r["args"]["status"] for r in env_rows] == ["env_spec_missing"]

    _write_evidence("prompt_bytes_report.json", json.dumps({
        "baseline_bytes": baseline,
        "baseline_expected": PROMPT_BASELINE_BYTES + SCRATCHPAD_RULE_BYTES,
        "with_env_spec_and_skills_bytes": full_bytes,
        "soft_limit_bytes": PROMPT_SOFT_BYTES,
        "hard_limit_bytes": PROMPT_BUDGET_BYTES,
        "env_spec_block_bytes": len(env_block.encode("utf-8")),
        "skills_block_bytes": len(skills_block.encode("utf-8")),
        "skills": list(names),
        "soak_prompt_sha256_round1": sha256_text(first),
        "soak_prompt_sha256_round20": sha256_text(last),
        "scratchpad_rule_sha256": hashlib.sha256(
            SCRATCHPAD_RULE.encode("utf-8")).hexdigest(),
    }, ensure_ascii=False, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# AC-T3-2：容器内渲染预览（container marker）
# ---------------------------------------------------------------------------

@pytest.mark.container
def test_ac_t3_2_preview_probe(tmp_path):
    """真容器渲一帧：ok / 256×256 / bbox ≥2 行 / verdict 非空 / 冷热耗时达标。

    冷启口径（2026-11-04 用户裁决的 G0-12 物）：判据仍是 ``≤ 60 s`` 不变，
    但测量前置条件是**编译缓存已预热**——taichi 内核 + 着色器/numba 编译在
    冷缓存下占 ~76 s（整进程 96.6 s），而它们本就可缓存。缓存挂在
    ``<phase27>/cache/genesis → /cache``（``run.py::_validate_cache_mount``）。
    缓存为空时本用例先跑一次预热并**如实记录** ``prime_wall_s``（一次性，
    非判据），再跑判据那一次。``cache_dir`` 必须等于 ``/cache``——否则说明
    挂载没生效、探针退回 tmpfs 慢路径，本 AC 的绿就是假的。
    """
    if not ZOO_ROOT.is_dir() or not (ZOO_ROOT / "zoo_manifest.json").is_file():
        pytest.fail(f"Asset Zoo 缺件：{ZOO_ROOT}（Task 1 未完成，无法判本 AC）")
    from orchestrator.agentic.tools.run import DockerSiblingBackend
    from orchestrator.common.runtime_paths import phase27_preview_cache_dir

    workdir = tmp_path / "work"
    workdir.mkdir()
    _scene_spec_file(workdir)
    cache_dir = phase27_preview_cache_dir()
    cold_cache = not any(cache_dir.iterdir())

    def _once():
        backend = DockerSiblingBackend(
            image=SCENE_BUILD_IMAGE, probes_path=str(PROBES_DIR),
            extra_ro_mounts=((str(ZOO_ROOT), "/assets"),),
            cache_path=str(cache_dir))
        ctx = _preview_ctx(workdir, preview=PreviewPermit(), backend=backend)
        res = render_quick_preview(ctx, scene_spec_path="scene_spec.json",
                                   camera="iso")
        assert not res.denied, res.output
        sc = json.loads(
            (workdir / ".preview" / "preview-iso.json").read_text(encoding="utf-8"))
        return res, sc

    result, sidecar = _once()
    prime_wall = sidecar["wall_s"] if cold_cache else None
    if cold_cache:
        result, sidecar = _once()      # 预热后的判据测量

    assert sidecar["schema"] == "simpraxis.preview_probe.v1"
    assert sidecar["ok"] is True, sidecar
    assert sidecar["size"] == [PREVIEW_MAX_PX, PREVIEW_MAX_PX] == [256, 256]
    assert len(sidecar["bbox_table"]) >= 2, sidecar["bbox_table"]
    assert sidecar.get("cache_dir") == "/cache", (
        "预览探针没吃到 /cache 编译缓存（挂载未生效，冷启必然超判据）；"
        f"实际 cache_dir={sidecar.get('cache_dir')!r}")

    png = (workdir / ".preview" / "preview-iso.png").read_bytes()
    assert png.startswith(_PNG_MAGIC)
    # PNG IHDR：宽高各 4 字节大端，紧跟 8 字节魔数 + 4 字节长度 + "IHDR"。
    assert int.from_bytes(png[16:20], "big") == 256
    assert int.from_bytes(png[20:24], "big") == 256

    lines = result.output.splitlines()
    verdict = [ln for ln in lines if ln.startswith("verdict: ")]
    assert verdict and verdict[0][len("verdict: "):].strip(), lines
    assert "bbox_table" in result.output

    # 冷启 / 热调用实测（阈值只从常量读，**超限即红**，不得放宽）。
    cold = sidecar["wall_s"]
    warm = sidecar["render_seconds_second"]
    assert cold <= PREVIEW_COLD_TIMEOUT_S, (
        f"冷启 {cold}s 超 {PREVIEW_COLD_TIMEOUT_S}s（先查镜像/GPU，不得放宽阈值）")
    assert warm <= PREVIEW_WARM_TIMEOUT_S, (
        f"热调用 {warm}s 超 {PREVIEW_WARM_TIMEOUT_S}s")

    prime_line = (
        f"# prime_wall_s={prime_wall}（缓存空时首次预热，一次性，非判据）"
        if prime_wall is not None else "# prime_wall_s=n/a（缓存已预热）")
    _write_evidence("preview_probe.log", "\n".join([
        f"# AC-T3-2 容器内预览探针（image={SCENE_BUILD_IMAGE}）",
        f"# cache_dir={sidecar.get('cache_dir')}（宿主 {cache_dir}）",
        prime_line,
        f"# cold_wall_s={cold}（判据 <= {PREVIEW_COLD_TIMEOUT_S}）",
        f"# warm_render_seconds_second={warm}（判据 <= {PREVIEW_WARM_TIMEOUT_S}）",
        f"# render_seconds_first={sidecar['render_seconds_first']}",
        f"# unique_colors={sidecar['unique_colors']} std={sidecar['std']} "
        f"light_added={sidecar['light_added']}",
        f"# png_sha256={hashlib.sha256(png).hexdigest()}",
        "",
        "--- receipt ---",
        json.dumps(sidecar, ensure_ascii=False, indent=2, sort_keys=True),
        "",
        "--- tool output ---",
        result.output,
        "",
    ]))


# ---------------------------------------------------------------------------
# AC-T3-3：20 轮 soak（工具混合，无截断，无 CapExceeded）
# ---------------------------------------------------------------------------

_PATCH_TEMPLATE = (
    "--- a/{name}\n"
    "+++ b/{name}\n"
    "@@ -1,3 +1,3 @@\n"
    " alpha\n"
    "-beta\n"
    "+BETA\n"
    " gamma\n"
)

_SOAK_GRANTS = GrantTable(
    tools=frozenset({"read_file", "list_dir", "grep", "apply_patch",
                     "write_file", "run_expr", "run_cmd"}),
    run_argv=(("echo",),),
    write_roots=("",),
    expr_modules=frozenset({"math"}),
)


def _soak_plays(cycles: int = 4) -> list[str]:
    """逐轮调工具（read/list/expr/patch/run，§T3.5 步骤 6）的确定性脚本。"""
    plays = []
    for cycle in range(cycles):
        target = f"patch-{cycle}.txt"
        plays.extend([
            _play('read_file(path="a.txt")'),
            _play('list_dir(path=".")'),
            _play('run_expr(src="import math\\nprint(round(math.pi, 3))")'),
            _play(f"apply_patch(patch={json.dumps(json.dumps({'unified_diff': _PATCH_TEMPLATE.format(name=target)}))})"),
            _play('run_cmd(argv=["echo", "hi"])'),
        ])
    return plays


def test_ac_t3_3_twenty_round_soak(tmp_path):
    """20 轮 soak：rounds_used==20、无截断、模型轮行==20、无 CapExceeded。"""
    files = {f"patch-{i}.txt": "alpha\nbeta\ngamma\n" for i in range(4)}
    # run_expr 只在 cfg.run_expr 时进授予面（loop.py 的 enabled 集合），soak 的
    # 五种工具调用因此必须开这一档，否则「expr」一路静默缺行。
    wd, budget, gateway, outcome = _run_loop(
        tmp_path, _soak_plays(4), cfg=AgenticConfig(run_expr=True),
        round_cap=20,
        files=files, grants=_SOAK_GRANTS, backend=StubRunBackend()
    )

    assert outcome.rounds_used == 20, outcome.reason
    assert budget.rounds_used == 20
    assert len(gateway.calls) == 20
    # 到帽是**优雅终局**（KIND_ROUND_CAP），不是抛进测试的 CapExceeded。
    assert outcome.kind == "round_cap", outcome.kind
    assert outcome.reason.startswith("coding 轮帽耗尽")

    rows = _rows(outcome.trace_path)
    model_rows = _model_rows(rows)
    assert len(model_rows) == 20
    assert not [r for r in rows if r["tool"] == "__answer__"], rows
    # 无截断：模型轮与工具行的 obs_truncated 全为 False。
    for row in rows:
        assert row["obs_truncated"] is False, row

    tool_rows = _tool_rows(rows)
    by_tool: dict[str, int] = {}
    for row in tool_rows:
        by_tool[row["tool"]] = by_tool.get(row["tool"], 0) + 1
        assert row["policy_decision"]["decision"] == "allow", row
    assert by_tool == {"read_file": 4, "list_dir": 4, "run_expr": 4,
                       "apply_patch": 4, "run_cmd": 4}, by_tool
    for name in files:
        assert "BETA" in (wd / name).read_text(encoding="utf-8")

    _write_evidence("soak_20_rounds.trace.jsonl",
                    Path(outcome.trace_path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# AC-T3-4：ENV_SPEC 在场 ⇒ 零盲搜
# ---------------------------------------------------------------------------

_BLIND_ARGV0 = frozenset({"find", "ls", "whereis", "locate", "grep"})


def test_ac_t3_4_no_blind_search(tmp_path, monkeypatch):
    """环境卡点名目标模块 ⇒ 首轮直达；``find /`` 被 policy 白名单拒；盲搜 0。"""
    spec = EnvSpec(
        python="/usr/bin/python",
        workdir_tree=("pkg/", "pkg/target.py", "pkg/util.py", "a.txt"),
        skills=("mujoco.contact_push",),
        privileged_state=PrivilegedState(available=False, accessor="unavailable"),
    )
    spec_path = tmp_path / ENV_SPEC_NAME
    spec_path.write_text(json.dumps(spec.to_json(), ensure_ascii=False),
                         encoding="utf-8")
    monkeypatch.setenv(ENV_SPEC_PATH_ENV, str(spec_path))

    grants = GrantTable(
        tools=frozenset({"read_file", "list_dir", "run_cmd"}),
        run_argv=(("python3",), ("echo",)),
        write_roots=("",))
    plays = [
        _play('read_file(path="pkg/target.py")'),
        _play('run_cmd(argv=["find", "/"])'),
    ] + [_play('read_file(path="a.txt")') for _ in range(6)]

    wd, _budget, gateway, outcome = _run_loop(
        tmp_path, plays, cfg=AgenticConfig(env_spec=True), round_cap=8,
        files={"pkg/target.py": "TARGET = 'ok'\n", "pkg/util.py": "X = 1\n"},
        grants=grants, backend=StubRunBackend())

    # ① 首轮直达：ENV_SPEC 里点名了 pkg/target.py，第 1 轮就读它。
    first_prompt = _first_system_text(gateway.calls[0]["messages"])
    assert ENV_SPEC_MARKER in first_prompt
    assert "pkg/target.py" in first_prompt
    rows = _rows(outcome.trace_path)
    env_rows = [r for r in rows if r.get("tool") == ENV_SPEC_TOOL]
    assert [r["args"]["status"] for r in env_rows] == ["env_spec_loaded"]
    assert env_rows[0]["args"]["path"] == str(spec_path)

    tool_rows = _tool_rows(rows)
    assert tool_rows[0]["tool"] == "read_file"
    assert "pkg/target.py" in json.dumps(tool_rows[0]["args"])
    assert tool_rows[0]["policy_decision"]["decision"] == "allow"

    # ② find / 被 argv 白名单拒：deny 观察 + 前缀理由。
    find_rows = [r for r in tool_rows if r["tool"] == "run_cmd"]
    assert len(find_rows) == 1
    denied = find_rows[0]
    assert denied["policy_decision"]["decision"] == "deny"
    assert denied["obs_head"].startswith(POLICY_DENY_MARKER)
    assert "argv 前缀" in denied["policy_decision"]["reason"]
    assert "/" in denied["policy_decision"]["reason"]

    # ③ 盲搜调用数 == 0（放行的搜索类调用一条都没有）。
    blind = [r for r in tool_rows
             if r["tool"] == "run_cmd"
             and r["policy_decision"]["decision"] == "allow"
             and str((r["args"] or {}).get("argv", [""])[0]) in _BLIND_ARGV0]
    assert blind == []

    _write_evidence("env_spec_blind_search.json", json.dumps({
        "env_spec_path": str(spec_path),
        "prompt_has_target": "pkg/target.py" in first_prompt,
        "env_spec_rows": [r["args"] for r in env_rows],
        "denied_argv": denied["args"],
        "deny_reason": denied["policy_decision"]["reason"],
        "blind_search_allowed": len(blind),
        "tool_rows": [{"round": r["round"], "tool": r["tool"],
                       "decision": r["policy_decision"]["decision"]}
                      for r in tool_rows],
    }, ensure_ascii=False, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# AC-T3-5：付费轮记账（3 实呼 + 1 缓存命中）
# ---------------------------------------------------------------------------

class _RecordingCache(CallReplay):
    """记录 put 顺序的 CallReplay（用于精确定位「第 4 轮」的缓存键）。"""

    def __init__(self, cache_dir):
        super().__init__(cache_dir)
        self.put_keys: list[str] = []

    def put(self, key, **kwargs):
        self.put_keys.append(key)
        return super().put(key, **kwargs)


def test_ac_t3_5_round_accounting(tmp_path):
    """4 轮：3 实呼（整数 seq）+ 1 命中（seq=null）；命中不产生免费第 5 轮。"""
    cache = _RecordingCache(tmp_path / "cache")
    plays = [_play('read_file(path="a.txt")') for _ in range(4)]
    _wd, _b, _gw, first = _run_loop(
        tmp_path / "run_a", plays, cfg=ALL_OFF, round_cap=4,
        call_cache=cache, seq_provider=itertools.count(1).__next__)
    assert first.rounds_used == 4
    assert first.cache_hits == 0
    assert len(cache.put_keys) == 4

    # 只保留第 4 轮的缓存件，前三轮删掉 ⇒ 第二次：3 miss + 1 hit。
    keep = cache.put_keys[3]
    for key in cache.put_keys[:3]:
        path = cache.dir / f"{key}.json"
        assert path.is_file()
        path.unlink()

    _wd2, _b2, _gw2, second = _run_loop(
        tmp_path / "run_b", plays, cfg=ALL_OFF, round_cap=4,
        call_cache=cache, seq_provider=itertools.count(1).__next__)
    assert second.rounds_used == 4
    assert second.cache_hits == 1
    assert (cache.dir / f"{keep}.json").is_file()

    model_rows = _model_rows(_rows(second.trace_path))
    assert len(model_rows) == 4
    seq_rows = [r for r in model_rows if isinstance(r.get("seq"), int)]
    hit_rows = [r for r in model_rows if r.get("seq") is None]
    assert len(seq_rows) == 3, [r.get("seq") for r in model_rows]
    assert len(hit_rows) == 1
    assert [r["seq"] for r in seq_rows] == [1, 2, 3]
    # 命中轮在第 4 轮（前三轮是 miss）。
    assert hit_rows[0]["round"] == 4
    assert hit_rows[0]["args"]["cache_hit"] is True
    assert all(r["args"]["cache_hit"] is False for r in seq_rows)

    _write_evidence("round_accounting.json", json.dumps({
        "rounds_used": second.rounds_used,
        "cache_hits": second.cache_hits,
        "model_rows": [{"round": r["round"], "seq": r.get("seq"),
                        "cache_hit": r["args"].get("cache_hit")}
                       for r in model_rows],
        "kept_cache_key": keep,
        "cache_dir": str(cache.dir),
    }, ensure_ascii=False, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# AC-T3-6：缓存纪元
# ---------------------------------------------------------------------------

def test_ac_t3_6_cache_epoch_v4(tmp_path):
    """新开关 ⇒ agentic-v4；旧开关组合仍 v3；全关仍 v2；未知纪元 fail-closed。"""
    matrix = {}

    def _record(label, cfg):
        version, logical, disk = cache_epoch_for(cfg)
        matrix[label] = {"version": version, "logical_prefix": logical,
                         "disk_prefix": disk}
        return version

    assert _record("all_off", ALL_OFF) == CACHE_VERSION_LEGACY
    assert _record("old_four", AgenticConfig(verify_repair_tail=True)) \
        == CACHE_VERSION
    assert _record("env_spec", AgenticConfig(env_spec=True)) == CACHE_VERSION_V4
    assert _record("render_preview", AgenticConfig(render_preview=True)) \
        == CACHE_VERSION_V4
    assert _record("both_v4_and_v3", AgenticConfig(env_spec=True,
                                                   run_expr=True)) == CACHE_VERSION_V4
    assert _record("v4_with_N", AgenticConfig(
        env_spec=True, render_preview=True, privileged_state_inject=True,
        run_mode="N")) == CACHE_VERSION_V4

    assert CACHE_VERSION_V4 == "agentic-v4"
    assert cache_prefixes(CACHE_VERSION_V4) == ("agentic-v4:", "agentic-v4-")
    assert cache_prefixes(CACHE_VERSION) == ("agentic-v3:", "agentic-v3-")
    assert cache_prefixes(CACHE_VERSION_LEGACY) == ("agentic-v2:", "agentic-v2-")
    with pytest.raises(ValueError):
        cache_prefixes("agentic-v9")

    # 纪元真的换了：v4 跑一次的磁盘键前缀必须是 agentic-v4-。
    cache = _RecordingCache(tmp_path / "cache_v4")
    _run_loop(tmp_path / "run", [_play('read_file(path="a.txt")')],
              cfg=AgenticConfig(env_spec=True), round_cap=1, call_cache=cache,
              seq_provider=itertools.count(1).__next__)
    assert cache.put_keys and all(k.startswith("agentic-v4-")
                                  for k in cache.put_keys)

    _write_evidence("cache_epoch_matrix.json", json.dumps(
        matrix, ensure_ascii=False, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# AC-T3-7：image block 仍被拒；预览观察不带图
# ---------------------------------------------------------------------------

def test_ac_t3_7_no_image_to_coding(tmp_path):
    """带 image 的消息经 model.py 抛硬规则 1 ValueError；回执无 base64/魔数。"""
    image_msg = [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": "aGVsbG8="}},
        {"type": "text", "text": "看图"},
    ]}]
    with pytest.raises(ValueError) as ei:
        flatten_text_messages(image_msg)
    assert "硬规则 1" in str(ei.value)

    # 纯文本仍可过（正对照：拒绝的是 image，不是全部消息）。
    flat = flatten_text_messages([{"role": "user", "content": "hi"}])
    assert isinstance(flat, list) and flat

    workdir = tmp_path / "wd"
    workdir.mkdir()
    _scene_spec_file(workdir)
    ctx = _preview_ctx(workdir, preview=PreviewPermit(
        privileged_relation=True, visual_caller=lambda p, b: "看起来没倒进去"))
    result = render_quick_preview(ctx, scene_spec_path="scene_spec.json")
    assert not result.denied, result.output
    for marker in _B64_MARKERS + ("iVBORw0KGgo",):
        assert marker not in result.output
        assert marker not in result.full
    assert _PNG_MAGIC.decode("latin-1") not in result.output
    # 图确实落盘了（拒绝的是「进观察」，不是「不看图」）。
    assert (workdir / ".preview" / "preview-ortho_top.png").read_bytes() \
        .startswith(_PNG_MAGIC)


# ---------------------------------------------------------------------------
# AC-T3-8：安全边界未松
# ---------------------------------------------------------------------------

def test_ac_t3_8_security_boundary_intact():
    """执行器零 import 授权；模型 env 白名单不含 PYTHONPATH（逐字断言）。"""
    executor = HardenedLocalPythonExecutor()
    assert executor.additional_authorized_imports == []
    assert "PYTHONPATH" not in _MODEL_ENV_ALLOWLIST
    assert "PYTEST_ADDOPTS" not in _MODEL_ENV_ALLOWLIST
    rejected = _validate_env({"PYTHONPATH": "/tmp"})
    assert isinstance(rejected, str) and "PYTHONPATH" in rejected
    assert _validate_env({"LANG": "C"}) == {"LANG": "C"}

    # 未装配后端时 fail-closed（不是默默跑本地）。
    bare = ToolContext(workdir=Path("."), grants=GrantTable(), thread_id="t")
    assert isinstance(check_backend(bare), object)
    from orchestrator.agentic.tools import ToolResult

    assert isinstance(check_backend(bare), ToolResult)


# ---------------------------------------------------------------------------
# AC-T3-9：真值段与门控（含容器内实测）
# ---------------------------------------------------------------------------

def _load_probe_module():
    spec = importlib.util.spec_from_file_location("probe_env_scan_t3",
                                                  ENV_SCAN_PROBE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ac_t3_9_privileged_state_gate(tmp_path, monkeypatch):
    """探针如实登记真值可及性；EnvSpec 默认不注入；仅 inject+N 才渲染。"""
    probe = _load_probe_module()
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "a.txt").write_text("alpha\n", encoding="utf-8")
    out = tmp_path / "env_scan.json"

    proc = subprocess.run(
        [sys.executable, str(ENV_SCAN_PROBE), "--out", str(out),
         "--workdir", str(workdir)],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, lines
    receipt = json.loads(lines[0])
    assert receipt["schema"] == probe.RECEIPT_SCHEMA
    assert receipt["ok"] is True
    # 未 --allow-reset ⇒ 真值段如实「未探测」，**不得**兜底填 True。
    assert receipt["privileged_state"] == {
        "available": False, "accessor": "unavailable", "returns": [],
        "reason": "未探测"}

    # --allow-reset 缺 --bddl ⇒ 拒绝 reset 并说明，真值段保持未探测。
    proc2 = subprocess.run(
        [sys.executable, str(ENV_SCAN_PROBE), "--out", str(out),
         "--workdir", str(workdir), "--allow-reset"],
        capture_output=True, text=True, timeout=120)
    assert proc2.returncode == 0
    receipt2 = json.loads([ln for ln in proc2.stdout.splitlines()
                           if ln.strip()][0])
    assert receipt2["privileged_state"]["available"] is False
    assert any("--allow-reset 需要 --bddl" in e for e in receipt2["errors"]), \
        receipt2["errors"]

    # _privileged_probe 三分支：sim.data / robots / 都没有。
    fake_sim = types.SimpleNamespace(
        sim=types.SimpleNamespace(data=types.SimpleNamespace(
            body_xpos=[[0.0, 0.0, 0.0]] * 3)))
    got = probe._privileged_probe(fake_sim)
    assert got["available"] is True
    assert got["accessor"] == "env.sim.data.body_xpos"
    assert "body_xpos" in got["returns"]

    fake_robots = types.SimpleNamespace(
        robots=[types.SimpleNamespace(eef_pos=[0.1, 0.2, 0.3])])
    got2 = probe._privileged_probe(fake_robots)
    assert got2["available"] is True
    assert got2["accessor"] == "env.robots[0].eef_pos"

    got3 = probe._privileged_probe(types.SimpleNamespace())
    assert got3["available"] is False and got3["reason"]

    # EnvSpec 门控：inject 默认 False；只有 inject=True 且 mode=="N" 才渲染。
    assert PrivilegedState(available=False,
                           accessor="unavailable").inject is False
    with pytest.raises(EnvSpecError):
        PrivilegedState(available=False, accessor="unavailable", inject=True)

    ps = PrivilegedState(available=True, accessor="env.sim.data.body_xpos",
                         returns=("body_xpos",), inject=True)
    spec = EnvSpec(python="/usr/bin/python", privileged_state=ps)
    rendered_r = spec.render_block(mode="R")
    rendered_n = spec.render_block(mode="N")
    rendered_none = spec.render_block()
    assert "PRIVILEGED_STATE" not in rendered_r
    assert "PRIVILEGED_STATE" not in rendered_none
    assert "[PRIVILEGED_STATE]" in rendered_n
    assert "env.sim.data.body_xpos" in rendered_n
    for text in (rendered_r, rendered_none):
        assert not re.search(r"body_xpos|body_xquat", text)
    # 默认（关）的配置渲染出的 prompt 里没有真值位姿段。
    prompt = build_system_prompt(cfg=AgenticConfig(env_spec=True),
                                 env_spec_block=spec.render_block(mode="R"))
    assert not re.search(r"\[PRIVILEGED_STATE\]", prompt)

    # 落盘的环境卡可往返（load_env_spec 逐字段校验）。
    card = static_env_spec(skills=("mujoco.contact_push",))
    card_path = tmp_path / ENV_SPEC_NAME
    card_path.write_text(json.dumps(card.to_json(), ensure_ascii=False),
                         encoding="utf-8")
    assert load_env_spec(card_path).to_json() == card.to_json()
    assert card.to_json()["schema"] == ENV_SPEC_SCHEMA

    _write_evidence("privileged_state_probe.json", json.dumps({
        "structured_probe_default": receipt["privileged_state"],
        "structured_probe_allow_reset_no_bddl": receipt2["privileged_state"],
        "allow_reset_errors": receipt2["errors"],
        "fake_sim": got, "fake_robots": got2, "fake_empty": got3,
        "render_gate": {"mode_R_has_privileged": "[PRIVILEGED_STATE]" in rendered_r,
                        "mode_N_has_privileged": "[PRIVILEGED_STATE]" in rendered_n,
                        "no_mode_has_privileged": "[PRIVILEGED_STATE]" in rendered_none},
    }, ensure_ascii=False, indent=2, sort_keys=True))


@pytest.mark.container
def test_ac_t3_9_container_scan_evidence(tmp_path):
    """真容器内跑 ENV_SPEC 扫描：回执落证据；取不到真值就如实写 False。"""
    from orchestrator.agentic.tools.run import DockerSiblingBackend

    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "task.py").write_text("print('hi')\n", encoding="utf-8")
    backend = DockerSiblingBackend(image=AGENTIC_IMAGE,
                                   probes_path=str(PROBES_DIR))
    result = backend.run(
        ["/usr/bin/python3", "/probes/probe_env_scan.py",
         "--out", "/work/env_scan.json", "--workdir", "/work"],
        cwd=workdir, timeout_s=600)
    assert result.rc == 0, result.stderr[-2000:]
    lines = [ln for ln in result.stdout.decode("utf-8", "replace").splitlines()
             if ln.strip()]
    assert len(lines) == 1, lines[:3]
    receipt = json.loads(lines[0])
    assert receipt["schema"] == "simpraxis.env_scan.v1", receipt
    assert receipt["ok"] is True, receipt
    ps = receipt["privileged_state"]
    assert set(ps) >= {"available", "accessor", "returns"}, ps
    if ps["available"]:
        assert ps["accessor"] != "unavailable" and ps["returns"], ps
    else:
        # 如实：取不到就 False + 理由（本镜像无 LIBERO，预期即此支）。
        assert ps["accessor"] == "unavailable" and ps["reason"], ps
    assert receipt["python"]["executable"], receipt["python"]

    _write_evidence("env_spec_probe.json", json.dumps(
        receipt, ensure_ascii=False, indent=2, sort_keys=True))

    card = EnvSpec(
        python=receipt["python"]["executable"],
        host_python="/mnt/venvs/simpraxis/bin/python",
        packages={k: v for k, v in (receipt["packages"] or {}).items()},
        workdir_tree=tuple(receipt["workdir_tree"] or ()),
        obs_keys=tuple(receipt["obs_keys"] or ()),
        action_dim=receipt["action_dim"],
        demo_entry=receipt["demo_entry"],
        privileged_state=PrivilegedState(
            available=bool(ps["available"]), accessor=str(ps["accessor"]),
            returns=tuple(ps.get("returns") or ())),
    )
    (EVIDENCE / ENV_SPEC_NAME).write_text(
        json.dumps(card.to_json(), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8")
    os.chmod(EVIDENCE / ENV_SPEC_NAME, 0o600)


# ---------------------------------------------------------------------------
# AC-T3-10：视觉诊断走 call_visual 且不留图（真网关 + 假传输 + 真账本）
# ---------------------------------------------------------------------------

class _FakeHttpResponse:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body


def _visual_transport(text: str):
    calls: list[dict] = []

    def post(url, *, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": dict(headers or {}),
                      "body": json})
        return _FakeHttpResponse({
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 5, "output_tokens": 7},
        })

    post.calls = calls
    return post


@pytest.fixture()
def p27_env(tmp_path, monkeypatch):
    """phase27 独立账命名空间 + 三阶段 stage_models（全假 ID，0 触网）。"""
    for key, name in (("SIMPRAXIS_PHASE1_ROOT", "phase1"),
                      ("SIMPRAXIS_PHASE2_ROOT", "phase2"),
                      ("SIMPRAXIS_PHASE25_ROOT", "phase25"),
                      ("SIMPRAXIS_PHASE26_ROOT", "phase26"),
                      ("SIMPRAXIS_PHASE27_ROOT", "phase27")):
        monkeypatch.setenv(key, str(tmp_path / name))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    monkeypatch.setenv("SIMPRAXIS_LLM_TOKEN", "t3-fixture-key-redacted-0001")
    monkeypatch.setenv("SIMPRAXIS_LLM_BASE_URL",
                       "https://ark.cn-beijing.volces.com/api/coding")
    oq = tmp_path / "open-questions.md"
    oq.write_text("# Open Questions\n\n- fixture anchor\n", encoding="utf-8")
    monkeypatch.setenv("SIMPRAXIS_OPEN_QUESTIONS", str(oq))
    map_path = tmp_path / "stage_models.yaml"
    stage_models = {}
    for stage in R.STAGES:
        stage_models[stage] = {
            "model_id": f"unit-fake-{stage}",
            "capabilities": (["text", "vision"] if stage == R.VISION_STAGE
                             else ["text"]),
            "purpose": "fixture",
        }
    map_path.write_text(yaml.safe_dump({
        "meta": {"schema_version": "1.0", "endpoint": "anthropic-messages",
                 "base_url_env": "ANTHROPIC_BASE_URL"},
        "stage_models": stage_models,
    }, allow_unicode=True, width=4096), encoding="utf-8")
    return {"tmp": tmp_path, "map": map_path}


def _real_gateway(env, transport):
    return R.ModelGateway(stage_models_path=env["map"], sleep=lambda s: None,
                         raw_transport=transport,
                         ledger=QuotaLedger.phase27())


def _phase27_rows(tmp: Path) -> list[dict]:
    path = tmp / "phase27" / "quota" / "ledger.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8")
            .splitlines() if line.strip()]


def test_ac_t3_10_visual_critic_accounting(p27_env, tmp_path):
    """visual_critic=True ⇒ 一次 call_visual（stage=visual_rollout）+ 一行账。

    回执/trace 均无 base64；visual_critic=False（默认）⇒ 零 call_visual。
    """
    tmp = p27_env["tmp"]
    batch = "P27-T3-FIXTURE-VISUAL"
    ql.write_approval(batch_id=batch, task=4, stage=R.VISION_STAGE, approved=2,
                      beyond_normal=False, note="T3 AC-T3-10 零付费夹具",
                      namespace=ql.PHASE27_NAMESPACE)

    visual_text = "壶口略高于杯沿，倾斜约 20 度即可倒出。" * 10  # 380 字符上下
    visual_text = visual_text[:380]
    transport = _visual_transport(visual_text)
    real = _real_gateway(p27_env, transport)

    workdir = tmp_path / "wd"
    workdir.mkdir()
    _scene_spec_file(workdir)
    gateway = HybridGateway(
        [_play('render_quick_preview(scene_spec_path="scene_spec.json")')]
        + [_play('read_file(path="a.txt")') for _ in range(2)],
        visual=real)
    cfg = AgenticConfig(render_preview=True, visual_critic=True)
    with billing_context(task=4, batch_id=batch, thread_id="thr-t3-visual"):
        wd2, _b, _gw, outcome = _run_loop(
            tmp_path / "run", gateway.plays, cfg=cfg, round_cap=3,
            gateway=gateway,
            grants=GrantTable(tools=frozenset({"render_quick_preview",
                                               "read_file"})),
            backend=PreviewStubBackend(PROBES_DIR),
            files={"scene_spec.json": _scene_spec_json()})
    assert outcome.rounds_used == 3
    assert len(gateway.visual_calls) == 1

    rows = _rows(outcome.trace_path)
    preview_rows = [r for r in rows if r["tool"] == "render_quick_preview"]
    assert len(preview_rows) == 1
    row = preview_rows[0]
    assert row["policy_decision"]["decision"] == "allow"
    assert "visual_critic:" in row["obs_head"]
    for marker in _B64_MARKERS:
        assert marker not in row["obs_head"]
        assert marker not in json.dumps(row, ensure_ascii=False)
    assert "\\x89PNG" not in json.dumps(row, ensure_ascii=False)

    critic_line = [ln for ln in row["obs_head"].splitlines()
                   if ln.startswith("visual_critic: ")][0]
    critic_text = critic_line[len("visual_critic: "):]
    assert 0 < len(critic_text) <= VISUAL_TEXT_MAX == 400

    # 账本：一行、stage=visual_rollout、by_stage +1、tokens 回填。
    ledger_rows = _phase27_rows(tmp)
    assert len(ledger_rows) == 1, ledger_rows
    entry = ledger_rows[0]
    assert entry["stage"] == R.VISION_STAGE == "visual_rollout"
    assert entry["ok"] is True and isinstance(entry["seq"], int)
    assert entry["tokens_in"] == 5 and entry["tokens_out"] == 7
    summary = ledger_summary(ledger_rows)
    assert summary["call_rows"] == 1
    assert summary["calls_by_stage"] == {"visual_rollout": 1}

    # 视觉请求体：1 image + 1 text（图走 call_visual，不经 coding 通道）。
    assert len(transport.calls) == 1
    blocks = transport.calls[0]["body"]["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["image", "text"]
    assert transport.calls[0]["body"]["model"] == f"unit-fake-{R.VISION_STAGE}"

    # visual_critic=False（默认）⇒ 零 call_visual、账本不增行。
    off = AgenticConfig(render_preview=True)
    permit = _preview_permit(off, real)
    assert permit is not None and permit.visual_caller is None
    ctx = _preview_ctx(workdir, preview=permit,
                       backend=PreviewStubBackend(PROBES_DIR))
    plain = render_quick_preview(ctx, scene_spec_path="scene_spec.json")
    assert not plain.denied
    assert "visual_critic:" not in plain.output
    assert len(transport.calls) == 1
    assert len(_phase27_rows(tmp)) == 1

    _write_evidence("visual_critic_accounting.json", json.dumps({
        "ledger_rows": len(ledger_rows),
        "ledger_entry": {k: entry.get(k) for k in
                         ("seq", "stage", "model", "ok", "tokens_in",
                          "tokens_out", "task", "batch_id")},
        "calls_by_stage": summary["calls_by_stage"],
        "visual_text_chars": len(critic_text),
        "visual_text_max": VISUAL_TEXT_MAX,
        "request_blocks": [b["type"] for b in blocks],
        "visual_critic_off_calls": len(transport.calls) - 1,
    }, ensure_ascii=False, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# AC-T3-11：spatial_relation 门控与分支
# ---------------------------------------------------------------------------

def _quat_rotate(quat, vec):
    """w,x,y,z → 旋转（测试侧独立实现，不复用被测代码的数学）。"""
    w, x, y, z = (float(v) for v in quat)
    n = (w * w + x * x + y * y + z * z) ** 0.5
    w, x, y, z = w / n, x / n, y / n, z / n
    rot = (
        (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
        (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
        (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)),
    )
    return tuple(sum(rot[i][j] * float(vec[j]) for j in range(3))
                 for i in range(3))


def _world(local, pos, quat):
    rotated = _quat_rotate(quat, local)
    return tuple(rotated[i] + float(pos[i]) for i in range(3))


def test_ac_t3_11_spatial_relation_gate(tmp_path):
    """默认不给真值几何；inject+N 才给；有嘴/无嘴两条支路由 manifest 决定。"""
    fixture = json.loads(SPOUT_FIXTURE.read_text(encoding="utf-8"))
    expected = fixture["expected_spatial_relation"]

    # ⑤ 分支 (a) 由合成夹具驱动（四件真实资产全 spout_defined=False）。
    scene_doc = fixture["scenes"]["spout_true"]
    spec_a = SceneSpec.from_json(json.dumps(scene_doc))
    relation_a = compute_spatial_relation(
        spec_a.bodies, resolve=resolve_from_manifest(fixture))
    assert relation_a["is_spout_pointing_towards_cup"] is True
    assert relation_a["pitcher_pitch_angle_deg"] == expected["pitcher_pitch_angle_deg"]
    assert relation_a["pitcher_spout_to_cup_rim_offset"] == \
        expected["pitcher_spout_to_cup_rim_offset"]

    # ② 独立复算：壶 pour_edge(世界) − 杯 rim_center(世界)，容差 1e-6。
    keypoints = {a["name"]: a["keypoints"] for a in fixture["assets"]}
    bodies_doc = scene_doc["bodies"]
    pitcher_body = [b for b in bodies_doc
                    if keypoints[b["asset"]]["spout_defined"] is True][0]
    cup_body = [b for b in bodies_doc if b["asset"] != pitcher_body["asset"]][0]
    spout_world = _world(keypoints[pitcher_body["asset"]]["pour_edge"],
                         pitcher_body["pos"], pitcher_body["quat"])
    cup_rim_world = _world(keypoints[cup_body["asset"]]["rim_center"],
                           cup_body["pos"], cup_body["quat"])
    recomputed = [spout_world[i] - cup_rim_world[i] for i in range(3)]
    got = relation_a["pitcher_spout_to_cup_rim_offset"]
    assert max(abs(got[i] - recomputed[i]) for i in range(3)) <= 1e-6, (
        got, recomputed)
    assert "pitcher_to_cup_distance" in relation_a

    # ②' 分支 (b)：真实轴对称件 ⇒ 不含两个壶嘴键，改含 tilt_axis_needed。
    real_spec = SceneSpec(engine="genesis", bodies=(
        Body(asset="vessel/pitcher_standard", pos=(0.20, 0.25, 1.00), fixed=True),
        Body(asset="vessel/cup_standard", pos=(0.32, 0.25, 1.00), fixed=True),
    ))
    assert mf.resolve_asset("vessel/pitcher_standard").keypoints.spout_defined is False
    relation_b = compute_spatial_relation(real_spec.bodies)
    assert "pitcher_spout_to_cup_rim_offset" not in relation_b
    assert "pitcher_pitch_angle_deg" not in relation_b
    assert "is_spout_pointing_towards_cup" not in relation_b
    tilt = relation_b["tilt_axis_needed"]
    assert set(tilt) == {"axis", "angle_deg"}
    assert abs(sum(v * v for v in tilt["axis"]) ** 0.5 - 1.0) <= 1e-6
    assert 0.0 <= tilt["angle_deg"] <= 180.0
    # ③ 两条支路都含距离。
    assert relation_b["pitcher_to_cup_distance"] > 0

    # ① 默认（privileged_state_inject=False）⇒ 回执不带 spatial_relation。
    workdir = tmp_path / "wd"
    workdir.mkdir()
    _scene_spec_file(workdir)
    default_ctx = _preview_ctx(workdir, preview=PreviewPermit())
    plain = render_quick_preview(default_ctx, scene_spec_path="scene_spec.json")
    assert not plain.denied
    assert "spatial_relation" not in plain.output
    assert "spatial_relation" not in plain.full

    # ② 开（inject=True + mode=N）⇒ 含 spatial_relation，走 (b) 支路。
    on_ctx = _preview_ctx(workdir, preview=PreviewPermit(privileged_relation=True))
    opened = render_quick_preview(on_ctx, scene_spec_path="scene_spec.json")
    assert not opened.denied
    payload = [ln for ln in opened.output.splitlines()
               if ln.startswith("spatial_relation: ")][0]
    relation_tool = json.loads(payload[len("spatial_relation: "):])
    assert set(relation_tool) == set(relation_b)
    assert relation_tool["tilt_axis_needed"] == relation_b["tilt_axis_needed"]

    # ④ 模式 R：prompt 与回执都不得出现关键点/空间关系词。
    cfg_r = AgenticConfig(env_spec=True, render_preview=True,
                          privileged_state_inject=True, run_mode="R")
    # 构造上 permit 必须为假（模式 R 一律不给真值几何）。
    permit_r = _preview_permit(cfg_r, None)
    assert permit_r is not None and permit_r.privileged_relation is False
    env_block = static_env_spec(skills=("mujoco.contact_push",)).render_block(
        mode=cfg_r.run_mode)
    prompt_r = build_system_prompt(cfg=cfg_r, env_spec_block=env_block)
    assert not _RELATION_WORDS.search(prompt_r), _RELATION_WORDS.findall(prompt_r)
    receipt_r = render_quick_preview(
        _preview_ctx(workdir, preview=permit_r),
        scene_spec_path="scene_spec.json")
    assert not receipt_r.denied
    assert not _RELATION_WORDS.search(receipt_r.output), \
        _RELATION_WORDS.findall(receipt_r.output)

    _write_evidence("spatial_relation_report.json", json.dumps({
        "fixture": str(SPOUT_FIXTURE),
        "branch_a_relation": relation_a,
        "branch_a_expected": expected,
        "branch_a_recomputed_offset": recomputed,
        "branch_a_max_abs_error": max(abs(got[i] - recomputed[i])
                                      for i in range(3)),
        "branch_b_relation": relation_b,
        "default_has_spatial_relation": "spatial_relation" in plain.output,
        "inject_has_spatial_relation": "spatial_relation" in opened.output,
        "mode_R_prompt_relation_words": _RELATION_WORDS.findall(prompt_r),
        "mode_R_receipt_relation_words": _RELATION_WORDS.findall(receipt_r.output),
    }, ensure_ascii=False, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# AC-T3-12：scratchpad 双保险
# ---------------------------------------------------------------------------

def test_ac_t3_12_scratchpad_safety_belt(tmp_path):
    """铁律行逐字在场；首压缩被动 seed；关则无文件；重放字节相同。"""
    # 字节口径绑「四个旧开关 + scratchpad」这一档（与 AC-T3-1 同一配置）；
    # soak 用的 cfg_on 只开 scratchpad（否则 budget 必须同时给追授轮数）。
    cfg_on = AgenticConfig(scratchpad=True)
    prompt = build_system_prompt(cfg=cfg_on)
    assert SCRATCHPAD_RULE in prompt
    assert "工作记忆铁律" in prompt
    assert hashlib.sha256(SCRATCHPAD_RULE.encode("utf-8")).hexdigest() == \
        SCRATCHPAD_RULE_SHA256
    assert prompt_bytes(AgenticConfig(
        verify_repair_tail=True, run_expr=True, tolerant_patch=True,
        scratchpad=True)) == PROMPT_BASELINE_BYTES + SCRATCHPAD_RULE_BYTES
    assert "工作记忆铁律" not in build_system_prompt(cfg=ALL_OFF)
    assert "read_file" in SEED_TOOLS and "render_quick_preview" in SEED_TOOLS

    plays = [_play('read_file(path="a.txt")') for _ in range(20)]

    def _once(root: Path):
        wd, _budget, _gw, outcome = _run_loop(
            root, plays, cfg=cfg_on, round_cap=20,
            grants=GrantTable(tools=frozenset({"read_file"}), write_roots=("",)))
        return wd, outcome

    wd_a, outcome_a = _once(tmp_path / "run_a")
    scratch = wd_a / SCRATCHPAD_REL
    assert scratch.is_file(), "首次压缩未做被动 seed"
    seeded = scratch.read_text(encoding="utf-8")
    first_line, body = seeded.split("\n", 1)

    rows = _rows(outcome_a.trace_path)
    condense_rows = [r for r in rows if r["tool"] == CONDENSE_TOOL]
    assert condense_rows, "20 轮 soak 未发生压缩"
    seeded_rows = [r for r in condense_rows
                   if r["args"].get("scratchpad_seeded") is True]
    assert len(seeded_rows) == 1
    seed_row = seeded_rows[0]
    seed_round = seed_row["args"]["seed_source_round"]
    assert isinstance(seed_round, int) and seed_round >= 1
    # 「指向真实轮次」：seed 轮是**压缩发生时**最近一条成功只读回执所在的轮，
    # 不早于被压缩的首轮（首轮压缩那条 `__condense__` 行的 round 指向旧轮，
    # 故这里用「不早于 + 该轮确实有成功只读回执 + 内容逐字相符」三连）。
    assert seed_round >= seed_row["round"]
    assert f"round={seed_round} tool=" in first_line
    assert first_line.startswith("<!-- auto-seeded at condense: round=")
    readonly = [r for r in rows if r["tool"] in SEED_TOOLS
                and r["round"] == seed_round
                and r["policy_decision"]["decision"] == "allow"]
    assert readonly, f"第 {seed_round} 轮没有成功的只读回执可 seed"
    assert f"tool={readonly[-1]['tool']} -->" in first_line
    # 内容逐字等于该回执的前 SEED_MAX_CHARS 字符。
    assert body.rstrip("\n") == readonly[-1]["obs_head"][:SEED_MAX_CHARS].rstrip("\n")
    # 后续压缩不再 seed（只做一次）。
    assert all(r["args"].get("scratchpad_seeded") is False
               for r in condense_rows if r is not seed_row)

    # ③ cfg.scratchpad=False ⇒ 文件不被创建，condense 行不带 seed 字段。
    wd_off, _b_off, _gw_off, outcome_off = _run_loop(
        tmp_path / "run_off", plays, cfg=ALL_OFF, round_cap=20,
        grants=GrantTable(tools=frozenset({"read_file"}), write_roots=("",)))
    assert not (wd_off / SCRATCHPAD_REL).exists()
    off_condense = [r for r in _rows(outcome_off.trace_path)
                    if r["tool"] == CONDENSE_TOOL]
    assert off_condense
    assert all("scratchpad_seeded" not in r["args"] for r in off_condense)

    # ④ 同轨迹重放两次 ⇒ seed 字节完全相同。
    wd_b, _outcome_b = _once(tmp_path / "run_b")
    assert (wd_b / SCRATCHPAD_REL).read_bytes() == seeded.encode("utf-8")

    _write_evidence("scratchpad_seed_report.json", json.dumps({
        "scratchpad_rule_sha256": SCRATCHPAD_RULE_SHA256,
        "scratchpad_rule_bytes": SCRATCHPAD_RULE_BYTES,
        "seed_max_chars": SEED_MAX_CHARS,
        "seed_header": first_line,
        "seed_body_chars": len(body.rstrip("\n")),
        "seed_bytes": len(seeded.encode("utf-8")),
        "seed_source_round": seed_row["args"]["seed_source_round"],
        "condense_rows": len(condense_rows),
        "seeded_rows": len(seeded_rows),
        "off_creates_file": (wd_off / SCRATCHPAD_REL).exists(),
        "replay_identical": (wd_b / SCRATCHPAD_REL).read_bytes() ==
                            seeded.encode("utf-8"),
    }, ensure_ascii=False, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# AC-T3-13：C1–C7 迁到评测期
# ---------------------------------------------------------------------------

def test_ac_t3_13_contracts_moved_to_gate2(tmp_path):
    """内环 prompt 不含归档合同块原文；Gate 2 侧 C1–C7 逐条可定位可执行。"""
    cfg = AgenticConfig(verify_repair_tail=True, run_expr=True,
                        tolerant_patch=True, scratchpad=True, env_spec=True)
    monkeypatch_path = tmp_path / ENV_SPEC_NAME
    monkeypatch_path.write_text(json.dumps(
        static_env_spec().to_json(), ensure_ascii=False), encoding="utf-8")
    env_block = static_env_spec().render_block(mode="R")
    prompt = build_system_prompt(cfg=cfg, env_spec_block=env_block)
    assert contracts.prompt_leaks(prompt) == []

    # 正对照：归档块原文确实能被同一判定器认出来（不是「恒空」的假绿）。
    archived = contracts.archived_contract_blocks()
    assert set(archived) == {"kinematic", "vessel_structure"}
    for name, block in archived.items():
        assert block, f"归档块 {name} 为空"
        assert name in contracts.prompt_leaks(prompt + block)

    assert contracts.EXPECTED_CONTRACTS == frozenset(
        {"C1", "C2", "C3", "C4", "C5", "C6", "C7"})
    assert contracts.registry_errors() == [], contracts.registry_errors()
    for cid in sorted(contracts.EXPECTED_CONTRACTS):
        fn = contracts.CONTRACT_CHECKS[cid]
        assert callable(fn), cid
        assert (fn.__doc__ or "").strip(), f"{cid} 无说明（不可定位）"
        assert cid in contracts.CONTRACT_TITLES and contracts.CONTRACT_TITLES[cid]
    # 报告 schema 与三态常量在场（Gate 2 / WJ 侧逐条消费）。
    assert contracts.CONTRACTS_SCHEMA == "simpraxis.contracts_report.v1"
    assert {contracts.PASS, contracts.VIOLATION,
            contracts.UNDECIDABLE} == {"pass", "violation", "undecidable"}

    _write_evidence("contracts_moved_to_gate2.json", json.dumps({
        "prompt_leaks": contracts.prompt_leaks(prompt),
        "prompt_bytes": len(prompt.encode("utf-8")),
        "archived_blocks": {k: len(v.encode("utf-8"))
                            for k, v in archived.items()},
        "expected_contracts": sorted(contracts.EXPECTED_CONTRACTS),
        "registry_errors": contracts.registry_errors(),
        "contracts_schema": contracts.CONTRACTS_SCHEMA,
    }, ensure_ascii=False, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# T3.7 证据树布局（九件 + 环境卡）
# ---------------------------------------------------------------------------

_T37_FILES = (
    "prompt_bytes_report.json",
    "env_spec_probe.json",
    "privileged_state_probe.json",
    "preview_probe.log",
    "spatial_relation_report.json",
    "scratchpad_seed_report.json",
    "soak_20_rounds.trace.jsonl",
    "round_accounting.json",
    "cache_epoch_matrix.json",
)


@pytest.mark.container
def test_t37_evidence_tree():
    """§T3.7 九件证据齐全、0700/0600、JSON 可解析（容器跑后判布局）。"""
    missing = [n for n in _T37_FILES + (ENV_SPEC_NAME,)
               if not (EVIDENCE / n).is_file()]
    assert not missing, f"缺证据件：{missing}（先跑 container 用例与全量机判）"
    assert stat.S_IMODE(EVIDENCE.stat().st_mode) == 0o700
    for name in _T37_FILES + (ENV_SPEC_NAME,):
        path = EVIDENCE / name
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, name
        if name.endswith(".json"):
            json.loads(path.read_text(encoding="utf-8"))
        elif name.endswith(".jsonl"):
            lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
                     if ln.strip()]
            assert lines and all(json.loads(ln) for ln in lines), name
