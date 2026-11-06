"""P281-2 出生隔离测试的共用夹具：候选 workdir、规格、跑一次出生。

为什么单独一个模块而不是让两个测试文件互相 import：``test_phase281_birth_isolation``
（AC-01..11/15）与 ``test_phase281_runtime_attestation``（AC-12..14）要共用同一份
「正常候选」与同一个 ``run_birth`` 入口（AC-13 的时序断言必须在**同一次**出生运行
上取证）。测试模块之间互相 import 会让 pytest 的模块身份与收集顺序变成隐式依赖，
与 ``phase281_parity.py`` 的先例一样，抽成显式夹具模块。

候选是**鸭子类型**的：``mujoco_push`` 契约只读 ``action_dim``/``step``/``dt`` 三个
属性（``orchestrator/benchmark/contracts.py:145``），所以一份纯 Python 的
``_Scene`` 就能在**不 import mujoco** 的前提下走完 B0–B4——这样默认车道（不依赖
活数据层/GPU）也能建 parity 证据，而不是把整条 AC-10 塞进 ``kb_integration``。

候选侧要写的文件一律用**绝对路径**：子进程的 cwd 是父进程临时建的
``.birth_tmp/birth-*``（跑完即删），相对路径会写进一个事后不存在的目录——
这不是刁难候选，是出生隔离本身的拓扑。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from orchestrator.benchmark.phase281 import birth_protocol as P
from orchestrator.benchmark.phase281 import candidate_birth as cb

__all__ = (
    "BIRTH_ALL", "CANDIDATE_OK", "parse_wall", "read_events",
    "run_birth", "spec", "wait_gone", "write_workdir",
)

#: 五层全必需（M1 的 ``birth_contract`` 就是这个形状）
BIRTH_ALL = {"import_required": True, "construction_required": True,
             "contract_required": True, "minimal_step_required": True}

#: 正常候选：``build_scene()`` 返回一个可步进、每步都推进的鸭子类型对象。
CANDIDATE_OK = '''\
class _Scene:
    action_dim = 3
    dt = 0.002

    def __init__(self):
        self.n = 0

    def step(self, action):
        self.n += 1
        return {"x": float(self.n), "y": 0.0}


def build_scene():
    return _Scene()
'''


def write_workdir(path: "str | Path", files: "dict[str, str]",
                  *, mode: int = 0o700) -> Path:
    """建一个 0700 的候选工作目录并写入文件（内容里的 ``__WORKDIR__`` 会被替换）。"""
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, mode)
    for name, text in files.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text.replace("__WORKDIR__", str(directory)),
                          encoding="utf-8")
    return directory


def spec(workdir: "str | Path", *, level: str = "ALL",
         limits: "cb.LimitsV2 | None" = None, contract: str = "mujoco_push",
         birth_contract: "dict[str, bool] | None" = None,
         entry: str = "solution.py", min_bytes: int = 16) -> cb.BirthSpecV2:
    return cb.BirthSpecV2(
        level=level, entry_relpath=entry,
        limits=limits or cb.LimitsV2(wall_s=120.0),
        workdir=Path(workdir), contract=contract, entry_kind="python_script",
        birth_contract=dict(BIRTH_ALL if birth_contract is None else birth_contract),
        min_bytes=min_bytes)


def run_birth(workdir: "str | Path", trial_dir: "str | Path", *,
              write: bool = True, **kwargs):
    """跑一次出生检查并（缺省）落盘，返回 ``(result, doc, validator)``。

    读回的是**文件里的事实**而不是内存对象：AC-11 要验 0600/0700，AC-04 要验
    父进程只有 ``<trial_dir>/birth.json`` 这一个读点，两者都只有从盘上读才作数。
    """
    if isinstance(trial_dir, (str, Path)):
        trial = Path(trial_dir)
        trial.mkdir(parents=True, exist_ok=True)
        os.chmod(trial, 0o700)
    validator = cb.CandidateBirthValidator(trial_dir=trial, trial_id=trial.name)
    result = validator.run(spec(workdir, **kwargs))
    if not write:
        return result, result.to_json(), validator
    saved = validator.write(result)
    return result, cb.load_birth(saved["birth_path"]), validator

def read_events(path: "str | Path") -> "tuple[list[dict], list[str]]":
    return P.read_events(path)


def parse_wall(text: str) -> datetime:
    """解析协议墙钟（严格 ``Z``）——AC-13 的「早于」比对靠它，不用字符串比较。"""
    return datetime.strptime(str(text), P._ISO)


def wait_gone(pid: int, timeout: float = 5.0) -> bool:
    """等一个 pid 从 ``/proc`` 消失（进程组清理的机判面，AC-06/07）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not Path(f"/proc/{int(pid)}").exists():
            return True
        time.sleep(0.05)
    return not Path(f"/proc/{int(pid)}").exists()


def write_json(path: "str | Path", doc) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(doc, ensure_ascii=False, indent=2,
                                 sort_keys=True) + "\n", encoding="utf-8")
    return target
