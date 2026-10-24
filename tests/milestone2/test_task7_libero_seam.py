"""Phase 2 Task7 B 线 Stage0 接缝四测（附录 A，L954-957；0 付费）。

  1. test_stage0_report_schema       simpraxis.task7.stage0_compat.v1 校验 fail-closed；
  2. test_tour_dir_not_in_observed_stage_map  蓝图 observed 四键不动，LIBERO 永不进映射；
  3. test_isolated_venv_paths        安装只写隔离 venv 绝对路径，共享 site-packages
                                     今天确实无 robosuite/bddl/libero；
  4. test_libero_wj_report_contract  simpraxis.wj_report.v1：可溯源/来源标签/媒体错配。

纪律：全程离线 hermetic（HF_*OFFLINE 由 shell 导出；本文件零网络/零容器/零
tour 执行，硬规则 10）；fake tour 目录只用 tmp_path 占位。
"""
from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.graph import task7_libero as t7

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE_PATH = REPO_ROOT / "sandbox" / "probes" / "probe_libero_compat.py"
RUN_SH = REPO_ROOT / "sandbox" / "probes" / "run_libero_compat_probe.sh"
BLUEPRINT = REPO_ROOT / "templates" / "tour_blueprint.json"
CHECK_BLUEPRINT = REPO_ROOT / "scripts" / "verification" / "check_blueprint_evidence.py"
VENV_PY = "/mnt/venvs/simpraxis/bin/python"

# 探针模块按文件路径直装载验（与 task7_libero 的装载方式同构；sandbox/ 非包）。
_spec = importlib.util.spec_from_file_location("probe_libero_compat_under_test",
                                               PROBE_PATH)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


# ---------------------------------------------------------------------------
# 合成件构造器（非测试函数）
# ---------------------------------------------------------------------------

def _ok_import_chain() -> list[dict]:
    versions = [("numpy", "1.26.4"), ("torch", "2.4.1"), ("mujoco", "3.15.0"),
                ("robosuite", "1.4.0"), ("bddl", "1.0.1"),
                ("libero", "libero-import-ok")]
    return [probe.summarize_import(m, 0, f"{v}\n", "") for m, v in versions]


def _ok_install_steps() -> list[dict]:
    # 锁内适配（轮次 4→5）：3 步扩为 6 步——isolated_leaf_deps（只补
    # termcolor/future 两件叶子依赖）、libero_src_copy（tour 实体复制到 runs
    # 区可写副本）、libero_pkg_init_shim（轮次 5：仅在可写副本补空
    # libero/__init__.py marker，upstream 8f1084e 缺件的 canonical 补法）；
    # libero_editable 的 -e 改指可写副本（不再是只读 TOUR_DIR）。
    constraints = "/tmp/constraints.txt"
    py = probe.venv_python(probe.VENV_PATH)
    return [
        {"target": "venv_create",
         "argv": probe.venv_create_cmd(probe.VENV_PATH), "rc": 0,
         "log_path": "/tmp/venv_create.log", "tail_verbatim": ".pth 引导完成"},
        {"target": "robosuite_bddl",
         "argv": probe.pip_install_chain_cmd(probe.VENV_PATH, constraints),
         "rc": 0,
         "log_path": "/tmp/pip_robosuite_bddl.log",
         "tail_verbatim": "Successfully installed robosuite-1.4.0 bddl-1.0.1"},
        {"target": "isolated_leaf_deps",
         "argv": probe.pip_install_leaf_deps_cmd(probe.VENV_PATH, constraints),
         "rc": 0,
         "log_path": "/tmp/pip_isolated_leaf_deps.log",
         "tail_verbatim": "Successfully installed termcolor future"},
        {"target": "libero_src_copy",
         "argv": probe.copy_tour_src_cmd(
             probe.VENV_PATH, probe.TOUR_DIR, probe.WRITABLE_TOUR_COPY),
         "rc": 0,
         "log_path": "/tmp/libero_src_copy.log",
         "tail_verbatim": "src=/mnt/assets\ndst=/home/agent/runs\ncopied ok"},
        {"target": "libero_pkg_init_shim",
         "argv": probe.libero_pkg_init_shim_cmd(py, probe.WRITABLE_TOUR_COPY),
         "rc": 0,
         "log_path": "/tmp/libero_pkg_init_shim.log",
         "tail_verbatim": "shim created: empty libero/__init__.py marker"},
        {"target": "libero_editable",
         "argv": probe.pip_install_libero_cmd(
             probe.VENV_PATH, probe.WRITABLE_TOUR_COPY),
         "rc": 0,
         "log_path": "/tmp/pip_libero_editable.log",
         "tail_verbatim": "Successfully installed libero"},
    ]


def _ok_report(**overrides) -> dict:
    chain = _ok_import_chain()
    report = {
        "schema": probe.REPORT_SCHEMA,
        "version": probe.REPORT_VERSION,
        "ok": True,
        "outcome": "compatible",
        "venv_path": probe.VENV_PATH,
        "index_url": probe.INDEX_URL,
        "tour_dir": probe.TOUR_DIR,
        "installed": {"numpy": "1.26.4", "torch": "2.4.1", "mujoco": "3.15.0",
                      "mujoco-py": None, "robosuite": "1.4.0",
                      "bddl": "1.0.1", "libero": "0.1.0"},
        "install_steps": _ok_install_steps(),
        "import_chain": chain,
        "shared_pins_observed": {"numpy": "1.26.4", "torch": "2.4.1",
                                 "mujoco": "3.15.0"},
        "pin_facts": {"numpy_is_shared_pin": True, "torch_is_shared_pin": True,
                      "mujoco_is_shared_pin": True, "mujoco_diverged": False},
        "reasons": [],
        "notes": [],
        "ts_utc": "2026-10-24T03:00:00Z",
    }
    report.update(overrides)
    return report


def _valid_wj_report(**overrides) -> dict:
    """最小合法 wj_report v1（gate2_ready=False 也必须媒体齐备、数字可溯源）。"""
    media = [
        {"kind": "video", "path": "runs/t1/video.mp4", "sha256": None},
        {"kind": "log", "path": "runs/t1/results.json", "sha256": None},
    ]
    tasks = [
        {"task_id": "libero_spatial_basketball_in_hoop",
         "bddl": "libero_spatial/basketball_in_hoop.bddl",
         "success": 1, "episodes": 2,
         "metrics_ref": {"path": "runs/t1/results.json",
                         "keys": ["success_count", "episode_count"],
                         "sha256": None},
         "judge_source": "自补",
         "rungs": {"rung1": True, "rung2": True, "rung3": True},
         "media": ["runs/t1/video.mp4", "runs/t1/results.json"]},
    ]
    report = t7.build_wj_report(
        thread_id="t7l-seam-test", stage=t7.STAGE1_SPATIAL,
        suite_name="libero_spatial", n_episodes_per_task=2,
        tasks=tasks, media=media,
        stage0_compat_ref="evidence/task7/stage0_compat_report.json",
        gate2_ready=False, ts_utc="2026-10-24T03:00:00Z")
    report.update(overrides)
    return report


# ---------------------------------------------------------------------------
# ① Stage0 兼容报告 schema（fail-closed）
# ---------------------------------------------------------------------------

