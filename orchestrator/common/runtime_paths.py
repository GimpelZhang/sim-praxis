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

Phase 2.8.1（Task0，D0-2/D0-3）：封存集再扩为 {phase1, phase2, phase25, phase26,
phase27}，五账【永不再写】；新代码写账一律走 ``phase281_ledger_path()``。
``open_ledger_append()`` 的缺省目标仍留在 phase26，故它在 2.8.1 起同样必然抛。
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
_PHASE281_ENV = "SIMPRAXIS_PHASE281_ROOT"
_RSI_R0_ROOT_ENV = "SIMPRAXIS_RSI_R0_ROOT"          # R0 家族根（campaign 的父目录）
_RSI_R0_CAMPAIGN_ENV = "SIMPRAXIS_RSI_R0_CAMPAIGN"  # R0 campaign id（写路径必填）
_RSI_R01_ROOT_ENV = "SIMPRAXIS_RSI_R01_ROOT"        # R0.1 家族根（campaign 的父目录）
_RSI_R01_CAMPAIGN_ENV = "SIMPRAXIS_RSI_R01_CAMPAIGN"  # R0.1 campaign id（写路径必填）


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


# ---- Phase 2.8.1 路径族（D0-1 复制而非泛化；env 缺省回退兄弟 phase281/） ----

def phase281_root() -> Path:
    """Phase 2.8.1 根：显式 env 优先；否则取 phase1 根同级 ``phase281/``。

    生产默认 ``/mnt/artifacts/simpraxis/phase281``——与 phase1/2/25/26/27 同父，
    故与五者的父子关系一律由 ``assert_phase281_roots_isolated()`` 拦下。
    """
    root = os.environ.get(_PHASE281_ENV)
    if root:
        return Path(root)
    return phase1_root().parent / "phase281"


def phase281_runs_root() -> Path:
    return phase281_root() / "runs"


def phase281_quota_dir() -> Path:
    return ensure_private_dir(phase281_root() / "quota")


def phase281_ledger_path() -> Path:
    return phase281_quota_dir() / "ledger.jsonl"


def phase281_counters_path() -> Path:
    return phase281_quota_dir() / "counters.json"


def phase281_approvals_dir() -> Path:
    return ensure_private_dir(phase281_quota_dir() / "approvals")


def phase281_gateway_state_dir() -> Path:
    # Phase 2.8.1 熔断态随本账命名空间隔离（不读不写全局 SIMPRAXIS_GATEWAY_STATE）。
    return ensure_private_dir(phase281_root() / "quota" / "gateway_state")


def phase281_gateway_state_path() -> Path:
    return phase281_gateway_state_dir() / "quota_pause.json"


def assert_phase281_roots_isolated() -> None:
    """phase281 与五份封存根（phase1/2/2.5/2.6/2.7）两两互异且互不嵌套。

    Phase 2.8.1 起启用（D0-2）：phase27 已封存，phase281 是当前唯一写入者；
    别名/嵌套时 approvals/*.json、ledger.degraded.*、gateway_state/、
    .ledger.lock、resume_audit.jsonl、runs/ 都会落进封存树，故须在任何
    mkdir/写系统调用之前 fail-fast（同 phase25/26/27 的 _assert_roots_isolated）。

    只读引用既有 ``_assert_roots_isolated``（唯一实现），不另写一套判定。
    """
    p1 = phase1_root().resolve(strict=False)
    p2 = phase2_root().resolve(strict=False)
    p25 = phase25_root().resolve(strict=False)
    p26 = phase26_root().resolve(strict=False)
    p27 = phase27_root().resolve(strict=False)
    p281 = phase281_root().resolve(strict=False)
    _assert_roots_isolated(
        phase281_root(), (phase1_root(), phase2_root(), phase25_root(),
                          phase26_root(), phase27_root()),
        label="SIMPRAXIS_PHASE281_ROOT",
        message=("必须与 Phase 1/2/2.5/2.6/2.7 封存根互异且互不嵌套"
                 f"（解析后）：phase1={p1} phase2={p2} phase25={p25} "
                 f"phase26={p26} phase27={p27} phase281={p281}"))


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
    """Phase 1/2/2.5/2.6/2.7 封存账的绝对路径集（resolve 归一；strict=False 允许账体不在场）。

    路径以【调用时点】env 解析为准：测试把 SIMPRAXIS_PHASE{1,2,25,26,27}_ROOT
    指向临时根时，守卫随之指向临时根里的账，绝不误判真实 /mnt 账。

    Phase 2.6（D0-2）：phase25 已结案封存，本集扩为三份；任何写入一律走
    phase26_ledger_path()。
    Phase 2.7（D0-2）：phase26 随 2.6 收束一并封存，本集扩为四份；任何写入一律走
    phase27_ledger_path()（``open_ledger_append()`` 的缺省目标**仍**是 phase26，
    D0-3：改缺省会让 Phase 2.6 既有断言红，故调用方显式传参）。
    Phase 2.8.1（D0-2，2026-11-06）：phase27 随 2.7 收束一并封存，本集扩为**五份**；
    任何写入一律走 phase281_ledger_path()（缺省目标同样不动，理由同上）。
    RSI-R0（T1，2026-11-08）：phase281 随 2.8.1 按 HOLD 结案一并封存，本集扩为**六份**；
    任何写入一律走 rsi_r0_ledger_path()（R0 新账，见下）。
    """
    return frozenset({
        phase1_ledger_path().resolve(strict=False),
        phase2_ledger_path().resolve(strict=False),
        phase25_ledger_path().resolve(strict=False),
        phase26_ledger_path().resolve(strict=False),
        phase27_ledger_path().resolve(strict=False),
        phase281_ledger_path().resolve(strict=False),
    })


