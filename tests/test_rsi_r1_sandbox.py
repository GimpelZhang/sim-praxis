"""RSI-R1 T3：沙盒精确视图与容器安全面（AC16/AC17）。

全部离线：runner 是内存假实现，不触 docker；只校验 argv 与视图/输出守卫。
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import sandbox as SB

DESIGN_SRC = "print('design')\n"
ANALYSIS_SRC = "print('analysis')\n"
SERVICE_SRC = "print('service')\n"
IMAGE = "simpraxis/agentic@sha256:" + "f" * 64


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entry(root: Path, relpath: str, data: bytes) -> SB.ViewEntry:
    target = root / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return SB.ViewEntry(relpath=relpath, sha256=_sha(data))


class FakeRunner:
    def __init__(self, *, timeout=False, residue=False, write=None, exit_code=0,
                 expected_timeout=None):
        self.argv: "list[list[str]]" = []
        self.removed: "list[str]" = []
        self.timeout = timeout
        self._residue = residue
        self.exit_code = exit_code
        self.write = write or {}
        self.expected_timeout = (SB.WALL_CLOCK_S if expected_timeout is None
                                 else expected_timeout)

    def _out_dir(self, argv) -> Path:
        for i, arg in enumerate(argv):
            if arg == "-v" and argv[i + 1].endswith(":/out:rw"):
                return Path(argv[i + 1][: -len(":/out:rw")])
        raise AssertionError("argv 没有 /out:rw 挂载")

    def run(self, argv, *, timeout_s, container):
        self.argv.append(list(argv))
        assert timeout_s == self.expected_timeout
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
    assert argv[-4:] == ["python3", "/data/design.py", "/data", "/out"]
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
    assert runner.argv[0][-4:] == ["python3", "/data/analysis.py", "/data", "/out"]
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


# ---------------------------------------------------------------------------
# T1b：可信实验服务（同一 §5.4 安全面，两项帽放宽）
# ---------------------------------------------------------------------------

SPEC_DOC = {"schema": "simpraxis.rsi_r1.experiment_spec.v1",
            "schema_version": 1, "run_id": "r1-a-000", "spec_hash": "ab" * 32,
            "study_id": "study_A", "nsteps": 4, "dt_s": 0.0005,
            "mujoco_version": "3.15.0", "scene_xml": "<mujoco/>",
            "run": {"kind": "A", "settle_steps": 2, "initial_vx_mps": 0.4}}


def _canonical_spec_text(doc) -> str:
    """宿主 canonical JSON（独立于实现重算，钉住"字节可复现"这一契约）。"""
    return json.dumps(doc, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False) + "\n"


def _run_experiment(tmp_path, runner, spec=None, source=SERVICE_SRC):
    root = tmp_path / "evidence"
    root.mkdir(exist_ok=True)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return SB.run_experiment(source, dict(SPEC_DOC if spec is None else spec),
                             evidence_root=root, work_root=work,
                             runner=runner, image=IMAGE)


def test_run_experiment_uses_service_entry_and_relaxed_caps(tmp_path) -> None:
    runner = FakeRunner(write={"reply.json": '{"status":"ok"}',
                               "raw.npz": b"PK\x03\x04raw"},
                        expected_timeout=SB.PHYSICS_WALL_CLOCK_S)
    ev = _run_experiment(tmp_path, runner)
    argv = runner.argv[0]
    assert argv[-4:] == ["python3", "/data/service.py", "/data", "/out"]
    # 与设计/分析同一安全面：只换脚本名与两项帽，别的旗标不得少
    assert _flag_value(argv, "--network") == ["none"]
    assert "--read-only" in argv
    assert _flag_value(argv, "--cap-drop") == ["ALL"]
    assert _flag_value(argv, "--user") == [f"{SB.CONTAINER_UID}:{SB.CONTAINER_GID}"]
    assert _flag_value(argv, "--pids-limit") == [str(SB.PIDS_LIMIT)]
    assert ev.kind == "experiment"
    assert ev.exit_code == 0 and ev.reply == {"status": "ok"}
    # 帽：4 MiB 输出、120 s 墙钟，且真写进 evidence limits
    assert ev.limits["output_max_bytes"] == SB.PHYSICS_OUTPUT_MAX_BYTES == 4 << 20
    assert ev.limits["wall_clock_s"] == SB.PHYSICS_WALL_CLOCK_S == 120
    assert ev.limits["network"] == "none"
    # spec.json 由宿主附加，sha 与 canonical 字节严格相等（不是"随便 dump"）
    assert [i["name"] for i in ev.inputs] == ["spec.json"]
    text = _canonical_spec_text(SPEC_DOC).encode("utf-8")
    assert ev.inputs[0]["sha256"] == _sha(text)
    assert ev.inputs[0]["bytes"] == len(text)
    assert runner.removed == [ev.container]
    assert list((tmp_path / "work").iterdir()) == []


def test_run_experiment_prepares_container_modes(tmp_path) -> None:
    """容器 uid 65534 必须能读 /data 视图、写 /out（T3 旧实现只测过假 runner）。"""

    class ModeProbe(FakeRunner):
        def run(self, argv, *, timeout_s, container):
            view = Path(_flag_value(argv, "-v")[0][: -len(":/data:ro")])
            out = self._out_dir(argv)
            self.seen = {
                "view": stat.S_IMODE(os.stat(view).st_mode),
                "out": stat.S_IMODE(os.stat(out).st_mode),
                "files": {c.name: stat.S_IMODE(os.lstat(c).st_mode)
                          for c in view.iterdir()},
            }
            return super().run(argv, timeout_s=timeout_s, container=container)

    runner = ModeProbe(write={"reply.json": "{}"},
                       expected_timeout=SB.PHYSICS_WALL_CLOCK_S)
    _run_experiment(tmp_path, runner)
    # 目录必须可列（glob 找 plan-<sha12>.json）；只给穿越的 0711 在容器内
    # （uid 65534=others）会让候选始终读不到列入的输入。
    assert runner.seen["view"] == 0o755
    assert runner.seen["out"] == 0o777
    assert runner.seen["files"] == {"service.py": 0o644, "spec.json": 0o644}


def test_run_experiment_output_cap_is_physics_scoped(tmp_path) -> None:
    big = b"0" * (SB.OUTPUT_MAX_BYTES + 1)          # 1 MiB + 1：设计帽下会炸
    ok = FakeRunner(write={"reply.json": "{}", "raw.bin": big},
                    expected_timeout=SB.PHYSICS_WALL_CLOCK_S)
    ev = _run_experiment(tmp_path, ok)
    assert {o["name"] for o in ev.outputs} == {"reply.json", "raw.bin"}

    with pytest.raises(SB.SandboxError) as exc:
        _run_design(tmp_path, FakeRunner(write={"raw.bin": big}))
    assert exc.value.code == "OUTPUT_TOO_LARGE"

    over = FakeRunner(write={"reply.json": "{}",
                             "raw.bin": b"0" * (SB.PHYSICS_OUTPUT_MAX_BYTES + 1)},
                      expected_timeout=SB.PHYSICS_WALL_CLOCK_S)
    with pytest.raises(SB.SandboxError) as exc:
        _run_experiment(tmp_path, over)
    assert exc.value.code == "OUTPUT_TOO_LARGE"


def test_run_experiment_retains_payload_bytes(tmp_path) -> None:
    raw = b"PK\x03\x04raw-bytes\x00\x01"
    reply_text = '{"status":"ok","nsteps":4}'
    runner = FakeRunner(write={"reply.json": reply_text, "raw.npz": raw},
                        expected_timeout=SB.PHYSICS_WALL_CLOCK_S)
    ev = _run_experiment(tmp_path, runner)
    assert ev.blobs["raw.npz"] == raw
    assert ev.blobs["reply.json"] == reply_text.encode()
    # payload 是数据不是证据元数据：不得进 to_dict（证据包只收 sha/bytes）
    doc = ev.to_dict()
    assert "blobs" not in doc
    assert json.loads(json.dumps(doc))["kind"] == "experiment"

    # 设计/分析路径不保留 payload（候选输出永不回流宿主）
    design = _run_design(tmp_path, FakeRunner(write={"reply.json": "{}"}))
    assert dict(design.blobs) == {}


def test_run_experiment_rejects_non_serialisable_spec(tmp_path) -> None:
    doc = dict(SPEC_DOC)
    doc["run"] = {"kind": "A", "nan": float("nan")}
    with pytest.raises(ValueError):
        _run_experiment(tmp_path, FakeRunner(), spec=doc)


def test_container_argv_matches_candidate_contract(tmp_path) -> None:
    """容器 argv 必须**真的**把视图/输出根交给候选程序（T6 probe 缺陷回归）。

    排演台 ``LocalRunner`` 一直是 ``[script, view, out]``，容器路径曾漏掉后两个
    参数并且三处断言把错误 argv 当正确——本条把契约钉在两条路径共同的语义上：
    脚本之后必须紧跟 ``argv[1]=/data``（只读视图根）与 ``argv[2]=/out``。
    """
    seen = {}

    class ModeProbe(FakeRunner):
        def run(self, argv, *, timeout_s, container):
            view = Path(_flag_value(argv, "-v")[0][: -len(":/data:ro")])
            seen["mode"] = stat.S_IMODE(os.stat(view).st_mode)
            seen["listable"] = sorted(p.name for p in view.iterdir())
            return super().run(argv, timeout_s=timeout_s, container=container)

    runner = ModeProbe(write={"reply.json": "{}"})
    _run_design(tmp_path, runner)
    argv = runner.argv[0]
    assert argv[-4] == "python3"
    assert argv[-3].startswith("/data/"), argv[-3]
    assert argv[-2] == "/data"
    assert argv[-1] == "/out"
    mounts = _flag_value(argv, "-v")
    assert any(m.endswith(":/data:ro") for m in mounts)
    assert any(m.endswith(":/out:rw") for m in mounts)
    # 容器 uid 65534=others：视图目录必须 r+x（glob 才看得见列入的文件）
    assert seen["mode"] & 0o005 == 0o005, oct(seen["mode"])
    assert seen["listable"] == ["design.py"]
