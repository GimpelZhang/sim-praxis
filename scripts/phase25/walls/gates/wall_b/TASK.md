# 墙 B 任务书（WHAT-only，首呼冻结）

你在一个纯 CPU、禁网、只允许写当前工作目录的沙盒里工作。可用运行时：
Python 3.10、numpy 1.26.4、trimesh 5.1.1（标准库亦可）；没有 GPU、没有
Genesis/MuJoCo，不要尝试导入它们或联网获取任何资源。

在当前工作目录创建**单个文件** `agent_solution.py`，实现下面四个必需 API
和一个可选 API。评审在你结束后由独立隐藏评分器导入该文件、独立复算判定；
评分器源码、测试向量与任何历史见证件对你不可见，因此不要试图读取或猜测
它们，面向测试硬编码（字面量、路径探针、哈希比对等）直接判负。

## 背景（WHAT）

一段「源容器倾斜倒水」的运动学，在进入任何物理仿真之前必须先通过构造性
自检：测量仪器沿时间轴算出若干见证量，再按冻结阈值逐条判违。你要交付的
正是这套**测量仪器**与一件配套的程序化容器网格。仪器必须对任意合规/违例
输入给出正确数值与正确违例名；非法输入必须抛异常，禁止返回哨兵数值。

## API 1：`edge_manifold_counts(faces)`

- 入参 `faces`：形状 (F,3) 的整型数组，每行一个三角形的三个顶点下标。
- 边按**无向**理解：顶点对先排序再计数。每条边被三角形引用恰两次为内边
  （不计），恰一次为边界边，三次及以上为非流形边。
- 返回 `(boundary_count, nonmanifold_count)`：两个 Python int，分别为
  只出现一次的边数与出现 ≥3 次的边数。

## API 2：`make_vessel_mesh(out_path)`

- 用 trimesh **程序化生成**一件开口薄壁中空容器（壶/罐类，壁与底一体），
  导出 OBJ 到给定路径 `out_path`，返回任意可 JSON 序列化的 stats 字典。
- 仪器级合格条件（导出后由评分器重新加载独立复检，模型自报不算数）：
  - 单一刚体：重载后只含 **1** 个连通实体；
  - 水密流形：边界边 0、非流形边 0、`is_watertight` 为真；
  - 三角面数 ≥ 96；
  - 真薄壁中空：网格体积 / 凸包体积之比严格落在 (0, 0.6)；
  - 必须是你程序化生成的新网格：禁止复制/搬运任何现成网格文件或历史件，
    也禁止把入参路径指向已有文件。
- 允许开口/壶嘴（水密的薄壁带唇边结构即可），工艺路线自选（旋转、拉伸、
  布尔等），评分器不要求特定拓扑做法。

## API 3：`kinematic_witness(times, thetas, spout_xyz, clearances, *, flow_start, flow_end)`

纯 numpy 计算，返回 dict，键与定义**逐字**如下（θ 输入为弧度，输出角度键
为度；p 为壶嘴尖点世界坐标，单位米；clearance 为有符号最小间隙，单位米）：

| 键 | 定义 |
|---|---|
| `dt_s` | `median(diff(times))`（float） |
| `theta_range_deg` | 全程 maxθ−minθ，度 |
| `theta_max_deg` | 全程最大俯仰角，度 |
| `flow_hold_s` | 流出窗内（`flow_start ≤ t ≤ flow_end`）采样点数 × `dt_s` |
| `flow_hold_theta_min_deg` | 流出窗内 θ 最小值，度 |
| `rho_max_flow` | 流出窗内 max hypot(p_x, p_y) |
| `spout_z_min_flow` | 流出窗内壶嘴 z 最小值 |
| `spout_z_max_flow` | 流出窗内壶嘴 z 最大值 |
| `clearance_min` | **全程**（不是只看流出窗）最小有符号间隙 |
| `drift_max` | 流出窗内各点相对窗内首点的最大位移范数 |
| `speed_max` | max ‖diff(spout_xyz)‖ / `dt_s`（相邻采样点，全程） |
| `angular_speed_peak_degps` | max |diff(θ)| / `dt_s`，度/秒 |

输入校验（任一不满足必须抛 `ValueError`，禁止返回哨兵）：

- 四个数组长度一致且 ≥ 2；times 等间隔单调递增；
- 全部数值有限（出现 NaN/Inf 即抛）；
- 单采样点等退化输入即抛。

返回 dict 必须恰好包含上述 12 个键且所有值有限；`speed_max`、
`drift_max` 等幅度量非负。

## API 4：`evaluate_precheck(witness)`

入参为 API 3 的返回 dict。返回
`{"passed": <bool>, "violations": [<违例名>, ...]}`，按下表逐条判违
（阈值即冻结合同，禁止自行松紧）：

| 判违条件 | 违例名（逐字） |
|---|---|
| `theta_range_deg < 120` | `contract 1 theta range` |
| `theta_max_deg < 120` | `contract 1 theta max` |
| `flow_hold_theta_min_deg < 105` | `contract 1 hold angle` |
| `rho_max_flow > 0.035` | `contract 2 spout alignment` |
| `spout_z_min_flow < 1.025` 或 `spout_z_max_flow > 1.07` | `contract 2 spout height` |
| `clearance_min < 0.012` | `contract 3 clearance` |
| `drift_max > 0.010` | `contract 6 spout drift` |
| `speed_max > 0.20` | `contract 6 spout speed` |
| `angular_speed_peak_degps > 80` | `contract 6 angular speed` |

无违例时 `passed=True`、`violations=[]`；违例名只能取自本表，禁止自造。

## API 5（可选）：`design_pour_trajectory()`

返回 `(times, thetas, spout_xyz, clearances, flow_start, flow_end)`，即你
按同一套合同设计的合规倾倒轨迹（直立静置→平滑起倒→流出停留→回正收尾）。
该 API 只用于描述性能力轴记录，不影响工程闸判决。

## 交付纪律

- 只允许写 `agent_solution.py`（make_vessel_mesh 运行时写出的 OBJ 除外，
  且只能写当前工作目录）；
- 禁止联网、禁止子进程下载、禁止改 site-packages、禁止读工作目录之外的
  文件去寻找答案；
- 可以反复运行自己的脚本自我验证；最终评分以隐藏评分器一次性独立复算为准。
