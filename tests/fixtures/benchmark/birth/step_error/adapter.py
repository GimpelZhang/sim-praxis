"""合成反例：step 抛异常（B4 → step_error）。"""


class Env:
    action_dim = 7
    observation_keys = ("rgb",)

    def reset(self, seed=0):
        return {"time": 0.0}

    def step(self, action):
        raise RuntimeError("step 阶段故意失败（合成反例）")


def build_env():
    return Env()
