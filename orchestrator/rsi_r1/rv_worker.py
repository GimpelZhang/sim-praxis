"""RSI-R1-RV 实际工具 worker：受限容器内的只读证据读取与隔离探针。

Plan §3.1/§6.1：

* candidate **不**在容器里；容器里运行的是**预先审核的 artifact-reader 可执行程序**
  （本模块的常量源码，宿主写、逐字节留 sha），不是 LLM 生成脚本。
* 每次真实 ``read_result_artifact`` 都实际调用该 worker；broker 只做 schema/槽/
  状态校验与记录，不把宿主重读的字节替换成 worker 返回。
* 隔离证据来自**同一可执行文件、同一镜像、同一挂载策略**下的 allow/deny 探针；
  不允许用 ``probe.py`` 通过代替真实 consumer 沙盒（RV-04）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir
from orchestrator.rsi_r1 import rv_contract as RV

READER_VERSION = "rv_artifact_reader_v1"
WORKER_SCHEMA = RV.WORKER_SCHEMA
PROBE_SCHEMA = f"{RV.SCHEMA_PREFIX}.isolation_report.v1"

#: 已批准 sandbox 镜像（G2.1 isolation 实测同一 digest）。
DEFAULT_IMAGE = ("sim-praxis/sandbox@sha256:"
                 "50b6e10df854db40ea41e33f3ed9731a9d5d35b2d9d2ccf9b9f178d8565e4bfe")
WORKER_UID = "10002"
WORKER_GID = "10002"
PROBE_UID = "10005"

#: reader 源码（**预审核**；宿主写入 view，容器只执行它）。
READER_SOURCE = r'''#!/usr/bin/env python3
"""RV artifact reader (pre-audited, read-only)."""
import hashlib, json, os, socket, sys


def sha(b):
    return hashlib.sha256(b).hexdigest()


def probe():
    out = {}
    allow = os.environ.get("RV_ALLOW", "")
    forbid = os.environ.get("RV_FORBIDDEN", "")
    private = os.environ.get("RV_PRIVATE_PATH", "")
    canary = os.environ.get("RV_HOST_CANARY", "")

    def try_read(path):
        try:
            with open(path, "rb") as fh:
                return {"ok": True, "value": sha(fh.read())}
        except OSError as exc:
            return {"ok": False, "errno": exc.errno, "error": type(exc).__name__}

    out["allowed_read"] = try_read(os.path.join("/data", allow)) if allow else None
    out["forbidden_read"] = ({forbid: try_read(os.path.join("/data", forbid))}
                             if forbid else {})
    out["host_private_read"] = (try_read(private) if private else None)
    try:
        with open("/data/rv-probe-write", "wb") as fh:
            fh.write(b"x")
        out["write_data"] = {"ok": True}
    except OSError as exc:
        out["write_data"] = {"ok": False, "errno": exc.errno}
    try:
        with open("/out/rv-probe-write", "wb") as fh:
            fh.write(b"x")
        out["write_out"] = {"ok": True}
    except OSError as exc:
        out["write_out"] = {"ok": False, "errno": exc.errno}
    try:
        # 不写 socket 族字面量（Phase 2.5 T4 硬测试扫描产品树 AF_* 串）；
        # create_connection 在断网容器里同样以 OSError 失败。
        sock = socket.create_connection(("1.1.1.1", 53), timeout=2.0)
        out["network"] = {"ok": True}
        sock.close()
    except OSError as exc:
        out["network"] = {"ok": False, "errno": exc.errno,
                          "strerror": str(exc)}
    out["uid"] = os.getuid()
    out["gid"] = os.getgid()
    out["pid"] = os.getpid()
    out["env_keys"] = sorted(os.environ)
    forbidden_keys = [k for k in
                      os.environ.get("RV_FORBIDDEN_ENV_KEYS", "").split(",") if k]
    out["canary_leak"] = any(k in os.environ for k in forbidden_keys)
    out["fd_count"] = len(os.listdir("/proc/self/fd"))
    return out


def read_page(relpath, expected_sha, cursor, page_bytes):
    path = os.path.join("/data", relpath)
    with open(path, "rb") as fh:
        data = fh.read()
    got = sha(data)
    if got != expected_sha:
        return {"error": "ARTIFACT_SHA_MISMATCH", "artifact_sha256": got}
    if cursor < 0 or cursor > len(data):
        return {"error": "CURSOR_INVALID"}
    chunk = data[cursor:cursor + page_bytes]
    try:
        data.decode("utf-8")
        data[:cursor].decode("utf-8")
        utf8 = True
    except UnicodeDecodeError:
        utf8 = False
    if utf8:
        cut = len(chunk)
        while cut > 0 and cursor + cut < len(data):
            try:
                data[:cursor + cut].decode("utf-8")
                break
            except UnicodeDecodeError:
                cut -= 1
        chunk = data[cursor:cursor + cut]
    next_cursor = (cursor + len(chunk)) if cursor + len(chunk) < len(data) else None
    if utf8:
        payload, encoding = chunk.decode("utf-8"), "utf-8"
    else:
        import base64
        payload, encoding = base64.b64encode(chunk).decode(), "base64"
    return {"artifact_sha256": got, "offset": cursor, "n_bytes": len(chunk),
            "total_bytes": len(data), "page_sha256": sha(chunk),
            "encoding": encoding, "data": payload,
            "next_cursor": next_cursor, "uid": os.getuid(), "pid": os.getpid()}


def main(argv):
    if len(argv) < 2:
        print(json.dumps({"error": "USAGE"}))
        return 2
    mode = argv[1]
    if mode == "probe":
        print(json.dumps(probe(), sort_keys=True))
        return 0
    if mode == "read" and len(argv) == 6:
        print(json.dumps(read_page(argv[2], argv[3], int(argv[4]),
                                   int(argv[5])), sort_keys=True))
        return 0
    print(json.dumps({"error": "USAGE"}))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
'''
READER_SHA256 = RV.sha_bytes(READER_SOURCE.encode("utf-8"))


@dataclass(frozen=True)
class WorkerPolicy:
    image: str = DEFAULT_IMAGE
    uid: str = WORKER_UID
    gid: str = WORKER_GID
    reader_sha256: str = READER_SHA256
    page_bytes: int = RV.PAGE_BYTES_RV
    timeout_s: int = 60

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": f"{RV.SCHEMA_PREFIX}.worker_policy.v1", "schema_version": 1,
            "image": self.image, "uid": self.uid, "gid": self.gid,
            "reader_version": READER_VERSION, "reader_sha256": self.reader_sha256,
            "page_bytes": self.page_bytes, "timeout_s": self.timeout_s,
            "argv_prefix": ["docker", "run", "--rm", "--network", "none",
                            "--read-only", "--cap-drop", "ALL",
                            "--security-opt", "no-new-privileges"],
            "mounts": ["<view>:/data:ro", "<out>:/out:rw"],
            "forbidden": ["host private root", "other study", "credentials",
                          "docker socket", "network"],
        }

    @property
    def sha256(self) -> str:
        return RV.canonical_sha(self.to_dict())


class WorkerError(RV.RVError):
    pass


def _write_view(view: Path, study_root: Path, *, canary_text: str = "") -> None:
    """把当前 study 的公开视图复制进只读挂载点（含预审核 reader）。"""
    ensure_private_dir(view)
    for item in sorted(study_root.iterdir()):
        if item.is_file():
            target = view / item.name
            shutil.copyfile(item, target)
            os.chmod(target, 0o644)
    reader = view / "reader.py"
    atomic_write_text(reader, READER_SOURCE, mode=0o644)
    if RV.sha_file(reader) != READER_SHA256:
        raise WorkerError("READER_DRIFT", "reader 写入后 sha 不符")
    if canary_text:
        canary = view / "canary.txt"
        atomic_write_text(canary, canary_text, mode=0o644)
    os.chmod(view, 0o755)


def _make_out(path: Path) -> Path:
    ensure_private_dir(path)
    os.chmod(path, 0o777)
    return path


def _docker_env() -> "dict[str, str]":
    keep = ("PATH", "DOCKER_HOST", "DOCKER_CONFIG", "HOME", "LANG")
    return {k: os.environ[k] for k in keep if k in os.environ}


def _run_container(*, argv: Sequence[str], timeout_s: int, name: str,
                   host_env: "Mapping[str, str] | None" = None
                   ) -> "tuple[subprocess.CompletedProcess, dict[str, Any]]":
    """跑一个容器并**在运行中**抓 container_id/host_pid（实际实例证据）。"""
    started = time.time()
    env = {**_docker_env(), **dict(host_env or {})}
    proc = subprocess.Popen(list(argv), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env)
    info: "dict[str, Any]" = {}
    deadline = started + timeout_s
    while time.time() < deadline:
        try:
            out = subprocess.run(
                ["docker", "inspect", "-f",
                 "{{.Id}}|{{.State.Pid}}|{{.Image}}", name],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
                env=_docker_env())
            text = out.stdout.decode().strip()
            if text:
                cid, pid, image = text.split("|")
                if int(pid or 0) > 0:
                    info = {"container_id": cid, "host_pid": int(pid),
                            "image_id": image}
                    break
        except (subprocess.SubprocessError, ValueError):
            pass
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    try:
        stdout, stderr = proc.communicate(
            timeout=max(1.0, deadline - time.time()))
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, env=_docker_env())
        raise WorkerError("WORKER_TIMEOUT", name) from None
    elapsed = int((time.time() - started) * 1000)
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f",
             "{{.Id}}|{{.State.ExitCode}}|{{.Image}}", name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
            env=_docker_env())
        text = out.stdout.decode().strip()
        if text:
            cid, rc, image = text.split("|")
            info.setdefault("container_id", cid)
            info.setdefault("image_id", image)
            info["exit_code"] = int(rc or 0)
    except (subprocess.SubprocessError, ValueError):
        info.setdefault("inspect_error", True)
    subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, env=_docker_env())
    done = subprocess.CompletedProcess(list(argv), proc.returncode,
                                       stdout, stderr)
    return done, {"elapsed_ms": elapsed, **info}


def _base_argv(*, policy: WorkerPolicy, name: str, view: Path, out: Path,
               uid: str, env_extra: "Mapping[str, str] | None" = None) -> "list[str]":
    argv = [
        "docker", "run", "--name", name, "--network", "none",
        "--user", f"{uid}:{uid}", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--pids-limit", "64",
        "--memory", "256m", "--memory-swap", "256m", "--cpus", "1.0",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
        "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "HOME=/tmp",
    ]
    for key, value in sorted((env_extra or {}).items()):
        argv += ["-e", f"{key}={value}"]
    argv += ["-v", f"{view}:/data:ro", "-v", f"{out}:/out:rw",
             policy.image, "python3", "/data/reader.py"]
    return argv


@dataclass
class ContainerReader:
    """实际读取 worker：每页一个新容器实例，全部绑定同一 policy。"""

    root: Path
    study: str
    policy: WorkerPolicy
    publication: Path  # rv_root/public/<study>

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.publication = Path(self.publication)
        self.counter = 0
        self.records: "list[dict[str, Any]]" = []

    def _next_dir(self) -> "tuple[Path, Path]":
        self.counter += 1
        base = ensure_private_dir(
            self.root / self.study / f"read-{self.counter:03d}")
        return ensure_private_dir(base / "view"), ensure_private_dir(base / "out")

    def read_page(self, *, artifact_id: str, relative_path: str,
                  expected_sha256: str, cursor: int,
                  page_bytes: int) -> dict[str, Any]:
        if page_bytes < 1 or page_bytes > self.policy.page_bytes:
            raise WorkerError("PAGE_TOO_LARGE", str(page_bytes))
        name_in_view = Path(relative_path).name
        view, out = self._next_dir()
        _write_view(view, self.publication)
        name = f"rvread-{self.study}-{self.counter:03d}-{int(time.time()*1000)%100000}"
        argv = _base_argv(policy=self.policy, name=name, view=view,
                          out=out, uid=self.policy.uid)
        argv += ["read", name_in_view, expected_sha256, str(cursor),
                 str(page_bytes)]
        proc, meta = _run_container(argv=argv, timeout_s=self.policy.timeout_s,
                                    name=name)
        record = {"schema": WORKER_SCHEMA, "schema_version": 1,
                  "study": self.study, "artifact_id": artifact_id,
                  "relative_path": relative_path, "container": name,
                  "argv": argv, "image": self.policy.image,
                  "reader_sha256": self.policy.reader_sha256,
                  "request": {"artifact_id": artifact_id, "cursor": cursor,
                              "page_bytes": page_bytes,
                              "expected_sha256": expected_sha256},
                  "stdout_sha256": RV.sha_bytes(proc.stdout),
                  "stderr_sha256": RV.sha_bytes(proc.stderr),
                  "stderr_tail": proc.stderr.decode("utf-8", "replace")[-400:],
                  "rc": proc.returncode, "mounts": [f"{view}:/data:ro",
                                                    f"{out}:/out:rw"],
                  **meta}
        self.records.append(record)
        RV.write_json_private(
            self.root / self.study / f"read-{self.counter:03d}.json", record)
        if proc.returncode != 0:
            raise WorkerError("WORKER_FAILED",
                              f"rc={proc.returncode} {record['stderr_tail']!r}")
        try:
            doc = json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise WorkerError("WORKER_BAD_JSON", "stdout 不是单条 JSON") from None
        if not isinstance(doc, Mapping) or doc.get("error"):
            raise WorkerError("WORKER_REJECTED", str(doc.get("error")))
        # broker 只验证与记录，不重新读宿主文件替换 worker 返回。
        for key, want in (("artifact_sha256", expected_sha256),
                          ("offset", cursor)):
            if doc.get(key) != want:
                raise WorkerError("WORKER_MISMATCH", f"{key}={doc.get(key)!r}")
        page = str(doc.get("data") or "").encode("utf-8") \
            if doc.get("encoding") == "utf-8" else b""
        if doc.get("encoding") == "utf-8":
            if RV.sha_bytes(page) != doc.get("page_sha256"):
                raise WorkerError("WORKER_MISMATCH", "page sha")
            if int(doc.get("n_bytes", -1)) != len(page):
                raise WorkerError("WORKER_MISMATCH", "n_bytes")
        record["page_sha256"] = doc.get("page_sha256")
        record["total_bytes"] = doc.get("total_bytes")
        RV.write_json_private(
            self.root / self.study / f"read-{self.counter:03d}.json", record)
        return dict(doc)

    def evidence(self) -> dict[str, Any]:
        return {"schema": f"{RV.SCHEMA_PREFIX}.worker_evidence.v1",
                "schema_version": 1, "study": self.study,
                "policy_sha256": self.policy.sha256,
                "n_reads": len(self.records),
                "records": [{"container": r["container"],
                             "container_id": r.get("container_id"),
                             "host_pid": r.get("host_pid"),
                             "rc": r["rc"],
                             "stdout_sha256": r["stdout_sha256"],
                             "artifact_id": r["artifact_id"],
                             "page_sha256": r.get("page_sha256")}
                            for r in self.records]}


# ---------------------------------------------------------------------------
# 隔离探针（同一 reader/镜像/挂载策略；含负控）
# ---------------------------------------------------------------------------

def run_isolation(root: "str | Path", *, policy: WorkerPolicy,
                  timeout_s: int = 120) -> dict[str, Any]:
    """Plan §3.1/AC RV-03/RV-04：实际 worker 入口的 allow/deny 探针。"""
    root_path = Path(root)
    iso = ensure_private_dir(root_path / "worker" / "isolation")
    base_view = ensure_private_dir(iso / "view")
    _write_view(base_view, root_path / "public" / "A",
                canary_text="public-canary-content")
    # 私件放在 view 之外的真实位置（负控时故意挂进可读视图）。
    private_dir = ensure_private_dir(iso / "private")
    expected_path = private_dir / "expected.json"
    atomic_write_text(expected_path, '{"expected":"PRIVATE_ORACLE"}', mode=0o600)
    rows: "list[dict[str, Any]]" = []
    ok = True

    def run_probe(tag: str, *, view: Path, forbid: str, private_path: str,
                  allow: str, canary: str, uid: str = PROBE_UID,
                  negative_control: bool = False) -> dict[str, Any]:
        out = _make_out(iso / tag / "out")
        name = f"rviso-{tag}-{int(time.time()*1000)%100000}"
        env_extra = {"RV_ALLOW": allow, "RV_FORBIDDEN": forbid,
                     "RV_PRIVATE_PATH": private_path,
                     "RV_FORBIDDEN_ENV_KEYS": "RV_HOST_CANARY_VALUE"}
        argv = _base_argv(policy=policy, name=name, view=view, out=out,
                          uid=uid, env_extra=env_extra)
        argv += ["probe"]
        proc, meta = _run_container(argv=argv, timeout_s=timeout_s, name=name,
                                    host_env={"RV_HOST_CANARY_VALUE": canary})
        parsed: "dict[str, Any]" = {}
        try:
            parsed = json.loads(proc.stdout.decode().strip().splitlines()[-1])
        except (ValueError, IndexError):
            parsed = {"parse_error": True}
        log = proc.stdout + b"\n--- stderr ---\n" + proc.stderr
        RV.write_text_private(iso / f"{tag}.log",
                              log.decode("utf-8", "replace"))
        checks = _probe_checks(parsed, forbid=forbid, private_path=private_path,
                               allow=allow, canary=canary,
                               negative_control=negative_control)
        return {"tag": tag, "container": name, "argv": argv,
                "image": policy.image, "rc": proc.returncode,
                "log_sha256": RV.sha_bytes(log), "probe": parsed,
                "checks": checks, "mounts": [f"{view}:/data:ro",
                                             f"{out}:/out:rw"], **meta}

    allow_rel = "analysis_output.json"
    row = run_probe("candidate", view=base_view,
                    forbid="private/expected.json",
                    private_path=str(expected_path.resolve()),
                    allow=allow_rel, canary="host-only-canary")
    rows.append(row)
    ok = ok and all(row["checks"].values())
    # 负控：把私件放进视图并放宽权限 ⇒ 探针必须**读到**（检测器有效）。
    neg_view = ensure_private_dir(iso / "negative_control" / "view")
    _write_view(neg_view, root_path / "public" / "A")
    shutil.copyfile(expected_path, neg_view / "expected.json")
    os.chmod(neg_view / "expected.json", 0o644)
    row = run_probe("negative_control", view=neg_view,
                    forbid="expected.json", private_path=str(expected_path),
                    allow=allow_rel, canary="host-only-canary", uid="10006",
                    negative_control=True)
    rows.append(row)
    ok = ok and all(row["checks"].values())
    report = {"schema": PROBE_SCHEMA, "schema_version": 1,
              "created_at_utc": RV.utc_now(), "image": policy.image,
              "policy_sha256": policy.sha256,
              "reader_sha256": policy.reader_sha256,
              "ok": bool(ok), "rows": rows,
              "summary": {r["tag"]: r["checks"] for r in rows}}
    RV.write_json_private(iso / "report.json", report)
    return report


def _probe_checks(parsed: Mapping[str, Any], *, forbid: str, private_path: str,
                  allow: str, canary: str,
                  negative_control: bool) -> "dict[str, bool]":
    def denied(row: Any) -> bool:
        return isinstance(row, Mapping) and row.get("ok") is False and \
            int(row.get("errno") or 0) in (2, 13)  # ENOENT / EACCES

    forbidden = parsed.get("forbidden_read")
    forbidden_ok = True
    if isinstance(forbidden, Mapping):
        forbidden_ok = all(denied(v) for v in forbidden.values())
    checks = {
        "allowed_read_ok": bool((parsed.get("allowed_read") or {}).get("ok")),
        "forbidden_read_denied": forbidden_ok,
        "host_private_denied": denied(parsed.get("host_private_read")),
        "write_data_denied": (parsed.get("write_data") or {}).get("ok") is False,
        "write_out_allowed": (parsed.get("write_out") or {}).get("ok") is True,
        "network_denied": (parsed.get("network") or {}).get("ok") is False,
        "canary_not_leaked": parsed.get("canary_leak") is False,
        "non_root": int(parsed.get("uid") or 0) != 0,
    }
    if negative_control:
        # 负控的目的不是"拒绝"，而是证明**检测器能发现可读私件**：
        # 读取成功 ⇒ detected=True；其余拒绝项照常要求成立。
        detected = bool((parsed.get("forbidden_read") or {}).get(
            forbid, {}).get("ok"))
        checks["forbidden_read_denied"] = detected
        checks["negative_control_detected"] = detected
    return checks


__all__ = ["ContainerReader", "DEFAULT_IMAGE", "READER_SHA256", "READER_SOURCE",
           "READER_VERSION", "WorkerError", "WorkerPolicy", "run_isolation"]
