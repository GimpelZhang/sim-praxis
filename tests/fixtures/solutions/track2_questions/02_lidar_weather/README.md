# Fixture：track2_questions/02_lidar_weather

合成测试夹具（Task0b 三层分离）。不是真实 Q2 天气光学方案；正本在 KB 公开仓
`solutions/track2_questions/02_lidar_weather/`（三件脚本 + 完整光学知识
README + 迁入的物理数值断言 tests/）。本夹具只承载引擎仓真人闸门接缝契约：
`optics_model.py` 源码常量 `QBACK_DEFAULT = 0.4`（读源码、不 import）。
