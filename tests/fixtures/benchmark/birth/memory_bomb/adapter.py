"""合成反例：步进申请超大内存（B4 → memory，靠 RLIMIT_AS 触发 MemoryError）。"""


class Env:
    action_dim = 7
    observation_keys = ("rgb",)

    def reset(self, seed=0):
        return {"time": 0.0}

    def step(self, action):
        blob = bytearray(8 * 1024 ** 3)          # 8 GiB：超过子进程 RLIMIT_AS
        return {"time": 0.002, "bytes": float(len(blob))}


def build_env():
    return Env()
