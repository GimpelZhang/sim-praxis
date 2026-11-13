"""RSI-R1.2 T5：离线生产准入（零 POST 全链）与验证 LOCK。

覆盖（对应 Plan L134/L227 的验收面）：

* 离线授权 fail-closed：非 offline_validation envelope / 账本已有实呼行 /
  消费者源码缺件或含宿主依赖记号 / 授权件身份漂移；
* 公开消费者静态审 + canary：源码无宿主 import/子进程/网络；私有盘、他 study、
  密钥前缀等字节串出现在消费者输入/输出即 ``CANARY_LEAK``（且报错不回显）；
* transport 故障矩阵（崩溃/被杀/未知动作/坏 JSON/超时）各自带回错误码且
  **不重复执行**；解析器对未知 action 报 ``UNKNOWN_ACTION``；
* 边界矩阵（63/64/65/96 格、未知条件、重复格、plan 漂移、账本形状、批上限）；
* 状态隔离（跨 study 隐藏、未 claim 不 conclude、死后不 claim、旧心跳）；
* validation LOCK 的引导期免签只对 ``purpose=validation`` 生效，付费 LOCK 一律拒；
* ``validate-production`` 在 ``SIMPRAXIS_ALLOW_PAID=1`` 下直接 BLOCKED，且
  未冻结的根一律 BLOCKED。

真容器车道（``docker_integration``）：正式 ``DockerRunner`` 起钉死镜像的
兄弟容器做零网络的 argv 烟测，验证镜像 pin/清理面；与收尾时同一 CLI 的
端到端六套演练（真 MuJoCo + 真容器 + 公开消费者）互为补充。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import calibration as CAL
from orchestrator.rsi_r1 import cli_r12
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import lock as LK
from orchestrator.rsi_r1 import offline_suite as OS
from orchestrator.rsi_r1 import offline_validation as OV
from orchestrator.rsi_r1 import prereg as PREREG
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import protocol as PT
from orchestrator.rsi_r1 import sandbox as SANDBOX
from tests.docker_support import require_docker_integration

REPO = Path(__file__).resolve().parents[1]
ENVELOPE = "env_off_t5"


# ---------------------------------------------------------------------------
# 夹具：真实 R12Context（family 根经环境变量重定向到 tmp）
# ---------------------------------------------------------------------------

def _init_root(tmp_path: Path, monkeypatch) -> Path:
    family = tmp_path / "rsi_r12"
    family.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_ROOT", str(family))
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_CAMPAIGN", ENVELOPE)
    plan = tmp_path / "plan.md"
    plan.write_text("RSI-R1.2 offline test plan bytes\n", encoding="utf-8")
    assert cli_r12.main(["init", "--envelope", ENVELOPE,
                         "--mode", "offline_validation",
                         "--plan", str(plan)]) == 0
    return family / ENVELOPE


def _ctx(root: Path) -> "cli_r12.R12Context":
    return cli_r12.R12Context(root, ENVELOPE)


def _authorized_root(tmp_path: Path, monkeypatch) -> Path:
    root = _init_root(tmp_path, monkeypatch)
    ctx = _ctx(root)
    envelope = ctx.read_json("contract/envelope.json")
    PREREG.write_prereg(
        ctx, envelope_id=ENVELOPE,
        plan_sha256=str(envelope["plan_sha256"]),
        model_config_sha256=envelope.get("model_config_sha256"))
    ledger = root / "quota" / "ledger.jsonl"
    auth = OV.authorize_offline(ctx, repo_root=REPO, ledger_path=ledger,
                                consumer_path=OV.consumer_source(REPO))
    ctx.write_json(OV.SPEC_RELPATH, OV.suite_spec_document(
        ctx, authorization_sha256=C.canonical_sha256(auth)))
    return root


def _env(root: Path, *, runner=None) -> "OS.SuiteEnv":
    ctx = _ctx(root)
    return OS.SuiteEnv(
        ctx=ctx, image="offline-image",
        runner_factory=runner or (lambda: pytest.fail("本用例不得起容器")),
        counter=lambda text: max(1, len(text) // 4),
        ledger_path=root / "quota" / "ledger.jsonl", repo_root=REPO,
        profile=P.profile_by_id(P.PROFILE_R12),
        progress=OS.Progress(ctx))


# ---------------------------------------------------------------------------
# 授权 fail-closed
# ---------------------------------------------------------------------------

def test_authorize_offline_requires_offline_mode(tmp_path, monkeypatch):
    root = _init_root(tmp_path, monkeypatch)
    ctx = _ctx(root)
    envelope = ctx.read_json("contract/envelope.json")
    envelope["mode"] = "paid"           # 制造错配（不重跑 init 的付费门）
    ctx.write_json("contract/envelope.json", envelope)
    with pytest.raises(OV.OfflineValidationError) as exc:
        OV.authorize_offline(ctx, ledger_path=root / "quota" / "ledger.jsonl")
    assert exc.value.code == "OFFLINE_MODE_REQUIRED"


def test_authorize_offline_refuses_nonzero_ledger(tmp_path, monkeypatch):
    root = _init_root(tmp_path, monkeypatch)
    ctx = _ctx(root)
    ledger = root / "quota" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"event": "call", "seq": 1}) + "\n",
                      encoding="utf-8")
    with pytest.raises(OV.OfflineValidationError) as exc:
        OV.authorize_offline(ctx, ledger_path=ledger)
    assert exc.value.code == "NOT_ZERO_POST"


def test_consumer_static_audit_rejects_host_dependency(tmp_path):
    bad = tmp_path / "bad_consumer.py"
    bad.write_text("import subprocess\nprint('x')\n", encoding="utf-8")
    with pytest.raises(OV.OfflineValidationError) as exc:
        OV.verify_consumer_source(bad)
    assert exc.value.code == "CONSUMER_HOST_DEPENDENCY"


def test_consumer_static_audit_rejects_absolute_and_dynamic_paths(tmp_path):
    """MINOR-5：静态审补三条源级规则（绝对路径 / 动态 open / 动态执行）。

    残余面（``sys.argv``/``Path`` 运算）由 sandbox 脚本目录 + 人工复核兜住，
    这里不宣称全闭包，只钉死「源码里不出现」的三类写法。
    """
    cases = {
        "CONSUMER_ABSOLUTE_PATH": "p = '/etc/passwd'\n",
        "CONSUMER_DYNAMIC_OPEN": "import sys\nopen(sys.argv[1])\n",
        "CONSUMER_DYNAMIC_EXEC": "eval('1+1')\n",
    }
    for code, src in cases.items():
        bad = tmp_path / f"bad_{code.lower()}.py"
        bad.write_text("import sys\n" + src, encoding="utf-8")
        with pytest.raises(OV.OfflineValidationError) as exc:
            OV.verify_consumer_source(bad)
        assert exc.value.code == code
    # 真实消费者在新的严口径下仍过（否则六套会整体失效）
    info = OV.verify_consumer_source(OV.consumer_source(REPO))
    assert info["isolation"] == "static_audit_plus_sandbox"
    assert info["checks"]


def test_real_consumer_source_is_clean_and_stable():
    path = OV.consumer_source(REPO)
    info = OV.verify_consumer_source(path)
    assert info["n_bytes"] > 0
    assert info["sha256"] == OV.consumer_sha256(path)


def test_assert_authorized_detects_consumer_drift(tmp_path, monkeypatch):
    root = _authorized_root(tmp_path, monkeypatch)
    ctx = _ctx(root)
    repo = tmp_path / "repo"
    consumer = repo / OV.CONSUMER_RELPATH
    consumer.parent.mkdir(parents=True)
    consumer.write_bytes(OV.consumer_source(REPO).read_bytes())
    ledger = root / "quota" / "ledger.jsonl"
    auth = OV.authorize_offline(ctx, repo_root=repo, ledger_path=ledger,
                                consumer_path=consumer)
    assert auth["ledger_call_rows_at_issue"] == 0
    OV.assert_authorized(ctx, repo_root=repo, ledger_path=ledger)
    consumer.write_bytes(consumer.read_bytes() + b"\n# drift\n")
    with pytest.raises(OV.OfflineValidationError) as exc:
        OV.assert_authorized(ctx, repo_root=repo, ledger_path=ledger)
    assert exc.value.code == "CONSUMER_DRIFT"


def test_canary_leak_is_reported_without_echo(tmp_path, monkeypatch):
    root = _authorized_root(tmp_path, monkeypatch)
    ctx = _ctx(root)
    secret = "/mnt/artifacts/private-study-B"
    log = OV.TurnLog(ctx, canaries=OV.CanarySet((secret.encode("utf-8"),)))
    with pytest.raises(OV.OfflineValidationError) as exc:
        log.append({"request_sha256": "0" * 64, "note": "x", "blob": secret})
    assert exc.value.code == "CANARY_LEAK"
    assert secret not in str(exc.value)


# ---------------------------------------------------------------------------
# suite3/4/5：宿主侧边界、transport 故障、状态隔离（无需容器）
# ---------------------------------------------------------------------------

def test_suite3_boundaries_pass(tmp_path, monkeypatch):
    root = _authorized_root(tmp_path, monkeypatch)
    doc = OS.suite3_boundaries(_env(root))
    assert doc["status"] == "PASS", json.dumps(doc["observed"], ensure_ascii=False)
    checks = doc["observed"]["checks"]
    assert checks["cells_65"] == "RESOURCE_EXCEEDED"
    assert checks["cells_96"] == "RESOURCE_EXCEEDED"
    # 独立评审后：拒绝一律绑具体生产码（前置与测量分开，不再用「任何异常」）。
    assert checks["batch_limit_8"] == "RESOURCE_EXCEEDED"
    assert checks["sandbox_33rd"] == "RESOURCE_EXCEEDED"
    assert checks["confirm_33_blocks"] == "CONFIRM_BLOCK_COUNT"
    assert checks["cumulative_65"] == "RESOURCE_EXCEEDED"
    assert checks["confirm_post_margin_refused"] == "BUDGET_UNREACHABLE"
    assert checks["confirm_post_margin_ok"] == "OK"


def test_suite4_transport_faults_pass(tmp_path, monkeypatch):
    root = _authorized_root(tmp_path, monkeypatch)
    doc = OS.suite4_transport_faults(_env(root))
    assert doc["status"] == "PASS", json.dumps(doc["observed"], ensure_ascii=False)
    checks = doc["observed"]["checks"]
    assert checks["consumer_crash"] == "CONSUMER_EXIT"
    assert checks["consumer_kill"] == "CONSUMER_EXIT"
    assert checks["timeout"] == "CONSUMER_TIMEOUT"
    assert checks["action_unknown_in_parser"] == "UNKNOWN_ACTION"


def test_suite5_state_isolation_pass(tmp_path, monkeypatch):
    root = _authorized_root(tmp_path, monkeypatch)
    doc = OS.suite5_state_isolation(_env(root))
    assert doc["status"] == "PASS", json.dumps(doc["observed"], ensure_ascii=False)
    checks = doc["observed"]["checks"]
    # 独立评审后：pause 走**生产准入**行为断言，死亡走真子进程 kill，跨 study
    # 正反两向（不再靠「B 列表为空」「从未存在的 pid」这种真空证据）。
    assert checks["pause_blocks_dispatch"] == "PAUSED"
    assert checks["pause_gate_control"] == "LOCK_MISSING"
    assert checks["pause_cleared"] is True
    assert checks["dead_pid_not_alive"] is True
    assert checks["cross_study_hidden"] is True
    assert checks["cross_study_binding_refused"] == "STUDY_MISMATCH"


def test_progress_is_append_only_jsonl(tmp_path, monkeypatch):
    root = _authorized_root(tmp_path, monkeypatch)
    progress = OS.Progress(_ctx(root))
    progress.note("a", status="PASS")
    progress.note("b", status="PASS")
    rows = [json.loads(line) for line in
            (root / OS.PROGRESS_RELPATH).read_text(encoding="utf-8").splitlines()
            if line.strip()]
    assert [r["step"] for r in rows] == ["a", "b"]


def test_suite_spec_covers_every_registered_suite():
    assert set(OV.SUITE_SPEC) == set(OS.SUITE_ORDER)
    assert len(OS.SUITE_ORDER) == 6


# ---------------------------------------------------------------------------
# 公开视图：contract/ 唯一源 → public/ 候选可见面（生产链只读 public/）
# ---------------------------------------------------------------------------

def test_publish_public_view_is_byte_identical_and_idempotent(tmp_path, monkeypatch):
    root = _init_root(tmp_path, monkeypatch)
    args = ["publish-public", "--root", str(root), "--envelope", ENVELOPE]
    assert cli_r12.main(args) == 0
    src = root / cli_r12.CONTRACT_JSON_RELPATH
    view = root / cli_r12.PUBLIC_VIEW_JSON_RELPATH
    assert view.read_bytes() == src.read_bytes()
    src_md = root / cli_r12.CONTRACT_MD_RELPATH
    view_md = root / cli_r12.PUBLIC_VIEW_MD_RELPATH
    assert view_md.read_bytes() == src_md.read_bytes()
    assert cli_r12.main(args) == 0            # 幂等
    assert view.read_bytes() == src.read_bytes()


def test_publish_public_refuses_tampered_source(tmp_path, monkeypatch):
    root = _init_root(tmp_path, monkeypatch)
    src = root / cli_r12.CONTRACT_JSON_RELPATH
    doc = json.loads(src.read_text(encoding="utf-8"))
    doc["_tamper"] = True                     # 改写语义但不改 envelope 绑定
    src.write_text(cli_r12._json_text(doc), encoding="utf-8")
    assert cli_r12.main(["publish-public", "--root", str(root),
                         "--envelope", ENVELOPE]) == 2
    assert not (root / cli_r12.PUBLIC_VIEW_JSON_RELPATH).exists()


def test_freeze_view_gate_requires_matching_public_view(tmp_path, monkeypatch):
    root = _init_root(tmp_path, monkeypatch)
    envelope = _read_envelope(root)
    with pytest.raises(cli_r12.Cli12Error) as exc:
        cli_r12._verify_public_view(root, envelope)
    assert exc.value.code == "PUBLIC_VIEW_MISSING"
    assert cli_r12.main(["publish-public", "--root", str(root),
                         "--envelope", ENVELOPE]) == 0
    cli_r12._verify_public_view(root, envelope)          # 在场且逐字节相等
    (root / cli_r12.PUBLIC_VIEW_MD_RELPATH).write_text("drift\n",
                                                       encoding="utf-8")
    with pytest.raises(cli_r12.Cli12Error) as exc:
        cli_r12._verify_public_view(root, envelope)
    assert exc.value.code == "PUBLIC_VIEW_DRIFT"


# ---------------------------------------------------------------------------
# 公开消费者 fixture 与宿主合同的逐项对齐（离线替身不得自造 schema）
# ---------------------------------------------------------------------------

def test_offline_consumer_parses_real_task_brief():
    """正式 brief 在 JSON 后有固定提示句：消费者必须只消费首个 JSON 对象。"""
    from orchestrator.rsi_r1 import loop as L
    from orchestrator.common import research_transport as T
    from orchestrator.rsi_r1 import offline_consumer as OC
    vis = L.make_visible_state(study="A", study_state="S0", artifacts={},
                               last_batch=None, budget_slots_remaining=12,
                               confirm_token=None, plan_doc=None)
    req = T.build_request(system="sys", task_brief=L.render_task_brief(vis),
                          turns=(), counter=lambda s: len(s) // 4)
    messages = json.loads(req.serialized)["messages"]
    doc = OC.decide(messages)
    assert doc["action"] == "write_artifact" and doc["kind"] == "design"


def test_offline_consumer_scene_ids_match_host():
    from orchestrator.rsi_r1 import offline_consumer as OC
    from orchestrator.rsi_r1 import scenes as SCENES
    assert (OC.SCENE_A, OC.SCENE_B) == (SCENES.SCENE_A, SCENES.SCENE_B)


def test_offline_consumer_toy_plan_passes_host_contract():
    from orchestrator.rsi_r1 import offline_consumer as OC
    for study in ("A", "B"):
        explore = C.ResearchPlan.from_dict(OC.toy_plan(study))
        assert explore.phase == "explore" and explore.revision == 1
        confirm = C.ResearchPlan.from_dict(
            OC.toy_plan(study, phase="confirm", revision=2, parent_sha="a" * 64,
                        design_sha="b" * 64, analysis_sha="c" * 64))
        assert confirm.phase == "confirm" and confirm.revision == 2


def test_fixture_templates_compile_and_over_cap_design_runs(tmp_path):
    """夹具模板必须是可编译的合法 Python，且超帽设计真能产出 65 格矩阵。

    反例（2026-11-12 实测）：模板尾部的 ``.replace("\\\\n", "\\n")`` 会把
    ``write_text(... + "\\n")`` 里的转义换成真换行，容器端直接
    ``SyntaxError: unterminated string literal``；而 LOCK 闭包当时不覆盖该文件，
    零漂移断言全绿也拦不住。这里用**执行**而非仅 compile 覆盖同一路径。
    """
    from orchestrator.rsi_r1 import offline_consumer as OC

    for src in (OC.DESIGN_SOURCE, OC.ANALYSIS_SOURCE, OS._OVER_CAP_DESIGN):
        compile(src, "<fixture-template>", "exec")
    view = tmp_path / "view"
    out = tmp_path / "out"
    view.mkdir(), out.mkdir()
    plan = OC.toy_plan("A")
    (view / f"plan-{OC.canonical_sha(plan)[:12]}.json").write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    script = tmp_path / "design.py"
    script.write_text(OS._OVER_CAP_DESIGN, encoding="utf-8")
    import subprocess
    import sys as _sys
    proc = subprocess.run([_sys.executable, "-I", str(script), str(view), str(out)],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    matrix = json.loads((out / "matrix.json").read_text(encoding="utf-8"))
    assert len(matrix["cells"]) == 65


def test_recovery_decider_reproposes_plan_after_design_rewrite():
    """超帽自修序列必须是：换设计 → 重报计划（revision+1）→ 再用新计划重跑。

    宿主 ``recovery.INVALIDATE_FROM`` 把「design 变字节」判为 plan 里程碑作废；
    只换设计不重报计划会让游标永不推进，最终撞 ``BUDGET_UNREACHABLE``
    （2026-11-12 实测 6 呼即停）。
    """
    from orchestrator.rsi_r1 import loop as L
    from orchestrator.rsi_r1 import offline_consumer as OC

    decide = OS._recovery_decider()
    vis = L.make_visible_state(study="A", study_state="EXPLORING",
                               artifacts={"design": ["d" * 64],
                                          "analysis": ["a" * 64]},
                               last_batch=None, budget_slots_remaining=6,
                               confirm_token=None,
                               plan_doc=OC.toy_plan("A", revision=1))
    brief = {"role": "user", "content": L.render_task_brief(vis)}
    rejected = {"role": "user",
                "content": json.dumps({"ok": False, "code": "RESOURCE_EXCEEDED"})}
    first = decide([brief, rejected])
    assert first["action"] == "write_artifact" and first["kind"] == "design"
    # 宿主回包：设计重写成功（真实历史里最后一格就是这条 ok 回包）
    accepted = {"role": "user",
                "content": json.dumps({"ok": True, "action": "write_artifact",
                                       "kind": "design"})}
    second = decide([brief, rejected, {"role": "assistant",
                                       "content": json.dumps(first)}, accepted])
    assert second["action"] == "propose_plan"
    assert int(second["plan"]["revision"]) == 2        # 必须 +1，否则 REVISION_GAP


def test_offline_consumer_uses_result_action_when_history_is_clipped():
    """历史原稿被宿主截断时，动作名必须从**回包**结构字段读（2026-11-12 实测）。

    ``Turn.clipped()`` 把历史 assistant 原文截到 4096 字节并追加
    ``[TRUNCATED_BY_HOST]``；第 7 呼 5.5 KB 的 analysis 原稿在第 8 呼请求里
    已不是合法 JSON。当时消费者靠解析原稿取 prev ⇒ 取空 ⇒ 把「分析已写完」
    误判成重跑 ``run_batch``，撞上重复回复 → ``STATE_UNKNOWN`` 整链处死。
    """
    from orchestrator.common import research_transport as T
    from orchestrator.rsi_r1 import loop as L
    from orchestrator.rsi_r1 import offline_consumer as OC

    vis = L.make_visible_state(
        study="A", study_state="EXPLORING",
        artifacts={"design": ["d" * 64], "analysis": ["a" * 64]},
        last_batch={"phase": "explore", "n_runs": 4, "run_ids": ["run-1"]},
        budget_slots_remaining=6, confirm_token=None,
        plan_doc=OC.toy_plan("A", revision=1))
    big = json.dumps({"action": "write_artifact", "kind": "analysis",
                      "source": "x" * 5500})
    turns = (T.Turn(model_text=big,
                    result={"ok": True, "action": "write_artifact",
                            "kind": "analysis",
                            "source_sha256": "a" * 64}),)
    req = T.build_request(system="sys", task_brief=L.render_task_brief(vis),
                          turns=turns, counter=lambda s: len(s) // 4)
    messages = json.loads(req.serialized)["messages"]
    assert messages[-2]["role"] == "assistant"
    assert "[TRUNCATED_BY_HOST]" in messages[-2]["content"]   # 原稿确实不可解析
    with pytest.raises(ValueError):
        json.loads(messages[-2]["content"])
    doc = OC.decide(messages)
    assert doc["action"] == "analyze"
    assert doc["source_sha256"] == "a" * 64
    assert doc["run_ids"] == ["run-1"]


def test_offline_transport_response_id_is_unique_per_call(tmp_path, monkeypatch):
    """同一回复字节出现在两个回合时，response_id 必须**逐呼唯一**。

    生产网关的 response_id 逐呼不同；离线运输若退化成「回复内容 sha」，复读
    回复会让 ``record_action`` 去重吞掉新动作事件，``_binding`` 随后对着上一个
    动作判 ``STATE_UNKNOWN``（2026-11-12 T5 实测第 8 呼）。
    """
    from orchestrator.common import research_transport as T

    root = _authorized_root(tmp_path, monkeypatch)
    log = OV.TurnLog(_ctx(root))
    doc = {"action": "run_batch", "plan_sha256": "p" * 64,
           "design_sha256": "d" * 64, "phase": "explore"}
    transport = OV.ScriptedTransport(decide=lambda messages: dict(doc), log=log)
    req = T.build_request(system="s", task_brief="b", turns=(),
                          counter=lambda s: len(s) // 4)
    first = transport(req)
    second = transport(req)
    assert first.text == second.text
    assert first.response_id != second.response_id
    import hashlib
    sha = hashlib.sha256(first.text.encode("utf-8")).hexdigest()
    assert first.response_id.endswith(sha[:12])     # 保留内容哈希供对账
    assert second.response_id.endswith(sha[:12])


def test_offline_response_id_separates_transports_with_identical_replies(
        tmp_path, monkeypatch):
    """不同 study/lane 的两个运输实例、首呼回复逐字相同 ⇒ 身份必须不同。

    2026-11-12 T5 全量演练实测：两个运输各从 ``0001`` 数起，A 与 B 的首呼请求
    不同、回复却是同一份 design 模板 ⇒ 旧身份 ``call-reply_sha`` 撞车 ⇒
    ``record_action`` 把 B 的动作当 A 的重复吞掉，``_binding`` 过滤 study 后
    判 ``STATE_UNKNOWN``。身份必须同时绑定请求与回复两侧字节。
    """
    from orchestrator.common import research_transport as T

    root = _authorized_root(tmp_path, monkeypatch)
    doc = {"action": "run_batch", "plan_sha256": "p" * 64,
           "design_sha256": "d" * 64, "phase": "explore"}
    brief_a = '### 当前可见状态\n{"study": "A"}\n请给出你的下一个动作。'
    brief_b = '### 当前可见状态\n{"study": "B"}\n请给出你的下一个动作。'
    ta = OV.ScriptedTransport(decide=lambda messages: dict(doc),
                              log=OV.TurnLog(_ctx(root)))
    tb = OV.ScriptedTransport(decide=lambda messages: dict(doc),
                              log=OV.TurnLog(_ctx(root)))
    req_a = T.build_request(system="s", task_brief=brief_a, turns=(),
                            counter=lambda s: len(s) // 4)
    req_b = T.build_request(system="s", task_brief=brief_b, turns=(),
                            counter=lambda s: len(s) // 4)
    ra, rb = ta(req_a), tb(req_b)
    assert ra.text == rb.text
    assert ra.response_id != rb.response_id


def test_offline_consumer_transport_runs_real_subprocess(tmp_path, monkeypatch):
    """真子进程跑一次公开消费者：回复可解析、usage 只带证据安全字段、逐呼留档。

    2026-11-12 T5 全量演练实测：``subprocess.run`` 返回 CompletedProcess，
    **没有** pid 属性，而 transport 拼了 ``{"pid": proc.pid}`` ⇒ AttributeError
    打断 suite1 practice 链。上面那条 response_id 用例走 ScriptedTransport，
    覆盖不到真运输，所以这里直接把真消费者拉起来固住形状。
    """
    from orchestrator.common import research_transport as T

    root = _authorized_root(tmp_path, monkeypatch)
    log = OV.TurnLog(_ctx(root))
    transport = OV.OfflineConsumerTransport(
        consumer=OV.consumer_source(REPO), log=log, timeout_s=60.0)
    brief = ("### 当前可见状态\n"
             + json.dumps({"study": "A", "artifacts": {}}, ensure_ascii=False)
             + "\n请给出你的下一个动作。")
    req = T.build_request(system="s", task_brief=brief, turns=(),
                          counter=lambda s: len(s) // 4)
    reply = transport(req)
    doc = json.loads(reply.text)
    assert doc["action"] == "write_artifact" and doc["kind"] == "design"
    assert dict(reply.usage) == {"offline": True}
    assert reply.response_id
    assert transport.calls == 1 and len(log.entries) == 1
    assert log.entries[0]["rc"] == 0 and log.entries[0]["note"] == "ok"
    assert log.entries[0]["cwd_is_temp"] is True


def test_receipt_producer_vocabulary_is_mapped_not_passed_through():
    """注册表说 ``candidate_model``，凭据 schema 只认 ``candidate``。

    2026-11-12 T5 离线演练首次走到 analyze 时实测：``_r12_analysis`` 把注册表
    原词直接塞给 ``build_receipt`` ⇒ ``PRODUCER_INVALID`` 整链处死。同一映射
    必须同时服务构造（build_receipt）与复核（validate_receipt）两处，否则另一处
    会把它误报成 ``PRODUCER_MISMATCH``。
    """
    from orchestrator.rsi_r1 import analysis_receipt as AR
    from orchestrator.rsi_r1 import assembly as AS
    from orchestrator.rsi_r1 import state as S

    assert AS.receipt_producer({"producer": S.PRODUCER_CANDIDATE}) \
        == AR.PRODUCER_CANDIDATE
    assert AS.receipt_producer({"producer": S.PRODUCER_MAINTAINER}) \
        == AR.PRODUCER_MAINTAINER
    assert AS.receipt_producer({"producer": "unknown_vendor"}) == ""
    assert AS.receipt_producer({}) == ""

    kwargs = dict(profile_id="rsi-r1.2", study_id="A", phase="explore",
                  plan_sha256="a" * 64, confirm_manifest_sha256=None,
                  source_sha256="b" * 64, registered_reply_id="r",
                  registered_action_id="c" * 64, inputs=(),
                  execution_id="exec", sandbox_status="ok", exit_code=0,
                  timed_out=False, output_bytes=b"{}", output_doc=None,
                  validity=True)
    receipt = AR.build_receipt(producer=AS.RECEIPT_PRODUCERS[
        S.PRODUCER_CANDIDATE], **kwargs)
    assert receipt["producer"] == AR.PRODUCER_CANDIDATE
    with pytest.raises(AR.ReceiptError):
        AR.build_receipt(producer=S.PRODUCER_CANDIDATE, **kwargs)


def test_analysis_estimator_matches_judge_path():
    """候选分析模板的估计量/区间/分类必须与宿主判决路径一致。

    宿主（``judge._judge_contrast`` → ``_bootstrap_ci``/``_classify``）从 raw 独立
    重算后按 1e-8+1e-6|ref| 逐字段比对候选自己的输出。若候选脚本按主张方向翻符号
    （2026-11-12 T5 前的实现就是这样），区间数值必然偏离宿主 → INVALID_NUMERICS。
    本回归把两条实现放到同一组配对差上逐位对照。
    """
    from orchestrator.rsi_r1 import judge as J
    from orchestrator.rsi_r1 import offline_consumer as OC

    ns: "dict" = {}
    exec(compile(OC.ANALYSIS_SOURCE, "<analysis_template>", "exec"), ns)
    diffs = [-0.0120 + 0.0007 * i for i in range(32)]
    effect, lower, upper = ns["interval"](diffs, 0.95)
    want_lower, want_upper = J._bootstrap_ci(diffs, level="0.95")
    assert lower == pytest.approx(want_lower, rel=1e-12, abs=1e-15)
    assert upper == pytest.approx(want_upper, rel=1e-12, abs=1e-15)
    assert effect == pytest.approx(sum(diffs) / len(diffs), rel=1e-12)
    # equivalent 的阴性控制用 90% 区间（不得拿 95% 当阴性）
    assert ns["interval"](diffs, 0.90)[1] == pytest.approx(
        J._bootstrap_ci(diffs, level="0.90")[0], rel=1e-12, abs=1e-15)
    for prop in ("decrease_gt", "increase_gt", "equivalent"):
        for lo, hi in ((-0.02, -0.005), (-0.001, 0.0009), (-0.0005, 0.002),
                       (0.004, 0.02), (-8e-4, 8e-4)):
            assert ns["classify"](prop, 0.001, lo, hi) \
                == J._classify(prop, 0.001, lo, hi), (prop, lo, hi)
    # B 的幅值真值在 meta["spec_envelope"]["run"]（宿主 spec），不在顶层；
    # 读错位置会让 RMSE 差比例因子。失稳 run 以量表 LOSS_CAP 入场，不剔除。
    assert ns["amplitude_of"](
        {"spec_envelope": {"run": {"target_amplitude_rad": 0.3}}}) == 0.3
    assert ns["amplitude_of"]({"target_amplitude_rad": "0.4"}) == 0.4
    loss = ns["metric_of"]({"time_s": [0.0, 0.1], "qpos": [[0.0], [0.0]],
                            "target_q": [[0.0], [0.0]],
                            "instability_events": [{"t": 0.1}]}, {})
    assert loss["value"] == float(10.0) and loss["loss"] is True


def test_analysis_template_runs_end_to_end_on_a_view(tmp_path):
    """整脚本冒烟：把模板当**真进程脚本**跑一遍，而不只是调它的函数。

    2026-11-12 实测事故：``main()`` 里残留旧变量名（``len(values)``，改成
    ``diffs`` 后漏改）⇒ 容器内 exit 1 ⇒ ``ANALYSIS_EXIT_NONZERO`` +
    ``ANALYSIS_OUTPUT_MISSING``，而函数级回归全绿。只测函数测不到这一类。
    这里按沙盒视图的真实形态摆文件（run 平铺 basename + plan-<sha12>.json +
    public_contract.json），跑 ``main()`` 并机判输出字段。
    """
    from orchestrator.rsi_r1 import offline_consumer as OC

    view = tmp_path / "view"
    out = tmp_path / "out"
    view.mkdir()
    out.mkdir()
    plan = OC.toy_plan("A")
    plan_sha = OC.canonical_sha(plan)
    (view / f"plan-{plan_sha[:12]}.json").write_text(
        json.dumps(plan, sort_keys=True), encoding="utf-8")
    (view / "public_contract.json").write_text("{}", encoding="utf-8")
    # 3 条件 × 2 block = 6 run：base/low_mu 差 −0.02（primary），base/sham 差 0
    # （negative 阴性控制）。位移按条件 id 显式给定，别用「非 sham 一律同值」的
    # 一眼写法——那会让两条对比都变成 0。
    displacement = {"base": -0.03, "low_mu": -0.01, "sham": -0.03}
    blocks = ("b01", "b02")
    for cond in plan["conditions"]:
        for block_id in blocks:
            rid = f"run-{cond['id']}-{block_id}"
            x0 = 0.5
            x1 = x0 + displacement[cond["id"]]
            rows = [{"run_id": rid, "block_id": block_id, "time_s": [0.0, 1.0],
                     "qpos": [[x0], [x1]]}]
            (view / f"{rid}.jsonl").write_text(
                "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            (view / f"{rid}.meta.json").write_text(
                json.dumps({"run_id": rid, "condition_id": cond["id"],
                            "initial_x_m": x0}), encoding="utf-8")

    ns = {"__name__": "__analysis__"}
    exec(compile(OC.ANALYSIS_SOURCE, "<analysis_template>", "exec"), ns)

    import sys as _sys
    argv = _sys.argv
    _sys.argv = ["analysis.py", str(view), str(out)]
    try:
        assert ns["main"]() == 0
    finally:
        _sys.argv = argv

    doc = json.loads((out / "analysis.json").read_text(encoding="utf-8"))
    assert doc["schema"] == "simpraxis.rsi_r1.analysis_output.v1"
    assert len(doc["run_ids"]) == 6
    by_id = {c["contrast_id"]: c for c in doc["contrasts"]}
    assert sorted(by_id) == ["negative", "primary"]
    for item in doc["contrasts"]:
        # n_blocks 必须真的按配对数报（残留名/剔除失稳都会让它失真）
        assert item["n_blocks"] == 2
        assert set(item) == {"contrast_id", "metric_id", "proposition",
                             "estimate", "lower", "upper", "status",
                             "n_blocks"}
    assert by_id["primary"]["proposition"] == "decrease_gt"
    assert by_id["primary"]["estimate"] == pytest.approx(-0.02)
    assert by_id["negative"]["estimate"] == pytest.approx(0.0)


def _read_envelope(root: Path) -> "dict":
    return json.loads((root / cli_r12.ENVELOPE_RELPATH).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# LOCK：引导期免签口径
# ---------------------------------------------------------------------------

def test_unsigned_bootstrap_only_allowed_for_validation_purpose():
    class _Ctx:
        def __init__(self, doc):
            self._doc = doc

        def exists(self, rel):
            return rel == LK.LOCK_RELPATH

        def read_json(self, rel):
            return dict(self._doc)

    paid = _Ctx({"schema": "simpraxis.rsi_r1.lock.v1", "purpose": "paid"})
    with pytest.raises(LK.LockError) as exc:
        LK.verify_lock(paid, require_signatures=False)
    assert exc.value.code == "SIGNATURES_SKIPPED"


def test_t5_does_not_lower_assembly_or_resource_gates(tmp_path, monkeypatch):
    """T5 不绕 Gate/资源检查——**行为**断言，不是 ``hasattr`` 存活断言。

    锁内适配（2026-11-12 T5 独立评审 MINOR-6）：旧版本只 ``hasattr`` 四个名字，
    任何 Gate 被清空都照样绿。这里逐门实打：
    ① 费用门：``zero_paid`` 账本出现 call 行 ⇒ ``SPEND_MODE_VIOLATION``；
    ② 准入 lane 门：未知 lane 构造即 ``LANE_UNKNOWN``；
    ③ 资源门：同一 study 预留 65 run ⇒ ``RESOURCE_EXCEEDED``（64 帽不放水）。
    """
    from orchestrator.rsi_r1 import assembly as A
    from orchestrator.rsi_r1 import resources as RS
    from orchestrator.rsi_r1 import state as S

    root = _authorized_root(tmp_path, monkeypatch)
    ctx = _ctx(root)
    budget = C.budget_from_dict(ctx.read_json("contract/budget.json"))
    zero = A.SpendContract(mode=A.MODE_OFFLINE_VALIDATION)
    envelope = A.Envelope(ctx.envelope_id, budget.sha256,
                          mode="offline_validation")
    zero.check(envelope=envelope, ledger_rows=[])          # 空账放行
    with pytest.raises(A.AssemblyError) as exc:
        zero.check(envelope=envelope, ledger_rows=[{"event": "call"}])
    assert exc.value.code == "SPEND_MODE_VIOLATION"
    with pytest.raises(A.AssemblyError) as exc:
        A.CampaignAdmission(ctx=ctx, state=S.ResearchState(ctx),
                            budget=budget, lock_sha256="ab" * 32,
                            spend=zero, lane="nope")
    assert exc.value.code == "LANE_UNKNOWN"
    ledger = RS.ResourceLedger(tmp_path / "gates-resources",
                               profile=P.PROFILE_R12_DEF)
    import types
    specs = [types.SimpleNamespace(nsteps=100) for _ in range(65)]
    with pytest.raises(RS.ResourceError) as exc:
        ledger.reserve_explore("A", specs, batch_id="gates-65")
    assert exc.value.code == "RESOURCE_EXCEEDED"


# ---------------------------------------------------------------------------
# CLI：validate-production 前置
# ---------------------------------------------------------------------------

def test_validate_production_refuses_paid_env(tmp_path, monkeypatch):
    root = _authorized_root(tmp_path, monkeypatch)
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    assert cli_r12.main(["validate-production", "--root", str(root),
                         "--envelope", ENVELOPE]) == 2


def test_validate_production_requires_frozen_root(tmp_path, monkeypatch):
    root = _authorized_root(tmp_path, monkeypatch)
    monkeypatch.delenv("SIMPRAXIS_ALLOW_PAID", raising=False)
    assert cli_r12.main(["validate-production", "--root", str(root),
                         "--envelope", ENVELOPE]) == 2


# ---------------------------------------------------------------------------
# 真容器车道：正式 DockerRunner 起钉死镜像（零网络 argv 烟测）
# ---------------------------------------------------------------------------

@pytest.mark.docker_integration
def test_docker_runner_pinned_image_smoke():
    require_docker_integration()
    image = CAL.CAL_IMAGE
    name = f"simpraxis-t5-offline-{uuid.uuid4().hex[:12]}"
    runner = SANDBOX.DockerRunner()
    try:
        result = runner.run(
            ["docker", "run", "--rm", "--name", name, "--network", "none",
             image, "python", "-c", "print('offline-ok')"],
            timeout_s=180, container=name)
        assert result.exit_code == 0, (result.stderr_tail, result.stdout_tail)
        assert "offline-ok" in result.stdout_tail
        assert runner.residue(name) is False
    finally:
        runner.force_remove(name)


# ---------------------------------------------------------------------------
# 零 POST 账本面 + 结案件帽绑定（T5 演练暴露的两处口径）
# ---------------------------------------------------------------------------

def test_offline_init_creates_zero_row_ledger(tmp_path, monkeypatch):
    """离线根必须有**空**账本：0 行是事实（零付费调用）。

    drill16：账本文件缺席被结案/复算器读成「缺账本」，离线验证的零 POST
    定义性质反而被判成账务缺口。init 于 offline_validation 模式建 0600 空件。
    """
    root = _init_root(tmp_path, monkeypatch)
    ledger = root / "quota" / "ledger.jsonl"
    assert ledger.is_file()
    assert ledger.read_bytes() == b""
    assert (ledger.stat().st_mode & 0o777) == 0o600


def test_closeout_readout_binds_r12_caps_not_legacy_table(tmp_path,
                                                          monkeypatch):
    """结案件登记的帽取 envelope 声明（12/16/48/48、总 124），不是 R1 字面量表。

    drill16：``check_rsi_r1_closure.py`` 的 R1 表（probe 8、总 144）对 R1.2 根
    恒报 ``B-DECLARED-CAPS``；结案件本身也必须不自称错误的帽。R1.2 的新版
    独立 checker 归 T9。
    """
    from orchestrator.rsi_r1 import closeout as CLOSEOUT

    root = _init_root(tmp_path, monkeypatch)
    ctx = _ctx(root)
    profile = P.profile_by_id(P.PROFILE_R12)
    readout = CLOSEOUT.build(
        ctx, image_pin=None,
        container_facts={"probe": "injected", "available": False, "own": None})
    budget = readout["budget"]
    assert int(budget["hard_cap"]) == profile.hard_cap == 124
    assert dict(budget["lane_caps"]) == {
        lane: int(profile.lane_post_caps[lane]) for lane in profile.lane_order}
    assert dict(budget["lane_caps"]) == {"probe": 12, "practice": 16,
                                         "study_A": 48, "study_B": 48}
    # 零行账本存在 ⇒ 不因「缺账本」记 process 原因（本次演练流程未跑协议，
    # 缺协议报告仍是**真**原因，照常登记——不放水）
    reasons = list(readout.get("process_reasons") or [])
    assert not any("账本" in r for r in reasons), reasons


# ---------------------------------------------------------------------------
# 独立评审修复批（2026-11-12 T5 MAJOR-1..5）的回归
# ---------------------------------------------------------------------------

def test_rejected_batch_gate_reads_disk_and_side_effects(tmp_path,
                                                        monkeypatch):
    """MAJOR-1：超帽被拒批次必须零物理 run（旧实现查错路径，恒真空通过）。"""
    root = _init_root(tmp_path, monkeypatch)
    ctx = _ctx(root)
    obs = root / PT.attempt_prefix("probe", 1) / "observations"
    (obs / "explore-A-0").mkdir(parents=True)
    (obs / "explore-A-0" / "run-r1.jsonl").write_text("{}\n", encoding="utf-8")
    # 确认批文件不在比较面（run_confirm 只回前 8 个 head id，无法逐字对账）
    (obs / "confirm-A-abc").mkdir()
    (obs / "confirm-A-abc" / "run-c1.jsonl").write_text("{}\n",
                                                        encoding="utf-8")
    rejected = {"action": "run_batch", "ok": False,
                "result": {"code": "RESOURCE_EXCEEDED",
                           "side_effect_status": "NONE"}}
    accepted = {"action": "run_batch", "ok": True,
                "result": {"run_ids": ["run-r1"]}}
    actions = [rejected, accepted]
    assert OS._rejected_batch_left_no_runs(ctx, "probe", actions) is True
    # 被拒设计跑过 ⇒ 另建探索批次目录 ⇒ 盘上多出 run ⇒ 不得 PASS
    (obs / "explore-A-1").mkdir()
    (obs / "explore-A-1" / "run-r2.jsonl").write_text("{}\n", encoding="utf-8")
    assert OS._rejected_batch_left_no_runs(ctx, "probe", actions) is False
    (obs / "explore-A-1" / "run-r2.jsonl").unlink()
    (obs / "explore-A-1").rmdir()
    # 被拒动作报告了副作用（宿主面事实）⇒ 不得 PASS
    tainted = dict(rejected, result={"code": "RESOURCE_EXCEEDED",
                                     "side_effect_status": "RUNS_STARTED"})
    assert OS._rejected_batch_left_no_runs(ctx, "probe",
                                           [tainted, accepted]) is False
    # 没有 RESOURCE_EXCEEDED / 没有唯一成功批 ⇒ 证据不成立，不得 PASS
    assert OS._rejected_batch_left_no_runs(ctx, "probe", [accepted]) is False
    assert OS._rejected_batch_left_no_runs(
        ctx, "probe", [rejected, accepted, accepted]) is False


def test_validation_verdict_subset_is_incomplete_not_pass():
    """MAJOR-2：``--only`` 子集不得产出可签署 PASS。"""
    six = {name: {"status": "PASS"} for name in OS.SUITE_ORDER}
    full = OS.validation_verdict(six)
    assert full["verdict"] == "PASS" and full["complete"] is True
    subset = {name: six[name] for name in OS.SUITE_ORDER[:2]}
    part = OS.validation_verdict(subset)
    assert part["verdict"] == "INCOMPLETE"
    assert part["complete"] is False
    assert part["missing_suites"] == list(OS.SUITE_ORDER[2:])
    broken = dict(six)
    broken[OS.SUITE_ORDER[0]] = {"status": "FAIL"}
    assert OS.validation_verdict(broken)["verdict"] == "FAIL"


def test_r12_closure_groups_cover_instrument_and_keep_legacy_bytes():
    """MAJOR-5：R1.2 闭包覆盖判据仪器与入口；旧 R1 闭包字节口径不变。"""
    # freeze 的组表口径取画像单一真值；R1.2 必须是 r12、legacy 画像仍是 legacy
    assert P.profile_by_id(P.PROFILE_R12).closure_groups_key == "r12"
    assert P.profile_by_id(P.PROFILE_LEGACY).closure_groups_key == "legacy"
    pin = "reg.invalid/simpraxis@sha256:" + "0" * 64
    legacy = LK.build_closure(REPO, image_pin=pin, groups_key="legacy")
    assert "groups_key" not in legacy
    assert set(legacy["groups"]) == set(LK.CLOSURE_GROUPS)
    LK.verify_closure(legacy, REPO)          # 旧口径仍可复核
    with pytest.raises(LK.LockError) as exc:
        LK.verify_closure(legacy, REPO, groups_key="r12")
    # 旧组表当作 r12 复核必然在组集上暴露（不可能是静默通过）
    assert exc.value.code in ("CLOSURE_GROUPS", "CLOSURE_GROUPS_KEY")
    r12 = LK.build_closure(REPO, image_pin=pin, groups_key="r12")
    assert r12["groups_key"] == "r12"
    assert set(r12["groups"]) == set(LK.CLOSURE_GROUPS_R12)
    covered = {f["path"] for grp in r12["groups"].values()
               for f in grp["files"]}
    for rel in ("orchestrator/rsi_r1/offline_suite.py",
                "orchestrator/rsi_r1/offline_validation.py",
                "orchestrator/rsi_r1/cli_r12.py",
                "orchestrator/rsi_r1/profiles.py",
                "orchestrator/rsi_r1/closeout.py",
                "orchestrator/rsi_r1/resources.py"):
        assert rel in covered, rel
    LK.verify_closure(r12, REPO)             # groups_key 由闭包自述
    with pytest.raises(LK.LockError) as exc:
        LK.closure_groups("nope")
    assert exc.value.code == "CLOSURE_GROUPS_KEY"


class _DiskCtx:
    """最小 ctx：``_signature_bindings`` 只用到 relative/exists/read_json。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.envelope_id = "env-test"

    def relative(self, rel):
        return self.root / str(rel)

    def exists(self, rel):
        return (self.root / str(rel)).is_file()

    def read_json(self, rel):
        return json.loads((self.root / str(rel)).read_text(encoding="utf-8"))


