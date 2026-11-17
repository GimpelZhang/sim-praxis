"""RSI-RV 修订状态：把"已指出的错误"和"已提交的草稿"从自然语言历史里拿出来。

背景（2026-11-17 零 POST 核验）：v3 第 4 呼的 13 KB 提交回复进入历史时被
``clip_text(4096)`` 截断，修正后的 ``primary_results`` 不在后续请求的可见文本里；
下一步请求只能看到摘要或残缺正文，已修正字段因而可能回退。

本模块只做四件事，**不代写候选内容**：

1. 把候选每次 ``submit_claim`` 的 report+claims 原样落盘（规范 JSON 字节）；
2. 逐次记录 precommit 的结构/类型错误与通过预检的顶层字段；
3. 由宿主按字节算出候选自己在上两版之间改了什么（``candidate_patch``）；
4. 提供一个确定性文本块，在每个请求里逐字重现"当前草稿 + 未解决错误"，
   使历史裁剪无法吞掉修订状态。

科学内容、结论、统计一律来自候选自己的提交；宿主不补、不改、不选。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from orchestrator.common.fsutil import ensure_private_dir
from orchestrator.rsi_r1 import research_report as RR
from orchestrator.rsi_r1 import rv_contract as RV

DRAFT_STATE_SCHEMA = f"{RV.SCHEMA_PREFIX}.draft_state.v1"
DRAFT_ATTEMPT_SCHEMA = f"{RV.SCHEMA_PREFIX}.draft_attempt.v1"

#: 请求里"当前草稿"段的字节上限；超限显式标记，不静默丢。
DRAFT_CONTEXT_MAX_BYTES = 16384

CONTEXT_HEADER = "### 修订状态（宿主持久化；内容全部来自你自己的历次提交）"
DRAFT_HEADER = "### 当前草稿（逐字，来自你最近一次提交）"


def _canonical(value: Any) -> str:
    return RV.canonical(value).decode("utf-8")


def _sha(text: str) -> str:
    return RV.sha_bytes(text.encode("utf-8"))


def _field_of(problem: str) -> str:
    """从 precommit 文本取顶层字段名（``primary_results[0].x: ...`` → ``primary_results``）。"""
    head = str(problem).split(":", 1)[0]
    for sep in ("[", "."):
        head = head.split(sep, 1)[0]
    return head.strip()


@dataclass
class DraftBook:
    """按 study 持久化候选草稿；幂等、可离线复核。"""

    ctx: Any                     # RVContext（study 根）
    study: str
    task: Any
    prefix: str = "draft"

    # -- 路径 ---------------------------------------------------------------
    def _state_rel(self) -> str:
        return f"{self.prefix}/state.json"

    def _attempts_rel(self) -> str:
        return f"{self.prefix}/attempts.jsonl"

    def _draft_rel(self, version: int) -> str:
        return f"{self.prefix}/v{int(version):04d}.json"

    # -- 读 -----------------------------------------------------------------
    def state(self) -> "dict[str, Any] | None":
        path = self.ctx.relative(self._state_rel())
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _draft_doc(self, rel: str) -> "dict[str, Any] | None":
        path = self.ctx.relative(rel)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    # -- 差异 ---------------------------------------------------------------
    @staticmethod
    def _patch(prev: "Mapping[str, Any] | None",
               payload: Mapping[str, Any]) -> dict[str, Any]:
        prev_report = (prev or {}).get("report")
        prev_report = prev_report if isinstance(prev_report, Mapping) else {}
        report = payload.get("report")
        report = report if isinstance(report, Mapping) else {}
        prev_keys = {k: _sha(_canonical(v)) for k, v in prev_report.items()}
        keys = {k: _sha(_canonical(v)) for k, v in report.items()}
        changed = sorted(k for k in keys if k in prev_keys
                         and keys[k] != prev_keys[k])
        added = sorted(k for k in keys if k not in prev_keys)
        removed = sorted(k for k in prev_keys if k not in keys)
        prev_claims = (prev or {}).get("claims")
        claims = payload.get("claims")
        return {"changed": changed, "added": added, "removed": removed,
                "claims_count": [len(prev_claims or []), len(claims or [])]}

    # -- 写 -----------------------------------------------------------------
    def record(self, *, report: Any, claims: "Sequence[Any] | None",
               problems: "Sequence[str]",
               clock: Any = RV.utc_now) -> dict[str, Any]:
        """记录一次提交尝试；返回写入后的状态。"""
        payload = {"report": report, "claims": list(claims or [])}
        payload_text = _canonical(payload)
        draft_sha = _sha(payload_text)
        prev = self.state() or {}
        if prev.get("draft_sha") == draft_sha:
            version = int(prev.get("draft_version") or 1)
            draft_rel = str(prev.get("draft_rel") or self._draft_rel(version))
            patch = dict(prev.get("candidate_patch") or {})
        else:
            version = int(prev.get("draft_version") or 0) + 1
            draft_rel = self._draft_rel(version)
            prev_doc = self._draft_doc(str(prev.get("draft_rel") or "")) if prev else None
            patch = self._patch(prev_doc, payload)
            self.ctx.write_text(draft_rel, payload_text + "\n")
        errors = [str(p) for p in problems]
        failed = {_field_of(p) for p in errors}
        if not isinstance(report, Mapping):
            validated: "list[str]" = []
        else:
            validated = sorted(k for k in RR.TOP_KEYS
                               if k not in failed and k in report)
        state = {
            "schema": DRAFT_STATE_SCHEMA, "schema_version": 1,
            "study": self.study,
            "draft_version": version,
            "draft_sha": draft_sha,
            "public_schema_sha": RV.canonical_sha(RR.public_schema()),
            "unresolved_errors": errors,
            "last_validated_fields": validated,
            "candidate_patch": patch,
            "attempts": int(prev.get("attempts") or 0) + 1,
            "sealed": not errors,
            "draft_rel": draft_rel,
            "updated_at_utc": clock(),
        }
        self.ctx.write_json(self._state_rel(), state)
        row = {"schema": DRAFT_ATTEMPT_SCHEMA, "schema_version": 1,
               "study": self.study, "attempt": state["attempts"],
               "draft_version": version, "draft_sha": draft_sha,
               "n_problems": len(errors), "sealed": state["sealed"],
               "ts_iso": state["updated_at_utc"]}
        path = self.ctx.relative(self._attempts_rel())
        ensure_private_dir(path.parent)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(_canonical(row) + "\n")
            fh.flush()
        return state

    # -- 请求中的呈现 -------------------------------------------------------
    def render_context(self, *, max_bytes: int = DRAFT_CONTEXT_MAX_BYTES) -> str:
        """确定性文本块；无草稿时返回空串（零差异）。"""
        state = self.state()
        if not state:
            return ""
        lines = [CONTEXT_HEADER,
                 f"- draft_version: {state['draft_version']} / attempts: "
                 f"{state['attempts']} / sealed: {str(bool(state['sealed'])).lower()}",
                 f"- draft_sha: {state['draft_sha']}",
                 f"- public_schema_sha: {state['public_schema_sha']}"]
        errors = list(state.get("unresolved_errors") or [])
        if errors:
            lines.append("- unresolved_errors:")
            lines.extend(f"  - {e}" for e in errors)
        else:
            lines.append("- unresolved_errors: []（上一版通过结构预检）")
        validated = list(state.get("last_validated_fields") or [])
        lines.append("- last_validated_fields: "
                     + json.dumps(validated, ensure_ascii=False))
        patch = state.get("candidate_patch") or {}
        lines.append("- candidate_patch（相对上一版草稿，宿主按字节比较）: "
                     + json.dumps(patch, ensure_ascii=False, sort_keys=True))
        doc = self._draft_doc(str(state.get("draft_rel") or ""))
        if doc is None:
            lines.append("- 当前草稿文件缺失（不得凭记忆改写字段）")
            return "\n".join(lines)
        text = _canonical(doc)
        raw = text.encode("utf-8")
        if len(raw) > int(max_bytes):
            sizes = {k: len(_canonical(v).encode("utf-8"))
                     for k, v in (doc.get("report") or {}).items()}
            lines.append(f"- 当前草稿 {len(raw)} B 超过呈现上限 {int(max_bytes)} B："
                         "下列逐键字节数，完整字节在 draft 文件里")
            lines.append("  - key_bytes: "
                         + json.dumps(sizes, ensure_ascii=False, sort_keys=True))
        else:
            lines.extend([DRAFT_HEADER, text])
        return "\n".join(lines)
