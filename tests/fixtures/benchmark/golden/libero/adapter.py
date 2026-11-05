"""合成夹具适配器（Task 5）：不 import 真实引擎、不实跑 tour。

只提供结构，供评测器静态扫描与契约比对。
"""
# _provenance: synthetic_fixture=true case_id=golden_l1


def build_env():
    return {"action_dim": 7, "observation_keys": ["obs", "state"]}


def reset(env):
    return {"obs": 0.0, "state": 0.0}


def step(env, action):
    return {"obs": 0.5, "state": 0.25}
