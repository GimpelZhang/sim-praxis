# Fixture：LIBERO VLA 多 Stage（合成，非真实脚本）

引擎侧 `tests/fixtures/solutions/` 的**合成**契约样例，只为
`orchestrator/solutions` 解析器与 `check_solutions` 树卫生测试提供形状。

- 真实内容层方案：KB `solutions/track1_tools/04_libero_vla/`
  （Phase 2.7 Task 6 于 2026-11-04 回收，KB 提交 `b5de4bd`）。
- 本目录不含任何解题逻辑，`vla_episode_runner.py` 只做参数解析与打印。