_LOCK_SHA = "ab" * 32
_CLOSURE_SHA = "0" * 64


def _matrix_observed() -> "dict[str, dict]":
    """按 ``FAILURE_MATRIX`` 逐行合成 observed（回归行除外）。

    独立评审后签前面会**重算**矩阵并与报告逐行比对，所以夹具也必须给出一份
    自洽的 observed 结构；这里直接从矩阵登记表派生，避免手抄第二份期望。
    """
    out: "dict[str, dict]" = {}
    for _name, spec in OS.FAILURE_MATRIX:
        if spec[0] == "regression":
            continue
        suite, path, want = spec
        node = out.setdefault(suite, {"observed": {}})["observed"]
        parts = path.split(".")
        cur = node
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = want
    return out


def _regression_evidence() -> "dict":
    """R4-M1：取证件必须带**具名用例**（``cases``）——行列由具名用例证明。"""
    files: "dict[str, dict]" = {}
    for _name, spec in OS.FAILURE_MATRIX:
        if spec[0] != "regression":
            continue
        rel, required = spec[1], spec[3]
        rec = files.setdefault(rel, {"passed": 0, "failed": 0, "skipped": 0,
                                     "cases": {}})
        for node in required:
            rec["cases"][node] = "passed"
        rec["passed"] = len(rec["cases"])
    return {"schema": OV.REGRESSION_SCHEMA, "schema_version": 1,
            "closure_sha256": _CLOSURE_SHA, "returncode": 0,
            "test_files": OV.regression_tests_sha256(),
            "files": files}


