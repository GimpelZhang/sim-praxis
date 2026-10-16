"""Gate2 视频体积五级阶梯（详细计划 §1.4）纯函数 + 容器内 ffmpeg 命令生成。

阈值事实（2026-10-13 官方文档已查证；H-14 锁内适配，2026-10-16）：
  - im/v1/files（file_type=mp4）硬上限 30MB；原片 ≤28MB 直接传（留 2MB 余量）；
  - **MB 口径为十进制 1_000_000**（飞书文档与上传接口按十进制计；历史实现误用
    1024*1024，28MB 判定实际放宽到约 29.36MB，贴近 30MB 硬上限——已在
    build_evidence_pack.ADAPTATIONS 登记）；
  - step2 crf23 + yuv420p + 短边 720；
  - step3 crf28 + 短边 540 + fps20；
  - step4 裁 ≤30s 关键片段（保留首尾与接触时刻，区间由 Task7/8 调用方传入）；
  - step5 全部失败（含上传接口非 0）：内网链接降级，video_fallback=true，人工照常裁决。

H-14 阶梯策略：**凡 >28MB 一律从 step2 起**，之后以压缩后实测体积对照
HARD_LIMIT 逐级 2→3→4→5，不再按 64/128/256MB 粗分档越级跳（旧逻辑 100MB
直达 step3、200MB 直达 step4，会跳过更便宜的成功档）。下列分档常量仅保留为
文档/日志锚点，不再参与选级。

宿主机不装 ffmpeg：压缩命令在 sim-sandbox 容器内执行（client 用
`docker compose -f sandbox/docker-compose.yml run --rm sim-sandbox ...` 调起）。
本模块纯函數不触网、不启容器，单测无需 mock ffmpeg 本身。
"""
from __future__ import annotations

import shlex
from pathlib import Path

# H-14：十进制 MB（飞书上传口径），非 1024*1024。
MB = 1_000_000
DIRECT_LIMIT = 28 * MB          # step1：原片 ≤28MB 直传
HARD_LIMIT = 30 * MB           # files 接口硬上限 30MB

# 历史粗分档（十进制 MB 重算）；仅作文档锚点，H-14 后不再用于越级选级。
_BAND_STEP2 = 64 * MB
_BAND_STEP3 = 128 * MB
_BAND_STEP4 = 256 * MB

# step4 截断区间允许的键（C1 跨 lane 契约，键名钉死，多键/错类型即拒）。
TRIM_KEYS = frozenset(
    {"trim_start", "trim_seconds", "contact_moment_s", "source_duration_s"}
)


def select_ladder_step(size_bytes: int) -> int:
    """按体积选**初始**压缩阶梯，返回 1..5。

    H-14：≤28MB -> 1 直传；其余一律 2 起步，由调用方按压缩后实测体积逐级走。
    """
    if size_bytes <= DIRECT_LIMIT:
        return 1
    return 2


def next_ladder_step(step: int) -> int:
    """压缩/上传失败后推进一级（2->3->4->5；1 的失败由调用方按体积另判）。"""
    if step < 1 or step > 5:
        raise ValueError(f"非法阶梯: {step}")
    return min(step + 1, 5)


def validate_trim(trim: "dict | None") -> "dict | None":
    """校验 C1 截断参数 dict；None 透传（step4 回退默认前 30s）。

    严格四键、数值类型（bool 不算数）、trim_start≥0、trim_seconds>0；
    contact_moment_s / source_duration_s 允许 None。
    """
    if trim is None:
        return None
    if not isinstance(trim, dict):
        raise ValueError(f"trim 必须为 dict，收到 {type(trim).__name__}")
    keys = set(trim)
    if keys != TRIM_KEYS:
        raise ValueError(
            f"trim 键集合必须恰为 {sorted(TRIM_KEYS)}，收到 {sorted(keys)}"
        )

    def _num(key: str, allow_none: bool) -> "float | None":
        v = trim[key]
        if v is None and allow_none:
            return None
        # bool 是 int 子类，显式挡掉（True 被当 1.0 会静默错裁）。
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"trim[{key!r}] 必须是数值，收到 {v!r}")
        return float(v)

    start = _num("trim_start", False)
    seconds = _num("trim_seconds", False)
    contact = _num("contact_moment_s", True)
    source = _num("source_duration_s", True)
    if start < 0:
        raise ValueError(f"trim_start 不得为负：{start}")
    if seconds <= 0:
        raise ValueError(f"trim_seconds 必须为正：{seconds}")
    if source is not None and source < 0:
        raise ValueError(f"source_duration_s 不得为负：{source}")
    if contact is not None and contact < 0:
        raise ValueError(f"contact_moment_s 不得为负：{contact}")
    return {
        "trim_start": start,
        "trim_seconds": seconds,
        "contact_moment_s": contact,
        "source_duration_s": source,
    }


