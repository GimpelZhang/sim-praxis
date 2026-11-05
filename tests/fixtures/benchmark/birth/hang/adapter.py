"""合成反例：步进死循环（B4 → timeout，靠 subprocess 墙钟上限击杀）。"""


class Env:
    action_dim = 7
    observation_keys = ("rgb",)

    def reset(self, seed=0):
        return {"time": 0.0}

    def step(self, action):
        while True:
            pass


def build_env():
    return Env()
