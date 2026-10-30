# 墙 A 任务书（WHAT-only，首呼冻结）

你在一个纯 CPU、无显示器、无 GPU、禁网的沙盒里工作。LIBERO/robosuite/
MuJoCo 已预装（numpy 1.26.4、mujoco 3.1.6、robosuite 1.4.0，LIBERO 包与其
任务文件已就位）。在当前工作目录创建**单个文件** `agent_solution.py`，
定义一个无参可调用对象：

```python
def build_env():
    ...
    return env
```

## 要交付的环境

- LIBERO benchmark **`libero_spatial` 中序号 0 的任务**
  （pick_up_the_black_bowl_between_the_plate_and_the_ramekin_and_place_it_on_the_plate）；
- 机器人 Panda，控制器 `OSC_POSE`；
- 返回值必须是 LIBERO 的 **`ControlEnv` 包装层实例本身**（不要拆包它的
  内部 env，不要再套 gym wrapper）；
- 评分器在构造后会独立执行：`reset()` 取观测字典、向 `step()` 送入 7 维
  全零动作连续推进 3 步、读取仿真模型与控制器内部量、查询任务成功状态。
  这些调用必须全部正常返回。

## 硬性运行条件（构造性，不允许事后绕过）

- **零 GL / 零渲染 / 纯 CPU**：沙盒里没有任何可用的 EGL/GL 设备，凡是构造
  离屏渲染上下文、相机观测、窗口的路径都会直接崩溃；环境必须以
  `use_camera_obs=False`、`has_renderer=False`、`has_offscreen_renderer=False`
  方式构造，reset 之后仿真对象上不得存在离屏渲染上下文；
- 构造与 reset 不得触发任何 GPU/显示初始化；
- `reset()` 必须返回该任务的完整观测字典；零动作步进后奖励为 0、任务未
  成功；
- 导入本文件时不得构造环境、不得产生副作用。

## 运行环境提示

- `LIBERO_CONFIG_PATH` 等环境变量已由外层注入，直接使用即可，不要改写；
- `datasets path ... does not exist!` 是 LIBERO 的良性告警，不影响构造；
- 可以反复运行你自己写的临时验证脚本自检；最终判定由独立隐藏评分器在
  一次性只读环境中完成，评分器源码与冻结期望向量对你不可见，禁止按字面量
  面向测试构造。

## 交付纪律

- 只允许写 `agent_solution.py` 与你自用的临时脚本；禁止联网、禁止改
  site-packages、禁止读工作目录之外寻找答案；
- 不允许通过 try/except 吞掉渲染初始化错误来伪装无渲染环境——评分器独立
  检查渲染上下文是否存在，并独立重驱底层模型断言，模型自报不作数。