def format_mmss(seconds: float) -> str:
    """秒 -> mm:ss（四舍五入到整秒，供卡片裁剪区间文案）。"""
    total = int(round(float(seconds)))
    if total < 0:
        total = 0
    return f"{total // 60}:{total % 60:02d}"


def _fmt_s(seconds: "float | None") -> str:
    """原片时长展示：整数秒不带小数，非整数保留一位。"""
    v = float(seconds)
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.1f}"


def source_duration_note(source_duration_s: "float | None") -> str:
    """「原片 Ns」注记（未给时长时返回空串）。step<4 与未裁剪视频也必须展示。"""
    if source_duration_s is None:
        return ""
    return f"原片 {_fmt_s(source_duration_s)}s"


def trim_interval_note(trim: dict) -> str:
    """step4 实际裁剪后的区间注记：

    「裁剪区间 mm:ss–mm:ss（接触时刻 mm:ss | 分屏无单一接触），原片 Ns」
    仅在确实发生裁剪时由调用方追加；原片时长部分另由 source_duration_note 给出。
    """
    start = float(trim["trim_start"])
    end = start + float(trim["trim_seconds"])
    contact = trim.get("contact_moment_s")
    if contact is None:
        contact_txt = "分屏无单一接触"
    else:
        contact_txt = f"接触时刻 {format_mmss(contact)}"
    return (
        f"裁剪区间 {format_mmss(start)}–{format_mmss(end)}"
        f"（{contact_txt}）"
    )


def _short_edge_scale(short_edge: int) -> str:
    # 横屏缩短边到 short_edge、竖屏同理；-2 保证宽高偶数（yuv420p 要求）
    return (f"scale='if(gt(iw,ih),-2,{short_edge})':"
            f"'if(gt(iw,ih),{short_edge},-2)'")


def compress_command(
    path: str,
    step: int,
    output_path: str,
    cover_path: str,
    *,
    trim_seconds: "float | int" = 30,
    trim_start: "float | int" = 0,
) -> str:
    """生成在 sim-sandbox 容器内 bash -lc 执行的 ffmpeg 命令字符串。

    每级都同时产出转码视频与首帧 PNG 封面（media 消息需要 image_key）。
    step1（直传）/step5（降级）无压缩语义，调用 ValueError——这两级不得调用本函数。
    step4 截断区间默认前 30s；真实关键片段区间（首尾+接触时刻，C1，支持小数秒）
    由 Task7/8 调用方传入。H-15：src/out/cover 一律 shlex.quote，路径含空格或
    中文/引号不再被 shell 拆词。
    """
    src = shlex.quote(str(path))
    out = shlex.quote(str(output_path))
    cover = shlex.quote(str(cover_path))
    if step == 2:
        vf = _short_edge_scale(720)
        extra = ""
    elif step == 3:
        vf = f"fps=20,{_short_edge_scale(540)}"
        extra = ""
    elif step == 4:
        vf = f"fps=20,{_short_edge_scale(540)}"
        # -ss 放 -i 前快速 seek；区间支持小数秒（C1 由调用方按接触时刻给定）
        start = float(trim_start)
        seconds = float(trim_seconds)
        if start < 0 or seconds <= 0:
            raise ValueError(
                f"非法截断区间：trim_start={start}, trim_seconds={seconds}"
            )
        extra = f" -ss {start:g} -t {seconds:g}"
    else:
        raise ValueError(f"step {step} 无容器内压缩命令（1=直传，5=内网降级）")
    return (
        f"ffmpeg -y{extra} -i {src} "
        f"-c:v libx264 -crf {23 if step == 2 else 28} -preset medium "
        f"-pix_fmt yuv420p -vf \"{vf}\" -c:a aac {out} && "
        f"ffmpeg -y -i {out} -frames:v 1 -an {cover}"
    )


def host_to_container_path(host_path: "str | Path", run_dir: "str | Path") -> str:
    """宿主 $SIMPRAXIS_RUN_DIR 路径 -> 容器内 /root/runs 挂载路径。

    非 RUN_DIR 下的产物无法被容器看到（compose 仅挂载该目录），直接报错而非隐式传错。
    shlex.quote 在命令生成处统一做；本函数只负责路径映射与越界守卫（H-15）。
    """
    p = Path(host_path).resolve()
    root = Path(run_dir).resolve()
    try:
        rel = p.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"视频产物 {p} 不在 {root} 下，容器内不可见（compose 仅挂载 RUN_DIR）"
        ) from exc
    return str(Path("/root/runs") / rel)


def expected_outputs(path: "str | Path", step: int,
                     out_dir: "str | Path | None" = None) -> tuple[str, str]:
    """给定输入与阶梯，返回 (视频输出路径, 封面路径) 的宿主侧字面量。"""
    src = Path(path)
    d = Path(out_dir) if out_dir else src.parent
    stem = src.stem
    return (str(d / f"{stem}_s{step}.mp4"), str(d / f"{stem}_s{step}_cover.png"))
