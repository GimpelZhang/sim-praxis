"""合成反例：reset 失败（B4 → step_error，detail 标注 reset）。"""


class Env:
    action_dim = 7
    observation_keys = ("rgb",)

    def reset(self, seed=0):
        raise RuntimeError("reset 阶段故意失败（合成反例）")

    def step(self, action):
        return {"time": 0.002, "qpos": [0.01]}


def build_env():
    return Env()
