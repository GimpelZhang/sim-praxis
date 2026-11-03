"""技能库索引（Phase 2.7 Task 2 / D2-3）：只解析 manifest，永不 import 技能代码。"""
from __future__ import annotations

from .registry import (  # noqa: F401
    DEFAULT_KB_ROOT,
    PACKAGE_NAME,
    SKILLS_SCHEMA_ID,
    SkillEntry,
    SkillsError,
    env_spec_skills_block,
    get_skill,
    list_skills,
    load_skills_manifest,
    manifest_sha12,
    skills_root,
    staged_path_for,
    verify_provenance,
)

__all__ = [
    "DEFAULT_KB_ROOT",
    "PACKAGE_NAME",
    "SKILLS_SCHEMA_ID",
    "SkillEntry",
    "SkillsError",
    "env_spec_skills_block",
    "get_skill",
    "list_skills",
    "load_skills_manifest",
    "manifest_sha12",
    "skills_root",
    "staged_path_for",
    "verify_provenance",
]
