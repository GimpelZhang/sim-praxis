"""Phase 2.8.1 T4：单 Trial 流水线（计划 §8.5.4 布局 / §8.5.6 顺序）。

一个 Trial = 冻结任务 × 一条实验臂 × 一个重复轮次。本模块是**唯一**把
「环境身份 → 输入 → 出生 → 引擎 → 交付 → 终态」串起来的落点；七步顺序即纪律
（§8.5.6「内部顺序（**顺序即纪律，不得调整**）」）。

为什么不能调换：

1. **运行时证明必须早于一切候选行为**（AC-P281-2-13）。晚一步，候选就有机会在
   自己的 ``env_probe.json`` 里编一份环境身份；「实测」先于「自述」正是这层证明
   的全部意义。
2. **输入 staging 与 ``TASK.md`` 渲染必须在引擎之前**：否则候选看到的说明与它
   实际拿到的字节可能不是同一批。
3. **出生在引擎之前是闸、不是断言**：如果 Trial 以 ``pre_seed`` 起（拿一份已知
   过不了出生的候选），一个连出生都过不了的入口不该花掉任何一次实呼。但引擎
   自产的候选在引擎跑完之前**没有**入口文件——那时「未出生」不可判定，故本步
   只在入口已在场时生效（见 :func:`_pre_birth`）。引擎之后的出生判定才是权威。
4. **``delivered/`` 在引擎结束后冻结**（AC-12）：候选在 ``workdir/`` 里随时可写，
   评测时刻的目录不是运行结束时刻的目录。
5. **``claimed`` 与 ``observed`` 必须出自同一次重算**：``claimed`` 抄自运行器写下
   的总结行，而总结行本身是 :func:`telemetry.observed` 的输出——两侧任何一处
   手填数字，都会让 AC-09 变成「自己跟自己比对」。

**两臂唯一的分叉点是引擎**（:func:`arm_adapter.run_arm`）。环境证明、输入、说明、
交付、遥测、清单逐字节同源；任何按臂分叉的输入面都会把一个引擎对比实验偷偷变成
两个实验。

分臂运行时证明的**锁内适配**（计划 §8.5.3 要求「需求件给容器变体」）：
``runtime_requirement.yaml`` 是 P281-5 的派生件，且 AC-P281-5-07 要求它与
``task.yaml.runtime`` **逐键相等**——预注册里并不存在容器变体；同时评测器的
``_runtime_gate`` 是臂盲的，只拿 ``task.yaml.runtime`` 比对
``delivered/runtime_fingerprint.json``。故本模块对两臂一律按**任务声明的运行
形态**取证（L1 在任务镜像内取证，M1/R1 在宿主 venv 取证），并在
``trial.json.deployment`` 里如实记下**实际部署形态**。M1/R1 的 agentic 臂把同一份
已被取证的宿主 venv 只读挂进容器，故被取证的对象（解释器 + 包版本）就是候选真正
用到的那份；**未被证明**的是容器镜像自带的 OS 层——该限制由 P281-6 的报告逐字
承担，不许说成「两臂环境完全等价」。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from orchestrator.benchmark import reference as reference_mod
from orchestrator.common.callcache import CallReplay
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

from . import arm_adapter, candidate_birth, candidate_manifest, candidate_runtime
from . import constants, sandbox, task_prompt, telemetry

__all__ = (
    "DRY_MODEL",
    "DRYRUN_MARKER",
    "RUN_MANIFEST",
    "RUN_MANIFEST_SCHEMA",
    "STATUSES",
    "TRIAL_JSON",
    "TRIAL_SCHEMA",
    "BirthWatch",
    "TrialContext",
    "TrialOutcome",
    "TrialRefused",
    "pair_index",
    "pair_seed",
    "probe_run_form",
    "run_trial",
    "seed_int",
)

TRIAL_JSON = "trial.json"
TRIAL_SCHEMA = "simpraxis.phase281.trial.v1"
#: 干跑标记件：与真实 Trial 同目录同布局，但 ``trial.json.simulated=true``。
#: 真实运行**拒绝**带此标记的目录——干跑残留与真实 Trial 混在同一个 ``rep<k>``
#: 里，事后无法只凭目录内容分辨（计划 §8.6 步骤 3 要的就是这个目录，故不换路径）。
DRYRUN_MARKER = "SIMULATED.json"
RUN_MANIFEST = "run_manifest.json"
RUN_MANIFEST_SCHEMA = "simpraxis.phase281.candidate_run_manifest.v1"
#: 干跑网关自报的模型 id（``stage_models.yaml`` 里**没有**这一项，故不可能被误认
#: 成一次真实模型的回包）。
DRY_MODEL = "dry-run"
STATUSES = ("FINISHED", "CAP_HIT", "BLOCKED_INFRA", "CRASHED")

#: 缺指纹时的清单占位（64 个 ``0``）。只出现在 ``BLOCKED_INFRA`` 分支：评测器的
#: ``_runtime_gate`` 在 ``_self_report`` 之前就已返回，读不到这个值。
_NO_FINGERPRINT = "0" * 64


class TrialRefused(RuntimeError):
    """Trial 目录不可用（非空/干跑残留）——**拒绝**而不是就地覆盖。"""


# ---------------------------------------------------------------------------
# 种子（公开、可复算；隐藏变体种子不经本模块）
# ---------------------------------------------------------------------------

def pair_index(rep: int) -> int:
    """重复轮次 → 一基配对序号（``rep=0`` → 1）。"""
    if type(rep) is not int or rep < 0:
        raise ValueError(f"rep 必须是非负整数：{rep!r}")
    return int(rep) + 1


def pair_seed(task_id: str, rep: int) -> str:
    """``sha256("<experiment>|<task>|<pair_index>")`` 的十六进制串。

    为什么不用 ``rep`` 本身当种子：那会让三个重复轮次在任务之间重合（task A 的
    rep0 与 task B 的 rep0 同种子），任何与种子相关的偶然效应都会被读成任务效应。
    """
    raw = f"{constants.EXPERIMENT_ID}|{task_id}|{pair_index(rep)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def seed_int(hex_seed: str) -> int:
    """十六进制串 → 入口可用的整数种子（``< 2**31-1``，避开 C int 溢出）。"""
    if not isinstance(hex_seed, str) or len(hex_seed) < 16:
        raise ValueError(f"种子串太短：{hex_seed!r}")
    return int(hex_seed[:16], 16) % (2 ** 31 - 1)


# ---------------------------------------------------------------------------
# 结果与上下文
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TrialOutcome:
    """一次 Trial 的终态（计划 §8.5.6 的八字段，字段名逐字相同）。"""

    trial_id: str
    task_id: str
    arm_id: str
    rep: int
    status: str
    used_calls: int
    workdir: Path
    trial_dir: Path

    def to_json(self) -> dict:
        return {"trial_id": self.trial_id, "task_id": self.task_id,
                "arm_id": self.arm_id, "rep": self.rep,
                "status": self.status, "used_calls": self.used_calls,
                "workdir": str(self.workdir), "trial_dir": str(self.trial_dir)}


@dataclass
class TrialContext:
    """交给 :func:`arm_adapter.run_arm` 的上下文（该模块只按鸭子类型读它）。"""

    trial_id: str
    arm: arm_adapter.ArmSpec
    paths: Any
    runspec: Any
    gateway: Any
    call_cache: Any
    ledger: Any
    sandbox: Any
    evidence_dir: Path
    recall: Any
    input_dir: Path
    skills_dir: Path
    assets_dir: Path
    seed: int
    birth_watch: "BirthWatch"
    state_dir: Path


class BirthWatch:
    """在执行点之间做**廉价**的出生探测，记下「第一次可解析」的执行序号。

    正式出生判定（五层、起子进程/容器）只在引擎结束后跑一次；这里每次执行只看
    三件事——文件存在且不是符号链接、非空、``.py`` 能 ``ast.parse`` / ``.yaml`` 能
    ``yaml.safe_load``。这三件事足够回答 ``first_birth_round``，又不至于每轮都起
    一个子进程。
    """

    __slots__ = ("entry", "execs", "first_ok_exec")

    def __init__(self, entry: Path) -> None:
        self.entry = Path(entry)
        self.execs = 0
        self.first_ok_exec: "int | None" = None

    def observe(self) -> "int | None":
        self.execs += 1
        if self.first_ok_exec is None and _looks_born(self.entry):
            self.first_ok_exec = self.execs
        return self.first_ok_exec


def _looks_born(entry: Path) -> bool:
    try:
        if entry.is_symlink() or not entry.is_file():
            return False
        if entry.stat().st_size <= 0:
            return False
        if entry.suffix == ".py":
            import ast

            ast.parse(entry.read_text(encoding="utf-8"))
        elif entry.suffix in (".yaml", ".yml"):
            import yaml

            yaml.safe_load(entry.read_text(encoding="utf-8"))
    except (OSError, ValueError, SyntaxError, UnicodeDecodeError):
        return False
    return True


# ---------------------------------------------------------------------------
# 干跑网关（0 付费、0 触网）
# ---------------------------------------------------------------------------

class _DryRunGateway:
    """干跑网关：形状同 ``ModelGateway``，但一次网络都不发。

    返回一条**合法的空补丁**响应（``{"patches": []}``）。引擎会因此产不出入口
    （出生 = NOT_BORN），这是干跑的正常结局——干跑验证的是流水线骨架（目录、
    取证、说明、分派、交付、清单、终态），不是候选能力。**绝不**返回能骗过解析
    的假补丁：那会造出一个「看起来通过」的 Trial，后人拿它当证据。
    """

    guard_of = "phase281._DryRunGateway"

    def __init__(self) -> None:
        self.calls = 0

    def model_id(self, stage: str) -> str:
        return DRY_MODEL

    def call(self, stage: str, messages: Any = None, **kwargs: Any
             ) -> "tuple[str, tuple[int, int]]":
        self.calls += 1
        return json.dumps({"patches": []}, ensure_ascii=False), (0, 0)

    def call_visual(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(
            "干跑不提供视觉通道：本阶段三个任务都不发 image block")

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - 防御
        raise AttributeError(f"干跑网关没有 {name!r}")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, doc: Mapping) -> Path:
    ensure_private_dir(Path(path).parent)
    return atomic_write_text(
        path, json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        mode=0o600)


def _entry_rel(spec: Any) -> str:
    entrypoint = dict(getattr(spec, "entrypoint", {}) or {})
    rel = str(entrypoint.get("path") or "")
    if not rel:
        raise sandbox.SandboxError(f"{getattr(spec, 'task_id', '?')}: 入口为空")
    return rel


def _regular_files(base: Path) -> "list[Path]":
    """常规文件（**不跟符号链接**）：输入快照与交付采集共用同一口径。"""
    if not base.is_dir():
        return []
    return [p for p in sorted(base.rglob("*"))
            if not p.is_symlink() and p.is_file()]


def _tree_shas(base: Path) -> "dict[str, str]":
    return {p.relative_to(base).as_posix(): _sha256_file(p)
            for p in _regular_files(base)}


def _reset_own_dir(base: Path) -> None:
    """清空**本 Trial 自己的**目录树（只用于干跑重来）。

    守卫三重：名字必须是 ``rep<k>`` 形态、必须已存在、必须是目录；逐个删自己
    的直接子项，绝不 ``rmtree`` 上层（硬规则 6：只碰 simpraxis 自有字面路径）。
    """
    if not base.name.startswith("rep"):
        raise TrialRefused(f"拒绝清理非 rep 形态目录：{base}")
    if not base.is_dir():
        return
    for child in sorted(base.iterdir()):
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)


# ---------------------------------------------------------------------------
# ① 运行时证明
# ---------------------------------------------------------------------------

def _attest(spec: Any, base: Path, *, capture: "Mapping | None",
            image: "str | None") -> dict:
    """采集 + 比对（``gate`` **恒不抛**），把指纹件写进 ``delivered/``。

    指纹件只写一份、只写进 ``delivered/``：它是**交给评测器的那个对象**，不是
    第二份自述。``write_fingerprint`` 用 ``O_CREAT|O_EXCL`` 拒绝覆盖已冻结身份，
    故这里依赖「Trial 目录必然是空的」（干跑重来会整目录清空）。
    """
    verdict = candidate_runtime.RuntimeAttestation.gate(
        spec, base, image=image, attestation=capture)
    attestation = verdict.get("attestation") or {}
    fingerprint = dict(attestation.get("fingerprint") or {})
    state = "captured" if fingerprint else "absent"
    if fingerprint and capture is not None and not verdict.get("written_at"):
        state = "injected"
    if fingerprint:
        delivered = ensure_private_dir(Path(base) / "delivered")
        target = delivered / "runtime_fingerprint.json"
        try:
            candidate_runtime.rf.write_fingerprint(delivered, fingerprint)
        except candidate_runtime.rf.FingerprintError as exc:
            verdict["verdict"] = candidate_runtime.VERDICT_BLOCKED
            verdict["problems"] = list(verdict.get("problems") or []) + [
                {"code": "fingerprint_write", "detail": str(exc)}]
            state = "unwritable"
        if target.is_file():
            return {"verdict": verdict, "fingerprint_state": state,
                    "fingerprint_sha256": _sha256_file(target)}
    return {"verdict": verdict, "fingerprint_state": state,
            "fingerprint_sha256": None}


# ---------------------------------------------------------------------------
# ② 输入 staging + TASK.md
# ---------------------------------------------------------------------------

def _skills_rows(*, root: Path, manifest_sha256: str) -> dict:
    """把 KB 技能包 stage 到**本 Trial 私有根**，并与任务声明的清单 sha256 对账。

    为什么不落 ``benchmark/private``（``materialize_skills`` 的默认位置）：那是
    Phase 2.8.0 的私有根，本阶段无权往里写。这里按同一实现（``materialize_skills``）
    落到 ``<root>/private/skills_stage/<sha12>``，逐件 sha256 仍由它复核。
    """
    from orchestrator.skills.registry import manifest_sha12, skills_root

    sha12 = manifest_sha12(skills_root())
    rows = reference_mod.materialize_skills(
        stage_root=Path(root) / "private" / "skills_stage" / sha12)
    if manifest_sha256 and str(rows.get("manifest_sha256") or "") != manifest_sha256:
        raise sandbox.SandboxError(
            "技能清单 sha256 与任务声明不符："
            f"{rows.get('manifest_sha256')} != {manifest_sha256}")
    return rows


def _stage_inputs(spec: Any, base: Path, *, rows: Mapping) -> dict:
    """``input/`` 落地（0700/0600）并逐件算 sha256。

    技能包**同时**进 ``input/skills/``（只读面里的展示位）与工作根
    ``simpraxis_skills/``（候选 ``_HERE`` 相对导入的真实落点，与
    ``reference.stage_skills_into`` 同口径）。
    """
    input_dir = ensure_private_dir(Path(base) / "input")
    input_skills = ensure_private_dir(input_dir / "skills")
    reference_mod.stage_skills_into(rows, input_skills)

    spec_inputs = dict(getattr(spec, "inputs", {}) or {})
    snapshot = {
        "schema": "simpraxis.phase281.task_snapshot.v1",
        "task_id": str(getattr(spec, "task_id", "")),
        "objective": " ".join(str(getattr(spec, "objective", "") or "").split()),
        "entrypoint": dict(getattr(spec, "entrypoint", {}) or {}),
        "outputs": list(getattr(spec, "outputs", ()) or ()),
        "runtime_id": dict(getattr(spec, "runtime", {}) or {}).get("runtime_id"),
        "budget": dict(getattr(spec, "budget", {}) or {}),
        "constraints": dict(getattr(spec, "constraints", {}) or {}),
        # 只带清单 sha256，不带判据体（判据在 private/criteria/，不进候选面）
        "inputs": {str(k): (None if v is None else str(v))
                   for k, v in spec_inputs.items()},
        "skills_manifest_sha256": rows.get("manifest_sha256"),
        "created_at": _now(),
    }
    _write_json(input_dir / "task_snapshot.json", snapshot)
    return {
        "dir": input_dir,
        "relpaths": [p.relative_to(base).as_posix()
                     for p in _regular_files(input_dir)],
        "skills_dir": input_skills,
        "prompt_inputs": (["task_snapshot.json"]
                          + [f"skills/{row['staged']}"
                             for row in rows.get("files") or ()]),
    }


# ---------------------------------------------------------------------------
# ④ 出生
# ---------------------------------------------------------------------------

class _TaskRef:
    """``reference.birth_container`` 只读 ``ref.spec.task_id``（鸭子替身）。"""

    class _Spec:
        def __init__(self, task_id: str) -> None:
            self.task_id = task_id

    def __init__(self, task_id: str) -> None:
        self.spec = _TaskRef._Spec(task_id)


def _birth(spec: Any, *, base: Path, trial_id: str, workdir: Path,
           skills_dir: Path, assets_dir: Path, image_ref: "str | None",
           python: str) -> dict:
    """正式出生判定（五层；宿主任务在本机，L1 在任务镜像内）。

    为什么 L1 必须进容器：``task.yaml.runtime`` 把 LIBERO 钉在 robosuite 1.4.0 /
    mujoco 2.3.7 上，宿主 venv 是 3.15.0——在宿主上判出生会把每个 L1 Trial 都判成
    NOT_BORN，而那正是「环境不对被读成能力不足」的教科书式假阴性。

    v1/v2 双形态（锁内适配）：宿主任务走 P281-2 的 v2 验证器（父进程 spawn 子进程）；
    L1 走 ``reference.birth_container``（父进程起容器），后者产出 v1 件（键
    ``birth_status``）。评测器 ``_birth`` 两个键都认，故 L1 的件原样复制成
    ``birth.json``，不伪装成 v2。
    """
    task_id = str(getattr(spec, "task_id", ""))
    if constants.RUNTIME_BY_ID.get(task_id) == "docker":
        container_out = ensure_private_dir(Path(base) / "birth_container")
        result = reference_mod.birth_container(
            _TaskRef(task_id), workdir, container_out,
            image_ref=str(image_ref), skills_dir=skills_dir,
            assets_root=assets_dir, timeout=900.0)
        written = dict(result.get("written") or {})
        src = Path(str(written.get("birth_path") or ""))
        if not src.is_file():
            raise sandbox.SandboxError(f"容器出生验证未产出出生件：{src}")
        target = Path(base) / "birth.json"
        if target.exists():
            target.unlink()
        shutil.copyfile(src, target)
        os.chmod(target, 0o600)
        return {"status": str(result.get("status")), "path": str(target),
                "sha256": _sha256_file(target), "schema": "v1",
                "reason": None, "levels": dict(result.get("levels") or {})}

    bspec = candidate_birth.spec_from_task(task_id, workdir=workdir)
    validator = candidate_birth.CandidateBirthValidator(
        trial_dir=base, trial_id=trial_id, python=python)
    written = validator.write(validator.run(bspec))
    doc = candidate_birth.load_birth(written["birth_path"])
    return {"status": str(written["status"]), "path": str(written["birth_path"]),
            "sha256": str(written["birth_sha256"]), "schema": "v2",
            "reason": written.get("reason"),
            "levels": dict(doc.get("levels") or {})}


def _pre_birth(spec: Any, *, base: Path, trial_id: str, workdir: Path,
               skills_dir: Path, assets_dir: Path, image_ref: "str | None",
               python: str, pre_seed: "Mapping[str, str] | None",
               birth_runner: Callable[..., dict]) -> "dict | None":
    """第 4 步的**闸**：只在「入口已在场」时判定。

    引擎自产的候选在引擎跑完之前没有入口文件——那时「未出生」不可判定，把
    「文件不在」当成 NOT_BORN 会让每个 Trial 都以 0 呼结案。故本步只在
    ``pre_seed`` 播种过（或工作根里本来就有入口）时生效；这也是 AC-11 的真实
    场景：拿一份过不了出生的候选起 Trial，**一次调用都不该花**。
    """
    for rel, text in dict(pre_seed or {}).items():
        target = Path(workdir) / rel
        ensure_private_dir(target.parent)
        atomic_write_text(target, text, mode=0o600)
    entry = Path(workdir) / _entry_rel(spec)
    if not (pre_seed or entry.is_file()):
        return None
    return birth_runner(spec, base=base, trial_id=trial_id, workdir=workdir,
                        skills_dir=skills_dir, assets_dir=assets_dir,
                        image_ref=image_ref, python=python)


# ---------------------------------------------------------------------------
# ⑤ 引擎
# ---------------------------------------------------------------------------

def _build_runspec(spec: Any, arm: arm_adapter.ArmSpec, *, trial_id: str,
                   workdir: Path, cmd: "Sequence[str]",
                   telemetry_relpath: str, task_title: str,
                   env_extra: "Mapping[str, str]") -> Any:
    """两臂共享的同一份 ``RunSpec`` 形态（差异只由臂参数引入）。"""
    from orchestrator.agent_runner.runner import RunSpec

    return RunSpec(
        thread_id=trial_id,
        workdir=Path(workdir),
        cmd=[str(item) for item in cmd],
        telemetry_relpath=str(telemetry_relpath),
        batch_id=trial_id,
        run_idx=0,
        billing_task=constants.TASK_NO_BY_ID[str(spec.task_id)],
        env_extra=dict(env_extra),
        run_timeout_s=constants.WALLCLOCK_CAP_S,
        task_title=task_title,
        max_coding_rounds=arm.max_coding_rounds,
        extra_prompt_sections=(("作业说明（TASK.md）", task_title),),
        agentic=arm.runspec_agentic,
        tool_grants=tuple(arm.tool_grants),
        step_cap=arm.step_cap,
        token_cap=arm.token_cap,
        agentic_trace_relpath=(".agentic_trace" if arm.runspec_agentic else None),
        agentic_config=arm.agentic_config,
    )


def _deployment(arm: arm_adapter.ArmSpec, spec: Any, sandbox_obj: Any,
                image_ref: "str | None") -> dict:
    """实际部署形态（**与取证形态分开记**，见模块 docstring 的锁内适配）。"""
    form = str(getattr(sandbox_obj, "form", "unknown"))
    out = {"form": form, "engine": arm.engine,
           "engine_label": ("agent_runner" if arm.engine == "agent_runner"
                            else "agentic_loop")}
    if form == "host_process":
        out["deployment"] = "host_process"
        out["python"] = str((dict(getattr(spec, "runtime", {}) or {}))
                            .get("python"))
    else:
        out["deployment"] = f"sibling_container:{image_ref}"
        backend = getattr(sandbox_obj, "backend", None)
        out["backend"] = type(backend).__name__
        out["image"] = getattr(backend, "image", image_ref)
    return out


def _map_engine_error(exc: BaseException) -> str:
    if isinstance(exc, telemetry.TrialCapReached):
        return "CAP_HIT"
    if isinstance(exc, (sandbox.SandboxError, OSError,
                        candidate_runtime.InfraBlocked,
                        candidate_runtime.rf.FingerprintError)):
        return "BLOCKED_INFRA"
    return "CRASHED"


# ---------------------------------------------------------------------------
# ⑥ 交付
# ---------------------------------------------------------------------------

def _collect_delivered(spec: Any, *, workdir: Path, delivered: Path,
                       extra_rels: Iterable[str] = ()) -> "list[str]":
    """把任务声明过的产物从 ``workdir`` 冻进 ``delivered/``（AC-12）。

    包含式采集（不是排除式克隆）：只搬 ``task_prompt.candidate_outputs(spec)``
    与本模块显式追加的复现件。排除式克隆会把候选在 workdir 里留下的任何垃圾
    （包括它顺手抄走的判据件）一起搬进证据包。符号链接一律跳过——``delivered/``
    里不许有链接（AC-06），链接本身就是「评测时读到运行外内容」的载体。
    """
    ensure_private_dir(delivered)
    rels = list(task_prompt.candidate_outputs(spec)) + [r for r in extra_rels if r]
    written: "list[str]" = []
    for rel in rels:
        src = Path(workdir) / rel
        if src.is_symlink() or not src.is_file():
            continue
        dst = Path(delivered) / rel
        ensure_private_dir(dst.parent)
        if dst.exists():
            dst.unlink()
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o600)
        written.append(dst.relative_to(Path(delivered).parent).as_posix())
    return written


def _mirror_inputs(input_dir: Path, workdir: Path) -> None:
    """宿主任务的 ``input/`` 镜像进工作根（容器形态由 ``/work`` 挂载提供同一份）。

    只对 ``runtime == host`` 做：agentic 臂在兄弟容器里看不到宿主路径，它唯一
    的可读面就是被挂进 ``/out`` 的工作根。
    """
    target = Path(workdir) / "input"
    for path in _regular_files(Path(input_dir)):
        dst = target / path.relative_to(input_dir)
        ensure_private_dir(dst.parent)
        if dst.exists():
            continue
        shutil.copyfile(path, dst)
        os.chmod(dst, 0o600)


def _repro(spec: Any, *, workdir: Path, logs: Path, delivered: Path,
           sandbox_obj: Any, cmd: Sequence[str], seed: int,
           timeout_s: int) -> "list[str]":
    """M1 的复现件：同种子再跑一次 + 换种子再跑一次，各交两件原始产物。

    只对 ``mujoco_contact_push_v1`` 做（``task.yaml`` 的 ``reproducibility`` 要求
    同种子逐字段一致）。两次都在 ``logs/repro/`` 的工作根**副本**里进行——候选
    入口可能依赖相对导入，在别处跑会得到与主运行不同的失败模式，那会把「不独立」
    误读成「不确定性」。
    """
    if str(getattr(spec, "task_id", "")) != "mujoco_contact_push_v1":
        return []
    runner = sandbox_obj.subprocess_runner()
    out_rels: "list[str]" = []
    for label, run_seed in (("same_seed", int(seed)),
                            ("cross_seed", int(seed) + 1)):
        target = ensure_private_dir(Path(logs) / "repro" / label)
        _copy_workdir(Path(workdir), target)
        argv = [str(item) for item in cmd]
        for index, token in enumerate(argv):
            if token == "--seed" and index + 1 < len(argv):
                argv[index + 1] = str(run_seed)
        try:
            runner(argv, target, float(timeout_s),
                   {"PATH": os.environ.get("PATH", "")})
        except (OSError, subprocess.SubprocessError):
            continue
        for name in ("metrics.json", "telemetry.jsonl"):
            src = target / name
            if src.is_symlink() or not src.is_file():
                continue
            dst = Path(delivered) / "repro" / label / name
            ensure_private_dir(dst.parent)
            if dst.exists():
                dst.unlink()
            shutil.copyfile(src, dst)
            os.chmod(dst, 0o600)
            out_rels.append(dst.relative_to(Path(delivered).parent).as_posix())
    return out_rels


def _copy_workdir(source: Path, target: Path) -> None:
    """把工作根复制成一份可独立执行的副本（跳过追踪目录与输入镜像）。"""
    skip = {".agentic_trace", "input", "__pycache__"}
    for path in _regular_files(source):
        rel = path.relative_to(source)
        if rel.parts and rel.parts[0] in skip:
            continue
        dst = target / rel
        ensure_private_dir(dst.parent)
        if dst.exists():
            continue
        shutil.copyfile(path, dst)
        os.chmod(dst, 0o600)


def _copy_trace(workdir: Path, target: Path) -> int:
    """把工作根里的 agentic 追踪件搬进 ``<trial_dir>/trace/``（legacy 臂为空）。"""
    source = Path(workdir) / ".agentic_trace"
    ensure_private_dir(target)
    if not source.is_dir():
        return 0
    copied = 0
    for path in _regular_files(source):
        dst = Path(target) / path.relative_to(source)
        ensure_private_dir(dst.parent)
        if dst.exists():
            dst.unlink()
        shutil.copyfile(path, dst)
        os.chmod(dst, 0o600)
        copied += 1
    return copied


# ---------------------------------------------------------------------------
# 运行形态探针（AC-18 的 ``analysis/run_form_probe.json``）
# ---------------------------------------------------------------------------

_PROBE_SRC = """\
import json, sys
mods = {{}}
for name in {names!r}:
    try:
        mod = __import__(name)
        mods[name] = str(getattr(mod, "__version__", "?"))
    except Exception as exc:
        mods[name] = "ERR:%s" % type(exc).__name__
