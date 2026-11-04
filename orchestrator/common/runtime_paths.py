"""Phase 2 运行时路径解析的唯一入口（Task0 工程基座）。

Phase 1 的台账/状态路径散落在多个模块各自读 ``SIMPRAXIS_PHASE1_ROOT``；
Phase 2 起台账类路径（ledger/counters/approvals）统一由本模块解析到
**独立新账** ``$SIMPRAXIS_PHASE2_ROOT/quota/``，Phase 1 封存账保持只读、
永不被新代码以写方式打开（D-1：独立新账，seq 从 0 起）。

目录布局（全部 /mnt，仓外，0700；见 scripts/00_env.sh）：
  $SIMPRAXIS_PHASE1_ROOT/quota/ledger.jsonl      # Phase 1 封存账（29 行，只读）
  $SIMPRAXIS_PHASE2_ROOT/quota/ledger.jsonl      # Phase 2 封存账（455 行，只读）
  $SIMPRAXIS_PHASE25_ROOT/quota/ledger.jsonl     # Phase 2.5 独立新账（空账，seq 从 0）
  $SIMPRAXIS_PHASE2_ROOT/runs/                   # Phase 2 实跑产物根
  $SIMPRAXIS_PHASE25_ROOT/runs/                  # Phase 2.5 实跑产物根
  $SIMPRAXIS_PHASE26_ROOT/runs/                  # Phase 2.6 实跑产物根
  $SIMPRAXIS_GATEWAY_STATE/quota_pause.json      # Phase1/2 熔断态（沿用，不随账分根）
  $SIMPRAXIS_PHASE25_ROOT/quota/gateway_state/   # Phase 2.5 独立熔断态目录
  $SIMPRAXIS_PHASE26_ROOT/quota/gateway_state/   # Phase 2.6 独立熔断态目录
  $SIMPRAXIS_PHASE27_ROOT/quota/ledger.jsonl     # Phase 2.7 独立新账（空账，seq 从 0）
  $SIMPRAXIS_PHASE27_ROOT/runs/                  # Phase 2.7 实跑产物根

未导出 SIMPRAXIS_PHASE2_ROOT/SIMPRAXIS_PHASE25_ROOT 时回退到 phase1 根的同级
``phase2/``、``phase25/``：生产默认布局即
/mnt/artifacts/simpraxis/{phase1,phase2,phase25}；测试若只把
SIMPRAXIS_PHASE1_ROOT 指向临时根，新账自动落在临时根兄弟下，绝不误写真实
/mnt（锁内适配（Task0）：计划只写「读 SIMPRAXIS_PHASE2_ROOT」，未规定缺省值）。

Phase 2.5（Task0，D-1）：Phase 1/2 账【封存只读】——新代码写账一律走
phase25_ledger_path()；assert_not_sealed_ledger()/open_ledger_append() 是
唯一追加写入口，对 Phase 1/2 账路径直接 SealedLedgerError。

Phase 2.6（Task0，D0-2）：封存集扩为 {phase1, phase2, phase25}，三账【永不再写】；
新代码写账一律走 phase26_ledger_path()（open_ledger_append 的缺省目标亦已换期）。
四根隔离算法只保有一份（_assert_roots_isolated，D0-3），phase25/phase26 两侧都是
薄包装。

Phase 2.7（Task0，D0-2/D0-3）：封存集再扩为 {phase1, phase2, phase25, phase26}，四账
【永不再写】；新代码写账一律走 phase27_ledger_path()。``open_ledger_append()`` 的缺省
目标**仍**留在 phase26（改它会让 Phase 2.6 既有断言红），因此它在 2.7 起**必然抛
SealedLedgerError**——写入方必须显式传 phase27 路径（见 D0-3）。
"""
from __future__ import annotations

import os
from pathlib import Path

from orchestrator.common.fsutil import ensure_private_dir

DEFAULT_DATA_ROOT = "/mnt/artifacts/simpraxis"
_GATEWAY_STATE_ENV = "SIMPRAXIS_GATEWAY_STATE"
_PHASE1_ENV = "SIMPRAXIS_PHASE1_ROOT"
_PHASE2_ENV = "SIMPRAXIS_PHASE2_ROOT"
_PHASE25_ENV = "SIMPRAXIS_PHASE25_ROOT"
_PHASE26_ENV = "SIMPRAXIS_PHASE26_ROOT"
_PHASE27_ENV = "SIMPRAXIS_PHASE27_ROOT"


class SealedLedgerError(PermissionError):
    """封存台账被以写方式打开（Phase 1/2/2.5 账只读，Phase 2.6 起一律 phase26 新账）。"""