def test_stage0_report_schema(tmp_path, monkeypatch):
    # 合法件（锁内适配轮次 4→5：6 步按序全形 + 六模块 import 全 ok）
    report = _ok_report()
    assert probe.validate_report(report) == []
    assert [s["target"] for s in report["install_steps"]] == \
        list(probe.EXPECTED_STEP_TARGETS)
    assert len(report["install_steps"]) == 6
    assert len(report["import_chain"]) == 6
    assert all(e["ok"] for e in report["import_chain"])
    assert report["outcome"] == "compatible"
    # editable 规范指向 runs 区可写副本；leaf 步白名单两件叶子依赖。
    editable_step = next(s for s in report["install_steps"]
                         if s["target"] == "libero_editable")
    assert editable_step["argv"][editable_step["argv"].index("-e") + 1] == \
        probe.WRITABLE_TOUR_COPY
    leaf_step = next(s for s in report["install_steps"]
                     if s["target"] == "isolated_leaf_deps")
    assert probe._pip_install_spec_names(leaf_step["argv"]) == \
        ["termcolor", "future"]
    # 轮次 5 shim 步位于 copy 之后、editable 之前，rc=0。
    _targets = [s["target"] for s in report["install_steps"]]
    assert _targets.index("libero_pkg_init_shim") == \
        _targets.index("libero_src_copy") + 1
    assert _targets.index("libero_editable") == \
        _targets.index("libero_pkg_init_shim") + 1

    # mujoco 漂移但 import 全通：合法 compatible，漂移是事实不是失败（D-4）
    chain_div = _ok_import_chain()
    chain_div[2] = probe.summarize_import("mujoco", 0, "2.3.7\n", "")
    # M2 对账纪律：同 venv 内 metadata 与 import 必然一致，故 installed 也
    # 必须是漂移后的 2.3.7（停在 3.15.0 本身就是自相矛盾件）。
    div = _ok_report(
        import_chain=chain_div,
        installed={**_ok_report()["installed"], "mujoco": "2.3.7"},
        shared_pins_observed={"numpy": "1.26.4", "torch": "2.4.1",
                              "mujoco": "2.3.7"},
        pin_facts={"numpy_is_shared_pin": True, "torch_is_shared_pin": True,
                   "mujoco_is_shared_pin": False, "mujoco_diverged": True},
        notes=["隔离 venv 内 mujoco 2.3.7（D-4 允许）"])
    assert probe.validate_report(div) == []

    # 合法失败件：ok=false + 非空 reasons + 失败 import 必须带原文
    bad_chain = _ok_import_chain()
    bad_chain[3] = probe.summarize_import(
        "robosuite", 1, "", "ImportError: No module named 'mujoco_py'\n")
    failure = _ok_report(
        ok=False, outcome="conflict_isolated_failed",
        import_chain=bad_chain,
        shared_pins_observed={"numpy": "1.26.4", "torch": "2.4.1",
                              "mujoco": "3.15.0"},
        pin_facts={"numpy_is_shared_pin": True, "torch_is_shared_pin": True,
                   "mujoco_is_shared_pin": True, "mujoco_diverged": False},
        reasons=["import robosuite 失败"],
        degradation="structure_tour_plus_compat_whitepaper")
    assert probe.validate_report(failure) == []

    required = [k for k in report if k != "degradation"]  # degradation 可缺
    for k in required:
        bad = _ok_report()
        del bad[k]
        assert probe.validate_report(bad), f"缺字段 {k} 必须拒"

    # 版本/枚举/布尔一致性/时区格式
    for mutate in ("schema", "version", "outcome", "ok", "ts_utc"):
        bad = _ok_report()
        if mutate == "schema":
            bad["schema"] = "simpraxis.task7.stage0_compat.v0"
        elif mutate == "version":
            bad["version"] = 2
        elif mutate == "outcome":
            bad["outcome"] = "maybe_compatible"
        elif mutate == "ok":
            bad["ok"] = False  # 与 outcome=compatible 矛盾
        else:
            bad["ts_utc"] = "2026-10-24 03:00:00"
        assert probe.validate_report(bad), f"{mutate} 违规必须拒"

    # 失败 import 却空原文（手工伪造件的典型特征）必须拒
    forged = _ok_report(
        ok=False, outcome="conflict_isolated_failed",
        import_chain=[
            {**e, "ok": False, "error_verbatim": "",
             "stderr_verbatim": "", "version": None}
            if e["module"] == "libero" else e for e in _ok_import_chain()
        ],
        pin_facts={"numpy_is_shared_pin": True, "torch_is_shared_pin": True,
                   "mujoco_is_shared_pin": True, "mujoco_diverged": False},
        reasons=["import libero 失败（无原文）"])
    errs = probe.validate_report(forged)
    assert any("error_verbatim" in e for e in errs), errs

    # conflict 结局却空 reasons（空口降级）必须拒
    no_reasons = _ok_report(ok=False, outcome="conflict_isolated_failed",
                            reasons=[])
    assert any("reasons" in e for e in probe.validate_report(no_reasons))

    # ---- MAJOR（三签冷读）：validate 必须由事实反推结局，手标一律拒 ----
    # (1) 六 import 全败、steps 全败、observed pins 全 null，却口标 compatible。
    forged_win = _ok_report()
    forged_win["install_steps"] = [
        {**s, "rc": 1, "tail_verbatim": "synthetic failure"}
        for s in _ok_install_steps()]
    forged_win["import_chain"] = [
        probe.summarize_import(e["module"], 1, "",
                               f"ImportError: {e['module']} 未安装")
        for e in _ok_import_chain()]
    forged_win["shared_pins_observed"] = {"numpy": None, "torch": None,
                                          "mujoco": None}
    forged_win["pin_facts"] = {"numpy_is_shared_pin": False,
                               "torch_is_shared_pin": False,
                               "mujoco_is_shared_pin": False,
                               "mujoco_diverged": False}
    # outcome/ok/reasons 仍停在 compatible 真值件状态（伪造全胜）
    errs = probe.validate_report(forged_win)
    assert any("派生应为" in e and "conflict_isolated_failed" in e
               for e in errs), errs

    # (2) import 全 ok 但 observed pin 全假（9.9.9/8.8.8/1.1.1），pin_facts 却三真。
    forged_pins = _ok_report(
        shared_pins_observed={"numpy": "9.9.9", "torch": "8.8.8",
                              "mujoco": "1.1.1"},
        pin_facts={"numpy_is_shared_pin": True, "torch_is_shared_pin": True,
                   "mujoco_is_shared_pin": True, "mujoco_diverged": False})
    errs = probe.validate_report(forged_pins)
    assert any("派生应为" in e for e in errs), errs
    assert any("pin_facts" in e for e in errs), errs

    # (3) 事实全绿却口标 conflict（结局不可随意降级/口标）。
    forged_lose = _ok_report(
        ok=False, outcome="conflict_isolated_failed",
        reasons=["手标的非事实理由"],
        degradation="structure_tour_plus_compat_whitepaper")
    errs = probe.validate_report(forged_lose)
    assert any("派生应为" in e and "'compatible'" in e for e in errs), errs

    # ---- M1（MAJOR，2026-10-24 宿主实证）：PEP440 local 段归一化 ----
    # 共享 torch 实测逐字为 '2.4.1+cu124'（importlib.metadata 与
    # torch.__version__ 双实证）：链/pins/installed 三处带 +cu124 的全绿件
    # 必须判 compatible、torch_is_shared_pin=True——严格 == 会吐出字符串口径
    # 错误的假降级件，D-4 判决作废。
    chain_cu124 = _ok_import_chain()
    chain_cu124[1] = probe.summarize_import("torch", 0, "2.4.1+cu124\n", "")
    cu124_green = _ok_report(
        import_chain=chain_cu124,
        installed={**_ok_report()["installed"], "torch": "2.4.1+cu124"},
        shared_pins_observed={"numpy": "1.26.4", "torch": "2.4.1+cu124",
                              "mujoco": "3.15.0"})
    assert probe.validate_report(cu124_green) == []
    assert cu124_green["outcome"] == "compatible"
    assert cu124_green["pin_facts"]["torch_is_shared_pin"] is True

    # numpy 侧对称件：归一化不特判 torch，任意 PEP440 +local 段同样按 release 比。
    chain_np_local = _ok_import_chain()
    chain_np_local[0] = probe.summarize_import(
        "numpy", 0, "1.26.4+localbuild\n", "")
    np_local = _ok_report(
        import_chain=chain_np_local,
        installed={**_ok_report()["installed"], "numpy": "1.26.4+localbuild"},
        shared_pins_observed={"numpy": "1.26.4+localbuild", "torch": "2.4.1",
                              "mujoco": "3.15.0"})
    assert probe.validate_report(np_local) == []
    assert np_local["pin_facts"]["numpy_is_shared_pin"] is True

    # local 段是构建身份：'2.4.1+cu125' 虽满足公共说明符 torch==2.4.1（pip
    # resolver 侧），却不是共享那一件 torch（共享全称 2.4.1+cu124，硬规则 8）
    # ——隔离 venv 出现异 local 的 torch 即「自带 torch」，必须仍判 conflict，
    # PEP440 归一化不得成为换构建的伪造口子。
    chain_cu125 = _ok_import_chain()
    chain_cu125[1] = probe.summarize_import("torch", 0, "2.4.1+cu125\n", "")
    cu125 = _ok_report(
        import_chain=chain_cu125,
        installed={**_ok_report()["installed"], "torch": "2.4.1+cu125"},
        shared_pins_observed={"numpy": "1.26.4", "torch": "2.4.1+cu125",
                              "mujoco": "3.15.0"})
    errs = probe.validate_report(cu125)
    assert any("派生应为" in e and "conflict_isolated_failed" in e
               for e in errs), errs
    assert any("pin_facts.torch_is_shared_pin" in e for e in errs), errs
    # release 段漂移（2.5.0+cu124）同样必须判 conflict（双方向回归）。
    chain_drift = _ok_import_chain()
    chain_drift[1] = probe.summarize_import("torch", 0, "2.5.0+cu124\n", "")
    drift = _ok_report(
        import_chain=chain_drift,
        installed={**_ok_report()["installed"], "torch": "2.5.0+cu124"},
        shared_pins_observed={"numpy": "1.26.4", "torch": "2.5.0+cu124",
                              "mujoco": "3.15.0"})
    errs = probe.validate_report(drift)
    assert any("派生应为" in e and "conflict_isolated_failed" in e
               for e in errs), errs

    # ---- LOW-3：installed 多键拒；argv[0] 入口契约 ----
    extra_installed = _ok_report(installed={**_ok_report()["installed"],
                                            "evil_dist": "1.0"})
    assert any("TARGET_DISTRIBUTIONS" in e
               for e in probe.validate_report(extra_installed))
    bad_step = _ok_report()
    bad_step["install_steps"][0]["argv"][0] = "/bin/bash"  # venv_create 越权
    assert any("venv_create" in e for e in probe.validate_report(bad_step))
    bad_step2 = _ok_report()
    bad_step2["install_steps"][1]["argv"][0] = "/usr/bin/pip3"  # 绕开隔离 venv
    assert any("隔离 venv" in e for e in probe.validate_report(bad_step2))

    # ---- 锁内适配（轮次 4，2026-10-24 容器实证）：5 步白名单变异三连 ----
    # (i) libero_editable 的 -e 回指只读 /mnt/assets tour：必拒，错误须点名
    # 只读/assets（回归守卫：防 editable 装回只读 tour 还伪造 compatible）。
    back_to_assets = _ok_report()
    for st in back_to_assets["install_steps"]:
        if st["target"] == "libero_editable":
            ei = st["argv"].index("-e")
            st["argv"][ei + 1] = probe.TOUR_DIR
    errs = probe.validate_report(back_to_assets)
    assert any("只读" in e and "/assets" in e for e in errs), errs

    # (ii) isolated_leaf_deps 缺步 / 包名被换（numpy 顶替 termcolor；future 被
    # 换成主链件 robosuite）：白名单缺步与偷渡守卫都必须拒。
    drop_leaf = _ok_report()
    drop_leaf["install_steps"] = [
        s for s in drop_leaf["install_steps"]
        if s["target"] != "isolated_leaf_deps"]
    errs = probe.validate_report(drop_leaf)
    assert any("isolated_leaf_deps" in e and "按序" in e for e in errs), errs
    swap_numpy = _ok_report()
    for st in swap_numpy["install_steps"]:
        if st["target"] == "isolated_leaf_deps":
            st["argv"] = [a for a in st["argv"] if a != "termcolor"] + ["numpy"]
    errs = probe.validate_report(swap_numpy)
    assert any("isolated_leaf_deps" in e and "numpy" in e for e in errs), errs
    swap_robosuite = _ok_report()
    for st in swap_robosuite["install_steps"]:
        if st["target"] == "isolated_leaf_deps":
            st["argv"] = [("robosuite==1.4.0" if a == "future" else a)
                          for a in st["argv"]]
    errs = probe.validate_report(swap_robosuite)
    assert any("isolated_leaf_deps" in e and "robosuite" in e
               for e in errs), errs

    # 白名单恰 5 个、按序、各一次：重复步与乱序同罪。
    dup_step = _ok_report()
    dup_step["install_steps"].append(dict(dup_step["install_steps"][-1]))
    assert any("按序" in e for e in probe.validate_report(dup_step))
    swapped_order = _ok_report()
    _steps = swapped_order["install_steps"]
    _i3 = next(n for n, s in enumerate(_steps)
               if s["target"] == "isolated_leaf_deps")
    _i4 = next(n for n, s in enumerate(_steps)
               if s["target"] == "libero_src_copy")
    _steps[_i3], _steps[_i4] = _steps[_i4], _steps[_i3]
    assert any("按序" in e for e in probe.validate_report(swapped_order))

    # (iii) libero_src_copy 的 dst 跑到 runs 根外 / 缺 shutil.copytree：拒。
    dst_outside = _ok_report()
    for st in dst_outside["install_steps"]:
        if st["target"] == "libero_src_copy":
            st["argv"][2] = st["argv"][2].replace(
                probe.WRITABLE_TOUR_COPY, "/tmp/evil_copy/LIBERO_tour")
    errs = probe.validate_report(dst_outside)
    assert any("libero_src_copy" in e and "runs" in e for e in errs), errs
    dst_in_tour = _ok_report()
    for st in dst_in_tour["install_steps"]:
        if st["target"] == "libero_src_copy":
            st["argv"][2] = st["argv"][2].replace(
                probe.WRITABLE_TOUR_COPY,
                probe.TOUR_DIR + "/libero_src_copy")
    errs = probe.validate_report(dst_in_tour)
    assert any("libero_src_copy" in e and "TOUR_DIR" in e for e in errs), errs
    no_copytree = _ok_report()
    for st in no_copytree["install_steps"]:
        if st["target"] == "libero_src_copy":
            st["argv"][2] = st["argv"][2].replace(
                "shutil.copytree", "shutil.copyfile")
    errs = probe.validate_report(no_copytree)
    assert any("libero_src_copy" in e and "shutil.copytree" in e
               for e in errs), errs
    no_symlinks_false = _ok_report()
    for st in no_symlinks_false["install_steps"]:
        if st["target"] == "libero_src_copy":
            st["argv"][2] = st["argv"][2].replace(
                "symlinks=False", "symlinks=True")
    errs = probe.validate_report(no_symlinks_false)
    assert any("libero_src_copy" in e and "symlinks" in e for e in errs), errs

    # 锁内适配（轮次 5，fail-closed 臂回归）：copytree 禁止带
    # dirs_exist_ok=True——实证规格要求副本已存在时必须自然 FileExistsError
    # （rc 保留、不覆盖既有副本），静默覆盖会把旧副本与本轮事实搅在一起。
    silent_overwrite = _ok_report()
    for st in silent_overwrite["install_steps"]:
        if st["target"] == "libero_src_copy":
            st["argv"][2] = st["argv"][2].replace(
                "symlinks=False)",
                "symlinks=False, dirs_exist_ok=True)")
    errs = probe.validate_report(silent_overwrite)
    assert any("libero_src_copy" in e and "dirs_exist_ok" in e
               for e in errs), errs

    # ---- 锁内适配（轮次 5，2026-10-24 06:12Z 容器实证）：libero_pkg_init_shim
    # upstream 8f1084e 独缺顶层 libero/__init__.py（find_packages 0 包、finder
    # MAPPING={}、top_level.txt 空），仅许在可写副本原子补空 marker；下列变异
    # 全部必须 fail-closed。
    def _shim_step(report_obj):
        return next(s for s in report_obj["install_steps"]
                    if s["target"] == "libero_pkg_init_shim")

    # (iv-a) shim 写回只读 /mnt/assets 树：代码字面出现 /mnt/assets 即拒，
    # 错误须点名只读/assets（原 assets 树只读绝不触碰的回归守卫）。
    shim_assets = _ok_report()
    st = _shim_step(shim_assets)
    st["argv"][2] = st["argv"][2].replace(
        probe.WRITABLE_TOUR_COPY, probe.TOUR_DIR)
    errs = probe.validate_report(shim_assets)
    assert any("libero_pkg_init_shim" in e and "只读" in e
               and "/assets" in e for e in errs), errs

    # (iv-b) shim target 逃出副本（join 改打 ../evil）：target 必须逐字钉死
    # 为副本内 libero/__init__.py，拒绝点须点名逃出副本。
    shim_escape = _ok_report()
    st = _shim_step(shim_escape)
    st["argv"][2] = st["argv"][2].replace(
        'os.path.join(copy, "libero", "__init__.py")',
        'os.path.join(copy, "..", "evil", "__init__.py")')
    errs = probe.validate_report(shim_escape)
    assert any("libero_pkg_init_shim" in e and "逃出副本" in e
               for e in errs), errs

    # (iv-c) shim 顺序变异：跑到 editable 之后、或 copy 之前，皆按序拒绝。
    shim_after_editable = _ok_report()
    _steps = shim_after_editable["install_steps"]
    _i4 = next(n for n, s in enumerate(_steps)
               if s["target"] == "libero_pkg_init_shim")
    _i5 = next(n for n, s in enumerate(_steps)
               if s["target"] == "libero_editable")
    _steps[_i4], _steps[_i5] = _steps[_i5], _steps[_i4]
    assert any("按序" in e for e in probe.validate_report(shim_after_editable))
    shim_before_copy = _ok_report()
    _steps = shim_before_copy["install_steps"]
    _i3 = next(n for n, s in enumerate(_steps)
               if s["target"] == "libero_src_copy")
    _i4 = next(n for n, s in enumerate(_steps)
               if s["target"] == "libero_pkg_init_shim")
    _steps[_i3], _steps[_i4] = _steps[_i4], _steps[_i3]
    assert any("按序" in e for e in probe.validate_report(shim_before_copy))

    # (iv-d) 缺 shim 步：5 步旧形必须拒（按序错误列缺失名）。
    shim_missing = _ok_report()
    shim_missing["install_steps"] = [
        s for s in shim_missing["install_steps"]
        if s["target"] != "libero_pkg_init_shim"]
    errs = probe.validate_report(shim_missing)
    assert any("按序" in e and "libero_pkg_init_shim" in e
               for e in errs), errs

    # (iv-e) shim 非空写：handle.write('') 改成任意非空字面量即拒（只许空
    # marker；upstream 未来补真 __init__.py 时非空分支必须 fail-closed）。
    shim_nonempty = _ok_report()
    st = _shim_step(shim_nonempty)
    st["argv"][2] = st["argv"][2].replace(
        "handle.write('')", "handle.write('# package marker')")
    errs = probe.validate_report(shim_nonempty)
    assert any("libero_pkg_init_shim" in e and "非空" in e
               for e in errs), errs

    # (iv-f) shim 代码混入删树/复制/静默合并语义：一律拒。
    for banned in ("shutil.rmtree", "shutil.copytree", "dirs_exist_ok"):
        shim_banned = _ok_report()
        st = _shim_step(shim_banned)
        st["argv"][2] = banned + "\n" + st["argv"][2]
        errs = probe.validate_report(shim_banned)
        assert any("libero_pkg_init_shim" in e and banned in e
                   for e in errs), (banned, errs)

    # ---- M2（MAJOR）：pins/installed 与 import_chain 同源对账 ----
    # (a) 链实测 numpy=9.9.9（ok=true），pins/installed 却手填共享 1.26.4：
    # 结局反推可能仍 compatible，但三面互相矛盾必须 fail-closed。
    chain9 = _ok_import_chain()
    chain9[0] = probe.summarize_import("numpy", 0, "9.9.9\n", "")
    m2a = _ok_report(import_chain=chain9)  # pins/installed 停在 1.26.4
    errs = probe.validate_report(m2a)
    assert any("shared_pins_observed.numpy" in e
               and "9.9.9" in e for e in errs), errs

    # (b) 链 numpy ok=true，但 installed.numpy=null（import 成功与 metadata
    # 缺件矛盾，同 venv 同 sys.path 不可能）：拒。
    m2b = _ok_report(
        installed={**_ok_report()["installed"], "numpy": None})
    errs = probe.validate_report(m2b)
    assert any("installed.numpy" in e for e in errs), errs

    # (a2) 逐字对账（第 3 轮）：pins 是链 version 的逐字拷贝面，release 相等
    # 不等于同一观测——chain torch '2.4.1+cu125'、pins/installed 手填
    # '2.4.1+cu124' 是异 local 换构建伪造，必须在 pins↔chain 面即拒（错误落在
    # shared_pins_observed.torch），且不得误报 installed.torch（跨面 release
    # 相等合法：cu125 链版本与 cu124 metadata 也不可能同环境共存，但那是
    # pins 先被伪造导致的，拒绝点只在逐字面）。
    chain_cu125_m2 = _ok_import_chain()
    chain_cu125_m2[1] = probe.summarize_import(
        "torch", 0, "2.4.1+cu125\n", "")
    m2c = _ok_report(
        import_chain=chain_cu125_m2,
        installed={**_ok_report()["installed"], "torch": "2.4.1+cu124"},
        shared_pins_observed={"numpy": "1.26.4", "torch": "2.4.1+cu124",
                              "mujoco": "3.15.0"})
    errs = probe.validate_report(m2c)
    assert any("shared_pins_observed.torch" in e
               and "2.4.1+cu125" in e and "2.4.1+cu124" in e
               for e in errs), errs
    assert not any("installed.torch" in e for e in errs), errs

    # (a3) 对称真实件：chain/pins 逐字 '2.4.1+cu124'，installed 同值，全绿
    # （逐字化不得误伤带 local 段的诚实件）。
    chain_cu124_m2 = _ok_import_chain()
    chain_cu124_m2[1] = probe.summarize_import(
        "torch", 0, "2.4.1+cu124\n", "")
    m2d = _ok_report(
        import_chain=chain_cu124_m2,
        installed={**_ok_report()["installed"], "torch": "2.4.1+cu124"},
        shared_pins_observed={"numpy": "1.26.4", "torch": "2.4.1+cu124",
                              "mujoco": "3.15.0"})
    assert probe.validate_report(m2d) == []

    # 反向合法证据不被误伤：链 ok=false 时 installed 有值（装了但 import 挂）
    # 是合法 D-4 证据，不得拒（robosuite 失败件 installed 仍报 1.4.0）。
    assert probe.validate_report(failure) == []

    # ---- M3：installed 缺键（证据不完整）与多键同罪，列 missing/extra ----
    missing_installed = {k: v for k, v in _ok_report()["installed"].items()
                         if k != "bddl"}
    m3 = _ok_report(installed=missing_installed)
    errs = probe.validate_report(m3)
    assert any("installed" in e and "缺" in e and "bddl" in e
               for e in errs), errs

    # ---- M6：degradation 必须与结局一致 ----
    compat_with_deg = _ok_report(degradation=probe.DEGRADATION)
    assert any("degradation" in e
               for e in probe.validate_report(compat_with_deg))
    conflict_no_deg = _ok_report(
        ok=False, outcome="conflict_isolated_failed",
        reasons=["手标降级却缺 D-4 degradation 常量"], degradation=None)
    assert any("degradation" in e
               for e in probe.validate_report(conflict_no_deg))
    conflict_bad_deg = _ok_report(
        ok=False, outcome="conflict_isolated_failed",
        reasons=["degradation 常量被改写"], degradation="whitepaper_only")
    errs = probe.validate_report(conflict_bad_deg)
    assert any(probe.DEGRADATION in e for e in errs), errs

    # load：缺件/坏 JSON/违规件统一 Stage0ReportError（图接缝包 GraphStateError）
    with pytest.raises(probe.Stage0ReportError):
        probe.load_report(tmp_path / "absent.json")
    bad_path = tmp_path / "handcrafted.json"
    bad_path.write_text("{不是 JSON", encoding="utf-8")
    with pytest.raises(probe.Stage0ReportError):
        probe.load_report(bad_path)
    bad_path.write_text(json.dumps(_ok_report(schema="fake.schema")),
                        encoding="utf-8")
    with pytest.raises(probe.Stage0ReportError):
        probe.load_report(bad_path)

    # 合法件经图接缝装载：schema/常量透传
    good_path = tmp_path / "stage0_compat_report.json"
    good_path.write_text(json.dumps(report, ensure_ascii=False),
                         encoding="utf-8")
    loaded = t7.load_stage0_report(good_path)
    assert loaded["schema"] == t7.STAGE0_SCHEMA
    with pytest.raises(t7.GraphStateError):
        t7.load_stage0_report(tmp_path / "absent.json")

    # 回归（MAJOR A）：上游安装失败（venv_create rc=1）时探针必须吐出结构
    # 完整的 conflict_isolated_failed 报告，而不是自校验 RuntimeError 吞件。
    def _fake_run_fail(argv, *, timeout, env=None, log_path=None):
        if log_path is not None:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            Path(log_path).write_text(
                "synthetic venv_create rc=1 failure\n", encoding="utf-8")
        return 1, "", "ensurepip: synthetic failure\n"

    monkeypatch.setattr(probe, "_run", _fake_run_fail)
    failed_out = tmp_path / "sub" / "stage0_compat_report.json"
    work_dir = failed_out.parent / "stage0_compat_report_assets"
    fail_report = probe.execute_probe(
        failed_out, str(tmp_path / "LIBERO_tour"), probe.VENV_PATH,
        probe.INDEX_URL, probe.BASE_PYTHON, work_dir)

    assert failed_out.is_file()                          # 报告必须落盘
    assert fail_report["outcome"] == "conflict_isolated_failed"
    assert fail_report["ok"] is False
    chain = fail_report["import_chain"]
    assert len(chain) == 6
    assert {e["module"] for e in chain} == set(probe._IMPORT_SNIPPETS)
    assert all(not e["ok"] and e["rc"] == -1 for e in chain)
    assert all(e["error_verbatim"] and "未执行" in e["error_verbatim"]
               and "venv_create" in e["error_verbatim"] for e in chain)
    assert all(e["version"] is None and e["stdout_verbatim"] == ""
               for e in chain)
    assert probe.validate_report(fail_report) == []
    assert fail_report["degradation"] == probe.DEGRADATION
    assert fail_report["reasons"]                        # 空口降级禁止

    # 锁内适配（轮次 4→5）：上游断裂也必须落 6 步全形——venv_create 保留真实
    # rc=1，其余五步 rc=-1 占位且 tail 逐字「未执行：」（含 shim 占位，其
    # canonical argv 仍走隔离 venv python）。
    rsteps = fail_report["install_steps"]
    assert [s["target"] for s in rsteps] == \
        list(probe.EXPECTED_STEP_TARGETS)
    assert len(rsteps) == 6
    assert rsteps[0]["target"] == "venv_create"
    assert rsteps[0]["rc"] == 1
    assert [s["target"] for s in rsteps[1:]] == [
        "robosuite_bddl", "isolated_leaf_deps", "libero_src_copy",
        "libero_pkg_init_shim", "libero_editable"]
    assert all(s["rc"] == -1 for s in rsteps[1:])
    assert all("未执行" in s["tail_verbatim"] for s in rsteps[1:])
    assert all(s["argv"][0].startswith(probe.VENV_PATH + "/bin/")
               for s in rsteps[1:])
    # 轮次 4 三条 + 轮次 5 一条机制实证 notes 随任何结局留证（带「锁内适配」）。
    rnotes = fail_report["notes"]
    assert sum("锁内适配" in n for n in rnotes) >= 4
    assert any("Requires-Dist" in n for n in rnotes)
    assert any("Read-only file system" in n for n in rnotes)
    assert any("termcolor" in n and "future" in n for n in rnotes)
    assert any("8f1084e3132a39270c3a13ebe37270a43ece2a01" in n
               and "MAPPING={}" in n for n in rnotes)

    # 落盘件经 fail-closed 装载器读回仍合法。
    assert probe.load_report(failed_out)["outcome"] == \
        "conflict_isolated_failed"

    # LOW-2：报告 0600 原子落盘，父目录 0700（不依赖 umask）。
    import os as _os
    assert _os.stat(failed_out).st_mode & 0o777 == 0o600
    assert _os.stat(failed_out.parent).st_mode & 0o777 == 0o700
    atomic_tgt = tmp_path / "aw" / "r.json"
    probe._atomic_write_0600(atomic_tgt, "first")
    probe._atomic_write_0600(atomic_tgt, "second")  # 覆盖无半截件/无残留
    assert atomic_tgt.read_text(encoding="utf-8") == "second"
    assert _os.stat(atomic_tgt).st_mode & 0o777 == 0o600
    assert _os.stat(atomic_tgt.parent).st_mode & 0o777 == 0o700
    assert not list(atomic_tgt.parent.glob(".r.json.*"))  # mkstemp 临时件已 replace

    # main()：failure 结局也是合法机判产物 → rc 恒 0；只有自校验失败才非零。
    monkeypatch.setattr(sys, "argv", [
        "probe_libero_compat.py", "--out", str(failed_out),
        "--tour-dir", str(tmp_path / "LIBERO_tour"),
        "--venv", probe.VENV_PATH])
    assert probe.main() == 0


