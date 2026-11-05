"""合成样本：契约全过但入口 import 了网络模块（D3-6 的 audited_only 审计命中）。"""

import socket  # noqa: F401 - 只为让 AST 扫描命中，不建立任何连接


class Env:
    action_dim = 7
    observation_keys = ("rgb",)

    def reset(self, seed=0):
        return {"time": 0.0}

    def step(self, action):
        return {"time": 0.002, "qpos": [0.001]}


def build_env():
    return Env()
