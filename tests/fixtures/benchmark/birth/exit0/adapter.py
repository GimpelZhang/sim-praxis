"""合成反例：import 期直接自杀（AC-T3-5 隔离性）。

``os._exit(0)`` 发生在模块加载阶段：子进程当场死掉、没有标记行、rc=0。
主进程必须毫发无损（每层独立 subprocess，D3-2）。
"""

import os

os._exit(0)