# ---------------------------------------------------------------------------
# ② LIBERO 永不进蓝图 observed_stage_maps（机判四键基线不动）
# ---------------------------------------------------------------------------

def test_tour_dir_not_in_observed_stage_map():
    bp = json.loads(BLUEPRINT.read_text(encoding="utf-8"))
    keys = set(bp["observed_stage_maps"])
    assert keys == {"ge_sim_v2", "genesis_world", "lw_benchhub",
                    "genie_sim_v3"}, keys
    assert "LIBERO" not in keys and "libero" not in keys
    # 任何嵌套值也不得把 LIBERO 写成 observed stage map slug
    assert "libero" not in {k.lower() for k in keys}

    # 与机判脚本同口径：真实脚本在本机（A 线四克隆齐全）必须仍判 OK。
    # 不设 skip 分支（硬规则 7）：环境缺克隆即 fail-closed 红，不静默放过。
    p = subprocess.run([VENV_PY, str(CHECK_BLUEPRINT)],
                       capture_output=True, text=True, cwd=REPO_ROOT)
    assert p.returncode == 0, f"蓝图机判失败：\n{p.stdout}\n{p.stderr}"
    assert "blueprint OK" in p.stdout

    # A 线 LIBERO 结构件不进蓝图证据集（防回归：未来误加即测试红）
    observed_repos = {v["repo"] for v in bp["observed_stage_maps"].values()}
    assert not any("libero" in r.lower() for r in observed_repos)


