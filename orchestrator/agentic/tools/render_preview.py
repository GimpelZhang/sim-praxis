"""render_quick_preview：文本化预览探针（Phase 2.7 Task3 D3-5 / D3-8 / D3-10）。

为什么不是「把图塞给模型」：硬规则 1 规定视觉唯一入口是
``ModelGateway.call_visual``（stage 硬绑 ``visual_rollout``），coding/macro
两阶段**禁止**收 image block。本工具因此只回**文本**：

- ``png_relpath``：PNG 落在 workdir 的 ``.preview/`` 下（模型可 read_file
  回查旁侧回执 JSON），但**图字节永不进** ``ToolResult.output``（AC-T3-7）；
- ``bbox_table``：世界系 AABB 表（由容器内 trimesh 从网格本身算出，确定性）；
- ``verdict``：模板生成的几何陈述（不由模型猜）；
- ``spatial_relation``：**默认不出现**。它是 manifest 关键点解析算出的真值
  几何，受 ``PreviewPermit.privileged_relation`` 同一道门控（BR-3 第 5 项 /
  AC-T3-11）：只有模式 N 且预注册写明 ``privileged_state_inject`` 时才给，
  模式 R 一律不给，回执里连关键点数字都不出现。

``visual_critic``（BR-1）走既有 ``call_visual`` 通道：引擎在
``PreviewPermit.visual_caller`` 注入一个转调适配闭包，本工具把 PNG 编码成
base64 传进去、只收**文本**（≤400 字符）回显——账本由网关记，图不出工具层。

冷/热口径（D3-8）：``cold_wall_s`` 是整进程冷启（含引擎创建 + 建场），
``render_second_s`` 是建场后第二帧的渲染耗时（同一会话内的热路径；再起一个
容器必然又是冷启，不是热调用）。阈值 ``PREVIEW_COLD_TIMEOUT_S`` /
``PREVIEW_WARM_TIMEOUT_S`` 只用于**报告**是否越界，执行安全帽是
``PREVIEW_TIMEOUT_S``。

**锁内适配（计划 §T3.4-D vs 实现）**：计划写「``render_quick_preview`` 调用
``run_probe('render_preview')``」，但 ``ProbeRegistry`` 的 argv 是**固定**的
（模型只能传探针名），而预览需要逐次传 ``--spec/--out/--camera``。故本工具
自建 argv 并复用 ``probe.resolve_probe_paths`` 的 M3 路径解析 + ``execute_render``
的渲染/取证路径——**语义等价、argv 仍由引擎构造**，模型只能选相机与场景文件。
"""
from __future__ import annotations

import base64
import json
import math
from pathlib import Path

from ..image_pin import SCENE_BUILD_PYTHON
from ..policy import PolicyDecision, PreToolUsePolicy, ToolContext
from ..schema import SchemaError, str_field, validate
from . import ToolResult
from .probe import CONTAINER_PROBES_DIR
from .run import check_backend, execute_render

# 预览尺寸硬固定（D3-8）：模型不可改，避免「大图 = 大耗时/大内存」的自由度。
PREVIEW_MAX_PX = 256
# D3-8 的两档判据（报告越界即红；不是执行超时）。
PREVIEW_COLD_TIMEOUT_S = 60
PREVIEW_WARM_TIMEOUT_S = 5
# 执行安全帽（远高于判据，只防挂死；不是判据）。
PREVIEW_TIMEOUT_S = 180

PREVIEW_REL_DIR = ".preview"
PREVIEW_RECEIPT_SCHEMA = "simpraxis.preview_probe.v1"
CAMERAS = ("ortho_top", "iso", "front")
DEFAULT_CAMERA = "ortho_top"

# BR-1 视觉诊断的提示词（固定，模型不可写：避免把图「问」出额外信息）。
VISUAL_PROMPT = (
    "这是一张 256x256 的仿真场景俯视/斜视渲染图。请只用一句中文说明："
    "画面中能辨认出哪些物体、它们是否彼此穿模或悬空。不要推测数值。")