# 已结案 campaign 的封存标记：closeout 产物件存在即视为该 campaign 已关闭。
# RSI 家族以 campaign 为单位（同族可有几十个根，无法枚举进 sealed_ledger_paths），
# 因此用「结案件在盘」做标记，而不是维护一份会漏项的路径清单。
_CLOSE_MARKER_RELPATHS: tuple[str, ...] = (
    "closeout/r12_readout.json",
    "closeout/r1_readout.json",
    "closeout/closeout_zero_paid.json",
    "closeout/closeout_report.json",
)


def closed_campaign_root(path: "str | Path") -> "Path | None":
    """若 ``path`` 是某已结案 campaign 的 ``quota/ledger.jsonl``，返回该 campaign 根。

    判据是 closeout 产物件（各 RSI 家族统一写在 ``closeout/``）存在；只认
    ``<campaign>/quota/ledger.jsonl`` 这一形状，避免把同名文件误判为账本。
    """
    p = Path(path)
    if p.name != "ledger.jsonl" or p.parent.name != "quota":
        return None
    root = p.parent.parent
    for rel in _CLOSE_MARKER_RELPATHS:
        if (root / rel).is_file():
            return root
    return None


def assert_not_sealed_ledger(path: "str | Path") -> Path:
    """写打开前置守卫：目标解析到任一封存账（Phase 1/2/2.5/2.6/2.7/2.8.1）即抛。

    RSI 家族（R0/R0.1/R0.2/R1/R1.2）另按「campaign 已结案」判定：账本同根存在
    closeout 产物件时同样拒绝写入（campaign 数不可枚举，不能并入路径集）。
    """
    p = Path(path)
    if p.resolve(strict=False) in sealed_ledger_paths():
        raise SealedLedgerError(
            f"封存台账禁止以写方式打开：{p}（Phase 1/2/2.5/2.6/2.7/2.8.1 封存账只读；"
            "RSI-R0 新写账必须走 rsi_r0_ledger_path()）")
    closed_root = closed_campaign_root(p)
    if closed_root is not None:
        raise SealedLedgerError(
            f"已结案 campaign 台账禁止以写方式打开：{p}"
            f"（{closed_root} 含 closeout 产物，族账只读；新战役请新建 campaign 根）")
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


# ---- RSI-R0 路径族（T1，2026-11-08；计划 §5.1/§5.3） ----
#
# R0 以 **campaign** 为单位而非按期号一次一封：同一家族根下可有多个 campaign，
# 每个 campaign 自带 protocol/private/quota/gateway_state/runs/closeout 六族。
# 因此路径解析比历史四期多一层 ``SIMPRAXIS_RSI_R0_CAMPAIGN``：家族根由
# ``SIMPRAXIS_RSI_R0_ROOT``（缺省 ``$DATA_ROOT/rsi_r0``）给出，campaign 名由
# 环境变量给出；未设置时任何写路径函数直接 fail-closed，绝不回退到别人的
# campaign 里（AC03/AC05）。

class RsiR0CampaignUnsetError(RuntimeError):
    """未指定 R0 campaign 就解析写路径——fail-closed，不回退到任何缺省 campaign。"""


def _validate_campaign_id(campaign_id: str, *,
                          error_cls: type = RsiR0CampaignUnsetError,
                          label: str = "R0") -> str:
    """campaign id 只允许安全字符（它会成为目录名；拒绝路径穿越）。

    R0.1（T0）复用同一判定，只换错误类与标签；R0 的报错文案逐字节不变。
    """
    cid = str(campaign_id)
    if not cid or cid in (".", "..") or "/" in cid or "\\" in cid:
        raise error_cls(f"非法 {label} campaign id：{cid!r}")
    # ASCII 白名单（不是 str.isalnum：后者对 'é' 也返回 True，会放过非 ASCII 目录名）。
    if not all(ch.isascii() and (ch.isalnum() or ch in "-_.") for ch in cid):
        raise error_cls(f"{label} campaign id 含不安全字符：{cid!r}")
    return cid


def rsi_r0_root() -> Path:
    """R0 家族根：显式 env 优先；否则 ``$DATA_ROOT/rsi_r0``。"""
    root = os.environ.get(_RSI_R0_ROOT_ENV)
    if root:
        return Path(root)
    return Path(DEFAULT_DATA_ROOT) / "rsi_r0"


def rsi_r0_campaign_id() -> str:
    cid = os.environ.get(_RSI_R0_CAMPAIGN_ENV, "")
    if not cid:
        raise RsiR0CampaignUnsetError(
            f"{_RSI_R0_CAMPAIGN_ENV} 未设置：R0 写路径必须显式指定 campaign")
    return _validate_campaign_id(cid)


def rsi_r0_campaign_root() -> Path:
    return rsi_r0_root() / rsi_r0_campaign_id()


def rsi_r0_runs_root() -> Path:
    return rsi_r0_campaign_root() / "runs"


def rsi_r0_quota_dir() -> Path:
    return ensure_private_dir(rsi_r0_campaign_root() / "quota")


def rsi_r0_ledger_path() -> Path:
    return rsi_r0_quota_dir() / "ledger.jsonl"


def rsi_r0_counters_path() -> Path:
    return rsi_r0_quota_dir() / "counters.json"


