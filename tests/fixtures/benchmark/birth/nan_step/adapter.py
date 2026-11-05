"""合成反例：步进产生 NaN（B4 → non_finite）。"""


class Env:
    action_dim = 7
    observation_keys = ("rgb",)

    def reset(self, seed=0):
        return {"time": 0.0}

    def step(self, action):
        return {"time": 0.002, "qpos": [float("nan"), 0.0, 0.0]}


def build_env():
    return Env()
