"""Phase 2 学习回写面（Task5 起）：独立表 phase2_learnings 与演化志通路。

与 ``memory_flywheel.phase1`` 的隔离边界：
- 新表 ``phase2_learnings`` 与 ``phase1_learnings`` 同库不同表，schema 在
  phase1 12 列基础上逐字同构扩展 3 列（evolution_round/parent_run_id/
  pareto_gain_ratio）；
- 老两表 kb_chunks/phase1_learnings 对本包恒只读，重建新表不影响它们。
"""