def rsi_r0_approvals_dir() -> Path:
    return ensure_private_dir(rsi_r0_quota_dir() / "approvals")


def rsi_r0_reservations_dir() -> Path:
    """资源预留根（计划 §5.3：预留与结算分账，靠 request_id 关联）。"""
    return ensure_private_dir(rsi_r0_quota_dir() / "resource_reservations")


def rsi_r0_attempts_path() -> Path:
    """发网事实日志（每次逻辑调用一行 pre/post；与 ledger 分权）。"""
    return rsi_r0_quota_dir() / "attempts.jsonl"


def rsi_r0_gateway_state_dir() -> Path:
    # R0 熔断/暂停态随本 campaign 隔离，绝不读写全局 SIMPRAXIS_GATEWAY_STATE。
    return ensure_private_dir(rsi_r0_campaign_root() / "gateway_state")


def rsi_r0_gateway_state_path() -> Path:
    return rsi_r0_gateway_state_dir() / "quota_pause.json"


def rsi_r0_private_dir() -> Path:
    return ensure_private_dir(rsi_r0_campaign_root() / "private")


def rsi_r0_protocol_dir() -> Path:
    return ensure_private_dir(rsi_r0_campaign_root() / "protocol")


def rsi_r0_closeout_dir() -> Path:
    return ensure_private_dir(rsi_r0_campaign_root() / "closeout")


def assert_rsi_r0_roots_isolated() -> None:
    """R0 campaign 根与六份封存根（phase1/2/2.5/2.6/2.7/2.8.1）两两互异且互不嵌套。

    与 phase281 同一判定实现（``_assert_roots_isolated``），在任何 mkdir/写系统
    调用之前 fail-fast：R0 的 approvals/、gateway_state/、runs/、attempts.jsonl
    若与封存根嵌套，会直接污染旧资产台账（AC03/AC05）。
    """
    campaign = rsi_r0_campaign_root().resolve(strict=False)
    seals = (phase1_root(), phase2_root(), phase25_root(), phase26_root(),
             phase27_root(), phase281_root())
    _assert_roots_isolated(
        rsi_r0_campaign_root(), seals,
        label=_RSI_R0_CAMPAIGN_ENV,
        message=("R0 campaign 根必须与 Phase 1/2/2.5/2.6/2.7/2.8.1 封存根互异且"
                 f"互不嵌套（解析后）：campaign={campaign} "
                 f"sealed={[str(p.resolve(strict=False)) for p in seals]}"))


# ---- RSI-R0.1 路径族（T0，2026-11-09；计划 §3.1/§3.2） ----
#
# R0.1 是**新 campaign 家族**，与 R0 家族并列：家族根缺省
# ``$DATA_ROOT/rsi_r01``，campaign 名由 ``SIMPRAXIS_RSI_R01_CAMPAIGN`` 给出。
# 与 R0 的差别（计划 §3.1）：
#   * 前端有三个公开/半公开子目录（public/、observations/、analysis/）——
#     候选模型只读挂载的前两类与受限分析输出目录；
#   * private/ 从不挂候选容器；
#   * 旧 R0 家族根进入**本家族的不可写根集**（R0.1 任何写路径不得落进旧根）。

class RsiR01CampaignUnsetError(RuntimeError):
    """未指定 R0.1 campaign 就解析写路径——fail-closed，不回退到任何缺省 campaign。"""


#: R0.1 campaign 子目录（计划 §3.1 逐字；quota/approvals 嵌套在 quota 下）。
RSI_R01_CAMPAIGN_SUBDIRS: "tuple[str, ...]" = (
    "contract", "private", "public", "observations", "analysis",
    "trials", "quota", "quota/approvals", "protocol", "closeout",
    "evidence_pack",
)


def sealed_phase_roots() -> "dict[str, Path]":
    """六份已封存期根的具名快照（R0.1 写守卫与 hash 清单共用）。"""
    return {
        "phase1": phase1_root(),
        "phase2": phase2_root(),
        "phase25": phase25_root(),
        "phase26": phase26_root(),
        "phase27": phase27_root(),
        "phase281": phase281_root(),
    }


def protected_write_roots() -> "dict[str, Path]":
    """R0.1 写路径的不可写根集：六份封存期根 + **R0 家族根**（旧实验整体只读）。"""
    roots = sealed_phase_roots()
    roots["rsi_r0"] = rsi_r0_root()
    return roots


def rsi_r01_root() -> Path:
    """R0.1 家族根：显式 env 优先；否则 ``$DATA_ROOT/rsi_r01``。"""
    root = os.environ.get(_RSI_R01_ROOT_ENV)
    if root:
        return Path(root)
    return Path(DEFAULT_DATA_ROOT) / "rsi_r01"


def rsi_r01_admission_root() -> Path:
    """条件预准入批次根（计划 §6.3 R01-M0；只在模型/供应变更时使用）。"""
    return rsi_r01_root().parent / "rsi_r01_admission"


def rsi_r01_campaign_id() -> str:
    cid = os.environ.get(_RSI_R01_CAMPAIGN_ENV, "")
    if not cid:
        raise RsiR01CampaignUnsetError(
            f"{_RSI_R01_CAMPAIGN_ENV} 未设置：R0.1 写路径必须显式指定 campaign")
    return _validate_campaign_id(cid, error_cls=RsiR01CampaignUnsetError,
                                 label="R0.1")


def rsi_r01_campaign_root() -> Path:
    return rsi_r01_root() / rsi_r01_campaign_id()