def _validation_lock_files(tmp_path: Path, *, doc_over: "dict | None" = None,
                           **report_over) -> "tuple":
    root = tmp_path / "ctx"
    (root / "contract").mkdir(parents=True)
    frozen_tests = OV.regression_tests_sha256()
    auth = {"schema": "simpraxis.rsi_r1.offline_authorization.v1",
            "schema_version": 1, "envelope_id": "env-test"}
    doc = {"schema": "simpraxis.rsi_r1.lock.v1", "schema_version": 1,
           "purpose": "validation",
           "dependencies": [], "stage": "study", "phase": "study",
           "closure": {"closure_sha256": _CLOSURE_SHA},
           "closure_sha256": _CLOSURE_SHA,
           "regression_tests": dict(frozen_tests),
           "image": {"pin": "reg.invalid/simpraxis@sha256:" + "0" * 64},
           "validation": {"profile_sha256": "1" * 64,
                          "consumer_sha256": "2" * 64,
                          "report_relpath": LK.VALIDATION_REPORT_RELPATH}}
    if doc_over:
        doc.update(doc_over)
    (root / LK.LOCK_RELPATH).write_text(
        json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    (root / LK.LOCK_SHA_RELPATH).write_text(_LOCK_SHA + "\n", encoding="utf-8")
    (root / LK.VALIDATION_AUTH_RELPATH).write_text(
        json.dumps(auth, sort_keys=True) + "\n", encoding="utf-8")
    # MINOR-7：注册件进盘，报告须绑定其规范化字节（夹具/预期随 spec 走）。
    spec = {"schema": "simpraxis.rsi_r1.offline_suite_spec.v1", "version": 1,
            "envelope_id": "env-test", "suite_order": list(OS.SUITE_ORDER)}
    (root / OV.SPEC_RELPATH).write_text(
        json.dumps(spec, sort_keys=True) + "\n", encoding="utf-8")
    evidence = _regression_evidence()
    (root / OV.REGRESSION_EVIDENCE_RELPATH).parent.mkdir(
        parents=True, exist_ok=True, mode=0o700)
    (root / OV.REGRESSION_EVIDENCE_RELPATH).write_text(
        json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8")
    # R3-m3：报告 turns 要与盘上 append-only 回合日志逐条对账——夹具写真日志。
    (root / OV.TURN_LOG_RELPATH).write_text(
        json.dumps({"schema": OV.TURN_SCHEMA, "schema_version": 1, "seq": 1,
                    "request_sha256": "3" * 64, "reply_sha256": "4" * 64},
                   sort_keys=True) + "\n", encoding="utf-8")
    observed = _matrix_observed()
    suites = {n: {"status": "PASS", "observed": observed.get(n, {}).get("observed", {})}
              for n in OS.SUITE_ORDER}
    matrix = OS.failure_matrix(suites, regression_evidence=evidence,
                               closure_sha256=_CLOSURE_SHA,
                               regression_tests=frozen_tests)
    assert matrix["complete"] is True, matrix["missing"]
    report = {"schema": OV.REPORT_SCHEMA,
              "envelope_id": "env-test",
              "authorization_sha256": C.canonical_sha256(auth),
              "verdict": "PASS", "complete": True,
              "suites_required": list(OS.SUITE_ORDER),
              "suites_run": list(OS.SUITE_ORDER),
              "suites": suites,
              "profile_sha256": "1" * 64,
              "image": {"pin": doc["image"]["pin"]},
              "validation_lock_sha256": _LOCK_SHA,
              "ledger_call_rows": 0,
              "failure_matrix": matrix,
              "turns": {"n": 1, "request_sha256": ["3" * 64],
                        "reply_sha256": ["4" * 64]},
              "suite_spec_sha256": C.canonical_sha256(spec)}
    report.update(report_over)
    (root / LK.VALIDATION_REPORT_RELPATH).write_text(
        json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    return doc, _DiskCtx(root)


def _FULL_MATRIX() -> "dict":
    observed = _matrix_observed()
    suites = {n: {"status": "PASS",
                  "observed": observed.get(n, {}).get("observed", {})}
              for n in OS.SUITE_ORDER}
    return OS.failure_matrix(suites, regression_evidence=_regression_evidence(),
                             closure_sha256=_CLOSURE_SHA,
                             regression_tests=OV.regression_tests_sha256())


def _FORGED_MATRIX_ROWS() -> "list[dict]":
    """全 ok，但把最后一行的登记面改写（报告越权自述覆盖来源）。"""
    rows = [dict(r) for r in _FULL_MATRIX()["rows"]]
    rows[-1] = {**rows[-1], "covered_by": "forged/by-hand"}
    return rows


def test_signature_bindings_accept_complete_pass_report(tmp_path):
    """MAJOR-2/3/4 正向面：全套 PASS 报告可绑定。"""
    doc, ctx = _validation_lock_files(tmp_path)
    bind = LK._signature_bindings(ctx, doc)
    assert bind["report_sha256"]
    assert bind["implementation_sha256"]
    assert bind["profile_sha256"] == "1" * 64


@pytest.mark.parametrize("over,code", [
    ({"verdict": "INCOMPLETE"}, "VALIDATION_REPORT_INCOMPLETE"),
    ({"complete": False}, "VALIDATION_REPORT_INCOMPLETE"),
    ({"suites_run": list(OS.SUITE_ORDER[:5])}, "VALIDATION_REPORT_INCOMPLETE"),
    ({"suites_required": list(OS.SUITE_ORDER[:5])}, "VALIDATION_SUITE_SPEC"),
    ({"suites": {**{n: {"status": "PASS"} for n in OS.SUITE_ORDER},
                 OS.SUITE_ORDER[0]: {"status": "FAIL"}}},
     "VALIDATION_REPORT_INCOMPLETE"),
    ({"profile_sha256": "3" * 64}, "VALIDATION_PROFILE_MISMATCH"),
    ({"image": {"pin": "other@sha256:" + "4" * 64}},
     "VALIDATION_IMAGE_MISMATCH"),
    ({"validation_lock_sha256": "5" * 64}, "VALIDATION_LOCK_MISMATCH"),
    # MINOR-3：零 POST 必须实测自证——缺键/非零一律拒签。
    ({"ledger_call_rows": 1}, "VALIDATION_REPORT_INCOMPLETE"),
    ({"ledger_call_rows": None}, "VALIDATION_REPORT_INCOMPLETE"),
    # MAJOR-7：固定失败矩阵不完整（缺行/声明缺件）一律拒签。
    ({"failure_matrix": {}}, "VALIDATION_REPORT_INCOMPLETE"),
    ({"failure_matrix": {"complete": False, "missing": ["suite3.cumulative_65"]}},
     "VALIDATION_REPORT_INCOMPLETE"),
    # MINOR-7：报告未绑定当前注册件字节（或注册件被替换）一律拒签。
    ({"suite_spec_sha256": "9" * 64}, "VALIDATION_SUITE_SPEC"),
    # 独立评审新增：schema / 逐回合证据 / 矩阵重算三面必须自证。
    ({"schema": "simpraxis.other.v1"}, "VALIDATION_REPORT_SCHEMA"),
    ({"turns": {"n": 0, "request_sha256": [], "reply_sha256": []}},
     "VALIDATION_REPORT_INCOMPLETE"),
    ({"turns": {"n": None}}, "VALIDATION_REPORT_INCOMPLETE"),
    # 报告声明的矩阵行必须与签前重算逐行相符（删行/改写登记面即漂移）。
    ({"failure_matrix": {**_FULL_MATRIX(), "rows": []}},
     "VALIDATION_MATRIX_DRIFT"),
    ({"failure_matrix": {**_FULL_MATRIX(),
                         "rows": _FORGED_MATRIX_ROWS()}},
     "VALIDATION_MATRIX_DRIFT"),
    # R4-r4：报告自述面（envelope / 授权件 sha）签前必须与现场对账。
    ({"envelope_id": "env-other"}, "VALIDATION_REPORT_ENVELOPE"),
    ({"authorization_sha256": "e" * 64}, "VALIDATION_AUTHORIZATION_MISMATCH"),
])
def test_signature_bindings_refuse_unproven_reports(tmp_path, over, code):
    """MAJOR-2/3/4 反向面：未跑全/画像漂移/镜像漂移/非本 LOCK 一律拒签。"""
    doc, ctx = _validation_lock_files(tmp_path, **over)
    with pytest.raises(LK.LockError) as exc:
        LK._signature_bindings(ctx, doc)
    assert exc.value.code == code


#: R4-M1 **独立硬编码**：场景 → (文件, 具名用例)。故意不从 ``FAILURE_MATRIX``
#: 派生——派生会让「表写错」和「测试也写错」同时错成同一件事，等于没有断言。
_REGRESSION_GOLDEN = {
    "non_gateway_error": ("tests/test_rsi_r12_attempts.py",
                          ("test_every_attempt_leaves_exactly_one_report",)),
    "worker_sigterm_sigkill": (
        "tests/test_rsi_r12_lifecycle.py",
        ("test_sigterm_writes_terminal_report_and_teardown",
         "test_death_without_handshake_gets_watcher_crash_note")),
    "republish_trusted_result": (
        "tests/test_rsi_r12_recovery.py",
        ("test_known_complete_with_open_intent_is_republish_only",)),
}


def test_regression_rows_point_at_real_cases():
    """R4-M1：矩阵三行必须落在**真实存在的具名用例**上（不是「文件里有一批绿」）。"""
    rows = {name: spec for name, spec in OS.FAILURE_MATRIX
            if spec[0] == "regression"}
    assert set(rows) == set(_REGRESSION_GOLDEN)
    for name, (rel, cases) in _REGRESSION_GOLDEN.items():
        spec = rows[name]
        assert (spec[1], tuple(spec[3])) == (rel, cases), name
        assert rel in OV.REGRESSION_TESTS, name
        src = (REPO / rel).read_text(encoding="utf-8")
        for case in cases:
            assert f"def {case}(" in src, (rel, case)
    # 独立评审实报的错配不得回潮：信号终态行不许再挂在 attempts 文件上。
    assert rows["worker_sigterm_sigkill"][1] != "tests/test_rsi_r12_attempts.py"
    assert set(OV.REGRESSION_TESTS) == {rel for rel, _ in
                                        _REGRESSION_GOLDEN.values()}


def test_regression_row_requires_the_named_case_not_just_a_green_file():
    """R4-M1 反向：同文件里别的用例全绿**不足以**证明这一行（旧实现正因此真空通过）。"""
    frozen = OV.regression_tests_sha256()
    rel, cases = _REGRESSION_GOLDEN["worker_sigterm_sigkill"]
    evidence = {"schema": OV.REGRESSION_SCHEMA, "closure_sha256": _CLOSURE_SHA,
                "returncode": 0, "test_files": dict(frozen),
                "files": {rel: {"passed": 10, "failed": 0, "skipped": 0,
                                "cases": {"test_unrelated": "passed"}}}}
    ok, detail = OS._regression_row_ok(evidence, rel, _CLOSURE_SHA, frozen,
                                       cases)
    assert ok is False and detail["missing_cases"] == list(cases)
    # 具名用例真跑过（参数化实例也算）⇒ 通过
    evidence["files"][rel]["cases"] = {cases[0]: "passed",
                                       f"{cases[1]}[case]": "passed"}
    ok, detail = OS._regression_row_ok(evidence, rel, _CLOSURE_SHA, frozen,
                                       cases)
    assert ok is True, detail
    # 其中一个具名用例失败 ⇒ 仍拒
    evidence["files"][rel]["cases"][cases[0]] = "failed"
    ok, _ = OS._regression_row_ok(evidence, rel, _CLOSURE_SHA, frozen, cases)
    assert ok is False
    # 取证件没有用例名面 ⇒ fail-closed
    evidence["files"][rel].pop("cases")
    ok, detail = OS._regression_row_ok(evidence, rel, _CLOSURE_SHA, frozen,
                                       cases)
    assert ok is False and detail == "<no-case-names>"


def test_regression_row_requires_no_skipped_cases():
    """LOW-2：具名用例「跳过」不算跑过——整文件 skipped 必须为 0。"""
    frozen = OV.regression_tests_sha256()
    rel, cases = _REGRESSION_GOLDEN["worker_sigterm_sigkill"]
    evidence = {"schema": OV.REGRESSION_SCHEMA, "closure_sha256": _CLOSURE_SHA,
                "returncode": 0, "test_files": dict(frozen),
                "files": {rel: {"passed": 10, "failed": 0, "skipped": 0,
                                "cases": {c: "passed" for c in cases}}}}
    ok, _ = OS._regression_row_ok(evidence, rel, _CLOSURE_SHA, frozen, cases)
    assert ok is True
    evidence["files"][rel]["skipped"] = 1
    ok, detail = OS._regression_row_ok(evidence, rel, _CLOSURE_SHA, frozen,
                                       cases)
    assert ok is False and detail == "skipped=1"
    # 缺 skipped 键同样不许当 0（fail-closed）
    evidence["files"][rel].pop("skipped")
    ok, detail = OS._regression_row_ok(evidence, rel, _CLOSURE_SHA, frozen,
                                       cases)
    assert ok is False and detail == "skipped=None"


def test_covered_by_regressions_derives_from_failure_matrix():
    """LOW-1：叙述字段只许一处真值——从 FAILURE_MATRIX 派生，禁手写副本。"""
    rows = OS.regression_rows()
    assert rows == {"non_gateway_error": "tests/test_rsi_r12_attempts.py",
                    "worker_sigterm_sigkill": "tests/test_rsi_r12_lifecycle.py",
                    "republish_trusted_result": "tests/test_rsi_r12_recovery.py"}
    # 与矩阵逐行一致（改矩阵即改叙述，不会出现第二套归属）
    assert rows == {name: spec[1] for name, spec in OS.FAILURE_MATRIX
                    if spec[0] == "regression"}


def test_signature_bindings_wrap_unreadable_regression_evidence(tmp_path):
    """LOW-4：取证件坏 JSON 须落 LOCK 域错误码（而非串出 ValueError）。"""
    doc, ctx = _validation_lock_files(tmp_path)
    path = tmp_path / "ctx" / OV.REGRESSION_EVIDENCE_RELPATH
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(LK.LockError) as exc:
        LK._signature_bindings(ctx, doc)
    assert exc.value.code == "VALIDATION_REGRESSION_MISSING"


def test_parse_junit_keeps_case_names(tmp_path):
    """R4-M1：junit 解析必须保留用例名（否则上面那条无从判定）。"""
    xml = tmp_path / "junit.xml"
    xml.write_text(
        '<testsuites><testsuite name="p">'
        '<testcase classname="tests.test_x" name="test_alpha" time="0"/>'
        '<testcase classname="tests.test_x" name="test_beta[c1]" time="0">'
        '<failure message="boom"/></testcase>'
        '<testcase classname="tests.test_x" name="test_gamma" time="0">'
        '<skipped/></testcase></testsuite></testsuites>', encoding="utf-8")
    rec = OV._parse_junit(xml)["tests/test_x.py"]
    assert rec["passed"] == 1 and rec["failed"] == 1 and rec["skipped"] == 1
    assert rec["cases"] == {"test_alpha": "passed", "test_beta[c1]": "failed",
                            "test_gamma": "skipped"}


def test_signature_bindings_refuse_unfrozen_regression_tests(tmp_path):
    """R4-r4：LOCK 未登记冻结测试字节 ⇒ ``VALIDATION_REGRESSION_UNFROZEN``。"""
    doc, ctx = _validation_lock_files(tmp_path,
                                      doc_over={"regression_tests": None})
    with pytest.raises(LK.LockError) as exc:
        LK._signature_bindings(ctx, doc)
    assert exc.value.code == "VALIDATION_REGRESSION_UNFROZEN"


def test_signature_bindings_refuse_missing_regression_evidence(tmp_path):
    """R4-r4：取证件缺席 ⇒ ``VALIDATION_REGRESSION_MISSING``（不许当无事）。"""
    doc, ctx = _validation_lock_files(tmp_path)
    (tmp_path / "ctx" / OV.REGRESSION_EVIDENCE_RELPATH).unlink()
    with pytest.raises(LK.LockError) as exc:
        LK._signature_bindings(ctx, doc)
    assert exc.value.code == "VALIDATION_REGRESSION_MISSING"


def test_signature_bindings_refuse_regression_tests_drift(tmp_path):
    """R4-r4：取证件自述的测试字节 ≠ LOCK 冻结值 ⇒ ``TESTS_DRIFT``。"""
    doc, ctx = _validation_lock_files(tmp_path)
    path = tmp_path / "ctx" / OV.REGRESSION_EVIDENCE_RELPATH
    ev = json.loads(path.read_text(encoding="utf-8"))
    ev["test_files"] = {rel: "f" * 64 for rel in ev["test_files"]}
    path.write_text(json.dumps(ev, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(LK.LockError) as exc:
        LK._signature_bindings(ctx, doc)
    assert exc.value.code == "VALIDATION_REGRESSION_TESTS_DRIFT"


def test_signature_bindings_wrap_missing_regression_file_as_lock_error(
        tmp_path, monkeypatch):
    """R4-r6：现场测试文件缺失须落 LOCK 域错误码（而非串出 OfflineValidationError）。"""
    doc, ctx = _validation_lock_files(tmp_path)
    monkeypatch.setattr(OV, "REGRESSION_TESTS",
                        OV.REGRESSION_TESTS + ("tests/does_not_exist.py",))
    with pytest.raises(LK.LockError) as exc:
        LK._signature_bindings(ctx, doc)
    assert exc.value.code == "VALIDATION_REGRESSION_MISSING"


def test_signature_bindings_refuse_turn_log_drift(tmp_path):
    """R4-r4：报告 turns 与盘上回合日志不符 ⇒ ``VALIDATION_TURNS_DRIFT``。"""
    doc, ctx = _validation_lock_files(tmp_path)
    (tmp_path / "ctx" / OV.TURN_LOG_RELPATH).write_text("", encoding="utf-8")
    with pytest.raises(LK.LockError) as exc:
        LK._signature_bindings(ctx, doc)
    assert exc.value.code == "VALIDATION_TURNS_DRIFT"


def test_canary_disclosure_is_serializable_and_hides_raw():
    """R5-C1：observed 里的 canary 披露面必须可 JSON 化且不回显原文。"""
    info = {"canaries": (b"run-0123456789abcdef", b"run-fedcba9876543210"),
            "available": True, "n": 2, "manifest": "confirm/manifest_A.json"}
    doc = OS._canary_disclosure(info)
    blob = json.dumps(doc, sort_keys=True)
    assert "run-0123456789abcdef" not in blob
    assert doc["available"] is True and doc["n"] == 2
    assert len(doc["canary_sha256_prefix"]) == 2
    # 缺席方向如实说「不可用」，不伪装成已检查
    empty = OS._canary_disclosure({"canaries": (), "available": False, "n": 0,
                                   "manifest": "confirm/manifest_B.json"})
    assert empty["available"] is False and empty["canary_sha256_prefix"] == []


def test_suite_record_refuses_non_json_observed(tmp_path, monkeypatch):
    """R5-C1：observed 不可序列化必须在**套内**当场失败，而不是四十后写报告才崩。"""
    root = _authorized_root(tmp_path, monkeypatch)
    env = _env(root)
    with pytest.raises(OS.OfflineSuiteError) as exc:
        env.record("suite1_single_chains", expected="PASS",
                   observed={"runid_canary": (b"run-x",)}, status="PASS")
    assert exc.value.code == "SUITE_OBSERVED_NOT_SERIALIZABLE"


def test_assert_authorized_refuses_foreign_ledger_path(tmp_path, monkeypatch):
    """R4-r4/R3-m2：调用方传入别的账本路径不构成零 POST 证据。"""
    root = _authorized_root(tmp_path, monkeypatch)
    ctx = _ctx(root)
    other = root / "quota" / "other.jsonl"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("", encoding="utf-8")
    with pytest.raises(OV.OfflineValidationError) as exc:
        OV.assert_authorized(ctx, repo_root=REPO, ledger_path=other)
    assert exc.value.code == "LEDGER_PATH_MISMATCH"


def test_freeze_and_verify_validation_lock_uses_r12_closure(tmp_path,
                                                            monkeypatch):
    """冻结链集成：验证 LOCK 落 r12 组表，引导期免签仍全量复核闭包/依赖/校准。"""
    root = _authorized_root(tmp_path, monkeypatch)
    ctx = _ctx(root)
    pin = "reg.invalid/simpraxis@sha256:" + "7" * 64
    ctx.write_json("contract/calibration_report_v2.json", {
        "schema": "simpraxis.rsi_r12.calibration_report.v2", "schema_version": 2,
        "envelope_id": ENVELOPE, "verdict": "CALIBRATION_PASS",
        "design_version": "v2", "container_image": pin})
    ctx.write_json(LK.INSTRUMENT_PREREG, {
        "schema": "simpraxis.rsi_r1.instrument_prereg.v1", "schema_version": 1})
    frozen = LK.freeze_lock(ctx, repo_root=REPO)
    doc = frozen["doc"]
    assert doc["purpose"] == "validation"
    assert doc["closure"]["groups_key"] == "r12"
    assert set(doc["closure"]["groups"]) == set(LK.CLOSURE_GROUPS_R12)
    assert doc["image"]["pin"] == pin
    # 报告未生成 ⇒ 签名必然缺；引导期口径不要求签名，但闭包/依赖/校准照旧全量
    out = LK.verify_lock(ctx, repo_root=REPO, require_signatures=False)
    assert out["signatures_required"] is False
    assert out["purpose"] == "validation"
    assert out["lock_sha256"] == frozen["sha256"]
    # 任何代码改动都会让闭包复核失败（这里用临时篡改闭包对应的一项证明非真空）
    bad = dict(doc["closure"])
    bad["groups"] = dict(bad["groups"])
    first = sorted(bad["groups"])[0]
    bad["groups"][first] = dict(bad["groups"][first])
    bad["groups"][first]["files"] = list(bad["groups"][first]["files"])[:-1]
    with pytest.raises(LK.LockError) as exc:
        LK.verify_closure(bad, REPO)
    assert exc.value.code == "CLOSURE_FILES"


def _freeze_validation_root(tmp_path, monkeypatch):
    """建一个可冻结的验证根，返回 (ctx, frozen)。"""
    root = _authorized_root(tmp_path, monkeypatch)
    ctx = _ctx(root)
    pin = "reg.invalid/simpraxis@sha256:" + "7" * 64
    ctx.write_json("contract/calibration_report_v2.json", {
        "schema": "simpraxis.rsi_r12.calibration_report.v2", "schema_version": 2,
        "envelope_id": ENVELOPE, "verdict": "CALIBRATION_PASS",
        "design_version": "v2", "container_image": pin})
    ctx.write_json(LK.INSTRUMENT_PREREG, {
        "schema": "simpraxis.rsi_r1.instrument_prereg.v1", "schema_version": 1})
    return ctx, LK.freeze_lock(ctx, repo_root=REPO)


def test_verify_lock_recomputes_budget_bytes(tmp_path, monkeypatch):
    """MINOR-4：冻结后改 contract/budget.json ⇒ ``BUDGET_DRIFT``（不认旧哈希）。"""
    ctx, _ = _freeze_validation_root(tmp_path, monkeypatch)
    budget = ctx.read_json("contract/budget.json")
    budget["max_steps"] = int(budget["max_steps"]) + 1
    ctx.write_json("contract/budget.json", budget)
    # 篡改后的预算件在``budget_from_dict`` 处即被契约拒（更早），或在 verify 的
    # 复算处被拒——两条路径的码都是 ``BUDGET_DRIFT``，关键是 verify 不认旧哈希。
    with pytest.raises((LK.LockError, C.ContractError)) as exc:
        LK.verify_lock(ctx, repo_root=REPO, require_signatures=False)
    assert getattr(exc.value, "code", None) == "BUDGET_DRIFT"


def test_verify_lock_recomputes_suite_spec_bytes(tmp_path, monkeypatch):
    """MINOR-7：冻结后改注册件（夹具/预期）⇒ ``SUITE_SPEC_DRIFT``。"""
    ctx, frozen = _freeze_validation_root(tmp_path, monkeypatch)
    assert frozen["doc"]["offline_suite_spec"]["path"] == OV.SPEC_RELPATH
    spec = ctx.read_json(OV.SPEC_RELPATH)
    spec["registered_at_utc"] = "2000-01-01T00:00:00Z"
    ctx.write_json(OV.SPEC_RELPATH, spec)
    with pytest.raises(LK.LockError) as exc:
        LK.verify_lock(ctx, repo_root=REPO, require_signatures=False)
    assert exc.value.code == "SUITE_SPEC_DRIFT"


def test_validation_lock_digest_covers_anchor_fields(tmp_path):
    """R3-M1 补强：验证 LOCK 的冻结锚点字段改动即 ``LOCK_TAMPERED``。

    摘要口径覆盖**整份**验证文档（闭包、regression_tests、注册件、画像/授权、
    校准件……），否则冻结后同时改 LOCK 与现场文件仍能互洽、三签也照样有效。
    付费 LOCK（无 ``purpose`` 键）摘要口径逐字节不变。
    """
    from orchestrator.rsi_r1 import loop as L

    doc = {"schema": L.LOCK_SCHEMA, "schema_version": 1,
           "purpose": "validation", "envelope_id": "env-test",
           "budget_sha256": "a" * 64, "stage": "study", "phase": "study",
           "dependencies": {"orchestrator/x.py": "b" * 64},
           "regression_tests": {"tests/test_x.py": "c" * 64},
           "closure": {"closure_sha256": "d" * 64, "image": {"pin": "p"},
                       "groups": {}}}
    root = tmp_path / "ctx"
    (root / "contract").mkdir(parents=True)
    sha = L.LockDocument.from_doc(doc).sha256
    (root / LK.LOCK_RELPATH).write_text(
        json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    (root / LK.LOCK_SHA_RELPATH).write_text(sha + "\n", encoding="utf-8")
    assert LK.load_frozen(_DiskCtx(root)).sha256 == sha
    for key, value in (("regression_tests", {"tests/test_x.py": "e" * 64}),
                       ("closure", {**doc["closure"], "groups": {"x": []}}),
                       ("envelope_id", "env-other")):
        bad = {**doc, key: value}
        (root / LK.LOCK_RELPATH).write_text(
            json.dumps(bad, sort_keys=True) + "\n", encoding="utf-8")
        with pytest.raises(LK.LockError) as exc:
            LK.load_frozen(_DiskCtx(root))
        assert exc.value.code == "LOCK_TAMPERED", key
    # 付费口径回归：同一份 body 不含 purpose 时摘要仍是旧公式（旧 sha 可复算）。
    paid = {k: doc[k] for k in ("schema", "envelope_id", "budget_sha256",
                                "stage", "phase", "dependencies")}
    legacy = L.LockDocument.from_doc(paid).sha256
    assert legacy == C.canonical_sha256({
        "envelope_id": doc["envelope_id"],
        "budget_sha256": doc["budget_sha256"], "stage": "study",
        "phase": "study", "dependencies": doc["dependencies"],
        "schema": L.LOCK_SCHEMA})


def test_parse_junit_rejects_conflicting_duplicate_case(tmp_path):
    """T5-M2：同名用例状态冲突不得后写覆盖前写。"""
    from orchestrator.rsi_r1 import offline_validation as OV

    xml = tmp_path / "j.xml"
    xml.write_text(
        "<testsuites><testsuite>"
        "<testcase classname='tests.test_x' name='c1'/>"
        "<testcase classname='tests.test_x' name='c1'><failure/></testcase>"
        "</testsuite></testsuites>", encoding="utf-8")
    with pytest.raises(OV.OfflineValidationError) as exc:
        OV._parse_junit(xml)
    assert exc.value.code == "REGRESSION_CASE_CONFLICT"
