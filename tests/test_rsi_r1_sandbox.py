"""RSI-R1 T3：沙盒精确视图与容器安全面（AC16/AC17）。

全部离线：runner 是内存假实现，不触 docker；只校验 argv 与视图/输出守卫。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import sandbox as SB

DESIGN_SRC = "print('design')\n"
ANALYSIS_SRC = "print('analysis')\n"
IMAGE = "simpraxis/agentic@sha256:" + "f" * 64


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entry(root: Path, relpath: str, data: bytes) -> SB.ViewEntry:
    target = root / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return SB.ViewEntry(relpath=relpath, sha256=_sha(data))


class FakeRunner:
    def __init__(self, *, timeout=False, residue=False, write=None, exit_code=0):
        self.argv: "list[list[str]]" = []
        self.removed: "list[str]" = []
        self.timeout = timeout
        self._residue = residue
        self.exit_code = exit_code
        self.write = write or {}

    def _out_dir(self, argv) -> Path:
        for i, arg in enumerate(argv):
            if arg == "-v" and argv[i + 1].endswith(":/out:rw"):
                return Path(argv[i + 1][: -len(":/out:rw")])
        raise AssertionError("argv 没有 /out:rw 挂载")

    def run(self, argv, *, timeout_s, container):
        self.argv.append(list(argv))
        assert timeout_s == SB.WALL_CLOCK_S
        out = self._out_dir(argv)
        for name, payload in self.write.items():
            target = out / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(payload, bytes):
                target.write_bytes(payload)
            else:
                target.write_text(payload, encoding="utf-8")
        return SB.RunResult(exit_code=self.exit_code, stdout_tail="ok",
                            stderr_tail="", timed_out=self.timeout)

    def residue(self, container):
        return self._residue

    def force_remove(self, container):
        self.removed.append(container)


# ---------------------------------------------------------------------------
# 视图构造
# ---------------------------------------------------------------------------

def test_view_copies_only_listed_files_with_hash_check(tmp_path) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    (root / "private").mkdir()
    e1 = _entry(root, "observations/run-1.jsonl", b'{"a":1}\n')
    e2 = _entry(root, "observations/run-2.jsonl", b'{"a":2}\n')
    view = SB.build_view(root, [e1, e2])
    assert view.path.is_dir()
    assert sorted(p.name for p in view.path.iterdir()) == \
        ["run-1.jsonl", "run-2.jsonl"]
    for path in view.path.iterdir():
        assert (path.stat().st_mode & 0o777) == 0o600
    assert set(view.file_map()) == {"run-1.jsonl", "run-2.jsonl"}
    # 未列出文件不在视图里
    assert not (view.path / "run-3.jsonl").exists()


def test_view_rejects_traversal_private_and_duplicates(tmp_path) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    cases = [
        (SB.ViewEntry("../escape.json", "a" * 64), "PATH_TRAVERSAL"),
        (SB.ViewEntry("/etc/passwd", "a" * 64), "PATH_TRAVERSAL"),
        (SB.ViewEntry("confirm/seed.bin", "a" * 64), "PRIVATE_INPUT"),
        (SB.ViewEntry("observations/../secret", "a" * 64), "PATH_TRAVERSAL"),
    ]
    for entry, code in cases:
        with pytest.raises(SB.SandboxError) as exc:
            SB.build_view(root, [entry])
        assert exc.value.code == code, f"{entry.relpath} 期望 {code}"
    # 重名（不同目录同 basename）拒绝
    a = _entry(root, "observations/run.jsonl", b"1")
    b = _entry(root, "trials/run.jsonl", b"2")
    with pytest.raises(SB.SandboxError) as exc:
        SB.build_view(root, [a, b])
    assert exc.value.code == "DUPLICATE_ID"


def test_view_rejects_symlink_hardlink_and_hash_drift(tmp_path) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    real = _entry(root, "observations/real.jsonl", b"payload")
    link = root / "observations" / "link.jsonl"
    link.symlink_to(root / "observations" / "real.jsonl")
    with pytest.raises(SB.SandboxError) as exc:
        SB.build_view(root, [SB.ViewEntry("observations/link.jsonl", real.sha256)])
    assert exc.value.code == "SYMLINK_FORBIDDEN"

    hard = root / "observations" / "hard.jsonl"
    os.link(root / "observations" / "real.jsonl", hard)
    with pytest.raises(SB.SandboxError) as exc:
        SB.build_view(root, [SB.ViewEntry("observations/hard.jsonl", real.sha256)])
    assert exc.value.code == "HARDLINK_FORBIDDEN"

    drift = _entry(root, "observations/drift.jsonl", b"other")
    with pytest.raises(SB.SandboxError) as exc:
        SB.build_view(root, [SB.ViewEntry("observations/drift.jsonl", "0" * 64)])
    assert exc.value.code == "HASH_MISMATCH"
    # 失败后不得留下半成品视图目录
    assert [p for p in root.iterdir() if p.name.startswith("view-")] == []


# ---------------------------------------------------------------------------
# run_design / run_analysis：argv 安全面与输出守卫
# ---------------------------------------------------------------------------

def _run_design(tmp_path, runner, inputs=(), image=IMAGE, source=DESIGN_SRC):
    root = tmp_path / "evidence"
    root.mkdir(exist_ok=True)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return SB.run_design(source, list(inputs), evidence_root=root,
                         work_root=work, runner=runner, image=image)


def _flag_value(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def test_run_design_passes_every_security_flag(tmp_path) -> None:
    runner = FakeRunner(write={"reply.json": '{"ok":true}', "out.json": '{"x":1}'})
    ev = _run_design(tmp_path, runner)
    argv = runner.argv[0]
    assert _flag_value(argv, "--network") == ["none"]
    assert "--read-only" in argv
    assert _flag_value(argv, "--cap-drop") == ["ALL"]
    assert _flag_value(argv, "--user") == [f"{SB.CONTAINER_UID}:{SB.CONTAINER_GID}"]
    assert _flag_value(argv, "--pids-limit") == [str(SB.PIDS_LIMIT)]
    assert _flag_value(argv, "--memory") == [SB.MEMORY_LIMIT]
    assert _flag_value(argv, "--cpus") == [SB.CPU_LIMIT]
    assert "--security-opt" in argv and "no-new-privileges" in argv
    assert _flag_value(argv, "-v")[-1].endswith(":/out:rw")
    assert _flag_value(argv, "-v")[0].endswith(":/data:ro")
    assert IMAGE in argv and "docker" in argv[0]
    assert argv[-2:] == ["python3", "/data/design.py"]
    # 不传凭据/网关/宿主 docker.sock
    joined = " ".join(argv)
    for needle in ("ANTHROPIC", "DOCKER_HOST", "docker.sock", "AWS_", "/root"):
        assert needle not in joined
    assert ev.exit_code == 0 and ev.reply == {"ok": True}
    assert {o["name"] for o in ev.outputs} == {"reply.json", "out.json"}
    assert ev.source_sha256 == _sha(DESIGN_SRC.encode())
    # 视图与 out 目录在 finally 里清理，容器被回收
    assert list((tmp_path / "work").iterdir()) == []
    assert runner.removed == [ev.container]


def test_run_design_mounts_only_manifest_inputs(tmp_path) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    entry = _entry(root, "observations/run-1.jsonl", b'{"run":"1"}\n')
    _entry(root, "observations/run-2.jsonl", b'{"run":"2"}\n')   # 未列入，不得进视图
    runner = FakeRunner(write={"reply.json": "{}"})
    ev = _run_design(tmp_path, runner, inputs=[entry])
    assert [i["name"] for i in ev.inputs] == ["run-1.jsonl"]
    assert ev.inputs[0]["sha256"] == entry.sha256


def test_run_analysis_uses_same_sandbox_and_script_name(tmp_path) -> None:
    runner = FakeRunner(write={"reply.json": '{"claim":"x"}'})
    root = tmp_path / "evidence"
    root.mkdir()
    entry = _entry(root, "observations/run-1.jsonl", b"{}")
    work = tmp_path / "work"
    work.mkdir()
    ev = SB.run_analysis(ANALYSIS_SRC, [entry], evidence_root=root,
                         work_root=work, runner=runner, image=IMAGE)
    assert runner.argv[0][-2:] == ["python3", "/data/analysis.py"]
    assert ev.kind == "analysis" and ev.reply == {"claim": "x"}
    assert "--network" in runner.argv[0]


def test_output_and_reply_guards(tmp_path) -> None:
    runner = FakeRunner(write={"big.bin": b"0" * (SB.OUTPUT_MAX_BYTES + 1)})
    with pytest.raises(SB.SandboxError) as exc:
        _run_design(tmp_path, runner)
    assert exc.value.code == "OUTPUT_TOO_LARGE"
    assert runner.removed and runner.removed[0].startswith("rsir1-design-")

    runner = FakeRunner(write={"reply.json": "x" * (SB.REPLY_MAX_BYTES + 1)})
    with pytest.raises(SB.SandboxError) as exc:
        _run_design(tmp_path, runner)
    assert exc.value.code == "REPLY_TOO_LARGE"

    runner = FakeRunner(write={"reply.json": "not json"})
    with pytest.raises(SB.SandboxError) as exc:
        _run_design(tmp_path, runner)
    assert exc.value.code == "REPLY_INVALID"


def test_output_symlink_is_refused(tmp_path) -> None:
    class LinkRunner(FakeRunner):
        def run(self, argv, *, timeout_s, container):
            out = self._out_dir(argv)
            (out / "real.json").write_text("{}", encoding="utf-8")
            (out / "alias.json").symlink_to(out / "real.json")
            return SB.RunResult(exit_code=0)

    with pytest.raises(SB.SandboxError) as exc:
        _run_design(tmp_path, LinkRunner())
    assert exc.value.code == "SYMLINK_FORBIDDEN"


def test_timeout_and_residue_are_evidence_failures(tmp_path) -> None:
    runner = FakeRunner(timeout=True)
    with pytest.raises(SB.SandboxError) as exc:
        _run_design(tmp_path, runner)
    assert exc.value.code == "TIMEOUT"
    assert runner.removed, "超时必须回收本轮自有容器"

    runner = FakeRunner(residue=True, write={"reply.json": "{}"})
    with pytest.raises(SB.SandboxError) as exc:
        _run_design(tmp_path, runner)
    assert exc.value.code == "CONTAINER_RESIDUE"


def test_source_and_image_validation(tmp_path) -> None:
    with pytest.raises(SB.SandboxError) as exc:
        _run_design(tmp_path, FakeRunner(), image="")
    assert exc.value.code == "IMAGE_MISSING"
    with pytest.raises(SB.SandboxError) as exc:
        _run_design(tmp_path, FakeRunner(), source="")
    assert exc.value.code == "INPUT_INVALID"
    with pytest.raises(SB.SandboxError) as exc:
        _run_design(tmp_path, FakeRunner(), source="x" * (SB.SOURCE_MAX_BYTES + 1))
    assert exc.value.code == "SOURCE_TOO_LARGE"
    with pytest.raises(SB.SandboxError) as exc:
        _run_design(tmp_path, FakeRunner(), source="a\x00b")
    assert exc.value.code == "INPUT_INVALID"


def test_script_name_collision_with_input_is_refused(tmp_path) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    entry = _entry(root, "observations/design.py", b"print(1)")
    with pytest.raises(SB.SandboxError) as exc:
        _run_design(tmp_path, FakeRunner(), inputs=[entry])
    assert exc.value.code == "DUPLICATE_ID"


def test_evidence_document_is_serialisable(tmp_path) -> None:
    runner = FakeRunner(write={"reply.json": '{"ok":1}'})
    ev = _run_design(tmp_path, runner)
    doc = ev.to_dict()
    assert json.loads(json.dumps(doc))["kind"] == "design"
    assert doc["limits"]["network"] == "none"
    assert doc["limits"]["cap_drop"] == "ALL"
    assert "path" not in doc["outputs"][0]
