#!/usr/bin/env python3
"""v4：方舟模型清点（GET {ANTHROPIC_BASE_URL}/v1/models，Bearer 鉴权）。

定位：仅供人工/执行者查看账号下日期版本（如 glm-5-3-flash-260828），在浮动别名
被供应侧调整（404/400/回显漂移）时作为【换供请示取证】来源（v4.2 决策 3：清点后
必须报用户批准，获批前禁止改 stage_models.yaml）。它【不是】校验闸：2026-10-12 实证目录 135
个条目全是日期版本，deepseek-v4.1-flash / glm-5.3-flash / ark-code-latest 三个浮动
别名均不在册，router 不得据此做成员资格断言（v3 的 discovery 硬闸已作废）。
原始响应只落 /mnt（不入 git）：$SIMPRAXIS_RUN_DIR/inventory/<ts>/ark_models.json
+ 指向 <ts> 的相对 latest 软链（basename 规则，与探针产物一致）。
"""
import json
import os
import sys
import time

import requests

from orchestrator.common.fsutil import atomic_write_text

DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/coding"
INVENTORY_ROOT = os.path.join(
    os.environ.get("SIMPRAXIS_RUN_DIR", "/mnt/artifacts/simpraxis/runs"), "inventory")


def _base_url() -> str:
    return (os.environ.get("SIMPRAXIS_LLM_BASE_URL")
            or os.environ.get("ANTHROPIC_BASE_URL") or DEFAULT_BASE_URL)


def _token() -> str | None:
    return (os.environ.get("SIMPRAXIS_LLM_TOKEN")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def main() -> int:
    key = _token()
    if not key:
        print("SIMPRAXIS_LLM_TOKEN/ANTHROPIC_AUTH_TOKEN 未注入：先 source scripts/00_env.sh",
              file=sys.stderr)
        return 2
    r = requests.get(
        f"{_base_url()}/v1/models",
        headers={"Authorization": f"Bearer {key}"},
        timeout=30,
    )
    print(f"HTTP {r.status_code}")
    if r.status_code != 200:
        print(r.text[:500], file=sys.stderr)
        return 1
    payload = r.json()
    # 兼容 data / models 两种包裹；本脚本只清点、不做任何成员资格断言
    data = payload.get("data") or payload.get("models") or []
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(INVENTORY_ROOT, ts)
    os.makedirs(out_dir, exist_ok=True)
    raw = os.path.join(out_dir, "ark_models.json")
    # S-6：原子写 0600 收口 fsutil（原 open+chmod）。
    atomic_write_text(raw, json.dumps(payload, ensure_ascii=False, indent=2))
    # 相对软链（B1c 同源修法）：容器/宿主两侧路径前缀不同，绝对链会悬空
    latest = os.path.join(INVENTORY_ROOT, "latest")
    if os.path.islink(latest) or os.path.exists(latest):
        os.remove(latest)
    os.symlink(ts, latest)
    print(f"inventory saved: {raw}（共 {len(data)} 个）；latest -> {ts}\n")
    for item in data:
        print(f"  {item.get('id', '?')}")
    print(
        "\n说明：本清单【仅供换供请示时取证】（v4.2 决策 3）。stage_models.yaml 默认使用\n"
        "用户指定的三个浮动别名，其不在本清单中属正常，router 不会也不允许据此拒绝启动。\n"
        "仅当 --validate-models 对某 stage 报 404/400/model-not-found 或回显名漂移时：\n"
        "执行者必须 STOP，从上面挑同家族日期版本（如 glm-5-3-flash-260828）作为【建议】\n"
        "连同错误原文与回显对照报给用户；未获用户明确批准前禁止改 stage_models.yaml 的\n"
        "model_id、禁止换供应商、禁止现场猜 ID；获批后改 YAML 并重跑 --validate-models。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
