"""平台托底核心（非模型代码；Task6 run7，规格 v1.1a §2）。

torch Tensor 在 GPU→numpy 协议边界的主机迁移包装：``Tensor.__array__``
与 ``Tensor.numpy`` 两条入口，cuda 张量 ``detach().cpu()`` 后再委托原方法，
cpu 且 requires_grad 的张量仅 ``detach()``；纯 stderr banner/atexit 观测，
零写盘/零网络/零子进程/不改 os.environ（规格 §2.6/§2.9）。

本模块是全部机制词与人类文案的唯一规范源。顶层 import 零副作用：不装
finder、不注册 atexit、不读 marker——副作用只在显式 ``activate()``。
子进程里本模块只以合成名 ``simpraxis_host_shim_core_v1`` 缓存（见
sitecustomize 引导件），裸名永不入 sys.modules（规格 §2.3 d1）。
"""
from __future__ import annotations

import atexit
import importlib.util
import os
import re
import sys

MARKER_ENV = "SIMPRAXIS_HOST_SHIM"
CORE_MODULE_NAME = "simpraxis_host_shim_core_v1"
DISTRO_CHAIN_MODULE_NAME = "_simpraxis_host_shim_distro_chain_v1"
DISTRO_SITECUSTOMIZE_PATH = "/usr/lib/python3.10/sitecustomize.py"

PRELUDE_DIR = os.path.dirname(os.path.abspath(__file__))

# A1 脱敏词表（规格 §2.8 逐字）：引擎侧引用只走包路径 import。
_FORBIDDEN = ("SIMPRAXIS_HOST_SHIM", "simpraxis-host-shim", "host_shim",
              "平台托底", "sitecustomize", "shim_core")

# label 防注入：只留 [0-9A-Za-z_.-]，违例片段替 ?；硬截断 40 字符（§2.7 m5）。
_LABEL_ALLOWED_RE = re.compile(r"[^0-9A-Za-z_.-]")
_LABEL_MAX_CHARS = 40

_BANNER_FMT = (
    "[simpraxis-host-shim v1] 平台托底激活（非模型代码） "
    "site=torch pid={pid} label={label} torch={version}\n")
_ATEXIT_FMT = (
    "[simpraxis-host-shim v1] 平台托底（非模型代码） "
    "site={site} pid={pid} label={label} "
    "array_migrated={a} numpy_migrated={b} cpu_grad_detached={c}\n")

# 迁移计数（GIL 下 +=，不加锁，§2.5）。
cnt = {"array_migrated": 0, "numpy_migrated": 0, "cpu_grad_detached": 0}

_PATCHED = False
_torch_module = None
_label = "?"
_atexit_registered = False


# ---------------------------------------------------------------------------
# A1 模型面脱敏（规格 §2.8 逐字；幂等纯函数，零替身影子文本）
# ---------------------------------------------------------------------------

def redact_for_model(text: str) -> str:
    prelude = PRELUDE_DIR
    kept = []
    for line in (text or "").splitlines(keepends=True):
        if any(w in line for w in _FORBIDDEN):
            continue                      # 物理行整行丢弃，零替身影子文本
        if prelude and prelude in line:
            continue                      # 含 prelude 绝对路径的行整行丢弃
        kept.append(line)
    return "".join(kept)


# ---------------------------------------------------------------------------
# label 解析（m5，纯字符串防注入）
# ---------------------------------------------------------------------------

def _sanitize_label(raw: str) -> str:
    return _LABEL_ALLOWED_RE.sub("?", raw)[:_LABEL_MAX_CHARS]


def resolve_label(argv) -> str:
    """优先 ``--label`` 后值；否则 basename(argv[0])[-argv[1]]（§2.7）。

    argv[1] 以 ``-`` 开头（如 ``--sidecar``）时不拼接。白名单净化 + 40
    字符硬截断在最后统一施加，超长派生 out_dir 形态同样截断（W1b）。
    """
    argv = list(argv or [])
    for i, tok in enumerate(argv):
        if tok == "--label" and i + 1 < len(argv):
            return _sanitize_label(str(argv[i + 1]))
    if not argv:
        return "?"
    base = os.path.basename(str(argv[0])) or "?"
    if len(argv) >= 2 and not str(argv[1]).startswith("-"):
        base = f"{base}-{argv[1]}"
    return _sanitize_label(base)