print(json.dumps({{"python_v": sys.version.split()[0],
                  "executable": sys.executable, "mods": mods}}))
"""


#: 包名 → import 名（``task.yaml`` 写发行名，``import`` 用模块名）。
#: 不映射就会漏掉 ``PyYAML``→``yaml``、``jsonschema``→``jsonschema`` 这类
#: 只在发行名上差一个大小写的项——R1 的探针曾因此报出「import_ok=False 而
#: rc=0」的假阴性（一次纯属映射表缺项的红色）。
_IMPORT_NAME_BY_PACKAGE = {
    "mujoco": "mujoco",
    "robosuite": "robosuite",
    "bddl": "bddl",
    "numpy": "numpy",
    "trimesh": "trimesh",
    "PyYAML": "yaml",
    "pyyaml": "yaml",
    "jsonschema": "jsonschema",
}


def probe_run_form(spec: Any, *,
                   runner: Callable[..., Any] = subprocess.run) -> dict:
    """实测「这条任务的运行形态能不能跑起来」，写成**可机读**的判据件。

    L1 的三项（``python_v`` / ``import_ok`` / ``rc``）正是 AC-P281-4-18 要求逐项
    在场的那三格；M1/R1 记宿主解释器的同三项。探针只读运行，不产生任何候选
    可见面，不发网络（容器形态带 ``--network none``）。
    """
    task_id = str(getattr(spec, "task_id", ""))
    runtime = constants.RUNTIME_BY_ID.get(task_id)
    runtime_doc = dict(getattr(spec, "runtime", {}) or {})
    python = str(runtime_doc.get("python") or "")
    packages = dict(runtime_doc.get("packages") or {})
    imports = sorted({_IMPORT_NAME_BY_PACKAGE[name] for name in packages
                      if name in _IMPORT_NAME_BY_PACKAGE})
    src = _PROBE_SRC.format(names=imports)
    doc: "dict[str, Any]" = {
        "schema": "simpraxis.phase281.run_form_probe.v1",
        "task_id": task_id, "runtime": runtime, "python": python,
        "image": None, "python_v": None, "import_ok": False, "rc": None,
        "mods": {}, "probed_at": _now(), "stderr_tail": "",
    }
    if runtime == "docker":
        image_ref = reference_mod.libero_image_ref(spec)
        doc["image"] = image_ref
        argv = ["docker", "run", "--rm", "--network", "none", image_ref,
                "python3", "-I", "-c", src]
    else:
        if not python:
            doc["stderr_tail"] = "runtime.python 为空"
            return doc
        argv = [python, "-I", "-c", src]
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=300,
                      env=sandbox.clean_env())
    except (OSError, subprocess.SubprocessError) as exc:
        doc["stderr_tail"] = f"{type(exc).__name__}: {exc}"
        return doc
    doc["rc"] = int(proc.returncode)
    doc["stderr_tail"] = (proc.stderr or "")[-600:]
    if int(proc.returncode) == 0:
        lines = (proc.stdout or "").strip().splitlines()
        try:
            payload = json.loads(lines[-1]) if lines else {}
        except ValueError:
            payload = {}
        doc["python_v"] = payload.get("python_v")
        doc["mods"] = dict(payload.get("mods") or {})
        # 无可导入项（任务没声明已知包）时，rc=0 就是全部能证明的东西：
        # 记 import_ok=True 并让 mods 空表自证「什么都没验」，不许反过来
        # 把「没东西可验」判成红色。
        doc["import_ok"] = (not doc["mods"] and not imports) or (
            bool(doc["mods"]) and all(
                not str(v).startswith("ERR:") for v in doc["mods"].values()))
    return doc


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_trial(*, spec: Any, arm_id: str, rep: int, root: Path,
              gateway: Any, ledger: Any, dry_run: bool = False,
              capture: "Mapping | None" = None,
              sandbox_factory: "Callable[..., Any] | None" = None,
              engine_runner: "Callable[..., Any] | None" = None,
              pre_seed: "Mapping[str, str] | None" = None,
              skills_rows: "Mapping | None" = None,
              assets_dir: "Path | None" = None,
              runner: Callable[..., Any] = subprocess.run,
              birth_runner: "Callable[..., dict] | None" = None,
              ) -> TrialOutcome:
    """跑一个 Trial。**七步顺序即纪律**（计划 §8.5.6）。"""
    from . import paths as paths_mod

    paths_mod.assert_no_benchmark_root_env()
    arm = arm_adapter.ARMS.get(str(arm_id))
    if arm is None:
        raise arm_adapter.ArmSpecError(
            f"未知臂：{arm_id!r}（合法值 {constants.ARMS}）")
    task_id = str(getattr(spec, "task_id", ""))
    runtime = constants.RUNTIME_BY_ID.get(task_id)
    if runtime is None:
        raise sandbox.SandboxError(
            f"未登记的任务：{task_id!r}（{constants.TASK_IDS}）")
    trial_id = constants.trial_id(task_id, arm.arm_id, rep)
    root = Path(root)
    base = root / "runs" / task_id / arm.arm_id / f"rep{rep}"

    if dry_run:
        _reset_own_dir(base)            # 干跑重来：只清自己的 rep<k>
    elif base.is_dir() and any(base.iterdir()):
        raise TrialRefused(
            f"Trial 目录非空，拒绝覆盖：{base}（人工清理或换 rep；"
            f"干跑残留带 {DRYRUN_MARKER} 标记）")
    ensure_private_dir(base)
    if dry_run:
        _write_json(base / DRYRUN_MARKER,
                    {"simulated": True, "created_at": _now(),
                     "note": "干跑：假网关，0 付费；真实运行会拒绝此目录"})

    seed_hex = pair_seed(task_id, rep)
    seed = seed_int(seed_hex)
    state: "dict[str, Any]" = {
        "schema": TRIAL_SCHEMA,
        "experiment_id": constants.EXPERIMENT_ID,
        "trial_id": trial_id,
        "task_id": task_id,
        "arm_id": arm.arm_id,
        "rep": int(rep),
        "simulated": bool(dry_run),
        "state": "created",
        "transitions": [{"state": "created", "at": _now()}],
        "seed": {"pair_index": pair_index(rep), "hex": seed_hex, "int": seed},
        "arm": arm.to_json(),
        "config_digest": arm_adapter.arm_config_digest(arm),
        "problems": [],
        "created_at": _now(),
    }
    workdir = ensure_private_dir(base / "workdir")
    birth: "dict | None" = None
    deployment: "dict[str, Any]" = {}
    fingerprint_state = "absent"
    fingerprint_sha = _NO_FINGERPRINT
    observed: dict = {"calls": 0, "tokens_in": 0, "tokens_out": 0,
                      "coding_rounds": 0, "tool_steps": 0,
                      "first_birth_round": 0}
    status = "CRASHED"
    used_calls = 0

    def _transition(name: str) -> None:
        state["state"] = name
        state["transitions"].append({"state": name, "at": _now()})

    def _close(final: str, *, reason: "str | None" = None,
               manifest_rels: "Sequence[str]" = ()) -> TrialOutcome:
        return _finish(spec, arm, base, state, status=final,
                       used_calls=used_calls, workdir=workdir,
                       trial_id=trial_id, manifest_rels=manifest_rels,
                       observed=observed, fingerprint_sha=fingerprint_sha,
                       fingerprint_state=fingerprint_state,
                       birth=birth, deployment=deployment, reason=reason)

    # ---- ① 运行时证明（早于一切候选行为） --------------------------------
    image_ref = (reference_mod.libero_image_ref(spec)
                 if runtime == "docker" else None)
    attest = _attest(spec, base, capture=capture, image=image_ref)
    verdict = dict(attest["verdict"])
    fingerprint_state = str(attest["fingerprint_state"])
    if attest.get("fingerprint_sha256"):
        fingerprint_sha = str(attest["fingerprint_sha256"])
    state["runtime"] = {
        "mode": runtime,
        "attested_form": ("sibling_container" if runtime == "docker"
                          else "host_process"),
        "verdict": verdict.get("verdict"),
        "problems": list(verdict.get("problems") or []),
        "fingerprint_state": fingerprint_state,
        "fingerprint_sha256": fingerprint_sha,
        "image": image_ref,
    }
    _transition("attested")
    if verdict.get("verdict") != candidate_runtime.VERDICT_PASS:
        # 环境不符：不进入任何候选行为；清单照写，使评测器给出 BLOCKED_INFRA。
        deployment = {"form": "none", "deployment": "not_started",
                      "engine": arm.engine}
        return _close("BLOCKED_INFRA", reason="runtime_attestation")

    # ---- ② 输入 staging + TASK.md --------------------------------------
    try:
        rows = dict(skills_rows) if skills_rows is not None else _skills_rows(
            root=root,
            manifest_sha256=str(dict(getattr(spec, "inputs", {}) or {}).get(
                "skills_manifest_sha256") or ""))
        staged = _stage_inputs(spec, base, rows=rows)
        task_md_text = task_prompt.render_task_md(
            spec, rep=rep, seed=seed_hex,
            input_relpaths=staged["prompt_inputs"])
        task_md_path, task_md_sha = task_prompt.write_task_md(
            base, spec, rep=rep, seed=seed_hex,
            input_relpaths=staged["prompt_inputs"])
    except (sandbox.SandboxError, reference_mod.ReferenceError, OSError) as exc:
        state["problems"].append(f"输入 staging 失败：{type(exc).__name__}: {exc}")
        return _close("BLOCKED_INFRA", reason="input_staging")
    state["task_md"] = {"path": str(task_md_path), "sha256": task_md_sha}
    input_before = _tree_shas(base / "input")
    _transition("staged")

    # ---- ③ 子目录（0700）；call_cache 独立 ------------------------------
    for name in ("trace", "evidence", "delivered", "call_cache", "logs"):
        ensure_private_dir(base / name)
    call_cache = CallReplay(base / "call_cache")
    if runtime == "host":
        _mirror_inputs(base / "input", workdir)

    # ---- ④ 出生闸（只在入口已在场时判定） ------------------------------
    python = str(dict(getattr(spec, "runtime", {}) or {}).get("python") or "")
    if runtime == "docker":
        assets = (Path(assets_dir) if assets_dir is not None
                  else Path(reference_mod.REPO_ROOT))
    else:
        assets = (Path(assets_dir) if assets_dir is not None
                  else base / "input" / "assets")
    birth_fn = birth_runner or _birth
    try:
        birth = _pre_birth(spec, base=base, trial_id=trial_id, workdir=workdir,
                           skills_dir=staged["skills_dir"], assets_dir=assets,
                           image_ref=image_ref, python=python,
                           pre_seed=pre_seed, birth_runner=birth_fn)
    except (sandbox.SandboxError, OSError, reference_mod.ReferenceError) as exc:
        state["problems"].append(
            f"出生检查无法完成：{type(exc).__name__}: {exc}")
        birth = {"status": "BLOCKED_INFRA", "reason": str(exc), "schema": None}
    if birth is not None:
        state["birth"] = {"status": birth.get("status"),
                          "reason": birth.get("reason"),
                          "schema": birth.get("schema")}
        state["birth_phase"] = "pre"
        _transition("born")
        if str(birth.get("status")) != "BORN":
            # 未出生：**不发任何调用**（AC-11）。终态仍是 FINISHED——「没出生」
            # 是候选的结局，不是基础设施的失败。
            return _close("FINISHED", reason="birth_not_born")

    # ---- ⑤ 引擎运行（唯一分叉点） --------------------------------------
    cmd, telemetry_relpath, env_extra = _invocation(spec, seed=seed)
    runspec = _build_runspec(spec, arm, trial_id=trial_id, workdir=workdir,
                             cmd=cmd, telemetry_relpath=telemetry_relpath,
                             task_title=task_md_text, env_extra=env_extra)
    factory = sandbox_factory or sandbox.sandbox_for
    try:
        sandbox_obj = factory(spec, arm, trial_workdir=workdir,
                              input_dir=base / "input",
                              skills_dir=staged["skills_dir"],
                              assets_dir=assets, runner=runner)
    except (sandbox.SandboxError, OSError) as exc:
        state["problems"].append(f"执行底座装配失败：{exc}")
        deployment = {"form": "none", "deployment": "not_started",
                      "engine": arm.engine, "error": str(exc)}
        return _close("BLOCKED_INFRA", reason="sandbox_setup")
    deployment = _deployment(arm, spec, sandbox_obj, image_ref)
    state["deployment"] = deployment

    budget = telemetry.TrialBudget(cap=arm.call_cap, ledger=ledger,
                                   trial_id=trial_id)
    guarded = telemetry.CapGuardGateway(
        _DryRunGateway() if dry_run else gateway, budget)
    if not dry_run:
        _ensure_approval(ledger, trial_id=trial_id, arm=arm)

    watch = BirthWatch(workdir / _entry_rel(spec))
    ctx = TrialContext(
        trial_id=trial_id, arm=arm, paths=None, runspec=runspec,
        gateway=guarded, call_cache=call_cache, ledger=ledger,
        sandbox=_WatchingSandbox(sandbox_obj, watch),
        evidence_dir=base / "evidence", recall=arm_adapter.NullRecallEngine(),
        input_dir=base / "input", skills_dir=staged["skills_dir"],
        assets_dir=assets, seed=seed, birth_watch=watch,
        state_dir=ensure_private_dir(base / "state"))
    _transition("running")

    from orchestrator.gateway.quota_ledger import billing_context

    engine_runner = engine_runner or arm_adapter.run_arm
    engine_result: dict = {}
    try:
        with billing_context(task=constants.TASK_NO_BY_ID[task_id],
                             batch_id=trial_id, thread_id=trial_id):
            engine_result = dict(engine_runner(arm, ctx) or {})
        status = "FINISHED"
    except BaseException as exc:                     # noqa: BLE001 - 终态映射
        status = _map_engine_error(exc)
        state["problems"].append(
            f"引擎异常（{status}）：{type(exc).__name__}: {exc}")
        engine_result = {"status": "failed", "reason": state["problems"][-1]}
    used_calls = budget.used()
    state["engine"] = {"status": engine_result.get("status"),
                       "coding_round": engine_result.get("coding_round"),
                       "executions": engine_result.get("executions"),
                       "reason": engine_result.get("reason")}

    # ---- ⑥ 交付冻结 + ⑦ 出生判定（引擎之后，权威） ---------------------
    state["trace_files"] = _copy_trace(workdir, base / "trace")
    delivered_rels = _collect_delivered(spec, workdir=workdir,
                                        delivered=base / "delivered")
    if (workdir / _entry_rel(spec)).is_file():
        delivered_rels += _repro(
            spec, workdir=workdir, logs=base / "logs",
            delivered=base / "delivered", sandbox_obj=sandbox_obj, cmd=cmd,
            seed=seed, timeout_s=constants.WALLCLOCK_CAP_S)

    input_after = _tree_shas(base / "input")
    if input_after != input_before:
        changed = sorted(set(input_after.items()) ^ set(input_before.items()))
        state["problems"].append(
            "输入面在运行期间被改动（AC-13）：" + json.dumps(
                [list(item) for item in changed[:5]], ensure_ascii=False))

    if birth is None:
        try:
            birth = birth_fn(spec, base=base, trial_id=trial_id,
                             workdir=workdir, skills_dir=staged["skills_dir"],
                             assets_dir=assets, image_ref=image_ref,
                             python=python)
        except (sandbox.SandboxError, OSError,
                reference_mod.ReferenceError) as exc:
            state["problems"].append(
                f"出生判定无法完成：{type(exc).__name__}: {exc}")
            birth = {"status": "BLOCKED_INFRA", "reason": str(exc),
                     "schema": None}
        state["birth"] = {"status": birth.get("status"),
                          "reason": birth.get("reason"),
                          "schema": birth.get("schema")}
        state["birth_phase"] = "post"
        _transition("born")
    if str(birth.get("status")) == "BLOCKED_INFRA" and status == "FINISHED":
        # 出生判定本身被基础设施挡住：不许把它记成候选的失败
        status = "BLOCKED_INFRA"
        state["problems"].append("出生判定被判 BLOCKED_INFRA（环境侧）")

    # 三源遥测：总结行 → 回读 → 清单 claimed 抄同一份
    observed = telemetry.observed(base, ledger, trial_id=trial_id)
    _fill_absent(observed, engine_result=engine_result, arm=arm,
                 trace_files=int(state.get("trace_files") or 0),
                 problems=state["problems"])
    observed["first_birth_round"] = _first_birth_round(watch, birth)
    observed["birth_probe"] = _birth_probe(birth, watch)
    telemetry.write_summary(base, observed)
    observed = telemetry.observed(base, ledger, trial_id=trial_id)
    observed["birth_probe"] = _birth_probe(birth, watch)
    return _close(status, manifest_rels=sorted(set(delivered_rels)))


def _birth_probe(birth: "Mapping | None", watch: BirthWatch) -> dict:
    return {"birth_status": (birth or {}).get("status"),
            "first_ok_exec": watch.first_ok_exec, "execs": watch.execs}


def _fill_absent(observed: dict, *, engine_result: Mapping, arm: Any,
                 trace_files: int, problems: "list[str]") -> None:
    """把 ``observed`` 里的 ``None`` 补成**可对账的整数**，来源如实记账。

    为什么必须补：评测器的 ``_self_report`` 用
    ``equal = got is not None and want == got``——一侧是 ``None`` 时**永远**
    不等，一份完美 Trial 也会被判 ``INVALID_ARTIFACT``。而 ``None`` 的语义是
    「这一源不在场」，不是「值为空」，故补值时必须同时写清补的依据：

    * ``coding_rounds``：引擎自己的 ``coding_round`` 优先（它就是编码轮次的定义），
      缺则退回账本里 ``coding*`` 阶段的实呼行数；
    * ``tool_steps``：无追踪件 ⇒ 0 步；但 **agentic 臂**无追踪件本身是异常
      （那意味着内环根本没跑起来），记一条 problem，不许静默当 0 步。
    """
    sources = observed.setdefault("sources", {})
    if observed.get("coding_rounds") is None:
        engine_round = engine_result.get("coding_round") if engine_result else None
        if type(engine_round) is int and engine_round >= 0:
            observed["coding_rounds"] = int(engine_round)
            sources["coding_rounds"] = "engine_return"
        else:
            stages = list(observed.get("stages") or ())
            observed["coding_rounds"] = sum(
                1 for stage in stages if str(stage).startswith("coding"))
            sources["coding_rounds"] = "ledger_stage_count"
    if observed.get("tool_steps") is None:
        observed["tool_steps"] = 0
        sources["tool_steps"] = "absent"
        if bool(getattr(arm, "runspec_agentic", False)) and trace_files == 0:
            problems.append(
                "agentic 臂没有产出追踪件（tool_steps 记 0，来源 absent）")


# ---------------------------------------------------------------------------
# 主流程的零件
# ---------------------------------------------------------------------------

def _invocation(spec: Any, *, seed: int) -> "tuple[list[str], str, dict]":
    """入口调用形状：**两臂逐字相同**，且在两种部署形态下都成立。

    三条分支与 ``task_prompt._invocation_block`` 一一对应（说明里写什么，这里就
    调什么——两处不一致会让候选按说明写、却被按另一套调用）。

    * ``document_bundle``（R1）：入口是机读文档，运行器不以 argv 执行业务语义，
      只做「在场且非空」的结构自查。
    * L1（任务镜像）：``python3`` 用**裸名**而非 ``/usr/bin/python3``。任务声明
      的 ``runtime.python`` 是容器内路径；宿主上同路径是系统 python（没有
      robosuite），故两头统一用裸名 + ``PATH`` 注入。
    * 其余（M1 宿主形态）：工作根即当前目录，``--out-dir .`` 与参考运行
      （``run_host_reference`` 的 ``--out-dir <target>`` + ``cwd=target``）同义。
    """
    task_id = str(getattr(spec, "task_id", ""))
    entry_rel = _entry_rel(spec)
    python = str(dict(getattr(spec, "runtime", {}) or {}).get("python")
                 or "python")
    env_extra = {"PATH": os.pathsep.join(
        [os.path.dirname(python) or "/usr/bin", os.environ.get("PATH", "")])}
    entrypoint = dict(getattr(spec, "entrypoint", {}) or {})
    if str(entrypoint.get("kind") or "") == "document_bundle":
        return ([python, "-I", "-c",
                 "import sys, pathlib; "
                 "p = pathlib.Path(sys.argv[1]); "
                 "sys.exit(0 if p.is_file() and p.stat().st_size else 2)",
                 entry_rel], entry_rel, env_extra)
    if task_id == "libero_env_adapter_v1":
        return (["python3", entry_rel, "--out-dir", ".", "--seed", str(seed),
                 "--chown-uid", str(os.getuid()),
                 "--chown-gid", str(os.getgid())],
                "one_step_result.json", env_extra)
    return ([python, "-I", entry_rel, "--out-dir", ".", "--seed", str(seed)],
            "telemetry.jsonl", env_extra)


def _ensure_approval(ledger: Any, *, trial_id: str,
                     arm: arm_adapter.ArmSpec) -> None:
    """批次凭据（硬规则 2：逐批落 ``quota/approvals/``）。已存在则不重开。"""
    from orchestrator.gateway.quota_ledger import write_approval

    ns = getattr(ledger, "ns", None)
    if ns is None:
        return
    target = Path(ns.approvals_dir_fn()) / f"{trial_id}.json"
    if target.exists():
        return
    task_no = int(constants.TASK_NO_BY_ID[trial_id.split("__")[0]])
    write_approval(batch_id=trial_id, task=task_no, stage="coding_debug",
                   approved=arm.call_cap, beyond_normal=False,
                   note=(f"Phase 2.8.1 Trial {trial_id}：臂 {arm.arm_id}，"
                         f"帽 {arm.call_cap} 呼（自批，硬规则 2）"),
                   namespace=ns)


def _first_birth_round(watch: BirthWatch, birth: "Mapping | None") -> int:
    """首次可解析执行点对应的**执行序号**（0 = 从未可解析）。

    用执行序号而不是计费轮次：一轮 coding 可能跑多次执行、也可能一次都不跑，
    只有执行序号能回答「到第几次执行时入口第一次像个入口」。计费轮次另由账本
    给出（``coding_rounds``），两者分列，不混成一个数。
    """
    if birth is None or str(birth.get("status")) != "BORN":
        return 0
    return int(watch.first_ok_exec or 0)


class _WatchingSandbox:
    """在执行点之间插一次廉价出生探测的**透明**代理。

    ``arm_adapter`` 只按鸭子类型读 ``subprocess_runner()`` / ``run_backend()``，
    故代理把未知属性原样转给内层；两个执行入口都被包一层 ``observe()``。
    """

    def __init__(self, inner: Any, watch: BirthWatch) -> None:
        self.inner = inner
        self.watch = watch

    @property
    def form(self) -> str:
        return str(getattr(self.inner, "form", "unknown"))

    @property
    def kind(self) -> str:
        return str(getattr(self.inner, "kind", "unknown"))

    def subprocess_runner(self) -> Any:
        inner_runner = self.inner.subprocess_runner()

        def _run(cmd, workdir, timeout_s, env_extra):
            try:
                return inner_runner(cmd, workdir, timeout_s, env_extra)
            finally:
                self.watch.observe()

        return _run

    def run_backend(self) -> Any:
        return _BackendProxy(self.inner.run_backend(), self.watch)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


class _BackendProxy:
    """容器后端的透明代理（只为了在每次 ``run`` 之后看一眼入口）。"""

    def __init__(self, inner: Any, watch: BirthWatch) -> None:
        self.inner = inner
        self.watch = watch

    def run(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return self.inner.run(*args, **kwargs)
        finally:
            self.watch.observe()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def _run_manifest(spec: Any, *, trial_id: str, arm: arm_adapter.ArmSpec,
                  base: Path, status: str, deployment: Mapping,
                  birth: "Mapping | None", observed: Mapping,
                  fingerprint_state: str, reason: "str | None",
                  simulated: bool) -> Path:
    """运行器视角的运行记录（``task.yaml.outputs`` 里由运行器代写的那一件）。

    与 Phase 1/2 的 ``run_manifest.v1`` **不同 schema**：那一份把
    ``paid_model_calls`` 钉成 ``const 0``（图为无付费设计），本阶段的 Trial 本来
    就要花钱，硬套会把每次实呼写成一次契约违规。
    """
    doc = {
        "schema": RUN_MANIFEST_SCHEMA,
        "experiment_id": constants.EXPERIMENT_ID,
        "trial_id": trial_id,
        "task_id": str(getattr(spec, "task_id", "")),
        "arm": arm.arm_id,
        "engine": arm.engine,
        "status": status,
        "reason": reason,
        "simulated": bool(simulated),
        "deployment": dict(deployment),
        "birth": {"status": (birth or {}).get("status"),
                  "reason": (birth or {}).get("reason"),
                  "schema": (birth or {}).get("schema")},
        "observed": {k: observed.get(k) for k in
                     ("calls", "tokens_in", "tokens_out", "coding_rounds",
                      "tool_steps", "first_birth_round")},
        "runtime_fingerprint_state": fingerprint_state,
        "created_at": _now(),
    }
    return _write_json(Path(base) / "delivered" / RUN_MANIFEST, doc)


def _claimed_of(observed: Mapping) -> dict:
    """``claimed`` 抄自观测（同一份数字，两个名字）。

    ``claimed`` 的语义是「引擎自报花了多少」，而本阶段引擎的自报值由运行器
    **从账本重算**得出；候选不参与任何 ``claimed`` 字段。
    """
    return {
        "paid_model_calls": int(observed.get("calls") or 0),
        "coding_rounds": int(observed.get("coding_rounds") or 0),
        "first_birth_round": int(observed.get("first_birth_round") or 0),
        "tool_steps": int(observed.get("tool_steps") or 0),
        "tokens_in": int(observed.get("tokens_in") or 0),
        "tokens_out": int(observed.get("tokens_out") or 0),
    }


def _input_relpaths(base: Path) -> "list[str]":
    rels = [p.relative_to(base).as_posix()
            for p in _regular_files(Path(base) / "input")]
    if not rels:
        raise candidate_manifest.ManifestError(
            f"{base}/input 为空：inputs 不得为空（至少 task_snapshot.json）")
    return rels


def _finish(spec: Any, arm: arm_adapter.ArmSpec, base: Path, state: dict, *,
            status: str, used_calls: int, workdir: Path, trial_id: str,
            manifest_rels: "Sequence[str]", observed: Mapping,
            fingerprint_sha: str, fingerprint_state: str,
            birth: "Mapping | None", deployment: Mapping,
            reason: "str | None" = None) -> TrialOutcome:
    """⑥/⑦ 的收尾：运行记录 → 清单 → ``trial.json`` 终态 → ``TrialOutcome``。

    清单**总是**写：一份缺清单的 Trial 在评测器眼里是 ``INVALID_ARTIFACT``
    （「候选把清单弄丢了」），而真实原因可能是「运行器没跑到那一步」——后者
    必须落成 ``BLOCKED_INFRA``，两者不能混为一谈。
    """
    if status not in STATUSES:
        raise ValueError(f"非法终态：{status!r}")
    ensure_private_dir(Path(base) / "delivered")
    _run_manifest(spec, trial_id=trial_id, arm=arm, base=base, status=status,
                  deployment=deployment, birth=birth, observed=observed,
                  fingerprint_state=fingerprint_state, reason=reason,
                  simulated=bool(state.get("simulated")))
    rels = sorted(set(list(manifest_rels) + [f"delivered/{RUN_MANIFEST}"]))
    # 指纹件连旁路 sha256 一起登记：``delivered/`` 里的每件都必须在清单里，
    # 否则「清单 ↔ 件」的集合对账会多出一件无主文件（AC-P281-4-06 的集合面，
    # 也是证据包 completeness 的前提）。旁路件由 ``write_fingerprint`` 与本体
    # 同批落盘，`FINGERPRINT_NAME` 与 `SHA256_NAME` 是那条路径的单一真值。
    for name in (candidate_runtime.rf.FINGERPRINT_NAME,
                 candidate_runtime.rf.SHA256_NAME):
        if (Path(base) / "delivered" / name).is_file():
            rels = sorted(set(rels + [f"delivered/{name}"]))

    manifest_sha = None
    try:
        manifest = candidate_manifest.build_manifest(
            base, trial=trial_id, arm=arm.arm_id,
            task_id=str(getattr(spec, "task_id", "")),
            runtime_mode=str(constants.RUNTIME_BY_ID.get(
                str(getattr(spec, "task_id", "")), "host")),
            fingerprint_sha256=str(fingerprint_sha),
            input_relpaths=_input_relpaths(base),
            deliverable_relpaths=rels,
            claimed=_claimed_of(observed),
            engine_entry=arm.engine,
            config_digest=arm_adapter.arm_config_digest(arm))
        path = _write_json(Path(base) / candidate_manifest.MANIFEST_NAME, manifest)
        manifest_sha = _sha256_file(path)
    except (candidate_manifest.ManifestError, OSError) as exc:
        state["problems"].append(f"清单构造失败：{exc}")

    state["state"] = "finished"
    state["transitions"].append({"state": "finished", "at": _now(),
                                 "status": status})
    state["status"] = status
    state["reason"] = reason
    state["used_calls"] = int(used_calls)
    state["workdir"] = str(workdir)
    state["finished_at"] = _now()
    state["observed"] = {k: observed.get(k) for k in
                         ("calls", "tokens_in", "tokens_out", "coding_rounds",
                          "tool_steps", "first_birth_round")}
    state["observed_sources"] = dict(observed.get("sources") or {})
    state["birth"] = {"status": (birth or {}).get("status"),
                      "reason": (birth or {}).get("reason"),
                      "schema": (birth or {}).get("schema")}
    state["manifest"] = {"path": str(Path(base) / candidate_manifest.MANIFEST_NAME),
                         "sha256": manifest_sha,
                         "entries": rels}
    _write_json(Path(base) / TRIAL_JSON, state)
    return TrialOutcome(trial_id=trial_id,
                        task_id=str(getattr(spec, "task_id", "")),
                        arm_id=arm.arm_id, rep=int(state["rep"]),
                        status=status, used_calls=int(used_calls),
                        workdir=Path(workdir), trial_dir=Path(base))