VISUAL_TEXT_MAX = 400

_ASSETS_DST = "/assets"
_PROBES_DST = CONTAINER_PROBES_DIR

_FIELDS = (
    str_field("scene_spec_path", max_chars=512),
    str_field("camera", required=False, default=DEFAULT_CAMERA, max_chars=32),
)

_B64_MARKERS = ("data:image", "iVBORw0KGgo")   # PNG 魔数的 base64 前缀
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# 纯几何（宿主侧解析计算，不依赖引擎/网格；确定性，可单测）
# ---------------------------------------------------------------------------

def _quat_matrix(quat) -> "tuple[tuple[float, float, float], ...]":
    """(w,x,y,z) → 3×3 旋转矩阵；非单位模长先归一（spec 已校验，防御性再归一）。"""
    w, x, y, z = (float(v) for v in quat)
    norm = math.sqrt(w * w + x * x + y * y + z * z)
    if norm == 0.0:
        raise ValueError("quat 不能全零")
    w, x, y, z = w / norm, x / norm, y / norm, z / norm
    return (
        (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
        (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
        (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)),
    )


def _rotate(quat, vec) -> "tuple[float, float, float]":
    m = _quat_matrix(quat)
    x, y, z = (float(v) for v in vec)
    return (m[0][0] * x + m[0][1] * y + m[0][2] * z,
            m[1][0] * x + m[1][1] * y + m[1][2] * z,
            m[2][0] * x + m[2][1] * y + m[2][2] * z)


def _world(local, pos, quat) -> "tuple[float, float, float]":
    r = _rotate(quat, local)
    p = (float(pos[0]), float(pos[1]), float(pos[2]))
    return (r[0] + p[0], r[1] + p[1], r[2] + p[2])


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _norm(v) -> float:
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def _distance(a, b) -> float:
    return _norm(_sub(a, b))


def _unit(v):
    n = _norm(v)
    if n <= 1e-12:
        return None
    return (v[0] / n, v[1] / n, v[2] / n)


def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _field(body, name: str):
    if isinstance(body, dict):
        return body[name]
    return getattr(body, name)


def _body_asset(body) -> str:
    return str(_field(body, "asset"))


def _role_of(body) -> "str | None":
    """按资产名尾段的第一个词定角色：``vessel/pitcher_standard`` → ``pitcher``。"""
    asset = _body_asset(body)
    stem = asset.rsplit("/", 1)[-1]
    for role in ("pitcher", "cup"):
        if stem.startswith(role):
            return role
    return None


def _vessel_center_local(keypoints) -> "tuple[float, float, float]":
    """容器类资产的局部「中心」：``(bottom_center + rim_center) / 2``。

    刻意**不用**「网格 AABB 中心」（那要网格，而 ``spatial_relation`` 必须
    只由 manifest 关键点 + 位姿解析算出，见 D3-10）；也刻意不用资产原点
    （收割件原点常在包围盒角上，见 ``pitcher_standard`` 的
    ``bottom_center≈(0.065,0.055,0)``——用它当中心会凭空引入半个体宽）。
    """
    bottom = keypoints.bottom_center
    rim = keypoints.rim_center
    if rim is None:
        return (float(bottom[0]), float(bottom[1]), float(bottom[2]))
    return ((float(bottom[0]) + float(rim[0])) / 2.0,
            (float(bottom[1]) + float(rim[1])) / 2.0,
            (float(bottom[2]) + float(rim[2])) / 2.0)


def _default_resolve(asset: str):
    """默认关键点解析：Task 1 Asset Zoo manifest（逐件 sha256 已校验）。"""
    from ...assets.manifest import resolve_asset   # 延迟导入（不拉重依赖）

    return resolve_asset(asset).keypoints


def resolve_from_manifest(manifest) -> "object":
    """从**给定** manifest 构造解析器（AC-T3-11 ⑤ 合成夹具入口）。

    ``manifest`` 可以是已解析的 ``simpraxis.asset_zoo.v1`` 字典或它的路径。
    与 ``_default_resolve`` 的差别只有一个：**不做磁盘 sha256 校验**——合成
    夹具刻意不带网格（RV-09），而现场 manifest 必须逐件校验。因此本函数
    **只**供夹具/单测使用，生产路径恒走 ``_default_resolve``。
    """
    from ...assets.manifest import Keypoints, AssetError

    if isinstance(manifest, (str, Path)):
        doc = json.loads(Path(manifest).read_text(encoding="utf-8"))
    else:
        doc = manifest
    if not isinstance(doc, dict) or doc.get("schema") != "simpraxis.asset_zoo.v1":
        raise AssetError("合成夹具 manifest schema 必须是 simpraxis.asset_zoo.v1")
    table = {}
    for item in doc.get("assets") or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            table[item["name"]] = Keypoints.from_json(item.get("keypoints") or {})

    def _resolve(name: str):
        if name not in table:
            raise AssetError(f"合成夹具 manifest 未登记资产：{name}")
        return table[name]

    return _resolve


def compute_spatial_relation(bodies, *, resolve=None) -> dict:
    """解析式空间关系（D3-10）：**纯算术**，无网格、无引擎、无模型推理。

    按 ``resolve(asset).spout_defined`` 分两条支路（**由 manifest 决定，
    不由模型选**）：

    - 有唯一出口（``spout_defined=True``）：给壶口到杯口的**世界系**偏移
      ``pitcher_spout_to_cup_rim_offset``（= 壶 ``pour_edge`` 变换到世界 −
      杯 ``rim_center`` 变换到世界；**两件不同资产**，不是壶自身的弦向量）、
      ``pitcher_pitch_angle_deg``（壶局部 +z 与世界 +z 的夹角）与
      ``is_spout_pointing_towards_cup``（壶心→壶口方向与壶心→杯口方向点积>0）。
    - 轴对称件（``spout_defined=False``）：**不输出**上面两个键（无唯一出口，
      硬算是坐标轴假象），改给 ``tilt_axis_needed{axis, angle_deg}``——把
      壶口平面法向转到「壶口→杯口」方向所需的转轴与转角。
    - 两支路都含 ``pitcher_to_cup_distance``（两件资产关键点中心的世界系距离）。

    找不到 pitcher/cup 角色、关键点缺失、或位姿退化时抛 ``ValueError``
    （fail-closed：宁可回一条明确错误，也不编造几何真值）。
    """
    resolve = resolve or _default_resolve
    roles: dict[str, object] = {}
    for body in bodies:
        role = _role_of(body)
        if role is not None and role not in roles:
            roles[role] = body
    for role in ("pitcher", "cup"):
        if role not in roles:
            raise ValueError(
                f"场景里找不到 {role} 角色的资产（按资产名尾段前缀判定："
                f"{[_body_asset(b) for b in bodies]}）")

    pitcher, cup = roles["pitcher"], roles["cup"]
    pitcher_kp = resolve(_body_asset(pitcher))
    cup_kp = resolve(_body_asset(cup))
    if getattr(pitcher_kp, "schema", None) != "vessel":
        raise ValueError(f"pitcher 资产不是 vessel：{_body_asset(pitcher)}")
    if getattr(cup_kp, "rim_center", None) is None:
        raise ValueError(f"cup 资产没有 rim_center 关键点：{_body_asset(cup)}")

    p_pos, p_quat = _field(pitcher, "pos"), _field(pitcher, "quat")
    c_pos, c_quat = _field(cup, "pos"), _field(cup, "quat")

    pitcher_center = _world(_vessel_center_local(pitcher_kp), p_pos, p_quat)
    cup_center = _world(_vessel_center_local(cup_kp), c_pos, c_quat)
    cup_rim_world = _world(cup_kp.rim_center, c_pos, c_quat)

    out: dict = {
        "pitcher_to_cup_distance": round(_distance(pitcher_center, cup_center), 6),
    }

    if bool(getattr(pitcher_kp, "spout_defined", False)):
        pour_edge = getattr(pitcher_kp, "pour_edge", None)
        if pour_edge is None:
            raise ValueError(
                f"manifest 声明 spout_defined=True 却无 pour_edge："
                f"{_body_asset(pitcher)}（自相矛盾的 manifest）")
        spout_world = _world(pour_edge, p_pos, p_quat)
        offset = _sub(spout_world, cup_rim_world)
        up_world = (0.0, 0.0, 1.0)
        pitcher_up = _rotate(p_quat, up_world)
        pitch = math.degrees(math.acos(max(-1.0, min(1.0, _dot(pitcher_up, up_world)))))
        toward_cup = _unit(_sub(cup_rim_world, pitcher_center))
        spout_dir = _unit(_sub(spout_world, pitcher_center))
        if toward_cup is None or spout_dir is None:
            raise ValueError("壶心到壶口/杯口的向量退化（位姿重合）")
        out.update({
            "pitcher_spout_to_cup_rim_offset": [round(v, 6) for v in offset],
            "pitcher_pitch_angle_deg": round(pitch, 3),
            "is_spout_pointing_towards_cup": bool(_dot(spout_dir, toward_cup) > 0.0),
        })
    else:
        pitcher_rim_world = _world(pitcher_kp.rim_center, p_pos, p_quat)
        pitcher_up = _rotate(p_quat, (0.0, 0.0, 1.0))
        desired = _unit(_sub(cup_rim_world, pitcher_rim_world))
        if desired is None:
            raise ValueError("壶口与杯口重合，倾斜轴无定义")
        axis = _unit(_cross(pitcher_up, desired))
        if axis is None:
            # 壶轴已与目标方向共线：无需倾斜（0°）或需 180° 翻转（取定 x 轴）。
            angle = 0.0 if _dot(pitcher_up, desired) > 0 else 180.0
            axis = (1.0, 0.0, 0.0)
        else:
            angle = math.degrees(math.acos(
                max(-1.0, min(1.0, _dot(pitcher_up, desired)))))
        out["tilt_axis_needed"] = {
            "axis": [round(v, 6) for v in axis],
            "angle_deg": round(angle, 3),
        }
    return out


# ---------------------------------------------------------------------------
# 工具层
# ---------------------------------------------------------------------------

def _deny(reason: str) -> ToolResult:
    return ToolResult.denied_result(PolicyDecision.deny(reason))


def _parse_receipt(stdout: str) -> "dict | None":
    """从探针 stdout 里取**最后一条**预览回执（引擎横幅已全转 stderr）。"""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(doc, dict) and doc.get("schema") == PREVIEW_RECEIPT_SCHEMA:
            return doc
    return None


def _bbox_lines(rows) -> list[str]:
    out = []
    for row in rows:
        out.append(
            f"- {row['asset']} center=(" + ", ".join(f"{v:.3f}" for v in row["center"])
            + ") extents=(" + ", ".join(f"{v:.3f}" for v in row["extents"]) + ")")
    return out


def _verdict(rows) -> str:
    """模板生成的几何陈述（不调模型、不猜）。"""
    if not rows:
        return "场景无 BBox 数据（回执缺件，按失败处理）。"
    if len(rows) == 1:
        r = rows[0]
        return (f"场景仅 1 件 {r['asset']}，中心 "
                f"({r['center'][0]:.3f}, {r['center'][1]:.3f}, {r['center'][2]:.3f}) m。")
    best = None
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            d = _distance(rows[i]["center"], rows[j]["center"])
            if best is None or d < best[0]:
                best = (d, rows[i]["asset"], rows[j]["asset"])
    d, a, b = best
    return (f"场景共 {len(rows)} 件；最近一对 {a} ↔ {b} 的 AABB 中心距 {d:.3f} m"
            f"（世界系；仅包围盒中心距，不代表表面间隙）。")


def render_quick_preview(ctx: ToolContext, **kwargs) -> ToolResult:
    """渲一帧 256×256 预览并回文本（图不进观察；真值几何受门控）。"""
    try:
        args = validate(_FIELDS, kwargs)
    except SchemaError as exc:
        return _deny(f"schema 拒绝：{exc}")
    camera = args["camera"]
    if camera not in CAMERAS:
        return _deny(f"相机只允许 {list(CAMERAS)}：{camera!r}")

    decision = PreToolUsePolicy(
        ctx.grants, workdir=ctx.workdir, nonprod=ctx.nonprod,
        probe_registry=ctx.probe_registry).check(
            "render_quick_preview", args)
    if decision.decision == "deny":
        return ToolResult.denied_result(decision)
    if ctx.preview is None:
        # 工具只在 cfg.render_preview 时进授予面；走到这里说明装配不一致。
        return _deny("预览门控未装配（PreviewPermit 缺失）：拒绝在未开开关的 "
                     "run 内起容器渲染")

    try:
        spec_path = ctx.jail.resolve(args["scene_spec_path"], must_exist=True)
    except Exception as exc:  # noqa: BLE001 - JailError 统一转 deny 观察
        return _deny(f"路径拒绝：{exc}")
    if not spec_path.is_file():
        return _deny(f"场景 spec 不是文件：{args['scene_spec_path']}")
    rel_spec = ctx.jail.relpath(spec_path)

    backend = check_backend(ctx)
    if isinstance(backend, ToolResult):
        return backend

    # 资产根：兄弟容器由 Deck 注入 /assets:ro；NONPROD 本地形态直接指宿主根。
    from ...assets.manifest import zoo_root
    if getattr(backend, "nonprod", False):
        assets_dir = str(zoo_root())
    else:
        mounts = tuple(getattr(backend, "extra_ro_mounts", ()) or ())
        if not any(dst == _ASSETS_DST for _src, dst in mounts):
            return _deny(
                "后端未挂载资产库 /assets（兄弟容器形态必须注入 "
                "extra_ro_mounts=((zoo_root, '/assets'),)：不能把宿主绝对路径"
                "塞进容器 argv，也不允许容器内 ENOENT 裸跑）")
        assets_dir = _ASSETS_DST

    out_rel = f"{PREVIEW_REL_DIR}/preview-{camera}.png"
    workdir = ctx.workdir
    out_path = workdir / out_rel
    receipt_path = out_path.with_suffix(".json")
    # 幂等：上一轮的旁侧回执先删，避免「这一轮没写成功却读到旧回执」。
    for stale in (out_path, receipt_path):
        if stale.is_file():
            stale.unlink()
    (workdir / PREVIEW_REL_DIR).mkdir(parents=True, exist_ok=True)

    argv = [SCENE_BUILD_PYTHON, f"{_PROBES_DST}/probe_render_preview.py",
            "--spec", rel_spec, "--assets", assets_dir,
            "--out", out_rel, "--camera", camera]
    from .probe import resolve_probe_paths
    resolved = resolve_probe_paths(backend, argv)
    if isinstance(resolved, ToolResult):
        return resolved
    argv = resolved

    rendered = execute_render(ctx, argv, tag="render_quick_preview",
                              timeout_s=PREVIEW_TIMEOUT_S, env=None)
    receipt = _parse_receipt(rendered.full)
    sidecar = None
    if receipt_path.is_file():
        try:
            sidecar = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            sidecar = None
    doc = sidecar or receipt or {}

    ok = bool(doc.get("ok")) and out_path.is_file()
    lines = [f"[render_quick_preview] camera={camera} size={PREVIEW_MAX_PX}x{PREVIEW_MAX_PX}"
             f" ok={str(ok).lower()}",
             f"png_relpath: {out_rel}（图字节不进观察；旁侧回执 "
             f"{Path(out_rel).with_suffix('.json')}；编译缓存 "
             f"{doc.get('cache_dir') or 'n/a'}）"]
    cold = doc.get("wall_s")
    first = doc.get("render_seconds_first")
    warm = doc.get("render_seconds_second")
    if isinstance(cold, (int, float)):
        lines.append(
            f"冷启 wall_s={cold:.2f}（判据 ≤ {PREVIEW_COLD_TIMEOUT_S}s，"
            f"{'PASS' if cold <= PREVIEW_COLD_TIMEOUT_S else 'FAIL'}）；"
            f"首帧 render_s={first if first is not None else 'n/a'}；"
            f"热调用 render_second_s={warm if warm is not None else 'n/a'}"
            f"（判据 ≤ {PREVIEW_WARM_TIMEOUT_S}s，"
            f"{'PASS' if isinstance(warm, (int, float)) and warm <= PREVIEW_WARM_TIMEOUT_S else 'FAIL'}）")
    if doc.get("err"):
        lines.append(f"err: {doc['err']}")

    rows = doc.get("bbox_table") or []
    if rows:
        lines.append(f"bbox_table（{len(rows)} 行，世界系 AABB，m）：")
        lines.extend(_bbox_lines(rows))
    lines.append("verdict: " + _verdict(rows))

    if ctx.preview.privileged_relation and ok:
        try:
            relation = compute_spatial_relation(_spec_bodies(spec_path))
        except (ValueError, KeyError) as exc:
            lines.append(f"spatial_relation: 未计算（{type(exc).__name__}: {exc}）")
        else:
            lines.append("spatial_relation: " + json.dumps(
                relation, ensure_ascii=False, sort_keys=True))

    if ctx.preview.visual_caller is not None and ok:
        lines.append("visual_critic: " + _visual_critic(
            ctx.preview.visual_caller, out_path))

    if doc.get("unique_colors") is not None:
        lines.append(f"frame unique_colors={doc['unique_colors']} std={doc.get('std')}")

    output = "\n".join(lines)
    full = output + "\n\n--- probe receipt ---\n" + json.dumps(
        doc, ensure_ascii=False, sort_keys=True)
    # 双保险（AC-T3-7）：任何图像字节/base64 形态都不许进观察文本。
    for marker in _B64_MARKERS:
        if marker in full:
            return _deny("预览观察含图像载体标记（内部错误：禁止把图带进文本链路）")
    return ToolResult(output=output, full=full, decision=PolicyDecision.allow())


def _spec_bodies(spec_path: Path) -> tuple:
    """读场景 spec 的 bodies（只取位姿，不实例化引擎）。"""
    from ...assets.scene_spec import SceneSpec

    return SceneSpec.from_json(spec_path.read_text(encoding="utf-8")).bodies


def _visual_critic(caller, png_path: Path) -> str:
    """把 PNG 交给引擎注入的 ``call_visual`` 适配器，只收文本（≤400 字符）。"""
    data = png_path.read_bytes()
    if not data.startswith(_PNG_MAGIC):
        return "未调用（落盘文件不是 PNG）"
    try:
        text = caller(VISUAL_PROMPT, base64.b64encode(data).decode("ascii"))
    except Exception as exc:  # noqa: BLE001 - 诊断失败必须如实回一条，不伪造
        return f"调用失败（{type(exc).__name__}: {exc}）"[:VISUAL_TEXT_MAX]
    text = str(text).replace("\n", " ").strip()
    if len(text) > VISUAL_TEXT_MAX:
        text = text[:VISUAL_TEXT_MAX] + "…[截断]"
    return text or "（空回复）"
