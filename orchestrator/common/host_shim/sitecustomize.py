"""Python 启动引导件（薄引导；Task6 run7，规格 v1.1a §2.2/§2.3）。

经 PYTHONPATH 前拼由 site 机制在脚本首行前自动导入。职责仅三件：
1. 无论开关先链载发行版 sitecustomize（apport 钩子安装语义不丢）；
2. marker 精确等值 ``1`` 才继续，其余值/缺失立即静默返回；
3. 以合成名急切加载同目录核心件并 ``activate()``——禁顶层裸 import
   核心件（d1：裸名永不入 sys.modules，启动期不受同名件劫持）。

本文件不含任何人类机制文案（文案唯一规范源在核心件）。
"""
import importlib.util
import os
import sys

_CORE_NAME = "simpraxis_host_shim_core_v1"
_CHAIN_NAME = "_simpraxis_host_shim_distro_chain_v1"
_DISTRO_PATH = "/usr/lib/python3.10/sitecustomize.py"


def _chain_load_distro(path=_DISTRO_PATH):
    """以合成名链载发行版钩子；文件缺失/任何异常静默吞掉（§2.2）。"""
    try:
        spec = importlib.util.spec_from_file_location(_CHAIN_NAME, path)
        if spec is None or spec.loader is None:
            return
        module = importlib.util.module_from_spec(spec)
        sys.modules[_CHAIN_NAME] = module
        spec.loader.exec_module(module)
    except Exception:
        pass


def _load_and_activate():
    core_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "shim_core.py")
    spec = importlib.util.spec_from_file_location(_CORE_NAME, core_path)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules[_CORE_NAME] = module        # 急切：脚本首行前已缓存
    spec.loader.exec_module(module)
    module.activate()


def _bootstrap():
    _chain_load_distro()
    if os.environ.get("SIMPRAXIS_HOST_SHIM", None) != "1":
        return
    _load_and_activate()


# 规格 v1.1a §2.1(c)：仅 site 机制以 sitecustomize 名导入时自启；被直接当
# 脚本执行（__main__）或以合成名 exec_module 时一律不自启。
if __name__ == "sitecustomize":
    _bootstrap()
