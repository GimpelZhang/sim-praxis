"""Milestone 0 端到端验收。前置：Task0~4 全部完成且其各自自检已绿。

共 7 个测试函数（六组验收口径 + 04b 卫生/置空校验）：
  test_01 blueprint：tour blueprint 4 阶段且证据对照真实克隆全部存在
  test_02 KB：全量中文真实召回（YAML 静默覆盖 -> lw_benchhub；EGL 黑屏 -> troubleshooting），
              provenance 非空且 source_commit==KB HEAD
  test_03 渲染：MP4 来自真实 GPU EGL（tier=gpu、NVIDIA A800、1920x1080x60、质心运动）；
              退出码 2 的 OSMesa 软渲不允许冒充成功
  test_04 网关：三阶段 --validate-models 实呼一批 4 请求（3 文本 ping max_tokens=8
              + 32x32 纯红视觉 ping max_tokens=16；Task2 首批、本测复验为第二批，
              计划内共 8 次付费调用；v4.2 决策 3 获批换供重验批次预算外另计），
              result.json 三 stage 全 200/type=message 且视觉
              词边界断言 red/红；失败证据落新时间戳目录 FAILURE.json 且 latest 不移动（m4）；
              null stage 必须有 SKIP-DUE-TO-NULL 行；熔断窗口内 deterministic 地失败
              （returncode=3 + 状态文件）
  test_04b 卫生：密钥扫描 0 命中、stage_models.yaml 无占位符/无 key、三阶段 schema 合法、
              null stage 带合规 open_question 锚点（锚点文件经 Task0 §8.10 播种，
              v4.1 M1：文件读取只在存在 null stage 时发生）
  test_05 仓库：两个仓库 git-clean、密钥扫描通过、全部提交 author 为 Junchuan Zhang；
              KB 零提交且 HEAD==基线全 SHA aac53313c620efa6217fd51a55834b006d78e689（M3）
  test_06 资源：沙箱容器全部停止，GPU 显存回到基线附近（<=200 MiB）
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
KB_ROOT = Path(os.environ.get("KB_ROOT", "/home/vipuser/embodied-ai-sim-knowledge-base"))
RUN_DIR = Path(os.environ.get("SIMPRAXIS_RUN_DIR", "/mnt/artifacts/simpraxis/runs"))
TOURS = Path(os.environ.get("SIMPRAXIS_TOURS_DIR", "/mnt/assets/simpraxis/tours"))
VENV_PY = "/mnt/venvs/simpraxis/bin/python"


def run(cmd, cwd=ROOT, check=True, env=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    if check and p.returncode != 0:
        raise AssertionError(f"命令失败 {cmd}\nstdout={p.stdout}\nstderr={p.stderr}")
    return p


# ---------- ① blueprint 真实证据 ----------

def test_01_blueprint_four_stages_with_real_evidence():
    p = run([VENV_PY, str(ROOT / "scripts/verification/check_blueprint_evidence.py")])
    assert "blueprint OK" in p.stdout
    bp = json.loads((ROOT / "templates/tour_blueprint.json").read_text(encoding="utf-8"))
    assert [s["stage_id"] for s in bp["canonical_spine"]] == [
        "Stage 0", "Stage 1", "Stage 2", "Stage 3"]
    for slug, info in bp["observed_stage_maps"].items():
        assert info["clone_status"] == "ok"
        assert info["commit"] not in ("", "__FILL__")
        assert (TOURS / info["repo"].split("/")[-1]).is_dir(), slug
        assert len(info["stages"]) >= 3, f"{slug} stage 映射过少，疑似臆造"


# ---------- ② 真实 KB 召回 + provenance ----------

def test_02_kb_real_chinese_recall():
    script = r'''
from memory_flywheel.vector_engine import VectorEngine
import json, sys
eng = VectorEngine()
out = {}
q1 = ("YAML 配置文件为什么会静默覆盖命令行参数？我在命令行传 --device cuda:0 "
      "为什么还是跑在 CPU 上？")
h1 = eng.query_kb(q1, namespace="capabilities", project="lw_benchhub", top_k=5)
out["q1"] = [{"kb_relpath": h["kb_relpath"], "section_path": h["section_path"],
              "source_commit": h["source_commit"], "content_hash": h["content_hash"],
              "doc_type": h["doc_type"]} for h in h1]
q2 = "headless 无显示器，EGL 离屏渲染初始化失败、黑屏怎么排查？"
h2 = eng.query_kb(q2, namespace="troubleshooting", top_k=5)
out["q2"] = [{"kb_relpath": h["kb_relpath"], "project": h["project"],
              "section_path": h["section_path"], "source_commit": h["source_commit"],
              "content_hash": h["content_hash"]} for h in h2]
ok1 = any("background_knowledge.md" in h["kb_relpath"]
          and ("静默覆盖" in h["text"] or ("update" in h["text"] and "命令行" in h["text"]))
          for h in h1)
ok2 = any(h["project"] == "lw_benchhub" and "EGL" in h["text"] for h in h2)
prov = all(h["kb_relpath"] and h["source_commit"] and h["content_hash"]
           for h in h1 + h2)
out["verdict"] = {"yaml_override_hit": ok1, "egl_black_screen_hit": ok2,
                  "provenance_nonempty": prov, "total": eng.count()}
print(json.dumps(out, ensure_ascii=False))
sys.exit(0 if (ok1 and ok2 and prov) else 1)
'''
    p = run([VENV_PY, "-c", script])
    data = json.loads(p.stdout.strip().splitlines()[-1])
    v = data["verdict"]
    assert v["yaml_override_hit"], "YAML 静默覆盖未命中 lw_benchhub background"
    assert v["egl_black_screen_hit"], "EGL 黑屏未命中 troubleshooting 路"
    assert v["provenance_nonempty"], "provenance 存在空字段"
    assert v["total"] >= 500, f"全量入库规模异常：{v['total']}"
    # 默认检索不得返回索引页（索引是导航，不是知识）
    for h in data["q1"] + data["q2"]:
        assert not h["kb_relpath"].endswith("00-index.md")
    # KB commit 必须与真实仓库 HEAD 一致
    kb_head = run(["git", "-C", str(KB_ROOT), "rev-parse", "HEAD"]).stdout.strip()
    assert data["q1"][0]["source_commit"] == kb_head


# ---------- ③ 真实 GPU EGL MP4 ----------

def test_03_mp4_from_real_gpu_egl():
    latest = RUN_DIR / "latest"
    assert latest.is_symlink(), "缺少 latest 软链（探针未成功产出）"
    d = latest.resolve()
    probe = json.loads((d / "probe_egl.json").read_text(encoding="utf-8"))
    # 必须是 GPU EGL 路径（OSMesa 软渲退出码 2 且 tier=software，不允许冒充成功）
    assert probe["tier"] == "gpu", probe
    assert probe["backend"].startswith("egl-"), probe
    assert probe["gpu_renderer_verified"] is True
    assert "NVIDIA" in probe["renderer"] and "A800" in probe["renderer"], probe["renderer"]
    assert probe["torch"]["cuda_available"] is True
    assert "A800" in probe["torch"]["device_name"], probe["torch"]
    # 像素级真实性：帧内容不重复、绿色方块质心确实在运动（与探针内断言同口径）
    assert probe["frame_unique_hashes"] >= int(0.8 * probe["frames"])
    cx_std, cy_std = probe["square_centroid_std"]
    assert cx_std > 100 and cy_std > 80, probe["square_centroid_std"]
    mp4 = d / "probe_egl.mp4"
    assert mp4.stat().st_size > 50_000, "MP4 体积异常小，疑似空壳"
    ff = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
              "-show_entries", "stream=width,height,nb_frames,codec_name",
              "-of", "json", str(mp4)])
    st = json.loads(ff.stdout)["streams"][0]
    # 锁内适配（计划 L5106 原文与字符串 "1920" 比较）：ffprobe -of json 的 width/height
    # 按 JSON 数字输出（probe 自身也 int() 转换），字符串元组恒假；按真实类型比较，
    # 仍是同一个 1920x1080 硬门，语义不放水。
    assert (int(st["width"]), int(st["height"])) == (1920, 1080), st
    assert st["codec_name"] == "h264", st
    # 容器探测报 60 帧（yuv420p 时序动画），与 JSON 内 frames 对账
    nb = int(st.get("nb_frames", 0) or 0)
    assert nb == 60 == probe["frames"], f"帧数不一致：ffprobe={nb} json={probe['frames']}"


# ---------- ④ 方舟三阶段网关实呼校验（文本 ping + 视觉 ping） ----------

@pytest.mark.paid
def test_04_gateway_real_min_call():
    # 付费守卫（2026-10-16）：本测真实触网计费一批 4 请求。裸跑/默认收集一律 skip，
    # 只有书面批准批次且显式 SIMPRAXIS_ALLOW_PAID=1 才执行（先例 test_hitl_drill）。
    # 背景：本日曾因裸跑本测在 --ignore-pause 下误触 3 次账外实呼（G-3/G-12）。
    if os.environ.get("SIMPRAXIS_ALLOW_PAID") != "1":
        pytest.skip("paid：test_04 真实触网计费，需书面批准批次并显式 "
                    "SIMPRAXIS_ALLOW_PAID=1（误触防护，见 G-3/G-12）")
    import yaml
    cfg_path = ROOT / "orchestrator/gateway/stage_models.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    stage_models = cfg["stage_models"]
    null_stages = [s for s, e in stage_models.items()
                   if e.get("model_id") is None]

    # 真实计费调用（本测是 Task2 之后的第二批，计划内两批共 8 次，见铁律 9；
    # v4.2 决策 3：仅在用户获批换供时才会产生预算外重验批次，另计、逐次记录）：
    # --validate-models 一批 4 请求 = 3 文本 ping（各 max_tokens=8）
    # + visual_rollout 一次 32x32 纯红图视觉 ping（max_tokens=16，输出上限 40 token）
    p = run([VENV_PY, "-m", "orchestrator.gateway.router", "--validate-models",
             "--max-tokens", "8"], cwd=ROOT, check=False)

    # 熔断窗口内：router deterministic 退出码 3 + 状态文件必须自洽（不是任何非零都算）
    state_p = Path(os.environ.get(
        "SIMPRAXIS_GATEWAY_STATE",
        "/mnt/artifacts/simpraxis/gateway_state")) / "quota_pause.json"
    if p.returncode == 3:
        assert state_p.is_file(), "exit 3 必须伴随 quota_pause.json"
        st = json.loads(state_p.read_text(encoding="utf-8"))
        assert st["status"] == "PAUSED_WAITING_QUOTA", st
        assert float(st["resume_at_epoch"]) > float(st["paused_at_epoch"]), st
        pytest.fail(f"网关处于配额熔断（真实阻塞），resume_at={st['resume_at_iso']}；"
                    "等到恢复时刻（或申请额度）后真跑重验，禁止删状态文件/mock 转绿")

    if p.returncode != 0:
        hint = ""
        if state_p.is_file():
            st = json.loads(state_p.read_text(encoding="utf-8"))
            hint = f"状态文件：{st.get('status')} resume_at={st.get('resume_at_iso')}"
        # m4：失败时部分结果+错误在新时间戳目录 FAILURE.json（latest 不会指向它）
        fail_docs = sorted((RUN_DIR / "model_validate").glob("*/FAILURE.json")) \
            if (RUN_DIR / "model_validate").is_dir() else []
        if fail_docs:
            fd = json.loads(fail_docs[-1].read_text(encoding="utf-8"))
            hint += f"\n最近失败证据：{fail_docs[-1]} status={fd.get('status')} " \
                    f"error_type={fd.get('error_type')} 已完成 stage={[r.get('stage') for r in fd.get('results', [])]}"
        pytest.fail(f"真实网关校验失败（returncode={p.returncode}）\n"
                    f"stdout={p.stdout}\nstderr={p.stderr}\n{hint}")

    # null stage（显式缺口，不付费）必须逐一出现在 SKIP-DUE-TO-NULL 行
    skip_lines = [ln for ln in p.stdout.splitlines() if "[gateway SKIP-DUE-TO-NULL]" in ln]
    for s in null_stages:
        assert any(f"stage={s} " in ln for ln in skip_lines), \
            f"null stage {s} 缺少 SKIP-DUE-TO-NULL 报告：{p.stdout}"

    # result.json 是机器证据：三 stage 全 200 且响应 type=message
    result_p = RUN_DIR / "model_validate" / "latest" / "result.json"
    assert result_p.is_file(), f"缺少校验证据 {result_p}（{p.stdout}）"
    doc = json.loads(result_p.read_text(encoding="utf-8"))
    assert doc["skipped_null"] == null_stages
    rows = {r["stage"]: r for r in doc["results"]}
    non_null = [s for s in ("macro_ideation", "coding_debug", "visual_rollout")
                if s not in null_stages]
    assert set(rows) == set(non_null), f"结果 stage 集合不符：{sorted(rows)}"
    for s in non_null:
        r = rows[s]
        assert r["http_status"] == 200 and r["type"] == "message", (s, r)
        assert r["requested_id"] == stage_models[s]["model_id"], (s, r)
        assert str(r["echo_model"]).strip(), "真实模型回显为空"
    # 视觉实证（2026-10-12 红蓝二测的可复验版）：32x32 纯红图必须答 red/红。
    # Milestone 口径：visual_rollout 已实证可用，不接受 null——供应失效时按 §10.6
    # v4.2 决策 3 流程处理：STOP→inventory 取证→请示用户，获批后换供（同家族日期版本）
    # 重验；执行者不得自行换 ID/置 null。未获批前 Milestone 保持红是诚实信号。
    assert "visual_rollout" in rows, \
        "visual_rollout 缺失（被置 null？）：Milestone 不接受该 stage 缺口；按 §10.6 停止并请示用户，获批后换供重验"
    v = rows["visual_rollout"]["vision"]
    assert v["red_assert"] is True and v["http_status"] == 200, v
    # n2：与 router 同口径的词边界断言（"tired" 含子串 red 不允许假绿）
    ans = v["answer"].strip().lower()
    import re as _re
    assert _re.search(r"\bred\b", ans) is not None or "红" in v["answer"], v
    stdout = p.stdout
    assert "[gateway VISION OK]" in stdout and "[gateway VALIDATE OK]" in stdout, stdout


def test_04b_gateway_no_secret_committed_and_null_path_valid():
    # e2e 内只跑快速密钥卫生；完整静态门禁（bash -n/py_compile/YAML/探针数学）
    # 由各 Task 收尾的 gate_static.sh 负责，此处不重复跑。
    run(["bash", str(ROOT / "scripts/verification/scan_secrets.sh")])
    cfg = ROOT / "orchestrator/gateway/stage_models.yaml"
    assert cfg.is_file(), "stage_models.yaml 必须直接提交（真实模型 ID，非密钥）"
    text = cfg.read_text(encoding="utf-8")
    assert "__" not in text, "stage_models.yaml 仍有未替换占位符"
    # 锁内适配（计划 L5201 原文禁 ark- 子串）：计划自己批准的 model_id 就是
    # ark-code-latest（test_04 依赖它成功），子串断言自相矛盾；改为与 scan_secrets.sh
    # 第 4 条同形的【密钥形状】断言（ark- + ≥13 位字母数字/连字符），model_id 不匹配，
    # 真 key（ark- 后 20+ 位）必中——防泄密意图原样保留，一行也没放宽。
    ARK_KEY_SHAPED = r"ark-[A-Za-z0-9][A-Za-z0-9-]{12,}"
    assert re.search(ARK_KEY_SHAPED, text) is None, \
        "疑似把 API key 写进了 stage_models.yaml（命中 ark 密钥形状）"
    assert "/api/v3" not in text, "残留旧 OpenAI 兼容端点"

    # null+open_question 合规路径的端到端校验（构造网关不触网、不计费）
    import sys as _sys
    _sys.path.insert(0, str(ROOT))
    from orchestrator.gateway.router import (  # noqa: E402
        ModelGateway, STAGES, StageUnresolvedError)
    import yaml  # noqa: E402
    mm = yaml.safe_load(text)
    assert mm["meta"]["endpoint"] == "anthropic-messages"
    assert set(mm["stage_models"]) == set(STAGES)
    # G-13：无账构造必须显式 unguarded=True（本路径只做配置校验，不触网不计费）
    gw = ModelGateway(stage_models_path=cfg, unguarded=True)  # null 必须有逐字锚点
    nulls = gw.null_stages()
    # 三阶段默认全部非 null（2026-10-12 三模型均实证 200）；null 仅为供应失效时的合规缺口
    assert nulls == [], f"默认配置不得有 null stage：{nulls}"
    # v4.1 M1：锚点文件由 Task0 §8.10 播种（.omc/ 不入 git）；只在确有 null stage 时
    # 才允许读它——默认无 null 的全新执行机没有该文件也不得 FileNotFoundError。
    oq_text = ""
    if nulls:
        oq_path = Path(os.environ.get(
            "SIMPRAXIS_OPEN_QUESTIONS",
            str(ROOT / ".omc" / "plans" / "open-questions.md")))
        assert oq_path.is_file(), (
            f"存在 null stage {nulls} 但锚点文件缺失：{oq_path}（Task0 §8.10 应已播种）")
        oq_text = oq_path.read_text(encoding="utf-8")
    for s in nulls:
        anchor = mm["stage_models"][s]["open_question"]
        assert anchor.strip() and anchor in oq_text, f"{s} 的锚点必须逐字在 open-questions.md"
        # null stage 调用必须抛 StageUnresolvedError 而不是发请求
        with pytest.raises(StageUnresolvedError):
            gw.call(s, [{"role": "user", "content": "x"}])
    # 非空 stage 与 null stage 恰好划分三个阶段
    assert set(nulls) | {s for s in STAGES if gw.model_id(s) is not None} == set(STAGES)


# ---------- ⑤ 两个仓库洁净 + author + KB 零提交 ----------

EXPECTED_AUTHOR = "Junchuan Zhang <zjunchuan@gmail.com>"


def test_05_repos_clean_and_author():
    # 产品仓：干净（允许仅有计划文档外的未跟踪？——不允许，Phase 0 要求全提交）
    status = run(["git", "-C", str(ROOT), "status", "--porcelain"]).stdout.strip()
    assert status == "", f"sim-praxis 有未提交内容：\n{status}"
    log = run(["git", "-C", str(ROOT), "log", "-1",
               "--format=%an <%ae>"]).stdout.strip()
    assert log == EXPECTED_AUTHOR, log
    # Phase 0 所有提交 author 都必须合规（不只是 HEAD）
    authors = run(["git", "-C", str(ROOT), "log", "--format=%an <%ae>"]).stdout
    bad = {a for a in authors.splitlines() if a != EXPECTED_AUTHOR}
    assert not bad, f"存在不合规 author：{bad}"
    run(["bash", str(ROOT / "scripts/verification/check_git_author.sh")])
    # e2e 内只跑快速密钥卫生；完整静态门禁（bash -n/py_compile/YAML/探针数学）
    # 由各 Task 收尾的 gate_static.sh 负责，此处不重复跑。
    run(["bash", str(ROOT / "scripts/verification/scan_secrets.sh")])
    # KB 仓：只读——工作区干净，且 HEAD 必须钉在基线全 SHA（v4.1 M3：
    # 仅查工作区干净可被"先向 KB 提交再清工作区"绕过）。
    kb_status = run(["git", "-C", str(KB_ROOT), "status", "--porcelain"]).stdout.strip()
    assert kb_status == "", f"KB 仓库被改动（Phase 0 禁止）：\n{kb_status}"
    kb_head = run(["git", "-C", str(KB_ROOT), "rev-parse", "HEAD"]).stdout.strip()
    assert kb_head == "aac53313c620efa6217fd51a55834b006d78e689", (
        f"KB HEAD 已移动（只读违规）：{kb_head}")


# ---------- ⑥ 容器停止 + 显存归零 ----------

def test_06_containers_down_and_vram_released():
    # Task 1 终态是 compose down（§9 自检要求 docker ps -a 查名为空）
    alive = run(["docker", "ps", "--filter", "name=sim_praxis_sandbox",
                 "--format", "{{.Names}}"]).stdout.strip()
    assert alive == "", f"沙箱容器仍在运行：{alive}"
    leftover = run(["docker", "ps", "-a", "--filter", "name=sim_praxis_sandbox",
                    "--format", "{{.Names}} {{.State}}"]).stdout.strip()
    assert leftover == "", f"沙箱容器残留（含已退出）：{leftover}"
    smi = run(["nvidia-smi", "--query-gpu=memory.used",
               "--format=csv,noheader,nounits"]).stdout
    used = max(int(x.strip()) for x in smi.splitlines() if x.strip())
    assert used <= 200, f"GPU 显存未释放：{used} MiB（基线约 14 MiB，上限 200）"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"]))