def rsi_r01_subdir(name: str) -> Path:
    """campaign 子目录解析（白名单；越界名直接拒绝，杜绝路径穿越）。"""
    if name not in RSI_R01_CAMPAIGN_SUBDIRS:
        raise RsiR01CampaignUnsetError(f"未知 R0.1 campaign 子目录：{name!r}")
    return rsi_r01_campaign_root() / name


def rsi_r01_contract_dir() -> Path:
    return ensure_private_dir(rsi_r01_subdir("contract"))


def rsi_r01_private_dir() -> Path:
    return ensure_private_dir(rsi_r01_subdir("private"))


def rsi_r01_public_dir() -> Path:
    return ensure_private_dir(rsi_r01_subdir("public"))


def rsi_r01_observations_dir() -> Path:
    return ensure_private_dir(rsi_r01_subdir("observations"))


def rsi_r01_analysis_dir() -> Path:
    return ensure_private_dir(rsi_r01_subdir("analysis"))


def rsi_r01_trials_dir() -> Path:
    return ensure_private_dir(rsi_r01_subdir("trials"))


def rsi_r01_quota_dir() -> Path:
    return ensure_private_dir(rsi_r01_subdir("quota"))


def rsi_r01_ledger_path() -> Path:
    return rsi_r01_quota_dir() / "ledger.jsonl"


def rsi_r01_counters_path() -> Path:
    return rsi_r01_quota_dir() / "counters.json"


def rsi_r01_approvals_dir() -> Path:
    return ensure_private_dir(rsi_r01_quota_dir() / "approvals")


def rsi_r01_reservations_dir() -> Path:
    return ensure_private_dir(rsi_r01_quota_dir() / "resource_reservations")


def rsi_r01_attempts_path() -> Path:
    return rsi_r01_quota_dir() / "attempts.jsonl"


def rsi_r01_gateway_state_dir() -> Path:
    # 熔断/暂停态随本 campaign 隔离，绝不读写全局 SIMPRAXIS_GATEWAY_STATE。
    return ensure_private_dir(rsi_r01_campaign_root() / "gateway_state")


def rsi_r01_gateway_state_path() -> Path:
    return rsi_r01_gateway_state_dir() / "quota_pause.json"


def rsi_r01_protocol_dir() -> Path:
    return ensure_private_dir(rsi_r01_subdir("protocol"))


def rsi_r01_closeout_dir() -> Path:
    return ensure_private_dir(rsi_r01_subdir("closeout"))


def rsi_r01_evidence_pack_dir() -> Path:
    return ensure_private_dir(rsi_r01_subdir("evidence_pack"))


def assert_rsi_r01_roots_isolated() -> None:
    """R0.1 campaign 根与六份封存期根 + R0 家族根两两互异且互不嵌套。

    在任何 mkdir/写系统调用之前 fail-fast（AC02/AC03）：R0.1 的 approvals/、
    gateway_state/、attempts.jsonl、observations/ 若与旧根嵌套，会直接污染
    旧实验台账与旧证据包。
    """
    campaign = rsi_r01_campaign_root().resolve(strict=False)
    protected = protected_write_roots()
    seals = tuple(protected.values())
    _assert_roots_isolated(
        rsi_r01_campaign_root(), seals,
        label=_RSI_R01_CAMPAIGN_ENV,
        message=("R0.1 campaign 根必须与 Phase 1/2/2.5/2.6/2.7/2.8.1 封存根及 "
                 f"RSI-R0 家族根互异且互不嵌套（解析后）：campaign={campaign} "
                 f"protected={[str(p.resolve(strict=False)) for p in seals]}"))


# ---- RSI-R0.2：独立新账（2026-11-09；照 RSI-R0.1 复制，不泛化；七账绝不并表）----
# 期号取 **2902**（Plan §3.3 建议值；T0 全仓扫描确认 2/25/26/27/281/2900/2901
# 已占用、2902 无冲突后锁定）。R0/R0.1 家族根全部进入 R0.2 的不可写根集：
# R0.2 的任何写路径不得落进旧实验根（AC02/AC36）。

_RSI_R02_ROOT_ENV = "SIMPRAXIS_RSI_R02_ROOT"        # R0.2 家族根（campaign 的父目录）
_RSI_R02_CAMPAIGN_ENV = "SIMPRAXIS_RSI_R02_CAMPAIGN"  # R0.2 campaign id（写路径必填）


class RsiR02CampaignUnsetError(RuntimeError):
    """未指定 R0.2 campaign 就解析写路径——fail-closed，不回退到任何缺省 campaign。"""


#: R0.2 campaign 子目录（Plan §3.3 逐字；quota/approvals 嵌套在 quota 下）。
RSI_R02_CAMPAIGN_SUBDIRS: "tuple[str, ...]" = (
    "contract", "private", "public", "observations", "analysis",
    "trials", "quota", "quota/approvals", "protocol", "closeout",
    "evidence_pack",
)


def rsi_r02_root() -> Path:
    """R0.2 家族根：显式 env 优先；否则 ``$DATA_ROOT/rsi_r02``。"""
    root = os.environ.get(_RSI_R02_ROOT_ENV)
    if root:
        return Path(root)
    return Path(DEFAULT_DATA_ROOT) / "rsi_r02"


def rsi_r02_campaign_id() -> str:
    cid = os.environ.get(_RSI_R02_CAMPAIGN_ENV, "")
    if not cid:
        raise RsiR02CampaignUnsetError(
            f"{_RSI_R02_CAMPAIGN_ENV} 未设置：R0.2 写路径必须显式指定 campaign")
    return _validate_campaign_id(cid, error_cls=RsiR02CampaignUnsetError,
                                 label="R0.2")


