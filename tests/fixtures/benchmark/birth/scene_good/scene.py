"""合成合法样本（M1 形状）：暴露 ``build_scene()``，无 reset 语义。"""


class FakeScene:
    action_dim = 3
    dt = 0.002

    def __init__(self):
        self.time = 0.0

    def step(self, action):
        self.time += self.dt
        return {"time": self.time, "qpos": [0.01 * (index + 1) for index in range(3)]}


def build_scene():
    return FakeScene()
