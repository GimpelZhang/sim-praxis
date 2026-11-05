"""合成反例：构造阶段抛异常（B2 → construction_error）。"""


def build_env():
    raise RuntimeError("构造阶段故意失败（合成反例）")
