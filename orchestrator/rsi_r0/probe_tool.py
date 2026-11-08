#!/usr/bin/env python3
"""协议探针的**容器内侧**工具（P3/P4 的唯一动作面）。

只做一件事：读 ``/work`` 下的一个文件并把内容回给宿主。路径由宿主在容器外先按白名单
校验过，这里再做一次独立校验（解析符号链接后必须仍落在 ``/work`` 里），因为容器内
这一层不能依赖宿主"已经检查过"。

用法（宿主调用，argv 只有一个参数）::

    python3 /svc/probe_tool.py /work/marker.txt

输出：一行 ``TOOL_JSON {...}``。任何失败都以 ``TOOL_JSON {"ok": false, ...}`` 回报，
不用异常栈（栈会泄露容器内路径细节，且不该影响宿主解析）。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

WORK_ROOT = "/work"
MAX_BYTES = 4096


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("TOOL_JSON " + json.dumps({"ok": False, "error": "usage: probe_tool.py <path>"}))
        return 2
    raw = argv[1]
    try:
        root = os.path.realpath(WORK_ROOT)
        target = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(WORK_ROOT, raw))
        if os.path.commonpath([root, target]) != root:
            print("TOOL_JSON " + json.dumps(
                {"ok": False, "error": "path_escape", "requested": raw}))
            return 3
        if not os.path.isfile(target):
            print("TOOL_JSON " + json.dumps(
                {"ok": False, "error": "not_a_file", "requested": raw}))
            return 4
        with open(target, "rb") as fh:
            data = fh.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            print("TOOL_JSON " + json.dumps({"ok": False, "error": "too_large"}))
            return 5
        payload = {
            "ok": True,
            "requested": raw,
            "resolved": target,
            "bytes": len(data),
            "content_sha256": hashlib.sha256(data).hexdigest(),
            "content": data.decode("utf-8", errors="replace"),
        }
        print("TOOL_JSON " + json.dumps(payload, ensure_ascii=False))
        return 0
    except OSError as exc:
        print("TOOL_JSON " + json.dumps({"ok": False, "error": f"os_error:{exc.errno}"}))
        return 6


if __name__ == "__main__":
    sys.exit(main(sys.argv))