class PhaseRootAliasedError(PermissionError):
    """Phase 2.5 根与 Phase 1/2 根同体或互相嵌套（M-B，2026-10-29 评审修单）。

    别名/嵌套时 phase25 的写不只碰 ledger（那一路有 SealedLedgerError 兜底），
    approvals/*.json、ledger.degraded.*、gateway_state/、.ledger.lock、
    resume_audit.jsonl、runs/ 都会落进封存根。故须在任何 mkdir/写系统调用
    之前 fail-fast（构造点与 write_approval/resume 模块级函数三个卡口，
    外加 scripts/00_env.sh 的 shell 镜像检查覆盖 runs/ 与非 Python 消费者）。
    """


def _assert_roots_isolated(active: Path, sealed: "tuple[Path, ...]", *,
                           label: str, message: str) -> None:
    """活跃根与全部封存根两两互异且互不嵌套，否则 fail-fast（唯一实现，D0-3）。

    路径按调用时点 env 做 resolve(strict=False)（不存在也归一，已存在组件
    中的符号链接被解析），与 sealed_ledger_paths() 同一口径；嵌套判定用
    Path.relative_to（相等或一方为另一方后代即拒，故
    PHASE25_ROOT=$PHASE2_ROOT/quota 这类嵌套同样被拦）。**封存根之间也两两
    互查**——保留相位 2.5 原实现对 (p25,p2,p1) 全配对的检查强度。

    label/message 由各期包装逐字给定：phase25 的报错文案必须与相位 2.5
    时点逐字节相同（历史断言与运维手册引用了它）。
    """
    roots = (Path(active).resolve(strict=False),
             *(Path(p).resolve(strict=False) for p in sealed))
    aliased = False
    for i, a in enumerate(roots):
        for b in roots[i + 1:]:
            if a == b:
                aliased = True
                continue
            try:
                a.relative_to(b)
            except ValueError:
                pass
            else:
                aliased = True
            try:
                b.relative_to(a)
            except ValueError:
                pass
            else:
                aliased = True
    if aliased:
        raise PhaseRootAliasedError(f"{label} {message}")


def assert_phase25_roots_isolated() -> None:
    """phase25/phase2/phase1 三根必须两两互异且互不嵌套，否则 fail-fast。"""
    p1 = phase1_root().resolve(strict=False)
    p2 = phase2_root().resolve(strict=False)
    p25 = phase25_root().resolve(strict=False)
    _assert_roots_isolated(
        phase25_root(), (phase2_root(), phase1_root()),
        label="SIMPRAXIS_PHASE25_ROOT",
        message=("必须与 Phase 1/2 根互异且互不嵌套"
                 f"（解析后）：phase1={p1} phase2={p2} phase25={p25}"))


def assert_phase26_roots_isolated() -> None:
    """phase26 与三份封存根（phase1/phase2/phase25）两两互异且互不嵌套。"""
    p1 = phase1_root().resolve(strict=False)
    p2 = phase2_root().resolve(strict=False)
    p25 = phase25_root().resolve(strict=False)
    p26 = phase26_root().resolve(strict=False)
    _assert_roots_isolated(
        phase26_root(), (phase1_root(), phase2_root(), phase25_root()),
        label="SIMPRAXIS_PHASE26_ROOT",
        message=("必须与 Phase 1/2/2.5 封存根互异且互不嵌套"
                 f"（解析后）：phase1={p1} phase2={p2} phase25={p25} "
                 f"phase26={p26}"))


def assert_phase27_roots_isolated() -> None:
    """phase27 与四份封存根（phase1/phase2/phase25/phase26）两两互异且互不嵌套。

    Phase 2.7 起启用（D0-2）：phase26 已封存，phase27 是当前唯一写入者；
    别名/嵌套时 approvals/*.json、ledger.degraded.*、gateway_state/、
    .ledger.lock、resume_audit.jsonl、runs/ 都会落进封存树，故须在任何
    mkdir/写系统调用之前 fail-fast（同 phase25/26 的 _assert_roots_isolated）。
    """
    p1 = phase1_root().resolve(strict=False)
    p2 = phase2_root().resolve(strict=False)
    p25 = phase25_root().resolve(strict=False)
    p26 = phase26_root().resolve(strict=False)
    p27 = phase27_root().resolve(strict=False)
    _assert_roots_isolated(
        phase27_root(), (phase1_root(), phase2_root(), phase25_root(),
                         phase26_root()),
        label="SIMPRAXIS_PHASE27_ROOT",
        message=("必须与 Phase 1/2/2.5/2.6 封存根互异且互不嵌套"
                 f"（解析后）：phase1={p1} phase2={p2} phase25={p25} "
                 f"phase26={p26} phase27={p27}"))


