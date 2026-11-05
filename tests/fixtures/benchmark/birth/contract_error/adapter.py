"""合成反例：结构契约不符（B3 → contract_error）：action_dim=0、观测键为空。"""


class BadEnv:
    action_dim = 0
    observation_keys = ()

    def reset(self, seed=0):
        return {"time": 0.0}

    def step(self, action):
        return {"time": 0.002, "qpos": [0.01]}


def build_env():
    return BadEnv()
