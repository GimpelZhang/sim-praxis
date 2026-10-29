#!/usr/bin/env python3
"""Phase 2.5 Task3 Stage0 — P0 smolagents 核心无遥测外发探针（AC-T3-1，0 付费）。

**一次性探针，不是生产路径**（红线同 probe_p1：本文件唯一调用点，
永不 import orchestrator.graph/orchestrator.agentic）。设计为在
``--network none`` 断网容器内运行（外层 harness 用 nsenter+tcpdump 在
容器独立 netns 抓包，抓包范围因此天然只含本探针进程）：

  1. 进程启动即设离线/禁遥测环境旋钮（HF_HUB_OFFLINE/
     TRANSFORMERS_OFFLINE/HF_DATASETS_OFFLINE/DO_NOT_TRACK/
     TOKENIZERS_PARALLELISM），全部在 ``import smolagents`` 之前；
  2. 进程内 socket 审计：包装 socket.connect/connect_ex/create_connection，
     记录每一次连接尝试（容器 netns 内本进程是唯一工作负载，与 pcap 双证）；
  3. 静态扫描已安装 smolagents 包：tracing/更新检查/skills clone/BPE 预热
     四类网络触原语逐项取证，区分「包内不存在/opt-in 未加载/已加载未调用」；
  4. import smolagents + FakeModel（零网络的本地假模型）驱动 CodeAgent
     跑一轮两只本地只读工具（probe_list/probe_read），验证核心运行不触网；
  5. HF/tiktoken 缓存目录前后快照：不得新增文件。

异框架（agentsdk tracing、Aider PostHog/更新检查、tiktoken 预热）事实
只引用四家调研 §5 结论，本探针不联网复测（N7）。

纪律：本探针 0 付费、0 触网；按 AC-T3-8 统一守 SIMPRAXIS_ALLOW_PAID=1
（探针统一显式运行开关，裸跑 exit 2）。证据 JSON 0600 落 --out。
退出码：0=核心无外发（socket 零非环回连接+mock 运行成功+缓存零新增+
静态清单齐）；2=守卫/参数错；1=任一外发或运行失败（负结果如实留证）。
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import os
import re
import socket
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir  # noqa: E402

EXIT_GUARD = 2
EXIT_FAIL = 1

# 离线/禁遥测旋钮：必须在 import smolagents/huggingface_hub 之前生效。
OFFLINE_ENV = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_DATASETS_OFFLINE": "1",
    "DO_NOT_TRACK": "1",
    "TOKENIZERS_PARALLELISM": "false",
}

_ALPHA_MARKER = "p0-egress-marker-5d2b"

# 静态清单四项（命中给 file:line；分类在 verdict 阶段做）。
STATIC_PATTERNS = {
    "tracing": re.compile(r"posthog|opentelemetry|import\s+sentry_sdk|"
                          r"sentry_sdk\.|analytics\.track|telemetry"),
    "update_check": re.compile(r"pypi\.org/pypi|check_for_update|"
                               r"latest[_-]?version|version[_-]?check"),
    "skills_clone": re.compile(r"clone_repository|git\s+clone|"
                               r"snapshot_download|create_repo|upload_folder"),
    "bpe_warmup": re.compile(r"tiktoken|get_encoding|AutoTokenizer"),
    "net_primitive": re.compile(r"^\s*(?:import|from)\s+(requests|httpx|"
                                r"urllib\.request|http\.client)\b"),
}
# 核心模块（import smolagents 即经 __init__ 加载）vs opt-in 子模块。
OPTIONAL_MODULES = ("default_tools", "remote_executors", "gradio_ui",
                    "mcp_client", "cli", "vision", "agent_types")


# ---------------------------------------------------------------------------
# socket 连接审计（进程内第二证；第一证是容器 netns pcap）
# ---------------------------------------------------------------------------

def install_socket_audit() -> dict:
    # 口径：只包 connect 系（connect/connect_ex/create_connection），不包
    # sendto/getaddrinfo——UDP/DNS 外发以 netns 级 SLL pcap 为地面真值，不扩审计面。
    log: list[dict] = []

    def _peer(args):
        try:
            addr = args[0]
            host = addr[0]
            port = addr[1] if len(addr) > 1 else None
        except (IndexError, TypeError):
            host, port = "?", None
        loopback = host in ("127.0.0.1", "::1", "localhost")
        return {"host": host, "port": port, "loopback": bool(loopback)}

    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex
    orig_create = socket.create_connection

    def _connect(self, address):
        rec = _peer((address,))
        rec["api"] = "socket.connect"
        log.append(rec)
        return orig_connect(self, address)

    def _connect_ex(self, address):
        rec = _peer((address,))
        rec["api"] = "socket.connect_ex"
        log.append(rec)
        return orig_connect_ex(self, address)

    def _create_connection(address, *a, **k):
        rec = _peer((address,))
        rec["api"] = "create_connection"
        log.append(rec)
        return orig_create(address, *a, **k)

    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex
    socket.create_connection = _create_connection
    return {"log": log,
            "non_loopback": lambda: [r for r in log if not r["loopback"]]}


# ---------------------------------------------------------------------------
# 静态扫描
# ---------------------------------------------------------------------------

def _is_optional(relpath: str) -> bool:
    # smolagents/default_tools.py 等 opt-in 模块（__init__ 仍 import 之，
    # 但调用面是显式工具；分类名保留以备报告区分）。
    return any(f"/{m}.py" in relpath or relpath.endswith(f"{m}.py")
               for m in OPTIONAL_MODULES)


def static_scan(pkg_dir: Path) -> dict:
    pkg_dir = Path(pkg_dir)
    report: dict[str, list] = {k: [] for k in STATIC_PATTERNS}
    for py in sorted(pkg_dir.rglob("*.py")):
        if py.name.endswith(".bak.py"):
            continue
        rel = py.relative_to(pkg_dir).as_posix()
        try:
            lines = py.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for no, line in enumerate(lines, 1):
            for key, rx in STATIC_PATTERNS.items():
                if rx.search(line):
                    report[key].append(f"{rel}:{no}: {line.strip()[:120]}")
    return report


def classify_static(report: dict, loaded_modules: set) -> dict:
    """每项给 absent / opt_in_present / loaded 三态 + 人读证据。"""
    out = {}
    for item in ("tracing", "update_check", "skills_clone", "bpe_warmup"):
        hits = report.get(item, [])
        if not hits:
            out[item] = {"status": "absent", "evidence": [],
                         "note": "已安装包内无此类触原语"}
            continue
        status = "opt_in_present"
        out[item] = {"status": status, "evidence": hits[:8],
                     "note": "存在命中行；是否加载/调用以运行期审计与缓存快照为准"}
    net = report.get("net_primitive", [])
    out["net_primitive_imports"] = {
        "status": "loaded_not_called" if "requests" in loaded_modules else "absent",
        "evidence": net[:8],
        "note": "requests 随 default_tools 加载不等于外发；零连接以 socket 审计/pcap 为准",
    }
    return out


# ---------------------------------------------------------------------------
# 缓存快照
# ---------------------------------------------------------------------------

def snapshot_dirs(paths: "list[Path]") -> dict:
    snap: dict[str, list] = {}
    for p in paths:
        if not p.exists():
            snap[str(p)] = []
            continue
        snap[str(p)] = sorted(str(x.relative_to(p))
                              for x in p.rglob("*") if x.is_file())
    return snap


def diff_snapshots(before: dict, after: dict) -> dict:
    added: dict[str, list] = {}
    for root, files_before in before.items():
        files_after = after.get(root, [])
        new = sorted(set(files_after) - set(files_before))
        if new:
            added[root] = new
    return added


# ---------------------------------------------------------------------------
# mock 一轮 CodeAgent（FakeModel 零网络；两只本地只读工具）
# ---------------------------------------------------------------------------

FAKE_STEP_1 = """```py
listing = probe_list(".")
print(listing)
```"""
FAKE_STEP_2 = """```py
text = probe_read("alpha.txt")
final_answer(text.strip())
```"""


def run_mock_agent(workspace: Path) -> dict:
    from smolagents import CodeAgent, tool  # noqa: PLC0415
    from smolagents.models import ChatMessage, MessageRole, Model  # noqa: PLC0415

    workspace = Path(workspace).resolve()

    @tool
    def probe_list(path: str) -> str:
        """List entries of the jailed probe workspace directory.

        Args:
            path: directory relative to workspace root, use '.' for root.
        """
        p = (workspace / path).resolve()
        if workspace not in (p, *p.parents):
            raise ValueError("escape")
        return "\n".join(sorted(x.name for x in p.iterdir()))

    @tool
    def probe_read(path: str) -> str:
        """Read a UTF-8 text file inside the jailed probe workspace.

        Args:
            path: file path relative to workspace root.
        """
        p = (workspace / path).resolve()
        if workspace not in (p, *p.parents) or not p.is_file():
            raise ValueError("escape or missing")
        return p.read_text(encoding="utf-8")

    class FakeModel(Model):
        name = "fake-egress-model"

        def __init__(self):
            super().__init__()
            self.scripts = [FAKE_STEP_1, FAKE_STEP_2]
            self.i = 0
            self.turns = 0

        def generate(self, messages, stop_sequences=None,
                     response_format=None, tools_to_call_from=None, **kwargs):
            if tools_to_call_from is not None:
                raise AssertionError("CodeAgent 路径不应传 tools_to_call_from")
            content = self.scripts[min(self.i, len(self.scripts) - 1)]
            self.i += 1
            self.turns += 1
            return ChatMessage(role=MessageRole.ASSISTANT, content=content)

    (workspace / "alpha.txt").write_text(
        f"p0 egress fixture\n{_ALPHA_MARKER}\n", encoding="utf-8")
    fake = FakeModel()
    agent = CodeAgent(tools=[probe_list, probe_read], model=fake,
                      max_steps=4)
    answer = str(agent.run("列出工作目录并读取 alpha.txt 内容。"))
    return {"ok": _ALPHA_MARKER in answer, "answer_tail": answer[-120:],
            "model_turns": fake.turns, "final_answer": answer}


def find_smolagents_pkg() -> "Path | None":
    spec = importlib.util.find_spec("smolagents")
    if spec and spec.origin:
        return Path(spec.origin).parent
    for base in sys.path:
        cand = Path(base) / "smolagents"
        if cand.is_dir():
            return cand
    return None


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_evidence(path: Path, payload: dict) -> None:
    ensure_private_dir(path.parent)
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2,
                                       sort_keys=True) + "\n", mode=0o600)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="probe_p0_egress.py",
        description="P0 smolagents 核心无遥测外发探针（断网容器内运行，0 付费）")
    p.add_argument("--out", required=True, help="证据 JSON 输出路径（0600）")
    p.add_argument("--workspace", default=None,
                   help="只读工具夹具目录（默认 --out 同级 p0_workspace）")
    p.add_argument("--hf-home", default=None,
                   help="隔离 HF_HOME（默认 --out 同级 .hfhome，必须可写）")
    return p


def main(argv: "list[str] | None" = None) -> int:
    # AC-T3-8 统一双守卫；本探针 0 付费，开关是统一的显式运行门禁。
    if os.environ.get("SIMPRAXIS_ALLOW_PAID") != "1":
        print("probe_p0: 拒绝运行——探针统一显式运行开关 SIMPRAXIS_ALLOW_PAID=1"
              "（本探针 0 付费、断网运行；裸跑 exit 2）", file=sys.stderr)
        return EXIT_GUARD
    args = build_parser().parse_args(argv)
    out = Path(args.out)
    workspace = Path(args.workspace) if args.workspace else out.parent / "p0_workspace"
    hf_home = Path(args.hf_home) if args.hf_home else out.parent / ".hfhome"
    hf_home.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)

    for k, v in OFFLINE_ENV.items():
        os.environ[k] = v
    os.environ["HF_HOME"] = str(hf_home)

    # 1) 包路径必须在 import 前确定（find_spec 不导入）。
    pkg = find_smolagents_pkg()
    if pkg is None:
        print("probe_p0: 找不到已安装 smolagents 包", file=sys.stderr)
        return EXIT_FAIL
    static_raw = static_scan(pkg)

    # 2) 缓存在 import/运行前快照。
    cache_paths = [hf_home, Path.home() / ".cache" / "tiktoken",
                   Path.home() / ".cache" / "huggingface"]
    snap_before = snapshot_dirs(cache_paths)

    # 3) socket 审计先于 import 安装（抓 import 期外发）。
    audit = install_socket_audit()

    # 4) import + mock 一轮。
    import_error = None
    mock_result = None
    loaded = set()
    try:
        import smolagents  # noqa: F401, PLC0415
        loaded = {m.split(".")[0] for m in sys.modules
                  if m.split(".")[0] in ("requests", "httpx", "urllib3",
                                         "huggingface_hub", "posthog")}
        mock_result = run_mock_agent(workspace)
    except Exception as exc:  # noqa: BLE001 —— 断网下任何异常都是外发依赖证据
        import_error = f"{type(exc).__name__}: {str(exc)[:400]}"

    snap_after = snapshot_dirs(cache_paths)
    added = diff_snapshots(snap_before, snap_after)
    non_loopback = audit["non_loopback"]()
    checklist = classify_static(static_raw, loaded)

    evidence = {
        "probe": "p0_egress", "captured_at_iso": _utcnow(),
        "smolagents_pkg": str(pkg),
        "env_knobs": {k: os.environ.get(k) for k in
                      (*OFFLINE_ENV, "HF_HOME")},
        "static_checklist": checklist,
        "static_raw_hits": {k: len(v) for k, v in static_raw.items()},
        "loaded_net_related_top_modules": sorted(loaded),
        "socket_attempts_total": len(audit["log"]),
        "socket_non_loopback": non_loopback,
        "cache_dirs_added_files": added,
        "mock_agent": mock_result,
        "import_error": import_error,
        "note_n7": "agentsdk/Aider/tiktoken 异框架遥测事实只引用四家调研 §5，"
                   "不联网复测（AC-T3-1 N7）",
    }
    egress_ok = (not non_loopback and import_error is None
                 and mock_result is not None and mock_result["ok"]
                 and not added)
    evidence["verdict"] = "pass" if egress_ok else "fail"
    write_evidence(out, evidence)
    if egress_ok:
        print(f"probe_p0: PASS 零非环回连接/缓存零新增/mock 运行 "
              f"{mock_result['model_turns']} 轮，证据→{out}")
        return 0
    print(f"probe_p0: FAIL 外发={len(non_loopback)} 缓存新增={bool(added)} "
          f"import_error={import_error} mock={mock_result} 证据→{out}",
          file=sys.stderr)
    return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