def phase1_root() -> Path:
    return Path(os.environ.get(_PHASE1_ENV, f"{DEFAULT_DATA_ROOT}/phase1"))


def phase2_root() -> Path:
    """Phase 2 根：显式 env 优先；否则取 phase1 根同级 ``phase2/``（不建目录）。"""
    root = os.environ.get(_PHASE2_ENV)
    if root:
        return Path(root)
    return phase1_root().parent / "phase2"


def phase2_runs_root() -> Path:
    return phase2_root() / "runs"


# ---- 台账类路径（Phase 2 独立新账；0700 目录在 quota_dir() 内收敛） ----

def phase2_quota_dir() -> Path:
    return ensure_private_dir(phase2_root() / "quota")


def phase2_ledger_path() -> Path:
    return phase2_quota_dir() / "ledger.jsonl"


def phase2_counters_path() -> Path:
    return phase2_quota_dir() / "counters.json"


def phase2_approvals_dir() -> Path:
    return ensure_private_dir(phase2_quota_dir() / "approvals")


# ---- Phase 2.5 路径族（D-1 独立新账；env 缺省回退兄弟 phase25/，形态同 phase2） ----

def phase25_root() -> Path:
    """Phase 2.5 根：显式 env 优先；否则取 phase1 根同级 ``phase25/``（不建目录）。"""
    root = os.environ.get(_PHASE25_ENV)
    if root:
        return Path(root)
    return phase1_root().parent / "phase25"


def phase25_runs_root() -> Path:
    return phase25_root() / "runs"


def phase25_quota_dir() -> Path:
    return ensure_private_dir(phase25_root() / "quota")


def phase25_ledger_path() -> Path:
    return phase25_quota_dir() / "ledger.jsonl"


def phase25_counters_path() -> Path:
    return phase25_quota_dir() / "counters.json"


def phase25_approvals_dir() -> Path:
    return ensure_private_dir(phase25_quota_dir() / "approvals")


def phase25_gateway_state_dir() -> Path:
    # Phase 2.5 熔断态随本账命名空间隔离（不读不写全局 SIMPRAXIS_GATEWAY_STATE）。
    return ensure_private_dir(phase25_root() / "quota" / "gateway_state")


def phase25_gateway_state_path() -> Path:
    return phase25_gateway_state_dir() / "quota_pause.json"


# ---- Phase 2.6 路径族（D0-1 复制而非泛化；env 缺省回退兄弟 phase26/） ----

def phase26_root() -> Path:
    """Phase 2.6 根：显式 env 优先；否则取 phase1 根同级 ``phase26/``（不建目录）。"""
    root = os.environ.get(_PHASE26_ENV)
    if root:
        return Path(root)
    return phase1_root().parent / "phase26"


def phase26_runs_root() -> Path:
    return phase26_root() / "runs"


def phase26_quota_dir() -> Path:
    return ensure_private_dir(phase26_root() / "quota")


def phase26_ledger_path() -> Path:
    return phase26_quota_dir() / "ledger.jsonl"


def phase26_counters_path() -> Path:
    return phase26_quota_dir() / "counters.json"


def phase26_approvals_dir() -> Path:
    return ensure_private_dir(phase26_quota_dir() / "approvals")


def phase26_gateway_state_dir() -> Path:
    # Phase 2.6 熔断态随本账命名空间隔离（不读不写全局 SIMPRAXIS_GATEWAY_STATE）。
    return ensure_private_dir(phase26_root() / "quota" / "gateway_state")


def phase26_gateway_state_path() -> Path:
    return phase26_gateway_state_dir() / "quota_pause.json"


# ---- Phase 2.7 路径族（D0-1 复制而非泛化；env 缺省回退兄弟 phase27/） ----

def phase27_root() -> Path:
    """Phase 2.7 根：显式 env 优先；否则取 phase1 根同级 ``phase27/``（不建目录）。"""
    root = os.environ.get(_PHASE27_ENV)
    if root:
        return Path(root)
    return phase1_root().parent / "phase27"


def phase27_runs_root() -> Path:
    return phase27_root() / "runs"


def phase27_quota_dir() -> Path:
    return ensure_private_dir(phase27_root() / "quota")


def phase27_ledger_path() -> Path:
    return phase27_quota_dir() / "ledger.jsonl"