def rsi_r02_campaign_root() -> Path:
    return rsi_r02_root() / rsi_r02_campaign_id()


def rsi_r02_subdir(name: str) -> Path:
    """campaign 子目录解析（白名单；越界名直接拒绝，杜绝路径穿越）。"""
    if name not in RSI_R02_CAMPAIGN_SUBDIRS:
        raise RsiR02CampaignUnsetError(f"未知 R0.2 campaign 子目录：{name!r}")
    return rsi_r02_campaign_root() / name


def rsi_r02_contract_dir() -> Path:
    return ensure_private_dir(rsi_r02_subdir("contract"))


def rsi_r02_private_dir() -> Path:
    return ensure_private_dir(rsi_r02_subdir("private"))


def rsi_r02_public_dir() -> Path:
    return ensure_private_dir(rsi_r02_subdir("public"))


def rsi_r02_observations_dir() -> Path:
    return ensure_private_dir(rsi_r02_subdir("observations"))


def rsi_r02_analysis_dir() -> Path:
    return ensure_private_dir(rsi_r02_subdir("analysis"))


def rsi_r02_trials_dir() -> Path:
    return ensure_private_dir(rsi_r02_subdir("trials"))


def rsi_r02_quota_dir() -> Path:
    return ensure_private_dir(rsi_r02_subdir("quota"))


def rsi_r02_ledger_path() -> Path:
    return rsi_r02_quota_dir() / "ledger.jsonl"


def rsi_r02_counters_path() -> Path:
    return rsi_r02_quota_dir() / "counters.json"


def rsi_r02_approvals_dir() -> Path:
    return ensure_private_dir(rsi_r02_quota_dir() / "approvals")


def rsi_r02_reservations_dir() -> Path:
    return ensure_private_dir(rsi_r02_quota_dir() / "resource_reservations")


def rsi_r02_attempts_path() -> Path:
    return rsi_r02_quota_dir() / "attempts.jsonl"


def rsi_r02_gateway_state_dir() -> Path:
    # 熔断/暂停态随本 campaign 隔离，绝不读写全局 SIMPRAXIS_GATEWAY_STATE。
    return ensure_private_dir(rsi_r02_campaign_root() / "gateway_state")


def rsi_r02_gateway_state_path() -> Path:
    return rsi_r02_gateway_state_dir() / "quota_pause.json"


def rsi_r02_protocol_dir() -> Path:
    return ensure_private_dir(rsi_r02_subdir("protocol"))


def rsi_r02_closeout_dir() -> Path:
    return ensure_private_dir(rsi_r02_subdir("closeout"))


def rsi_r02_evidence_pack_dir() -> Path:
    return ensure_private_dir(rsi_r02_subdir("evidence_pack"))


def rsi_r02_protected_write_roots() -> "dict[str, Path]":
    """R0.2 写路径的不可写根集：六份封存期根 + R0 家族根 + **R0.1 家族根**。

    与 R0.1 的 ``protected_write_roots()`` 分开命名：后者被 R0.1 的历史代码与
    测试引用，改动它会改变已结案 campaign 的写守卫语义。
    """
    roots = sealed_phase_roots()
    roots["rsi_r0"] = rsi_r0_root()
    roots["rsi_r01"] = rsi_r01_root()
    return roots


def assert_rsi_r02_roots_isolated() -> None:
    """R0.2 campaign 根与六份封存期根 + R0/R0.1 家族根两两互异且互不嵌套。

    在任何 mkdir/写系统调用之前 fail-fast（AC02）：R0.2 的 approvals/、
    gateway_state/、attempts.jsonl、observations/ 若与旧根嵌套，会直接污染
    已结案实验的台账与证据包。
    """
    campaign = rsi_r02_campaign_root().resolve(strict=False)
    protected = rsi_r02_protected_write_roots()
    seals = tuple(protected.values())
    _assert_roots_isolated(
        rsi_r02_campaign_root(), seals,
        label=_RSI_R02_CAMPAIGN_ENV,
        message=("R0.2 campaign 根必须与 Phase 1/2/2.5/2.6/2.7/2.8.1 封存根及 "
                 f"RSI-R0/R0.1 家族根互异且互不嵌套（解析后）：campaign={campaign} "
                 f"protected={[str(p.resolve(strict=False)) for p in seals]}"))


# ---- RSI-R1：独立新账（2026-11-11；照 RSI-R0.2 复制，不泛化；九账绝不并表）----
# 期号取 **2903**（Plan §1 末段建议的候选值；T0 全仓扫描确认
# 2/25/26/27/281/2900/2901/2902 已占用、2903 无冲突后锁定）。R0/R0.1/R0.2
# 三族家族根全部进入 R1 的不可写根集：R1 的任何写路径不得落进旧实验根
# （AC01/AC03/AC36）。旧账继续只读，不改写任何历史行。

_RSI_R1_ROOT_ENV = "SIMPRAXIS_RSI_R1_ROOT"          # R1 家族根（campaign 的父目录）
_RSI_R1_CAMPAIGN_ENV = "SIMPRAXIS_RSI_R1_CAMPAIGN"  # R1 campaign id（写路径必填）


class RsiR1CampaignUnsetError(RuntimeError):
    """未指定 R1 campaign 就解析写路径——fail-closed，不回退到任何缺省 campaign。"""


