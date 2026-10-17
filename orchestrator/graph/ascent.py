"""Phase 2 Task2：RSI 螺旋升阶（ascent）确定性分类与批判段组装（0 付费）。

三件事，全部本地确定性、零模型调用：

1. ``load_v1_index()``：读 ``$SIMPRAXIS_PHASE2_ROOT/v1_index.json``（缺省
   phase2_root()/v1_index.json）。**文件缺失与文件损坏语义不同**：
   - 文件缺失 → ``V1IndexMissing``：新克隆/CI 机没有 V1 档案是正常状态，
     图节点把它当**空索引**处理（所有运行 standard，标准题目不受影响）；
   - 文件存在但 JSON 损坏/schema 不符/条目非法 → ``V1IndexCorrupt``：
     fail-closed，图节点抛 GraphStateError，禁止拿半张表静默降级。
   ``classify_run`` 本身是纯函数：只接收已加载的 index dict，空索引
   （{"entries": []}）合法，结论一律 standard。
2. ``classify_run(payload, index)``：track2 + 题目字面量逐字相等命中
   档案 → ascent；track1/题目不匹配/空索引 → standard。匹配先走字符串
   逐字相等，再复算 sha256 与登记值对质（sha 是可解释的完整性快速校验，
   不作为匹配键）；sha 不符 = 档案损坏，fail-closed。
3. ``build_ascent_messages()``：在 setter 之外组装批判段（V1 指标摘要 +
   「逐条指出 V1 局限并给出可量化超越假设」硬约束 + 加严目标），只改
   user 消息头部；standard 路径（v1_archive=None）setter 根本不调它，
   消息逐字节不变。
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from orchestrator.common.runtime_paths import phase2_root
from orchestrator.judge.ascent_thresholds import build_targets

V1_INDEX_SCHEMA = "simpraxis.v1_index.v1"

STANDARD = "standard"
ASCENT = "ascent"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RELPATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")

# Task6 入场 LOW-6：groups 是 track2 summary.json 的三臂形（正常/结冰名义/
# 结冰+DR），不是 gait_rungs._group_stats 形；生产与夹具实样都恰为这三臂。
_GROUPS_ARMS = ("normal", "ice_nominal", "ice_dr")


class V1IndexError(RuntimeError):
    """V1 档案索引错误基类（fail-closed，不猜测）。"""


class V1IndexMissing(V1IndexError):
    """v1_index.json 不存在：按空索引处理（全部 standard），不是损坏。"""


class V1IndexCorrupt(V1IndexError):
    """v1_index.json 存在但损坏/schema 不符：禁止静默降级。"""


def sha256_text(text: str) -> str:
    """题面字面量 sha256（utf-8）；索引登记值与匹配后对质共用此实现。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def v1_index_path() -> Path:
    return phase2_root() / "v1_index.json"


