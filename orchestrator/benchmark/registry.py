"""Benchmark 注册表（Plan Task 1 / T1.5 第 4 步）。

职责边界与 ``orchestrator/skills/registry.py`` 同构：**只扫目录、只读 YAML/
JSON、只返回元数据**——不执行 ``solution.py``、不 import 内容层代码、不跑
subprocess（AC-T1-7 用 monkeypatch 机判这条）。任何不合规一律异常：

- 单文件不合规 → ``TaskSpecError``（由 ``task_spec`` 抛出，原样上抛）；
- ``task_id`` 重复 / 任务目录为空 / 清单（``manifest.yaml``）与磁盘不一致
  → ``RegistryError``。
"""
from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import yaml

from orchestrator.benchmark import task_spec as ts

SNAPSHOT_SCHEMA_ID = "simpraxis.benchmark_registry_snapshot.v1"


class RegistryError(RuntimeError):
    """注册表层面的不合规（重复 ID、清单不符、目录为空）。"""


class BenchmarkRegistry:
    """``benchmarks/research_engineering_v1`` 的只读注册表。"""

    def __init__(self, root: "str | Path | None" = None) -> None:
        self.root = Path(root) if root is not None else ts.BENCHMARK_ROOT
        self._cache: "dict[str, ts.TaskSpec] | None" = None

    # ------------------------------------------------------------------ 路径
    @property
    def tasks_dir(self) -> Path:
        return self.root / "tasks"

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.yaml"

    # ------------------------------------------------------------------ 加载
    def load_all(self, *, verify_manifest: bool = True) -> "Mapping[str, ts.TaskSpec]":
        """扫描 ``tasks/*/task.yaml`` 并全量校验；结果缓存且只读。"""
        if self._cache is None:
            if not self.tasks_dir.is_dir():
                raise RegistryError(f"任务目录不存在：{self.tasks_dir}")
            paths = sorted(self.tasks_dir.glob("*/task.yaml"))
            if not paths:
                raise RegistryError(f"任务目录为空：{self.tasks_dir}")
            specs: "dict[str, ts.TaskSpec]" = {}
            for path in paths:
                spec = ts.load_task_spec(path, criteria_dir=self.root / "criteria")
                if spec.task_id in specs:
                    other = specs[spec.task_id].task_file
                    raise RegistryError(
                        f"task_id 重复：{spec.task_id} 同时出现在 "
                        f"{other} 与 {spec.task_file}")
                specs[spec.task_id] = spec
            self._cache = dict(sorted(specs.items()))
        if verify_manifest:
            self.check_manifest()
        return MappingProxyType(self._cache)

    def get(self, task_id: str) -> ts.TaskSpec:
        specs = self.load_all()
        if task_id not in specs:
            raise RegistryError(
                f"未登记的任务：{task_id!r}（已登记：{sorted(specs)}）")
        return specs[task_id]

    def list_tasks(self) -> "tuple[str, ...]":
        return tuple(sorted(self.load_all()))

    # ------------------------------------------------------------------ 清单
    def criteria_locks(self) -> "dict[str, str]":
        specs = self.load_all(verify_manifest=False)
        return {tid: spec.criteria_lock_sha256 for tid, spec in specs.items()}

    def criteria_aggregate_sha256(self) -> str:
        return ts.criteria_aggregate_sha256(self.criteria_locks())

    def manifest_sha256(self) -> str:
        """对排序后的 ``(task_id, task.yaml sha256)`` 列表做确定性 JSON + sha256。"""
        specs = self.load_all(verify_manifest=False)
        payload = [[tid, specs[tid].task_file_sha256] for tid in sorted(specs)]
        return ts.sha256_bytes(ts.canonical_json(payload).encode("utf-8"))

    def read_manifest(self) -> dict:
        if not self.manifest_path.is_file():
            raise RegistryError(f"清单不存在：{self.manifest_path}")
        try:
            doc = yaml.safe_load(self.manifest_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise RegistryError(f"清单不是合法 YAML：{exc}") from exc
        if not isinstance(doc, dict):
            raise RegistryError(f"清单顶层必须是对象：{self.manifest_path}")
        return doc

    def check_manifest(self) -> dict:
        """清单与磁盘的一致性检查；返回结构化结论（不抛异常，由调用方判红）。"""
        problems: "list[str]" = []
        try:
            doc = self.read_manifest()
        except RegistryError as exc:
            return {"ok": False, "problems": [str(exc)], "declared": {}}

        if doc.get("schema") != ts.MANIFEST_SCHEMA_ID:
            problems.append(
                f"manifest.schema={doc.get('schema')!r} != {ts.MANIFEST_SCHEMA_ID!r}")
        if not isinstance(doc.get("version"), int) or isinstance(doc.get("version"), bool):
            problems.append(f"manifest.version 必须是整数：{doc.get('version')!r}")
        listed = doc.get("tasks")
        if not isinstance(listed, list) or not all(isinstance(t, str) for t in listed):
            problems.append(f"manifest.tasks 必须是字符串数组：{listed!r}")
            listed = []
        if listed != sorted(listed):
            problems.append("manifest.tasks 必须按 task_id 字典序排列")
        on_disk = sorted(self.load_all(verify_manifest=False))
        if listed != on_disk:
            problems.append(f"manifest.tasks={listed} 与磁盘任务={on_disk} 不一致")
        aggregate = self.criteria_aggregate_sha256()
        declared = doc.get("criteria_lock")
        if declared != aggregate:
            problems.append(
                f"manifest.criteria_lock 不符：声明 {declared!r}，重算 {aggregate}")
        return {
            "ok": not problems,
            "problems": problems,
            "declared": {"tasks": listed, "criteria_lock": declared},
            "computed": {"tasks": on_disk, "criteria_lock": aggregate},
        }

    # ------------------------------------------------------------------ 快照
    def snapshot(self) -> dict:
        """注册表快照（Task 1 证据件 + AC-T1-6 确定性比较对象）。"""
        specs = self.load_all(verify_manifest=False)
        return {
            "schema": SNAPSHOT_SCHEMA_ID,
            "manifest_sha256": self.manifest_sha256(),
            "criteria_lock": self.criteria_aggregate_sha256(),
            "tasks": [specs[tid].to_json() for tid in sorted(specs)],
        }

    def snapshot_json(self) -> str:
        return ts.canonical_json(self.snapshot())


def load_registry(root: "str | Path | None" = None) -> BenchmarkRegistry:
    return BenchmarkRegistry(root)
