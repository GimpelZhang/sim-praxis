# Fixture：track2_questions/01_ice_floor_humanoid_dr

合成测试夹具（Task0b 三层分离）。不是真实 Genesis humanoid DR batch 脚本；
正本在 KB 公开仓
`solutions/track2_questions/01_ice_floor_humanoid_dr/genesis_humanoid_dr.py`
（= v1 run 执行副本）。本夹具只承载两个契约形状：35 列 `CSV_FIELDS` 与
推后方窗字面量 2.5 s（与 `gait_thresholds.POSTPUSH_WINDOW_S` 同源断言）。