# ---------------------------------------------------------------------------
# ③ 隔离 venv：命令只打隔离绝对路径 + 共享 venv 今天确实干净
# ---------------------------------------------------------------------------

def test_isolated_venv_paths(tmp_path):
    vp = probe.VENV_PATH

    # 单点常量：Task0 非 root 化（compose user 1000:1000，
    # SIMPRAXIS_RUN_DIR=/home/agent/runs）后 D-4 锁内适配路径；
    # /root/runs 是 root 时代旧位，绝不允许回退。
    assert vp == "/home/agent/runs/phase2/venvs/libero"
    assert not vp.startswith("/mnt/venvs/")
    assert not vp.startswith("/root/")
    assert probe.SYSTEM_SITE_PACKAGES is True
    assert probe.SHARED_TORCH == "2.4.1"
    assert probe.SHARED_NUMPY == "1.26.4"
    assert probe.SHARED_MUJOCO == "3.15.0"

    # 锁内适配（轮次 4→5，2026-10-24 容器实证）：可写副本/叶子依赖/6 步白名单
    # 单点常量——副本必须在非 root runs 区，绝不落在只读 /mnt/assets。
    assert probe.WRITABLE_TOUR_COPY == \
        "/home/agent/runs/phase2/task7/libero_src/LIBERO_tour"
    assert probe.WRITABLE_TOUR_COPY.startswith("/home/agent/runs/")
    assert not probe.WRITABLE_TOUR_COPY.startswith("/mnt/assets/")
    assert not probe.WRITABLE_TOUR_COPY.startswith("/root/")
    assert probe.WRITABLE_TOUR_COPY != probe.TOUR_DIR
    assert probe.LEAF_PACKAGES == ("termcolor", "future")
    assert probe.EXPECTED_STEP_TARGETS == (
        "venv_create", "robosuite_bddl", "isolated_leaf_deps",
        "libero_src_copy", "libero_pkg_init_shim", "libero_editable")
    # shim 严格位于 copy 与 editable 之间（顺序单点钉死）。
    _names = probe.EXPECTED_STEP_TARGETS
    assert _names.index("libero_pkg_init_shim") == \
        _names.index("libero_src_copy") + 1
    assert _names.index("libero_editable") == \
        _names.index("libero_pkg_init_shim") + 1

    # 建 venv（锁内适配 2026-10-24）：--without-pip（镜像缺 ensurepip）+
    # --system-site-packages；唯一用共享解释器的一步，且只做布局创建。
    create = probe.venv_create_cmd()
    assert create[0] == probe.BASE_PYTHON
    assert create == [probe.BASE_PYTHON, "-m", "venv", "--without-pip",
                      "--system-site-packages", vp]
    assert "install" not in create

    # .pth 复用机制（purelib 一律 sysconfig 动态派生，禁止硬编码版本段）。
    probe_src = PROBE_PATH.read_text(encoding="utf-8")
    assert "lib/python" not in probe_src  # 不得硬编码 lib/python3.10/site-packages
    q_shared = probe.purelib_query_cmd(probe.BASE_PYTHON)
    assert q_shared[0] == probe.BASE_PYTHON and "sysconfig" in q_shared[2]
    assert probe.shared_pth_text("/a/sp") == "/a/sp\n"  # 单行内容
    assert probe.child_pth_path("/a/child/purelib").name == \
        probe.SHARED_PTH_NAME == "simpraxis_shared.pth"
    assert str(probe.child_pth_path("/a/child/purelib")).startswith("/a/child")
    assert probe.verify_shared_imports_cmd(f"{vp}/bin/python")[0] == \
        f"{vp}/bin/python"
    assert probe.pip_version_cmd(f"{vp}/bin/python") == \
        [f"{vp}/bin/python", "-m", "pip", "--version"]

    constraints = tmp_path / "c.txt"
    chain_cmd = probe.pip_install_chain_cmd(vp, constraints)
    # 锁内适配（轮次 4→5）：叶子依赖步、tour 复制步、__init__ shim 步、
    # editable 改指可写副本。
    leaf_cmd = probe.pip_install_leaf_deps_cmd(vp, constraints)
    copy_cmd = probe.copy_tour_src_cmd(
        vp, probe.TOUR_DIR, probe.WRITABLE_TOUR_COPY)
    shim_cmd = probe.libero_pkg_init_shim_cmd(
        probe.venv_python(vp), probe.WRITABLE_TOUR_COPY)
    editable_cmd = probe.pip_install_libero_cmd(vp, probe.WRITABLE_TOUR_COPY)
    metadata_cmd = probe.metadata_query_cmd(vp)

    # pip 一律 <child>/bin/python -m pip（--without-pip venv 没有 bin/pip）。
    for cmd in (chain_cmd, leaf_cmd, editable_cmd):
        assert cmd[0] == f"{vp}/bin/python", cmd
        assert cmd[1:4] == ["-m", "pip", "install"], cmd
        assert "-i" in cmd and probe.INDEX_URL in cmd  # 清华源单点
        assert "/bin/pip" not in " ".join(cmd)
    # 所有安装/查询/import 命令的可执行入口都是隔离 venv 绝对 bin 路径，且整条
    # argv 不得指向共享 site-packages 或 root 时代旧位。
    all_cmds = [chain_cmd, leaf_cmd, copy_cmd, shim_cmd,
                editable_cmd, metadata_cmd]
    all_cmds += [probe.import_check_cmd(m)
                 for m in ("numpy", "torch", "mujoco", "robosuite",
                           "bddl", "libero")]
    for cmd in all_cmds:
        assert cmd[0].startswith(f"{vp}/bin/"), cmd
        assert not cmd[0].startswith("/mnt/venvs/simpraxis/"), cmd
        assert not cmd[0].startswith("/root/"), cmd
        assert all(not a.startswith("/mnt/venvs/simpraxis/lib") for a in cmd)
        assert all(not a.startswith("/root/runs") for a in cmd)
    assert metadata_cmd[0] == f"{vp}/bin/python"
    assert not hasattr(probe, "venv_pip")  # 旧 bin/pip 入口已随机制切换删除
    assert probe.venv_python(vp) == f"{vp}/bin/python"

    # 约束只保护 numpy/torch（共享 ABI 前提）；刻意不钉 mujoco（D-4 允许漂移）。
    text = probe.CONSTRAINTS_TEXT
    assert "numpy==1.26.4" in text and "torch==2.4.1" in text
    assert "mujoco" not in text

    # 最小链不含 tour 全量训练期依赖（hydra/transformers/robomimic 留给 Gate1 后）。
    assert "robosuite==1.4.0" in chain_cmd and "bddl==1.0.1" in chain_cmd
    assert not any("hydra" in p or "transformers" in p or "robomimic" in p
                   for p in probe.MINIMAL_PACKAGES)
    # 锁内适配（轮次 4）：isolated_leaf_deps 白名单死钉 termcolor/future——
    # -c 同一约束只保护 numpy/torch，argv 里不得出现任何共享/主链分发名。
    assert leaf_cmd[1:4] == ["-m", "pip", "install"]
    assert "-c" in leaf_cmd and str(constraints) in leaf_cmd
    assert "termcolor" in leaf_cmd and "future" in leaf_cmd
    assert probe._pip_install_spec_names(leaf_cmd) == ["termcolor", "future"]
    assert not any(
        a.split("=", 1)[0].split("[", 1)[0].lower()
        in ("numpy", "torch", "mujoco", "mujoco-py",
            "robosuite", "bddl", "libero")
        for a in leaf_cmd if not a.startswith("-"))

    # libero_src_copy：<venv python> -c <code>；代码含 shutil.copytree、
    # TOUR_DIR 源字面、runs 区可写副本、symlinks=False 实体化、不静默覆盖。
    assert copy_cmd[0] == f"{vp}/bin/python"
    assert copy_cmd[1] == "-c" and len(copy_cmd) == 3
    copy_code = copy_cmd[2]
    assert "shutil.copytree" in copy_code
    assert probe.TOUR_DIR in copy_code
    assert probe.WRITABLE_TOUR_COPY in copy_code
    assert "symlinks=False" in copy_code
    assert "dirs_exist_ok=True" not in copy_code
    # 只是文件复制：复制代码里不得有 pip/setup 执行语义。
    assert "pip" not in copy_code and "setup.py" not in copy_code

    # 锁内适配（轮次 5，2026-10-24 06:12Z 容器实证 + git 取证）：shim 只许在
    # 可写副本 libero/ 下原子补空 __init__.py（upstream 8f1084e 缺件 canonical
    # 补法）。argv 契约与静态白名单逐条钉死：
    assert shim_cmd[0] == f"{vp}/bin/python"
    assert shim_cmd[1] == "-c" and len(shim_cmd) == 3
    shim_code = shim_cmd[2]
    assert probe.WRITABLE_TOUR_COPY in shim_code
    assert 'os.path.join(copy, "libero", "__init__.py")' in shim_code
    # 代码文本本身不得出现只读/assets 字面（运行时以片段拼 forbidden 再拒）。
    assert "/mnt/assets" not in shim_code
    assert "os.sep + 'mnt' + os.sep + 'assets'" in shim_code
    assert "os.path.realpath" in shim_code
    assert "target.startswith(root + os.sep)" in shim_code
    # 原子空写：mkstemp+fsync+replace、0o644、空串字面；禁非空内容写入。
    assert all(t in shim_code
               for t in ("mkstemp", "fsync", "os.replace", "0o644"))
    assert "handle.write('')" in shim_code
    import re as _re
    assert _re.search(r"\.write\(\s*f?['\"][^'\"]", shim_code) is None
    # 已存在空 marker → no-op 不覆写；非空 → 非零 rc fail-closed。
    assert "existing == ''" in shim_code and "no-op" in shim_code
    assert "SystemExit" in shim_code
    # 禁删树/复制/静默合并/写他径语义。
    assert not any(t in shim_code for t in (
        "rmtree", "copytree", "dirs_exist_ok"))

    # LIBERO editable 规范 -e 指向 runs 区可写副本（不再是只读 TOUR_DIR），
    # 仍 --no-deps（setup.py install_requires=[] 的事实）；构造器本身通用，
    # 仍可接 tmp_path（单测/接缝用）。
    assert "-e" in editable_cmd and "--no-deps" in editable_cmd
    assert editable_cmd[editable_cmd.index("-e") + 1] == \
        probe.WRITABLE_TOUR_COPY
    assert not any(a.startswith("/mnt/assets/") for a in editable_cmd)
    fake_tour = tmp_path / "LIBERO_tour"
    fake_tour.mkdir()
    assert str(fake_tour) in probe.pip_install_libero_cmd(vp, fake_tour)

    # 探针源码静态契约：6 步新名与可写副本根字面量真实接线（副本路径由
    # f"{WRITABLE_TOUR_ROOT}/LIBERO_tour" 单点拼出，防回退 3/5 步旧形/只读 tour）。
    assert "isolated_leaf_deps" in probe_src
    assert "libero_src_copy" in probe_src
    assert "libero_pkg_init_shim" in probe_src
    assert "def libero_pkg_init_shim_cmd" in probe_src
    assert probe.WRITABLE_TOUR_ROOT in probe_src
    assert probe.WRITABLE_TOUR_COPY == \
        f"{probe.WRITABLE_TOUR_ROOT}/LIBERO_tour"
    assert "锁内适配" in probe_src
    # shim 轮次证据字面必须在探针内留痕（ADAPTATION_NOTES 第四条）。
    assert "8f1084e3132a39270c3a13ebe37270a43ece2a01" in probe_src
    assert "MAPPING={}" in probe_src

    # 容器 runner：静态契约——产物与 venv 都在非 root runs 区，不直调共享 pip。
    runner = RUN_SH.read_text(encoding="utf-8")
    assert probe.VENV_PATH in runner
    assert "/home/agent/runs/phase2/task7/stage0" in runner  # 默认报告输出位
    assert "/root/runs" not in runner                        # root 时代旧位清零
    assert "/mnt/venvs/simpraxis/bin/pip" not in runner
    assert "PYTHONPATH" not in runner  # .pth-only 实证成立：禁止全局/导出注入
    assert "sudo" not in runner and "chown" not in runner   # 当前 uid 自建目录
    assert "set -euo pipefail" in runner
    assert probe.TOUR_DIR in runner

    # 图接缝与探针同一对象（路径常量单一来源，禁止接缝里复制字面量）。
    assert t7.VENV_PATH is probe.VENV_PATH or t7.VENV_PATH == probe.VENV_PATH
    assert t7.VENV_PY == probe.venv_python(probe.VENV_PATH)
    assert t7.STAGE0_SCHEMA == probe.REPORT_SCHEMA
    assert t7.DEGRADATION == probe.DEGRADATION

    # 真机零成本 smoke（2026-10-24 锁内适配机制实证；零网络/零装包/不构造仿真）：
    # 在 tmp_path 真跑引导全序列——--without-pip 建子 venv → 写 .pth →
    # 子解释器实测共享 numpy/torch 与 -m pip 自发现。全程不注入 PYTHONPATH。
    import os as _os
    env_snapshot = dict(_os.environ)
    child = tmp_path / "child_venv"
    logs = tmp_path / "logs"
    logs.mkdir(mode=0o700)
    step = probe._bootstrap_isolated_venv(str(child), probe.BASE_PYTHON, logs)
    assert step["rc"] == 0, Path(step["log_path"]).read_text(
        encoding="utf-8")[-2000:]
    assert step["target"] == "venv_create"
    assert "--without-pip" in step["argv"]
    assert dict(_os.environ) == env_snapshot          # 引导不污染宿主环境
    log_text = Path(step["log_path"]).read_text(encoding="utf-8")
    assert probe.SHARED_NUMPY in log_text and probe.SHARED_TORCH in log_text
    assert "pip 2" in log_text  # pip --version 原文落日志

    child_py = probe.venv_python(str(child))
    # purelib 双路动态取（不是硬编码猜的）。
    def _purelib(py_exe: str) -> str:
        q = subprocess.run(probe.purelib_query_cmd(py_exe),
                           capture_output=True, text=True, check=True)
        return q.stdout.strip().splitlines()[-1].strip()

    shared_purelib = _purelib(probe.BASE_PYTHON)
    child_purelib = _purelib(child_py)
    pth = probe.child_pth_path(child_purelib)
    assert pth.is_file() and str(pth).startswith(str(child))
    assert pth.read_text(encoding="utf-8") == probe.shared_pth_text(
        shared_purelib)

    # 不带 PYTHONPATH 的干净 env：.pth-only 必须同时给出 numpy/torch/pip。
    clean_env = {k: v for k, v in env_snapshot.items() if k != "PYTHONPATH"}
    chk = subprocess.run(
        probe.verify_shared_imports_cmd(child_py),
        capture_output=True, text=True, env=clean_env)
    assert chk.returncode == 0, chk.stderr
    # M4：恰好两行且 release 段等于共享 pin（真机逐字输出 1.26.4 /
    # 2.4.1+cu124——raw 行也钉死，证明 M1 归一化路径在真跑中被实际走过）。
    lines = chk.stdout.strip().splitlines()
    assert len(lines) == 2, lines
    assert lines[0].strip() == "1.26.4"
    assert probe._release_version(lines[0].strip()) == probe.SHARED_NUMPY
    assert lines[1].strip() == "2.4.1+cu124"
    assert probe._release_version(lines[1].strip()) == probe.SHARED_TORCH
    assert probe._local_segment(lines[1].strip()) == probe.SHARED_TORCH_LOCAL
    pipv = subprocess.run(probe.pip_version_cmd(child_py),
                          capture_output=True, text=True, env=clean_env)
    assert pipv.returncode == 0, pipv.stderr
    assert "pip" in pipv.stdout and "site-packages/pip" in pipv.stdout

    # 活宿主事实（fail-closed true today）：共享 venv 必须没有这三件分发，
    # 宿主零安装不是口号——importlib.metadata 实证。
    import importlib.metadata as im
    for name in ("robosuite", "bddl", "libero", "robomimic"):
        with pytest.raises(im.PackageNotFoundError):
            im.version(name)