#: R1 campaign 子目录（Plan §2.1/§5 的产物面；quota/approvals 嵌套在 quota 下）。
RSI_R1_CAMPAIGN_SUBDIRS: "tuple[str, ...]" = (
    "contract",          # §5.1 单源 schema / 公开合同 / 量表 / 预注册件
    "private",           # 维护侧校准私有件（候选不可挂载）
    "public",            # 候选可见的公开任务与边界
    "observations",      # 可信 raw 观测与 manifest
    "analysis",          # 候选分析程序的输出视图
    "trials",            # 逐 trial 证据（explore/confirm/replay 分 root）
    "designs",           # 候选 design/analysis 源码版本（按 hash 存储）
    "confirm",           # 确认集私有 block 与一次原子批次证据
    "judge",             # 独立判据报告（T4 judge.write_report 的消费面）
    "replay",            # 独立复跑 root
    "deliverable",       # 结案交付件（独立复算器读 deliverable/closure.json）
    "claim",             # 候选主张提交件（claim/submission_<study>.json）
    "research_packets",  # §11.3 两 study 的研究包
    "state",             # T2 事件日志/回复原文（journal.jsonl 与其 sha 链）
    "human_events",      # §8.4 HumanEvent 计时
    "quota",             # 本 R1 独立账
    "quota/approvals",   # 批次批准件
    "protocol",          # probe8 协议链
    "attempts",          # R1.2 T3：逐 attempt 生命周期报告（R1 留空不用）
    "studies",           # T7 两个正式研究 study 的驱动报告（report_A/B.json）
    "closeout",          # 结案件
    "evidence_pack",     # 证据包
)


def rsi_r1_root() -> Path:
    """R1 家族根：显式 env 优先；否则 ``$DATA_ROOT/rsi_r1``。"""
    root = os.environ.get(_RSI_R1_ROOT_ENV)
    if root:
        return Path(root)
    return Path(DEFAULT_DATA_ROOT) / "rsi_r1"


def rsi_r1_campaign_id() -> str:
    cid = os.environ.get(_RSI_R1_CAMPAIGN_ENV, "")
    if not cid:
        raise RsiR1CampaignUnsetError(
            f"{_RSI_R1_CAMPAIGN_ENV} 未设置：R1 写路径必须显式指定 campaign")
    return _validate_campaign_id(cid, error_cls=RsiR1CampaignUnsetError,
                                 label="R1")


def rsi_r1_campaign_root() -> Path:
    return rsi_r1_root() / rsi_r1_campaign_id()


def rsi_r1_subdir(name: str) -> Path:
    """campaign 子目录解析（白名单；越界名直接拒绝，杜绝路径穿越）。"""
    if name not in RSI_R1_CAMPAIGN_SUBDIRS:
        raise RsiR1CampaignUnsetError(f"未知 R1 campaign 子目录：{name!r}")
    return rsi_r1_campaign_root() / name


def rsi_r1_contract_dir() -> Path:
    return ensure_private_dir(rsi_r1_subdir("contract"))


def rsi_r1_private_dir() -> Path:
    return ensure_private_dir(rsi_r1_subdir("private"))


def rsi_r1_public_dir() -> Path:
    return ensure_private_dir(rsi_r1_subdir("public"))


def rsi_r1_observations_dir() -> Path:
    return ensure_private_dir(rsi_r1_subdir("observations"))


def rsi_r1_analysis_dir() -> Path:
    return ensure_private_dir(rsi_r1_subdir("analysis"))


def rsi_r1_trials_dir() -> Path:
    return ensure_private_dir(rsi_r1_subdir("trials"))


def rsi_r1_designs_dir() -> Path:
    return ensure_private_dir(rsi_r1_subdir("designs"))


def rsi_r1_confirm_dir() -> Path:
    return ensure_private_dir(rsi_r1_subdir("confirm"))


def rsi_r1_replay_dir() -> Path:
    return ensure_private_dir(rsi_r1_subdir("replay"))


def rsi_r1_research_packets_dir() -> Path:
    return ensure_private_dir(rsi_r1_subdir("research_packets"))


def rsi_r1_human_events_dir() -> Path:
    return ensure_private_dir(rsi_r1_subdir("human_events"))


def rsi_r1_quota_dir() -> Path:
    return ensure_private_dir(rsi_r1_subdir("quota"))


def rsi_r1_ledger_path() -> Path:
    return rsi_r1_quota_dir() / "ledger.jsonl"


def rsi_r1_counters_path() -> Path:
    return rsi_r1_quota_dir() / "counters.json"


def rsi_r1_approvals_dir() -> Path:
    return ensure_private_dir(rsi_r1_quota_dir() / "approvals")


def rsi_r1_reservations_dir() -> Path:
    return ensure_private_dir(rsi_r1_quota_dir() / "resource_reservations")


def rsi_r1_attempts_path() -> Path:
    return rsi_r1_quota_dir() / "attempts.jsonl"


def rsi_r1_gateway_state_dir() -> Path:
    # 熔断/暂停态随本 campaign 隔离，绝不读写全局 SIMPRAXIS_GATEWAY_STATE。
    return ensure_private_dir(rsi_r1_campaign_root() / "gateway_state")


def rsi_r1_gateway_state_path() -> Path:
    return rsi_r1_gateway_state_dir() / "quota_pause.json"


def rsi_r1_protocol_dir() -> Path:
    return ensure_private_dir(rsi_r1_subdir("protocol"))


def rsi_r1_closeout_dir() -> Path:
    return ensure_private_dir(rsi_r1_subdir("closeout"))


