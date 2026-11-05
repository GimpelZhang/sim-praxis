"""合成合法样本（L1 形状）：五层全 PASS。

**不依赖任何物理引擎**——出生验证只证明「能构造、能 reset、能 step 一次且推进
有限」，真实引擎通过由 Task 6 的参考执行覆盖（Plan T3.5 步骤 7）。
"""


class FakeEnv:
    action_dim = 7
    observation_keys = ("rgb", "proprio")

    def __init__(self):
        self.state = {"time": 0.0, "qpos": [0.0, 0.0, 0.0]}

    def reset(self, seed=0):
        self.state = {"time": 0.0, "qpos": [0.0, 0.0, 0.0], "seed": float(seed)}
        return dict(self.state)

    def step(self, action):
        self.state = {
            "time": self.state["time"] + 0.002,
            "qpos": [0.001 * (index + 1) for index in range(3)],
            "action": [float(item) for item in action],
        }
        return dict(self.state)


def build_env():
    return FakeEnv()