# ---------------------------------------------------------------------------
# ④ Stage3 WJ-Lite 报告 simpraxis.wj_report.v1
# ---------------------------------------------------------------------------

def test_libero_wj_report_contract(tmp_path):
    report = _valid_wj_report()
    assert t7.validate_wj_report(report) == []
    assert report["schema"] == "simpraxis.wj_report.v1"
    assert report["aggregate"] == {"success": 1, "episodes": 2,
                                   "success_rate": 0.5}
    assert report["judge_sources_used"] == ["自补"]
    assert report["gate2_ready"] is False
    assert report["stage0_compat_ref"].endswith("stage0_compat_report.json")

    # 必填字段缺失逐个拒
    for k in t7._WJ_REQUIRED:
        bad = copy.deepcopy(report)
        del bad[k]
        assert t7.validate_wj_report(bad), f"缺 {k} 必须拒"

    # 判分器来源标签：只接受 官方/自补（蓝图：自补必须显式标注）
    bad = copy.deepcopy(report)
    bad["tasks"][0]["judge_source"] = "GPT-4 自报"
    bad["judge_sources_used"] = ["GPT-4 自报"]
    assert any("judge_source" in e for e in t7.validate_wj_report(bad))

    # 数字溯源：metrics_ref 缺路径/缺字段键/坏 sha 均拒
    for patch in (
        lambda b: b["tasks"][0]["metrics_ref"].__setitem__("path", ""),
        lambda b: b["tasks"][0]["metrics_ref"].__setitem__("keys", []),
        lambda b: b["tasks"][0]["metrics_ref"].__setitem__(
            "sha256", "XYZ"),
    ):
        bad = copy.deepcopy(report)
        patch(bad)
        assert any("metrics_ref" in e for e in t7.validate_wj_report(bad))
    bad = copy.deepcopy(report)
    bad["tasks"][0]["metrics_ref"] = None
    assert any("metrics_ref" in e for e in t7.validate_wj_report(bad))

    # 成功率与分子/分母必须一致（禁止报告里写跟计数对不上的率）
    bad = copy.deepcopy(report)
    bad["tasks"][0]["success_rate"] = 0.99
    assert any("success_rate" in e for e in t7.validate_wj_report(bad))
    bad = copy.deepcopy(report)
    bad["aggregate"]["success"] = 2
    assert any("aggregate.success" in e for e in t7.validate_wj_report(bad))

    # 回归（MINOR B）：手构 aggregate episodes=0 + 数字 rate，校验器必须
    # 返回错误字符串而不是 ZeroDivisionError（load 吃的是外部文件）。
    bad = copy.deepcopy(report)
    bad["aggregate"] = {"success": 0, "episodes": 0, "success_rate": 0.5}
    errs = t7.validate_wj_report(bad)  # 抛异常即本测失败
    assert any("aggregate" in e for e in errs), errs
    # 坏类型分母同样不得抛 TypeError；success 上界比较也被类型守卫挡住。
    bad = copy.deepcopy(report)
    bad["aggregate"] = {"success": 1, "episodes": "0", "success_rate": 0.5}
    errs = t7.validate_wj_report(bad)
    assert any("aggregate" in e for e in errs), errs
    # 经 load_wj_report 吃手构文件：fail-closed 抛 WjReportError 而非除零。
    handcrafted = tmp_path / "wj_zero.json"
    bad = copy.deepcopy(report)
    bad["aggregate"] = {"success": 0, "episodes": 0, "success_rate": 0.5}
    handcrafted.write_text(json.dumps(bad, ensure_ascii=False),
                           encoding="utf-8")
    with pytest.raises(t7.WjReportError):
        t7.load_wj_report(handcrafted)

    # 媒体错配：任务引用顶层未声明的路径必须拒
    bad = copy.deepcopy(report)
    bad["tasks"][0]["media"].append("runs/ghost/vanish.mp4")
    assert any("媒体错配" in e for e in t7.validate_wj_report(bad))
    bad = copy.deepcopy(report)
    bad["media"][0]["kind"] = "telepathic"
    assert any("kind" in e for e in t7.validate_wj_report(bad))

    # gate2_ready=true：至少一条 video 媒体
    ready = copy.deepcopy(report)
    ready["gate2_ready"] = True
    assert t7.validate_wj_report(ready) == []
    no_video = copy.deepcopy(ready)
    no_video["media"] = [m for m in no_video["media"] if m["kind"] != "video"]
    # 任务媒体引用随之失配；断言里至少包含 video 缺失规则
    assert any("video" in e for e in t7.validate_wj_report(no_video))

    # Stage0 溯源断链必须拒（Stage3 数字须可回链 Stage0 兼容事实）
    bad = copy.deepcopy(report)
    bad["stage0_compat_ref"] = ""
    assert any("stage0_compat_ref" in e for e in t7.validate_wj_report(bad))

    # LOW-1：每任务局数必须 == suite.n_episodes_per_task（手构 5 vs 实 2）。
    bad = copy.deepcopy(report)
    bad["tasks"][0]["episodes"] = 5
    bad["aggregate"] = {"success": 1, "episodes": 5, "success_rate": 1 / 5}
    errs = t7.validate_wj_report(bad)
    assert any("n_episodes_per_task" in e for e in errs), errs

    # builder 自身 fail-closed：喂非法输入抛 WjReportError，不吐非法件
    with pytest.raises(t7.WjReportError):
        t7.build_wj_report(
            thread_id="x", stage=t7.STAGE1_SPATIAL, suite_name="s",
            n_episodes_per_task=1,
            tasks=[{"task_id": "t", "bddl": "x.bddl", "success": 3,
                    "episodes": 1,
                    "metrics_ref": {"path": "r.json", "keys": ["k"],
                                    "sha256": None},
                    "judge_source": "官方", "media": ["r.json"]}],
            media=[{"kind": "log", "path": "r.json", "sha256": None}],
            stage0_compat_ref="stage0_compat_report.json")

    # LOW-1：tasks=[]（裸 ZeroDivisionError）/ 非数值 episodes（裸 ValueError）
    # 都必须包成 WjReportError。
    with pytest.raises(t7.WjReportError):
        t7.build_wj_report(
            thread_id="x", stage=t7.STAGE1_SPATIAL, suite_name="s",
            n_episodes_per_task=2, tasks=[],
            media=[{"kind": "log", "path": "r.json", "sha256": None}],
            stage0_compat_ref="stage0_compat_report.json")
    with pytest.raises(t7.WjReportError):
        t7.build_wj_report(
            thread_id="x", stage=t7.STAGE1_SPATIAL, suite_name="s",
            n_episodes_per_task=2,
            tasks=[{"task_id": "t", "bddl": "x.bddl", "success": 0,
                    "episodes": "两局",
                    "metrics_ref": {"path": "r.json", "keys": ["k"],
                                    "sha256": None},
                    "judge_source": "官方", "media": ["r.json"]}],
            media=[{"kind": "log", "path": "r.json", "sha256": None}],
            stage0_compat_ref="stage0_compat_report.json")

    # evaluate 接缝：坏件 GraphStateError、缺路径 GraphStateError；好件出摘要
    seam = t7.make_evaluate_seam("t7l-seam-test")
    with pytest.raises(t7.GraphStateError):
        seam({"agent_runs": []})
    good = {"agent_runs": [{}], t7.WJ_REPORT_STATE_KEY: "inline://unread"}
    with pytest.raises(t7.GraphStateError):
        seam(good)  # 路径不存在

    # M5：正路径——合法 wj_report 落 tmp_path，接缝只读校验后返回九字段摘要。
    wj_path = tmp_path / "wj_report.json"
    wj_path.write_text(json.dumps(report, ensure_ascii=False),
                       encoding="utf-8")
    result = seam({t7.WJ_REPORT_STATE_KEY: str(wj_path)})
    assert set(result) == {
        "ok", "thread_id", "wj_report_path", "schema", "stage",
        "aggregate", "judge_sources_used", "gate2_ready", "provenance"}
    assert result["ok"] is True
    assert result["thread_id"] == "t7l-seam-test"
    assert result["wj_report_path"] == str(wj_path)
    assert result["schema"] == "simpraxis.wj_report.v1"
    assert result["stage"] == t7.STAGE1_SPATIAL
    assert result["aggregate"] == {"success": 1, "episodes": 2,
                                   "success_rate": 0.5}
    assert result["judge_sources_used"] == ["自补"]
    assert result["gate2_ready"] is False
    assert result["provenance"].startswith("file:")