def rsi_r1_evidence_pack_dir() -> Path:
    return ensure_private_dir(rsi_r1_subdir("evidence_pack"))


def rsi_r1_protected_write_roots() -> "dict[str, Path]":
    """R1 写路径的不可写根集：六份封存期根 + R0/R0.1/R0.2 三族家族根。

    与 R0.2 的 ``rsi_r02_protected_write_roots()`` 分开命名：后者被 R0.2 的
    历史代码与测试引用，改动它会改变已结案 campaign 的写守卫语义。
    """
    roots = sealed_phase_roots()
    roots["rsi_r0"] = rsi_r0_root()
    roots["rsi_r01"] = rsi_r01_root()
    roots["rsi_r02"] = rsi_r02_root()
    return roots


def assert_rsi_r1_roots_isolated() -> None:
    """R1 campaign 根与六份封存期根 + R0/R0.1/R0.2 家族根两两互异且互不嵌套。

    在任何 mkdir/写系统调用之前 fail-fast（AC03）：R1 的 approvals/、
    gateway_state/、attempts.jsonl、observations/ 若与旧根嵌套，会直接污染
    已结案实验的台账与证据包。
    """
    campaign = rsi_r1_campaign_root().resolve(strict=False)
    protected = rsi_r1_protected_write_roots()
    seals = tuple(protected.values())
    _assert_roots_isolated(
        rsi_r1_campaign_root(), seals,
        label=_RSI_R1_CAMPAIGN_ENV,
        message=("R1 campaign 根必须与 Phase 1/2/2.5/2.6/2.7/2.8.1 封存根及 "
                 f"RSI-R0/R0.1/R0.2 家族根互异且互不嵌套（解析后）：campaign={campaign} "
                 f"protected={[str(p.resolve(strict=False)) for p in seals]}"))


# ---- RSI-R1.2：独立新根（2026-11-12；照 RSI-R1 复制，不泛化；十账绝不并表）----
# 期号取 **2904**（RSI-R1.2 Plan §1 建议的候选值；T0 全仓扫描确认
# 2/25/26/27/281/2900/2901/2902/2903 已占用、2904 无冲突后锁定）。R1 家族根
# （``rsi_r1_root()``，含其全部 campaign）进入 R1.2 的不可写根集：R1.2 的任何写
# 路径不得落进旧实验根（AC02/AC03）。旧账继续只读，不改写任何历史行。
#
# R1.2 与 R1 的差异（Plan §1）：同构目录族**新增** validation/（离线生产准入
# 与 v2 校准件）与 aux/（aux 子信封占额与用量），其余逐名保留，便于旧工具的
# 只读复用。

_RSI_R12_ROOT_ENV = "SIMPRAXIS_RSI_R12_ROOT"          # R1.2 家族根（campaign 父目录）
_RSI_R12_CAMPAIGN_ENV = "SIMPRAXIS_RSI_R12_CAMPAIGN"  # R1.2 campaign id（写路径必填）


class RsiR12CampaignUnsetError(RuntimeError):
    """未指定 R1.2 campaign 就解析写路径——fail-closed，不回退到任何缺省 campaign。"""


#: R1.2 campaign 子目录（Plan §2.1/§5/§6 的产物面；quota/approvals 嵌套在 quota 下）。
RSI_R12_CAMPAIGN_SUBDIRS: "tuple[str, ...]" = (
    "contract",          # 单源 schema / 公开合同 v2 / 冻结公开科学范围
    "private",           # 维护侧校准私有件（候选不可挂载）
    "public",            # 候选可见的公开任务与边界
    "observations",      # 可信 raw 观测与 manifest
    "analysis",          # 候选分析程序的输出视图
    "trials",            # 逐 trial 证据（explore/confirm/replay 分 root）
    "designs",           # 候选 design/analysis 源码版本（按 hash 存储）
    "confirm",           # 确认集私有 block 与一次原子批次证据
    "judge",             # 独立判据报告
    "replay",            # 独立复跑 root
    "deliverable",       # 结案交付件
    "claim",             # 候选主张提交件
    "research_packets",  # 两 study 的研究包
    "state",             # 事件日志/回复原文（journal.jsonl 与其 sha 链）
    "human_events",      # HumanEvent 计时
    "quota",             # 本 R1.2 独立账（2904）
    "quota/approvals",   # 批次批准件
    "protocol",          # probe/practice 单链演练
    "attempts",          # T3：attempts/<lane>/<attempt_id>/report.json 唯一报告
    "studies",           # 两正式研究 study 的驱动报告
    "validation",        # 离线生产准入（production_validation_*.json）与 v2 校准
    "aux",               # aux 五子信封占额与逐项用量
    "closeout",          # 结案件
    "evidence_pack",     # 证据包
)


def rsi_r12_root() -> Path:
    """R1.2 家族根：显式 env 优先；否则 ``$DATA_ROOT/rsi_r12``。"""
    root = os.environ.get(_RSI_R12_ROOT_ENV)
    if root:
        return Path(root)
    return Path(DEFAULT_DATA_ROOT) / "rsi_r12"


def rsi_r12_campaign_id() -> str:
    cid = os.environ.get(_RSI_R12_CAMPAIGN_ENV, "")
    if not cid:
        raise RsiR12CampaignUnsetError(
            f"{_RSI_R12_CAMPAIGN_ENV} 未设置：R1.2 写路径必须显式指定 campaign")
    return _validate_campaign_id(cid, error_cls=RsiR12CampaignUnsetError,
                                 label="R1.2")


