"""P281-4 Trial 级测试的共用夹具：一个**真实流水线、0 付费**的试验台。

为什么要有这一层（与 ``phase281_parity.py`` / ``phase281_birth_fixtures.py`` 同一先例）：
``test_phase281_prompt_isolation.py``（AC-04/05/06/19）与
``test_phase281_telemetry.py``（AC-07..18）都要跑**真实的** ``run_trial`` 七个步骤，
两边各抄一遍装配代码，等于给「七步顺序」造两份互不相同的测试副本。抽成本模块后，
两份测试共用同一个装配点，谁改了装配就得同时面对两组 AC。

**「真实」到哪一步、假到哪一步（报告里要照抄）**：

===============  ==========================================  ======================
零件              真 / 假                                       理由
===============  ==========================================  ======================
七步流水线        真（``trial_harness.run_trial`` 本体）        被测对象
运行时证明        真采集一次后**注入**                          避免每 Trial 起探针进程
账本写读          真文件、真格式行、seq 连续                    账本读取面是被测对象
模型网关          假（``FakeGateway``：0 触网，只追加账行）      付费纪律（硬规则 2）
引擎              假（``Engine`` 桩：按测试需要写文件/花调用）   被测对象在别处（T2/T3）
出生判定          假（``birth_runner`` 桩）                     出生五层是 P281-2 的地盘
技能包            **合成**（自造一个文件 + 真 sha256）           不碰活 KB/活数据层
===============  ==========================================  ======================

合成的技能包是这一层唯一「不像生产」的地方：生产走
``reference.materialize_skills``（真 KB），测试走 :func:`skills_rows`（自造件）。
它保证的是「harness 的 staging 契约被真实执行」，不是「KB 技能包内容正确」——
后者由 P281-2/P281-3 的活库测试负责（``kb_integration`` 车道）。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from orchestrator.benchmark.phase281 import candidate_birth, candidate_runtime
from orchestrator.benchmark.phase281 import constants
from orchestrator.benchmark.phase281 import trial_harness

__all__ = (
    "Engine", "FakeGateway", "REPO_ROOT", "RunResult", "capture_for",
    "ledger_rows", "read_json", "run_case", "sha_tree", "skills_rows",
    "summary_of",
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 合成技能件的内容（不含任何判据/参考解信息；只用来走 staging 契约）
SKILL_TEXT = "# 合成技能件（测试用，不是 KB 内容）\nskill: phase281-test\n"

#: 运行时证明的模块级缓存：同一任务只真采集一次
_CAPTURE_CACHE: "dict[str, dict]" = {}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# 输入面：合成技能包
# --------------------------------------------------------------------------

def skills_rows(base: "str | Path") -> dict:
    """造一份**形状合法**的 ``materialize_skills`` 结果（件自造、sha256 真算）。

    ``stage_skills_into`` 会逐件复核 sha256，故这里必须给真值——假 sha 会在
    staging 阶段被拒，测试就变成在测「假 fixture 被拒」，毫无意义。
    """
    root = Path(base) / "skills_stage"
    root.mkdir(parents=True, exist_ok=True)
    data = SKILL_TEXT.encode("utf-8")
    target = root / "phase281_test_skill.md"
    target.write_bytes(data)
    os.chmod(target, 0o600)
    return {
        "schema": "simpraxis.phase281.input_manifest.v1",
        "kind": "skills",
        "source_root": str(root),
        "manifest_sha256": _sha256_bytes(SKILL_TEXT.encode("utf-8")),
        "manifest_sha12": "synthetic0000",
        "stage_root": str(root),
        "files": [{"relpath": "skills/phase281_test_skill.md",
                   "staged": target.name,
                   "sha256": _sha256_bytes(data)}],
    }


# --------------------------------------------------------------------------
# 运行时证明：真采一次、之后注入
# --------------------------------------------------------------------------

def capture_for(spec: Any, base: "str | Path") -> dict:
    """真采集一次宿主指纹并按 ``gate(attestation=...)`` 的入参形状返回。

    注入而不是每次重采：捕获会起子进程探测解释器与包版本，一个 AC 里跑五六个
    Trial 就起五六个探针进程，既慢又与「流水线」无关。缓存键是 task_id——
    同一任务在同一台机器上的环境身份本来就相同（这正是缓存成立的前提）。
    """
    task_id = str(getattr(spec, "task_id", ""))
    cached = _CAPTURE_CACHE.get(task_id)
    if cached is None:
        out = candidate_runtime.RuntimeAttestation.capture_for_trial(
            spec, Path(base) / "attest_capture")
        cached = {"fingerprint": out["fingerprint"],
                  "fingerprint_sha256": out["sha256"],
                  "written_at": out["written_at"]}
        _CAPTURE_CACHE[task_id] = cached
    return dict(cached)


# --------------------------------------------------------------------------
# 假网关：0 触网，但账本行是真的
# --------------------------------------------------------------------------

class FakeGateway:
    """``ModelGateway`` 形状的假网关：每次 ``call`` **追加一条真实格式的账行**。

    为什么不是「只记内存计数器」：``TrialBudget`` 的权威值来自账本文件，帽的
    执行是「先读账、再放行」。只记内存的话，被测的就不是生产那条路径了。
    账行字段与 ``QuotaLedger.record`` 同一组（``event``/``seq``/``thread_id``/
    ``task``/``stage``/``tokens_in``/``tokens_out``/``ok``/``ts_iso``），
    ``seq`` 从 1 起连续递增（AC-08 断言的正是这个连续性）。
    """

    def __init__(self, ledger_path: "str | Path", *, trial_id: str,
                 task_no: int, tokens: "tuple[int, int]" = (7, 5),
                 model: str = "fake-model") -> None:
        self.ledger_path = Path(ledger_path)
        self.trial_id = trial_id
        self.task_no = int(task_no)
        self.tokens = (int(tokens[0]), int(tokens[1]))
        self.model = model
        self.calls = 0

    # ---- 账行 ----
    def _seq(self) -> int:
        if not self.ledger_path.is_file():
            return 1
        rows = [json.loads(line) for line in
                self.ledger_path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        return max((int(r["seq"]) for r in rows), default=0) + 1

    def _append(self, stage: str) -> None:
        row = {"event": "call", "seq": self._seq(), "stage": stage,
               "model": self.model, "thread_id": self.trial_id,
               "task": self.task_no, "batch_id": self.trial_id,
               "tokens_in": self.tokens[0], "tokens_out": self.tokens[1],
               "ok": True, "err": None, "ts_iso": _now()}
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    # ---- 网关面 ----
    def model_id(self, stage: str) -> str:
        return self.model

    def call(self, stage: str, messages: Any = None, **kwargs: Any
             ) -> "tuple[str, tuple[int, int]]":
        self.calls += 1
        self._append(str(stage))
        return json.dumps({"patches": []}, ensure_ascii=False), self.tokens

    def call_visual(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("本实验三个任务都不发 image block（硬规则 1）")


# --------------------------------------------------------------------------
# 引擎桩
# --------------------------------------------------------------------------

@dataclass
class Engine:
    """按测试需要动作的引擎桩（``engine_runner`` 的形状：``(arm, ctx) -> dict``）。

    它替换的是**引擎**，不是流水线：上下文（网关/缓存/工作根/输入根）都是
    harness 真造的那一份，故「花了几次调用」「缓存落在哪」「输入有没有被改」
    全部是真实链路的结果。
    """

    #: 通过 ``ctx.gateway`` 花掉的调用次数（经 ``CapGuardGateway``，会触发帽）
    calls: int = 0
    #: 写进工作根的 ``{相对路径: 文本}``（模拟候选产出）
    write: Mapping[str, str] = field(default_factory=dict)
    #: 往输入面写一个文件的相对路径（模拟候选篡改输入，AC-13 的负例）
    tamper_input: "str | None" = None
    #: ``(key, value)``：往本 Trial 的 call_cache 写一条（AC-10）
    put_cache: "tuple[str, str] | None" = None
    #: ``(key, fingerprint)``：从本 Trial 的 call_cache 取（AC-10 断言取不到）
    get_cache: "tuple[str, str] | None" = None
    #: 桩自己看到的东西（供测试断言，不参与流水线）
    seen: dict = field(default_factory=dict)

    def __call__(self, arm: Any, ctx: Any) -> dict:
        workdir = Path(ctx.runspec.workdir)
        self.seen = {"arm_id": getattr(arm, "arm_id", None),
                     "workdir": str(workdir),
                     "input_dir": str(ctx.input_dir),
                     "cache_dir": str(Path(ctx.call_cache.dir)),
                     "runspec_agentic": bool(ctx.runspec.agentic),
                     "tool_grants": tuple(ctx.runspec.tool_grants),
                     "recall_calls": int(getattr(ctx.recall, "calls", 0))}
        for rel, text in dict(self.write).items():
            target = workdir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            os.chmod(target, 0o600)
        if self.tamper_input:
            target = Path(ctx.input_dir) / self.tamper_input
            target.write_text("tampered\n", encoding="utf-8")
        if self.put_cache is not None:
            key, value = self.put_cache
            ctx.call_cache.put(key, kind="coding", model="fake-model", raw=value,
                               usage=(1, 1), fingerprint="f" * 64)
        if self.get_cache is not None:
            key, fingerprint = self.get_cache
            self.seen["cache_hit"] = ctx.call_cache.get(key, fingerprint)
        for index in range(int(self.calls)):
            ctx.gateway.call("coding_debug", [{"role": "user",
                                               "content": f"round {index}"}])
        return {"status": "finished", "coding_round": int(self.calls)}


# --------------------------------------------------------------------------
# 出生桩
# --------------------------------------------------------------------------

def birth_runner(status: str = "BORN") -> Any:
    """``birth_runner`` 桩：恒报 ``status``（出生五层由 P281-2 单独测）。

    与真验证器一样**落一份 ``birth.json``**（0600），而不是只回一个字典：评测器
    的出生闸读的是盘上件（``base/birth.json``），只回字典的话真实 Trial 会在
    评测器那里被判「缺 birth.json（无法证明出生）」——那是夹具的缺口，不是
    流水线的缺陷，却会让每一个自述对账都假红。
    """
    def _run(spec, *, base, trial_id, workdir, skills_dir, assets_dir,
             image_ref, python) -> dict:
        doc = {"schema": "simpraxis.phase281.birth.v2", "status": status,
               "birth_status": status, "reason": None,
               "trial_id": trial_id, "levels": {}}
        target = Path(base) / "birth.json"
        target.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True)
                          + "\n", encoding="utf-8")
        os.chmod(target, 0o600)
        return {"status": status, "reason": None, "schema": "v2",
                "path": str(target), "sha256": _sha256_bytes(target.read_bytes()),
                "levels": {}}
    return _run


# --------------------------------------------------------------------------
# 跑一个 Trial
# --------------------------------------------------------------------------

@dataclass
class RunResult:
    outcome: Any                 # trial_harness.TrialOutcome
    base: Path                   # <root>/runs/<task>/<arm>/rep<k>
    root: Path                   # 数据根
    ledger: Path                 # 账本文件（真文件）
    gateway: FakeGateway
    engine: Engine
    skills: dict

    @property
    def trial(self) -> dict:
        return json.loads((self.base / trial_harness.TRIAL_JSON)
                          .read_text(encoding="utf-8"))

    @property
    def manifest(self) -> dict:
        return json.loads((self.base / "candidate_trial_manifest.json")
                          .read_text(encoding="utf-8"))


def run_case(tmp_path: "str | Path", *, task_id: str, arm: str = "legacy_v1",
             rep: int = 0, engine: "Engine | None" = None,
             birth: str = "BORN", pre_seed: "Mapping[str, str] | None" = None,
             capture: bool = True, root: "str | Path | None" = None,
             ledger: "str | Path | None" = None, **kwargs: Any) -> RunResult:
    """在 ``tmp_path`` 下跑一个真实流水线的 Trial（0 付费、0 触网）。

    ``root`` / ``ledger`` 可显式指定：AC-09 要求评测器读的账本就是这次运行
    写的账本（``<root>/quota/ledger.jsonl``），缺省布局（``tmp_path/data`` +
    ``tmp_path/quota``）满足不了那条「同一份字节」的断言。
    """
    base_root = Path(root) if root is not None else Path(tmp_path) / "data"
    base_root.mkdir(parents=True, exist_ok=True)
    os.chmod(base_root, 0o700)
    root = base_root
    ledger = (Path(ledger) if ledger is not None
              else Path(tmp_path) / "quota" / "ledger.jsonl")
    spec = candidate_birth.load_task(task_id)
    trial_id = constants.trial_id(task_id, arm, rep)
    gateway = FakeGateway(ledger, trial_id=trial_id,
                          task_no=constants.TASK_NO_BY_ID[task_id])
    engine = engine or Engine()
    rows = kwargs.pop("skills_rows", None) or skills_rows(tmp_path)
    outcome = trial_harness.run_trial(
        spec=spec, arm_id=arm, rep=int(rep), root=root,
        gateway=gateway, ledger=ledger,
        capture=capture_for(spec, tmp_path) if capture else None,
        engine_runner=engine, pre_seed=pre_seed,
        skills_rows=rows, birth_runner=birth_runner(birth),
        assets_dir=Path(tmp_path) / "assets", **kwargs)
    return RunResult(outcome=outcome, base=Path(outcome.trial_dir),
                     root=root, ledger=ledger, gateway=gateway,
                     engine=engine, skills=rows)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def sha_tree(base: "str | Path") -> "dict[str, str]":
    """目录树的 ``{相对路径: sha256}``（不跟符号链接；缺失即空表）。"""
    directory = Path(base)
    if not directory.is_dir():
        return {}
    out: "dict[str, str]" = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        out[path.relative_to(directory).as_posix()] = _sha256_bytes(
            path.read_bytes())
    return out


def ledger_rows(path: "str | Path") -> "list[dict]":
    target = Path(path)
    if not target.is_file():
        return []
    return [json.loads(line) for line in
            target.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_json(path: "str | Path") -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def summary_of(base: "str | Path") -> "dict | None":
    from orchestrator.benchmark.phase281 import telemetry
    return telemetry.read_summary(base)