def _finite_number(value: Any, where: str) -> float:
    """非 bool 有限实数校验（数值口径同 feishu_gateway.cards._finite_number：
    bool 先挡——True/False 是 int 子类；再限 int/float；math.isfinite 拒
    NaN/Inf）。本地保留副本而非 import：cards 随 feishu_gateway 包 __init__
    会拉起 lark client，分类模块须保持纯函数、零运行时依赖。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise V1IndexCorrupt(
            f"{where} 必须是实数（bool 不认），收到 {type(value).__name__}")
    f = float(value)
    if not math.isfinite(f):
        raise V1IndexCorrupt(f"{where} 必须是有限值（NaN/Inf 不认），收到 {value!r}")
    return f


def _finite_list(value: Any, where: str, length: int) -> None:
    """定长有限实数数组（summary.json 滑移/峰速组形态）。"""
    if not isinstance(value, list):
        raise V1IndexCorrupt(
            f"{where} 必须是 {length} 元素数组，收到 {type(value).__name__}")
    if len(value) != length:
        raise V1IndexCorrupt(
            f"{where} 长度必须恰为 {length}，收到 {len(value)}")
    for j, x in enumerate(value):
        _finite_number(x, f"{where}[{j}]")


def _validate_groups(v1_metrics: dict, where: str) -> None:
    """严格校验 v1_metrics.groups 的 summary.json 形（Task6 入场 LOW-6）。

    实样：tests/milestone2/fixtures/v1_summary_q1.json 与生产
    $SIMPRAXIS_PHASE2_ROOT/v1_index.json（同源）。臂集合必须恰为
    normal/ice_nominal/ice_dr（缺臂/多臂皆损坏）；每臂标量 n/n_fell 为
    非 bool 整数，success_rate/tilt_max_deg_max 为非 bool 有限数，
    slip_dist_m_mean 恰 2 元素、slip_vmax_postpush_mps_max 恰 1 元素、
    mu_per_seed 长度恰为 n。臂内额外键（如生产实样的 fall_frames）不校验、
    不拒绝——本闸只钉参与分类/对质的字段形态。
    """
    groups = v1_metrics.get("groups")
    gwhere = f"{where}.v1_metrics.groups"
    if not isinstance(groups, dict):
        raise V1IndexCorrupt(f"{gwhere} 必须是对象（summary.json 三臂形）")
    keys = set(groups)
    arms = set(_GROUPS_ARMS)
    if keys != arms:
        raise V1IndexCorrupt(
            f"{gwhere} 臂集合必须恰为 {sorted(arms)}："
            f"缺 {sorted(arms - keys)}，多余 {sorted(keys - arms)}")
    for arm in _GROUPS_ARMS:
        g = groups[arm]
        awhere = f"{gwhere}.{arm}"
        if not isinstance(g, dict):
            raise V1IndexCorrupt(f"{awhere} 必须是对象")
        n = g.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise V1IndexCorrupt(
                f"{awhere}.n 必须是 ≥1 的非 bool 整数，收到 {n!r}")
        n_fell = g.get("n_fell")
        if not isinstance(n_fell, int) or isinstance(n_fell, bool) \
                or not 0 <= n_fell <= n:
            raise V1IndexCorrupt(
                f"{awhere}.n_fell 必须是 0≤x≤n（={n}）的非 bool 整数，"
                f"收到 {n_fell!r}")
        sr = _finite_number(g.get("success_rate"), f"{awhere}.success_rate")
        if not 0 <= sr <= 1:
            raise V1IndexCorrupt(
                f"{awhere}.success_rate 必须在 [0,1]，收到 {sr!r}")
        tilt = _finite_number(g.get("tilt_max_deg_max"),
                              f"{awhere}.tilt_max_deg_max")
        if tilt < 0:
            raise V1IndexCorrupt(
                f"{awhere}.tilt_max_deg_max 必须 ≥0，收到 {tilt!r}")
        _finite_list(g.get("slip_dist_m_mean"),
                     f"{awhere}.slip_dist_m_mean", 2)
        _finite_list(g.get("slip_vmax_postpush_mps_max"),
                     f"{awhere}.slip_vmax_postpush_mps_max", 1)
        mu = g.get("mu_per_seed")
        if not isinstance(mu, list):
            raise V1IndexCorrupt(
                f"{awhere}.mu_per_seed 必须是 {n} 元素数组，"
                f"收到 {type(mu).__name__}")
        if len(mu) != n:
            raise V1IndexCorrupt(
                f"{awhere}.mu_per_seed 长度必须恰为 n（={n}），收到 {len(mu)}")
        for j, x in enumerate(mu):
            _finite_number(x, f"{awhere}.mu_per_seed[{j}]")


def _validate_entry(entry: Any, idx: int) -> None:
    where = f"entries[{idx}]"
    if not isinstance(entry, dict):
        raise V1IndexCorrupt(f"{where} 不是对象")
    required = ("track", "thread_id", "question", "question_sha256",
                "host_workdir", "files", "dr_envelope", "v1_metrics")
    missing = [k for k in required if k not in entry]
    if missing:
        raise V1IndexCorrupt(f"{where} 缺字段：{missing}")
    if entry["track"] != "track2":
        raise V1IndexCorrupt(f"{where} 目前只允许登记 track2 档案")
    for key in ("thread_id", "question", "question_sha256", "host_workdir"):
        if not isinstance(entry[key], str) or not entry[key].strip():
            raise V1IndexCorrupt(f"{where}.{key} 必须是非空字符串")
    if not _SHA256_RE.match(entry["question_sha256"]):
        raise V1IndexCorrupt(f"{where}.question_sha256 不是 64 位小写 hex")
    if sha256_text(entry["question"]) != entry["question_sha256"]:
        raise V1IndexCorrupt(f"{where} 题面 sha256 与逐字题面不一致（档案损坏）")
    files = entry["files"]
    if not isinstance(files, list) or not files:
        raise V1IndexCorrupt(f"{where}.files 必须是非空数组")
    rels: set[str] = set()
    for j, f in enumerate(files):
        fwhere = f"{where}.files[{j}]"
        if not isinstance(f, dict):
            raise V1IndexCorrupt(f"{fwhere} 不是对象")
        rel, digest = f.get("relpath"), f.get("sha256")
        if not isinstance(rel, str) or not _RELPATH_RE.match(rel) \
                or rel.startswith("/") or ".." in Path(rel).parts:
            raise V1IndexCorrupt(
                f"{fwhere}.relpath 非法（须为仓内相对路径，禁绝对路径/..）：{rel!r}")
        if not isinstance(digest, str) or not _SHA256_RE.match(digest):
            raise V1IndexCorrupt(f"{fwhere}.sha256 不是 64 位小写 hex")
        if rel in rels:
            raise V1IndexCorrupt(f"{fwhere} 重复登记：{rel}")
        rels.add(rel)
    if not isinstance(entry["dr_envelope"], dict):
        raise V1IndexCorrupt(f"{where}.dr_envelope 必须是对象")
    if not isinstance(entry["v1_metrics"], dict):
        raise V1IndexCorrupt(f"{where}.v1_metrics 必须是对象")
    # Task6 入场 LOW-6：groups 是 V1 数值对质的唯一来源，形态/类型严格钉死。
    _validate_groups(entry["v1_metrics"], where)


def load_v1_index(path: "str | Path | None" = None) -> dict:
    """加载并严格校验 V1 档案索引。

    缺失抛 ``V1IndexMissing``（调用方按空索引处理）；损坏/schema 不符抛
    ``V1IndexCorrupt``（fail-closed）。返回原样 dict（不复制：调用方只读）。
    """
    p = Path(path) if path is not None else v1_index_path()
    if not p.is_file():
        raise V1IndexMissing(f"V1 档案索引不存在：{p}（按空索引处理，全部 standard）")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise V1IndexCorrupt(f"V1 档案索引无法解析：{p}: {exc}") from exc
    if not isinstance(data, dict):
        raise V1IndexCorrupt(f"V1 档案索引顶层必须是对象：{p}")
    if data.get("schema") != V1_INDEX_SCHEMA:
        raise V1IndexCorrupt(
            f"V1 档案索引 schema 必须是 {V1_INDEX_SCHEMA}，"
            f"收到 {data.get('schema')!r}：{p}")
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise V1IndexCorrupt(f"{p}: entries 必须是数组")
    # Task6 入场 LOW-4：跨条目去重——同一冻结题（track, question_sha256）
    # 或同一 thread_id 登记两条即损坏（classify 命中哪条将不确定）。
    # 单条校验在前：此处读取的字段类型/非空都已被 _validate_entry 钉死。
    seen_questions: dict[tuple[str, str], int] = {}
    seen_threads: dict[str, int] = {}
    for i, entry in enumerate(entries):
        _validate_entry(entry, i)
        qkey = (entry["track"], entry["question_sha256"])
        if qkey in seen_questions:
            raise V1IndexCorrupt(
                f"{p}: entries[{seen_questions[qkey]}] 与 entries[{i}] "
                f"重复登记同一题（track, question_sha256）={qkey}")
        seen_questions[qkey] = i
        if entry["thread_id"] in seen_threads:
            j = seen_threads[entry["thread_id"]]
            raise V1IndexCorrupt(
                f"{p}: entries[{j}] 与 entries[{i}] 重复登记同一 "
                f"thread_id={entry['thread_id']!r}")
        seen_threads[entry["thread_id"]] = i
    return data


def _empty_classification() -> dict:
    return {"run_mode": STANDARD, "v1_archive": None, "ascent_targets": {}}


def _redacted_archive(entry: dict) -> dict:
    """进状态/检查点的档案副本：只含 thread/路径/sha 与数值摘要（无密钥面）。

    v1_index.json 本身在 0700 私目录、不含凭据；这里仍显式取白名单字段，
    避免索引将来加字段时无意写进 LangGraph 检查点。
    """
    return {
        "schema": V1_INDEX_SCHEMA,
        "track": entry["track"],
        "question_index": entry.get("question_index"),
        "thread_id": entry["thread_id"],
        "question": entry["question"],
        "question_sha256": entry["question_sha256"],
        "host_workdir": entry["host_workdir"],
        "files": copy.deepcopy(entry["files"]),
        "v1_metrics": copy.deepcopy(entry["v1_metrics"]),
    }


def classify_run(payload: "dict | None", index: "dict | None") -> dict:
    """确定性分类（纯函数，0 付费，0 IO）。

    payload: {"track": "track1|track2", "question": str}（track1 可用
    {"name": ...}）。index: load_v1_index 返回的 dict；None 或空 entries
    等价空索引。命中规则：track2 且题面与登记题面**逐字相等**（随后复算
    sha256 对质，不符即 V1IndexCorrupt）。其余一切（track1、题面不匹配、
    空索引、空题面）→ standard，v1_archive=None、ascent_targets={}。
    """
    if not isinstance(index, dict) or not index.get("entries"):
        return _empty_classification()
    if not isinstance(payload, dict) or payload.get("track") != "track2":
        return _empty_classification()
    question = payload.get("question")
    if not isinstance(question, str) or not question:
        return _empty_classification()
    for i, entry in enumerate(index["entries"]):
        if not isinstance(entry, dict) or entry.get("track") != "track2":
            continue
        if entry.get("question") != question:
            continue
        if sha256_text(question) != entry.get("question_sha256"):
            # 逐字命中但 sha 对不上：索引自身不一致，禁止当 standard 静默放过。
            raise V1IndexCorrupt(
                f"entries[{i}] 题面逐字命中但 sha256 不符（档案损坏，STOP 重取）")
        # build_targets 对 dr_envelope 与登记基线做逐值校验，漂移即 fail-closed。
        targets = build_targets(entry.get("dr_envelope"))
        return {"run_mode": ASCENT,
                "v1_archive": _redacted_archive(entry),
                "ascent_targets": targets}
    return _empty_classification()


# ---------------------------------------------------------------------------
# 批判段（setter 之外组装；standard 路径永不调用）
# ---------------------------------------------------------------------------

def render_v1_metrics(v1_archive: dict) -> str:
    """把档案中的三臂 V1 数值摘要渲染成确定性文本（只读书签快照，不读文件）。"""
    groups = (v1_archive.get("v1_metrics") or {}).get("groups")
    if not isinstance(groups, dict):
        return "（V1 档案无数值摘要）"
    label = {"normal": "正常地面", "ice_nominal": "结冰名义", "ice_dr": "结冰+DR"}
    lines = []
    for arm in ("normal", "ice_nominal", "ice_dr"):
        g = groups.get(arm)
        if not isinstance(g, dict):
            continue
        slip = g.get("slip_dist_m_mean")
        vmax = g.get("slip_vmax_postpush_mps_max")
        slip_txt = "/".join(f"{x:.6g}" for x in slip) \
            if isinstance(slip, list) else "?"
        vmax_txt = "/".join(f"{x:.6g}" for x in vmax) \
            if isinstance(vmax, list) else "?"
        lines.append(
            f"  - {label.get(arm, arm)}（n={g.get('n', '?')}）："
            f"success_rate={g.get('success_rate', '?')}，"
            f"n_fell={g.get('n_fell', '?')}，"
            f"滑移距离均值组={slip_txt} m，"
            f"推力后峰速组峰={vmax_txt} m/s，"
            f"倾角极值={g.get('tilt_max_deg_max', '?')}°")
    return "\n".join(lines)


def build_ascent_messages(messages: list[dict[str, str]], *,
                          v1_archive: dict,
                          ascent_targets: dict) -> list[dict[str, str]]:
    """在首轮 user 消息头部插入 RSI 升阶批判段（system 消息一字不动）。

    硬约束（计划 Task2 L411）：逐条指出 V1 局限、给出可量化超越假设
    （ascent_claims，含 metric/direction/task/target/epsilon）；RL 算法
    不动（Q1 原题前提），只允许改控制器/DR 参数与实验构造。Gate1 打回轮
    的消息历史已含本段（首轮插入后 repair/reject 都在其上追加），不重复插。
    """
    if not isinstance(messages, list) or len(messages) < 2:
        raise V1IndexError("ascent 批判段需要至少 system+user 两条消息")
    user_idx = next((i for i, m in enumerate(messages)
                     if isinstance(m, dict) and m.get("role") == "user"), -1)
    if user_idx < 0:
        raise V1IndexError("ascent 批判段找不到 user 消息")
    user_content = messages[user_idx].get("content")
    if not isinstance(user_content, str):
        raise V1IndexError("ascent 批判段要求 user content 为纯文本")
    thread_id = v1_archive.get("thread_id", "?")
    targets_json = json.dumps(ascent_targets, ensure_ascii=False,
                              sort_keys=True, indent=2)
    header = (
        "【RSI 螺旋升阶模式（ascent）｜本题存在冻结 V1 档案，禁止伪装首次求解】\n"
        f"V1 档案：thread_id={thread_id}；V1 宿主工作目录与全部证据文件"
        "（summary/分屏视频/9 份 telemetry）已逐文件登记 sha256 锁定，"
        "V2 证据将与该档案逐值对质。\n"
        "V1 指标摘要（三臂×3 种子，独立重算值；脚本自报 outcomes 不参与）：\n"
        f"{render_v1_metrics(v1_archive)}\n"
        "V1 加严验收目标（公差×0.8、接触/滑移界×0.8、DR 应力包络按登记基线"
        "外扩；FALL_Z_M/FALL_TILT_DEG/35N×0.2s 等物理与预注册锚点不变）：\n"
        f"{targets_json}\n"
        "本轮硬要求：\n"
        "1. 逐条指出 V1 方案的物理/策略局限（对照上述三臂真实数值，禁止泛泛而谈）；\n"
        "2. 每个候选必须给出可量化的超越假设 ascent_claims（可选字段，若给出"
        "必须为非空数组），逐项形如 "
        '{"metric": "success_rate|slip_dist_mean_m|postpush_vmax_mean_mps|'
        'tilt_max_deg_max|solve_wallclock_s|vram_peak_gb", '
        '"direction": 1 或 -1（只允许改善方向，1=增大，-1=减小；亦可写 '
        '"inc"/"dec"）, "task": "对应验证任务/对照臂的非空说明", '
        '"target": 数值（V2 目标值）, "epsilon": 非负数值（可接受容差）}；\n'
        "3. Pareto 诚实原则：V2 必须在全部六项指标上不差于 V1（含容差）且至少"
        "一项严格超越才算支配；互有胜负必须如实报告 non_dominated，禁止凑加权"
        "总分翻案；\n"
        "4. RL 算法端不修改（原题前提）：只允许动控制器参数、接触刚度/摩擦/"
        "关节阻尼的域随机化分布与实验构造；\n"
        "5. 加严后的 DR 包络必须覆盖 ascent_targets 给出的应力端，key_params "
        "逐值写明分布，量级可执行。\n\n"
    )
    out = list(messages)
    out[user_idx] = {"role": "user", "content": header + user_content}
    return out