def rsi_r12_campaign_root() -> Path:
    return rsi_r12_root() / rsi_r12_campaign_id()


def rsi_r12_subdir(name: str) -> Path:
    """campaign 子目录解析（白名单；越界名直接拒绝，杜绝路径穿越）。"""
    if name not in RSI_R12_CAMPAIGN_SUBDIRS:
        raise RsiR12CampaignUnsetError(f"未知 R1.2 campaign 子目录：{name!r}")
    return rsi_r12_campaign_root() / name


def rsi_r12_family_dir() -> Path:
    """族账目录（家族根下 ``family/``；不隶属任何 campaign）。"""
    return ensure_private_dir(rsi_r12_root() / "family")


def rsi_r12_family_doc_path() -> Path:
    return rsi_r12_family_dir() / "family.json"


def rsi_r12_contract_dir() -> Path:
    return ensure_private_dir(rsi_r12_subdir("contract"))


def rsi_r12_private_dir() -> Path:
    return ensure_private_dir(rsi_r12_subdir("private"))


def rsi_r12_public_dir() -> Path:
    return ensure_private_dir(rsi_r12_subdir("public"))


def rsi_r12_observations_dir() -> Path:
    return ensure_private_dir(rsi_r12_subdir("observations"))


def rsi_r12_analysis_dir() -> Path:
    return ensure_private_dir(rsi_r12_subdir("analysis"))


def rsi_r12_trials_dir() -> Path:
    return ensure_private_dir(rsi_r12_subdir("trials"))


def rsi_r12_designs_dir() -> Path:
    return ensure_private_dir(rsi_r12_subdir("designs"))


def rsi_r12_confirm_dir() -> Path:
    return ensure_private_dir(rsi_r12_subdir("confirm"))


def rsi_r12_replay_dir() -> Path:
    return ensure_private_dir(rsi_r12_subdir("replay"))


def rsi_r12_research_packets_dir() -> Path:
    return ensure_private_dir(rsi_r12_subdir("research_packets"))


def rsi_r12_human_events_dir() -> Path:
    return ensure_private_dir(rsi_r12_subdir("human_events"))


def rsi_r12_quota_dir() -> Path:
    return ensure_private_dir(rsi_r12_subdir("quota"))


def rsi_r12_ledger_path() -> Path:
    return rsi_r12_quota_dir() / "ledger.jsonl"


def rsi_r12_counters_path() -> Path:
    return rsi_r12_quota_dir() / "counters.json"


def rsi_r12_approvals_dir() -> Path:
    return ensure_private_dir(rsi_r12_quota_dir() / "approvals")


def rsi_r12_reservations_dir() -> Path:
    return ensure_private_dir(rsi_r12_quota_dir() / "resource_reservations")


def rsi_r12_attempts_path() -> Path:
    return rsi_r12_quota_dir() / "attempts.jsonl"


def rsi_r12_gateway_state_dir() -> Path:
    # 熔断/暂停态随本 campaign 隔离，绝不读写全局 SIMPRAXIS_GATEWAY_STATE。
    return ensure_private_dir(rsi_r12_campaign_root() / "gateway_state")


def rsi_r12_gateway_state_path() -> Path:
    return rsi_r12_gateway_state_dir() / "quota_pause.json"


def rsi_r12_protocol_dir() -> Path:
    return ensure_private_dir(rsi_r12_subdir("protocol"))


def rsi_r12_validation_dir() -> Path:
    return ensure_private_dir(rsi_r12_subdir("validation"))


def rsi_r12_aux_dir() -> Path:
    return ensure_private_dir(rsi_r12_subdir("aux"))


def rsi_r12_closeout_dir() -> Path:
    return ensure_private_dir(rsi_r12_subdir("closeout"))


def rsi_r12_evidence_pack_dir() -> Path:
    return ensure_private_dir(rsi_r12_subdir("evidence_pack"))


def rsi_r12_protected_write_roots() -> "dict[str, Path]":
    """R1.2 写路径的不可写根集：六份封存期根 + R0/R0.1/R0.2/R1 四族家族根。

    R1 家族根整棵进入（不只某个 campaign）：R1.2 写路径若嵌进任一 R1 campaign，
    会直接污染已结案实验的台账与证据包（AC03）。
    """
    roots = sealed_phase_roots()
    roots["rsi_r0"] = rsi_r0_root()
    roots["rsi_r01"] = rsi_r01_root()
    roots["rsi_r02"] = rsi_r02_root()
    roots["rsi_r1"] = rsi_r1_root()
    return roots


def assert_rsi_r12_roots_isolated() -> None:
    """R1.2 campaign 根与六份封存期根 + R0/R0.1/R0.2/R1 家族根两两互异且互不嵌套。

    在任何 mkdir/写系统调用之前 fail-fast（AC03）：approvals/、gateway_state/、
    attempts.jsonl、validation/ 若与旧根嵌套，会直接污染已结案实验的台账与证据包。
    """
    campaign = rsi_r12_campaign_root().resolve(strict=False)
    protected = rsi_r12_protected_write_roots()
    seals = tuple(protected.values())
    _assert_roots_isolated(
        rsi_r12_campaign_root(), seals,
        label=_RSI_R12_CAMPAIGN_ENV,
        message=("R1.2 campaign 根必须与 Phase 1/2/2.5/2.6/2.7/2.8.1 封存根及 "
                 f"RSI-R0/R0.1/R0.2/R1 家族根互异且互不嵌套（解析后）：campaign={campaign} "
                 f"protected={[str(p.resolve(strict=False)) for p in seals]}"))