def phase27_preview_cache_dir() -> Path:
    """预览探针的**编译缓存**宿主目录（0700；AC-T3-2 的 G0-12 裁决物）。

    2026-11-04 用户裁决（AC-T3-2 冷启实测 96.6 s > 60 s）：taichi/numba/
    着色器编译占了 ~76 s，而它们**本来就是可缓存的**——把缓存放到持久
    目录后同镜像第二次实测 wall_s=15.1 s。故新增一条 rw 挂载
    ``<本目录> → /cache``（`run.py::_validate_cache_mount` 是唯一校验点），
    首次预热 99.3 s 由 AC-T3-2 单独取证，判据数值 60 s 不变。
    """
    return ensure_private_dir(phase27_root() / "cache" / "genesis")


def phase27_counters_path() -> Path:
    return phase27_quota_dir() / "counters.json"


def phase27_approvals_dir() -> Path:
    return ensure_private_dir(phase27_quota_dir() / "approvals")


def phase27_gateway_state_dir() -> Path:
    # Phase 2.7 熔断态随本账命名空间隔离（不读不写全局 SIMPRAXIS_GATEWAY_STATE）。
    return ensure_private_dir(phase27_root() / "quota" / "gateway_state")


def phase27_gateway_state_path() -> Path:
    return phase27_gateway_state_dir() / "quota_pause.json"


def phase27_estimates_dir() -> Path:
    """Phase 2.7 估计/基线件目录（phase26 无此函数；D0-5 把 T0 基线落于此）。"""
    return ensure_private_dir(phase27_root() / "estimates")


# ---- 熔断态（沿用独立 env/默认根，Task0 只统一入口、不改 env 契约） ----

def gateway_state_dir() -> Path:
    return Path(os.environ.get(_GATEWAY_STATE_ENV,
                               f"{DEFAULT_DATA_ROOT}/gateway_state"))


def gateway_state_path() -> Path:
    return gateway_state_dir() / "quota_pause.json"


# ---- Phase 1/2 封存账写守卫（Phase 2.5 Task0；新账唯一追加写入口） ----

def phase1_ledger_path() -> Path:
    return phase1_root() / "quota" / "ledger.jsonl"


def sealed_ledger_paths() -> frozenset[Path]:
    """Phase 1/2/2.5/2.6 封存账的绝对路径集（resolve 归一；strict=False 允许账体不在场）。

    路径以【调用时点】env 解析为准：测试把 SIMPRAXIS_PHASE{1,2,25,26}_ROOT 指向
    临时根时，守卫随之指向临时根里的账，绝不误判真实 /mnt 账。

    Phase 2.6（D0-2）：phase25 已结案封存，本集扩为三份；任何写入一律走
    phase26_ledger_path()。
    Phase 2.7（D0-2）：phase26 随 2.6 收束一并封存，本集扩为四份；任何写入一律走
    phase27_ledger_path()（``open_ledger_append()`` 的缺省目标**仍**是 phase26，
    D0-3：改缺省会让 Phase 2.6 既有断言红，故调用方显式传参）。
    """
    return frozenset({
        phase1_ledger_path().resolve(strict=False),
        phase2_ledger_path().resolve(strict=False),
        phase25_ledger_path().resolve(strict=False),
        phase26_ledger_path().resolve(strict=False),
    })


def assert_not_sealed_ledger(path: "str | Path") -> Path:
    """写打开前置守卫：目标解析到 Phase 1/2/2.5/2.6 封存账即抛 SealedLedgerError。"""
    p = Path(path)
    if p.resolve(strict=False) in sealed_ledger_paths():
        raise SealedLedgerError(
            f"封存台账禁止以写方式打开：{p}（Phase 1/2/2.5/2.6 封存账只读；"
            "Phase 2.7 新写账必须走 phase27_ledger_path()）")
    return p


def open_ledger_append(path: "str | Path | None" = None):
    """台账追加写唯一入口：先过封存守卫，再以 "a" 打开，文件权限收敛 0600。

    缺省目标是 phase26 新账（Phase 2.7 D0-3：缺省值不动，故 2.7 起缺省调用
    必然抛 SealedLedgerError，写入方须显式传 phase27 路径）；调用方显式传 path
    时同样先过封存守卫，因此任何经本入口的写都不可能落到 Phase 1/2/2.5/2.6
    封存账。
    """
    target = path if path is not None else phase26_ledger_path()
    p = assert_not_sealed_ledger(target)
    ensure_private_dir(p.parent)
    fh = open(p, "a", encoding="utf-8")  # noqa: SIM115 —— 调用方负责关闭
    try:
        os.chmod(p, 0o600)
    except OSError:
        # 权限收敛失败不阻塞追加（目录已 0700 私有权限）；与既有 _append_line 口径一致。
        pass
    return fh