# ---------------------------------------------------------------------------
# 纯 stderr 观测（写端一律 \n；零写盘）
# ---------------------------------------------------------------------------

def _emit(line: str) -> None:
    try:
        sys.stderr.write(line)
        sys.stderr.flush()
    except Exception:
        pass


def _atexit_line() -> None:
    site = "torch" if _PATCHED else "idle"
    _emit(_ATEXIT_FMT.format(
        site=site, pid=os.getpid(), label=_label,
        a=cnt["array_migrated"], b=cnt["numpy_migrated"],
        c=cnt["cpu_grad_detached"]))


# ---------------------------------------------------------------------------
# torch 惰性补丁（post-import finder/loader，§2.4/§2.5）
# ---------------------------------------------------------------------------

class _ShimLoader:
    """包原 loader：先执行真 torch，再打补丁；torch 导入异常原样传播。"""

    def __init__(self, original):
        self._original = original

    def create_module(self, spec):
        create = getattr(self._original, "create_module", None)
        if create is not None:
            return create(spec)
        return None

    def exec_module(self, module):
        self._original.exec_module(module)
        _patch(module)


class _ShimFinder:
    """一次性 post-import finder：只匹配顶层 ``torch``，子模块不拦。"""

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "torch":
            return None
        # 命中先自摘除（防递归、一次性，不回插）。
        try:
            sys.meta_path.remove(self)
        except ValueError:
            pass
        # 剩余 finders 只查不执行；破损安装三异常（m2）→ None 不炸启动。
        try:
            real_spec = importlib.util.find_spec("torch")
        except (ModuleNotFoundError, ValueError, ImportError):
            return None
        if real_spec is None or real_spec.loader is None:
            return None
        real_spec.loader = _ShimLoader(real_spec.loader)
        return real_spec


def _patch(torch) -> None:
    """包 ``Tensor.__array__`` / ``Tensor.numpy``（§2.5 逐字语义）。"""
    global _PATCHED, _torch_module
    if _PATCHED:
        return
    tensor_cls = getattr(torch, "Tensor", None)
    if tensor_cls is None:
        return
    if getattr(tensor_cls, "_simpraxis_host_shim_v1", False):
        # 双保险幂等：sentinel 已在（重复 activate/重复导入），直接收口。
        _PATCHED = True
        _torch_module = torch
        return

    _orig_array = tensor_cls.__array__
    _orig_numpy = tensor_cls.numpy

    def _array(self, dtype=None):
        if self.is_cuda:
            cnt["array_migrated"] += 1
            self = self.detach().cpu()
        elif self.requires_grad:
            cnt["cpu_grad_detached"] += 1
            self = self.detach()
        return _orig_array(self, dtype)       # 已 cpu 张量内部调 numpy 直通不计数

    def _numpy(self, *a, **kw):               # kw 含 force=True：透传不特判（m8）
        if self.is_cuda:
            cnt["numpy_migrated"] += 1
            self = self.detach().cpu()
        elif self.requires_grad:
            cnt["cpu_grad_detached"] += 1
            self = self.detach()
        return _orig_numpy(self, *a, **kw)

    tensor_cls.__array__ = _array
    tensor_cls.numpy = _numpy
    tensor_cls._simpraxis_host_shim_v1 = True
    _PATCHED = True
    _torch_module = torch
    _emit(_BANNER_FMT.format(
        pid=os.getpid(), label=_label,
        version=getattr(torch, "__version__", "unknown")))


def activate() -> None:
    """注册 atexit（无条件）并补丁 torch（已导入立即补，否则装 finder）。

    重复调用幂等（§2.6）：atexit 只注册一次、finder 不重复安装、包装不
    双层、计数不翻倍。
    """
    global _label, _atexit_registered
    _label = resolve_label(getattr(sys, "argv", None))
    if not _atexit_registered:
        atexit.register(_atexit_line)
        _atexit_registered = True
    existing = sys.modules.get("torch")
    if existing is not None and getattr(existing, "Tensor", None) is not None:
        _patch(existing)
        return
    if not any(isinstance(f, _ShimFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _ShimFinder())
