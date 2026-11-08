"""Phase 2.7 Task 1（Asset & Template Zoo）机判：AC-T1-1..AC-T1-10（0 付费、0 联网）。

覆盖与纪律：

1. AC-T1-1 四件资产逐件几何闸（水密/绕序/体积/面数/单连通/边界边/非流形边/
   bbox 区间）+ manifest sha256 与磁盘逐件相等；
2. AC-T1-2 **容器场**（``container`` marker，裸跑收集期 deselect、不是 skip）：
   经 ``DockerSiblingBackend``（含 T1.4-G 的 ``extra_ro_mounts``）真起容器跑
   ``probe_scene_build.py``，回执 ``ok=true``/``engine=="genesis"``/``bodies``==2，
   且 ``gpu_visible``/``gpu_mib`` 与 T1.5 Step 0 的实测判定一致；
3. AC-T1-3 cad_guard：8 个非法片段 100% deny（含别名/属性两种写法）+ 4 个合法
   片段 0 误杀 + ``policy`` 写入前钩子真拦；逐条结果落 ``cad_guard_cases.jsonl``；
4. AC-T1-4 构建确定性：脚本连跑两次，逐件 sha256 清单逐字相同；
5. AC-T1-5 仓内无 >10MB 文件、``git ls-files`` 不含网格产物；
6. AC-T1-6 ``resolve_asset`` 正例 + 两条 fail-closed 反例；
7. AC-T1-7 ``extra_ro_mounts``/``gpus`` 白名单校验 + 全仓无生产调用点打开 ``--gpus``；
8. AC-T1-8 默认 argv 与改前 golden 列表逐元素相等（golden 钉在测试里，不从函数反推）；
9. AC-T1-9 来源可核（``origin`` 分支 fail-closed）+ 构建脚本引擎层 0 deny +
   ``probe_report.json`` 列出逐件探测路径；
10. AC-T1-10 keypoints 按 schema 分支 + 两次构建逐位相同；并用合成 manifest 文档
    验证 ``Keypoints`` 的诚实性闸本身（轴对称件不得硬造出口点）。

不写 ``pytest.skip``：容器场靠 marker + 收集期 deselect（Task 0 的 AC-T0-9 同构）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import trimesh

from orchestrator.agentic.image_pin import (AGENTIC_IMAGE, SCENE_BUILD_IMAGE,
                                            SCENE_BUILD_PYTHON)
from orchestrator.agentic.policy import GrantTable, ToolContext
from orchestrator.agentic.tools import cad_guard
from orchestrator.agentic.tools.run import (DockerSiblingBackend,
                                            build_docker_create_argv)
from orchestrator.agentic.tools.write import apply_patch, write_file
from orchestrator.assets import manifest as mf
from orchestrator.assets.scene_spec import Body, SceneSpec
from orchestrator.common import runtime_paths as rp
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

REPO_ROOT = Path(__file__).resolve().parents[2]
# 生产 phase27 根在**导入时点**取一次（此时任何 fixture 的 monkeypatch 未生效）：
# 本 Task 的证据件按计划落生产根（T1.7），与 tmp 隔离的用例并存只能靠快照。
PROD_P27_ROOT = rp.phase27_root()
EVIDENCE = PROD_P27_ROOT / "task1_asset_zoo"
ZOO_ROOT = mf.zoo_root()
BUILD_SCRIPT = REPO_ROOT / "scripts" / "phase27" / "build_asset_zoo.py"
PROBES_DIR = REPO_ROOT / "sandbox" / "probes"

ASSETS = ("vessel/pitcher_standard", "vessel/cup_standard",
          "tableware/plate_standard", "blocks/cube_standard")
# 与 build_asset_zoo.ASSET_SPECS 的 bbox_ranges 同一组区间（机判独立取字面量，
# 不从脚本反推——否则脚本改区间测试会跟着放水）。
BBOX_RANGES = {
    "vessel/pitcher_standard": (("x", 0.06, 0.15), ("y", 0.06, 0.15), ("z", 0.06, 0.15)),
    "vessel/cup_standard": (("x", 0.02, 0.09), ("y", 0.02, 0.09), ("z", 0.06, 0.13)),
    "tableware/plate_standard": (("x", 0.14, 0.18), ("y", 0.14, 0.18), ("z", 0.004, 0.05)),
    "blocks/cube_standard": (("x", 0.045, 0.055), ("y", 0.045, 0.055),
                             ("z", 0.045, 0.055)),
}
MAX_FACES = 5000
TWO_EXCLUSIONS = "-m 'not paid and not kb_integration'"
MESH_SUFFIXES = (".obj", ".stl", ".convex.json")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _edge_counts(mesh: trimesh.Trimesh) -> "tuple[int, int]":
    """boundary / nonmanifold 边数（trimesh 5.x 删了 ``edges_boundary`` 等属性）。"""
    boundary = len(trimesh.grouping.group_rows(mesh.edges_sorted, require_count=1))
    nonmanifold = len(trimesh.grouping.group_rows(mesh.edges_sorted, require_count=3))
    return boundary, nonmanifold


def _zoo_required() -> None:
    """资产库是 Task 1 的产物：缺件即**红**（不是 skip——那是硬规则 7 禁区）。"""
    if not mf.manifest_path().is_file():
        pytest.fail(f"Asset Zoo manifest 不存在：{mf.manifest_path()}"
                    "（先跑 scripts/phase27/build_asset_zoo.py）")


# ---------------------------------------------------------------------------
# AC-T1-1：四件资产逐件几何闸 + sha256
# ---------------------------------------------------------------------------

def test_ac_t1_1_assets_watertight():
    _zoo_required()
    zoo = mf.load_zoo_manifest()
    entries = zoo["assets"]
    assert sorted(entries) == sorted(ASSETS), f"资产集合不符：{sorted(entries)}"
    for name in ASSETS:
        entry = entries[name]
        path = entry.path
        assert path.is_file(), f"{name} 缺件：{path}"
        assert _sha256_file(path) == entry.sha256, f"{name} 磁盘 sha256 与 manifest 不符"
        assert path.stat().st_size == entry.size_bytes, f"{name} size 不符"

        mesh = trimesh.load(path, process=False, force="mesh")
        assert mesh.is_watertight is True, f"{name} 不水密"
        assert mesh.is_winding_consistent is True, f"{name} 绕序不一致"
        assert float(mesh.volume) > 0, f"{name} 体积非正：{mesh.volume}"
        faces = int(len(mesh.faces))
        assert faces == entry.faces, f"{name} 面数与 manifest 不符：{faces} vs {entry.faces}"
        assert faces <= MAX_FACES, f"{name} 面数超限：{faces} > {MAX_FACES}"
        parts = mesh.split(only_watertight=False)
        assert len(parts) == 1, f"{name} 非单连通：拆成 {len(parts)} 块"
        boundary, nonmanifold = _edge_counts(mesh)
        assert boundary == 0, f"{name} boundary_edges={boundary}"
        assert nonmanifold == 0, f"{name} nonmanifold_edges={nonmanifold}"

        extents = np.asarray(mesh.extents, dtype=float)
        for axis, lo, hi in BBOX_RANGES[name]:
            value = float(extents["xyz".index(axis)])
            assert lo <= value <= hi, f"{name} bbox.{axis}={value:.6f} 不在 [{lo}, {hi}]"
        # 归一化口径：最长边恰为目标的邻域、且底面贴 z=0（浮点容差 1e-6）。
        assert abs(float(mesh.bounds[0][2])) < 1e-6, f"{name} 底面不在 z=0：{mesh.bounds[0][2]}"


# ---------------------------------------------------------------------------
# AC-T1-2：容器场——probe_scene_build 对「壶+杯」回执 ok 且与 Step 0 判定一致
# ---------------------------------------------------------------------------

def _step0_segment(name: str) -> dict:
    report = _load(EVIDENCE / "probe_report.json")
    for row in report["step0_container_probe"]:
        if row.get("stage") == name:
            return row
    raise AssertionError(f"probe_report.json 缺 Step 0 分段 {name}")


def _parse_single_json_line(raw: bytes) -> dict:
    lines = [ln for ln in raw.decode("utf-8", "replace").splitlines() if ln.strip()]
    assert len(lines) == 1, f"回执必须恰好一行 JSON，实际 {len(lines)} 行：{lines[:3]}"
    return json.loads(lines[0])


@pytest.mark.container
def test_ac_t1_2_scene_probe_ok(tmp_path):
    _zoo_required()
    workdir = tmp_path / "work"
    workdir.mkdir()
    spec = SceneSpec(engine="genesis", bodies=(
        Body(asset="vessel/pitcher_standard", pos=(0.20, 0.25, 1.00), fixed=True),
        Body(asset="vessel/cup_standard", pos=(0.32, 0.25, 1.00), fixed=True),
    ))
    (workdir / "scene_spec.json").write_text(spec.to_json(), encoding="utf-8")

    backend = DockerSiblingBackend(
        image=SCENE_BUILD_IMAGE, probes_path=str(PROBES_DIR),
        extra_ro_mounts=((str(ZOO_ROOT), "/assets"),))
    result = backend.run([SCENE_BUILD_PYTHON, "/probes/probe_scene_build.py"],
                         cwd=workdir, timeout_s=900)
    assert result.rc == 0, (
        f"探针 rc={result.rc}；stderr 尾部："
        f"{result.stderr[-2000:].decode('utf-8', 'replace')}")
    receipt = _parse_single_json_line(result.stdout)
    assert receipt["schema"] == "simpraxis.scene_build.v1", receipt
    assert receipt["ok"] is True, receipt
    assert receipt["engine"] == "genesis", receipt
    assert [b["asset"] for b in receipt["bodies"]] == [
        "vessel/pitcher_standard", "vessel/cup_standard"], receipt["bodies"]

    # 与 T1.5 Step 0 的判定一致（CPU 路径：gpu_visible=false 且 gpu_mib is None）。
    s2 = _step0_segment("S2_mesh_entity_step")
    assert s2["ok"] is True and s2["backend"] == "cpu", s2
    assert receipt["backend"] == "cpu", receipt
    assert receipt["gpu_visible"] is s2["gpu_visible"] is False, (receipt, s2)
    assert receipt["gpu_mib"] is None and s2["gpu_mib"] is None, (receipt, s2)


def test_ac_t1_2_evidence_receipts_parse():
    """两条场景回执（空场景 / 壶杯）落证据目录且可解析（T1.7）。"""
    for name in ("scene_probe_empty.log", "scene_probe_pitcher_cup.log"):
        path = EVIDENCE / name
        assert path.is_file(), f"缺回执件：{path}"
        receipt = _parse_single_json_line(path.read_bytes())
        assert receipt["schema"] == "simpraxis.scene_build.v1"
        assert receipt["ok"] is True and receipt["backend"] == "cpu", receipt
        assert receipt["gpu_visible"] is False and receipt["gpu_mib"] is None
    empty = _parse_single_json_line((EVIDENCE / "scene_probe_empty.log").read_bytes())
    assert empty["empty"] is True and empty["bodies"] == [], empty
    pc = _parse_single_json_line((EVIDENCE / "scene_probe_pitcher_cup.log").read_bytes())
    assert pc["empty"] is False and len(pc["bodies"]) == 2, pc


# ---------------------------------------------------------------------------
# AC-T1-3：cad_guard 8 deny / 4 allow + policy 写入前拦截
# ---------------------------------------------------------------------------

DENY_CASES = {
    "creation_call":
        "import trimesh\nm = trimesh.creation.revolve([(0, 0), (1, 0), (0, 0)])\n",
    "creation_alias":
        "from trimesh.creation import box\nm = box(extents=(1, 1, 1))\n",
    "direct_construct":
        "from trimesh import Trimesh\nm = Trimesh(vertices=v, faces=f)\n",
    "base_construct":
        "import trimesh\nm = trimesh.base.Trimesh(vertices=v, faces=f)\n",
    "concat":
        "import trimesh\nm = trimesh.util.concatenate([a, b])\n",
    "extrude":
        "import trimesh.geometry as geo\nm = geo.extrude_polygon(poly, 1.0)\n",
    "assign_vertices":
        "import numpy as np\nm.vertices = np.array([[0.0, 0.0, 0.0]])\n",
    "facet_module":
        "import stl\nm = stl.mesh.Mesh(np.zeros(1, dtype=stl.mesh.Mesh.dtype))\n",
}
ALLOW_CASES = {
    "scene_spec":
        "from orchestrator.assets.scene_spec import SceneSpec, Body\n"
        "spec = SceneSpec(engine='genesis', bodies=["
        "Body(asset='vessel/cup_standard', pos=(0, 0, 1.0))])\n",
    "manifest_resolve":
        "from orchestrator.assets.manifest import resolve_asset\n"
        "e = resolve_asset('vessel/pitcher_standard')\n",
    "json_spec":
        "import json\nspec = json.loads(open('scene_spec.json').read())\n",
    "probe_call":
        "run_probe('scene_build')\n",
}


def test_ac_t1_3_cad_guard():
    cases = []
    for name, source in DENY_CASES.items():
        findings = cad_guard.scan_source(source, tier=cad_guard.TIER_MODEL)
        assert findings, f"非法片段 {name} 未被 deny（漏杀）"
        assert cad_guard.deny_reason(source, tier=cad_guard.TIER_MODEL) is not None
        cases.append({"case": name, "tier": "model", "expect": "deny",
                      "rules": [f.rule for f in findings], "denied": True})
    for name, source in ALLOW_CASES.items():
        findings = cad_guard.scan_source(source, tier=cad_guard.TIER_MODEL)
        assert not findings, f"合法片段 {name} 被误杀：{[f.rule for f in findings]}"
        cases.append({"case": name, "tier": "model", "expect": "allow",
                      "rules": [], "denied": False})

    # 引擎层（构建脚本自身）必须过 RULES_ASSEMBLY 一级。
    engine = cad_guard.scan_source(BUILD_SCRIPT.read_text(encoding="utf-8"),
                                   tier=cad_guard.TIER_ENGINE)
    assert not engine, f"构建脚本触发引擎层禁令：{[(f.rule, f.lineno) for f in engine]}"

    ensure_private_dir(EVIDENCE)
    atomic_write_text(
        EVIDENCE / "cad_guard_cases.jsonl",
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in cases),
        mode=0o600)


def test_ac_t1_3_policy_denies_mesh_building_write(tmp_path):
    """写入**前**就 deny：文件不得落盘（零副作用）。"""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    ctx = ToolContext(
        workdir=workdir, thread_id="thr-t1", tracer=None,
        grants=GrantTable(tools=("write_file",), write_roots=("",)),
        nonprod=False)
    result = write_file(ctx, path="pour.py", content=DENY_CASES["creation_alias"])
    assert result.denied, result
    assert "POLICY DENY" in result.output and cad_guard.DENY_MESSAGE.splitlines()[0] in result.output
    assert not (workdir / "pour.py").exists(), "被 deny 的内容不得落盘"
    # 合法内容照常可写（不误杀）。
    ok = write_file(ctx, path="scene.py", content=ALLOW_CASES["scene_spec"])
    assert not ok.denied, ok


def _diff_for(path: str, old_line: str, new_lines: "list[str]") -> str:
    body = "".join(f"+{ln}\n" for ln in new_lines)
    return (f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,1 +1,{1 + len(new_lines)} @@\n"
            f" {old_line}\n{body}")


def test_ac_t1_3_apply_patch_denies_mesh_building(tmp_path):
    """apply_patch 全通道的提交点同样过 cad_guard（D1-5），且零落盘。"""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    target = workdir / "model_code.py"
    target.write_text("x = 1\n", encoding="utf-8")
    ctx = ToolContext(
        workdir=workdir, thread_id="thr-t1", tracer=None,
        grants=GrantTable(tools=("apply_patch", "write_file"),
                          write_roots=("",)),
        nonprod=False)

    bad = _diff_for("model_code.py", "x = 1", [
        "import trimesh",
        "m = trimesh.creation.revolve([(0, 0), (1, 0), (0, 0)])"])
    denied = apply_patch(ctx, patch=json.dumps({"unified_diff": bad}))
    assert denied.denied, denied
    assert cad_guard.DENY_MESSAGE.splitlines()[0] in denied.output, denied.output
    assert target.read_text(encoding="utf-8") == "x = 1\n", "被拒补丁不得落盘"

    good = _diff_for("model_code.py", "x = 1", ["# 合规注释"])
    ok = apply_patch(ctx, patch=json.dumps({"unified_diff": good}))
    assert not ok.denied, ok
    assert target.read_text(encoding="utf-8").splitlines() == [
        "x = 1", "# 合规注释"]


# ---------------------------------------------------------------------------
# AC-T1-4：构建脚本连跑两次，产物 sha256 逐件相同（确定性）
# ---------------------------------------------------------------------------

def _run_build(zoo: Path, evidence: Path, hash_out: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), "--zoo-root", str(zoo),
         "--evidence-dir", str(evidence), "--hash-out", str(hash_out)],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=1800)
    assert proc.returncode == 0, f"构建失败：{proc.stderr[-3000:]}"


def test_ac_t1_4_deterministic_rebuild(tmp_path):
    hashes = []
    for run in (1, 2):
        zoo = tmp_path / f"zoo{run}"
        ev = tmp_path / f"ev{run}"
        out = tmp_path / f"run{run}.sha256"
        _run_build(zoo, ev, out)
        hashes.append(out.read_text(encoding="utf-8"))
    assert hashes[0] == hashes[1], "两次构建产物 sha256 清单不同（非确定性）"
    assert hashes[0].count("\n") == 9, f"清单行数应为 9（4 obj + 4 convex + manifest）：\n{hashes[0]}"
    # 生产证据件同样两轮逐字相同（构建当时留档）。
    r1 = (EVIDENCE / "build_run1.sha256").read_text(encoding="utf-8")
    r2 = (EVIDENCE / "build_run2.sha256").read_text(encoding="utf-8")
    assert r1 == r2, "证据目录 build_run1/build_run2 不一致"


# ---------------------------------------------------------------------------
# AC-T1-5：仓内无 >10MB 文件；git ls-files 不含网格产物
# ---------------------------------------------------------------------------

def test_ac_t1_5_repo_hygiene():
    tracked = subprocess.run(["git", "ls-files"], cwd=str(REPO_ROOT),
                             capture_output=True, text=True, check=True)
    files = [line for line in tracked.stdout.splitlines() if line.strip()]
    assert files, "git ls-files 为空（不在仓库内？）"
    bad = [f for f in files if f.endswith(MESH_SUFFIXES)]
    assert not bad, f"网格产物不得入库：{bad}"
    oversize = []
    for rel in files:
        path = REPO_ROOT / rel
        if path.is_file() and path.stat().st_size > 10 * 1024 * 1024:
            oversize.append((rel, path.stat().st_size))
    assert not oversize, f"仓内出现 >10MB 文件：{oversize}"


# ---------------------------------------------------------------------------
# AC-T1-6：resolve_asset 正例 + fail-closed 反例
# ---------------------------------------------------------------------------

def test_ac_t1_6_resolve_fail_closed():
    _zoo_required()
    entry = mf.resolve_asset("vessel/pitcher_standard")
    assert entry.name == "vessel/pitcher_standard"
    assert entry.path.is_file() and entry.sha256 == _sha256_file(entry.path)
    for bad_name in ("../etc/passwd", "vessel/nope"):
        with pytest.raises(mf.AssetError):
            mf.resolve_asset(bad_name)
    with pytest.raises(mf.AssetError):
        mf.resolve_asset("")
    with pytest.raises(mf.AssetError):
        mf.resolve_asset(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AC-T1-7：extra_ro_mounts / gpus 白名单 + 无生产调用点打开 --gpus
# ---------------------------------------------------------------------------

def _argv_base(tmp_path: Path) -> dict:
    workdir = tmp_path / "wd"
    workdir.mkdir(exist_ok=True)
    return dict(image="sim-praxis/x@sha256:" + "0" * 64,
                argv=["python", "-c", "print(1)"], workdir=workdir,
                venv_path="/mnt/venvs/simpraxis", label="golden")


def test_ac_t1_7_mount_whitelist(tmp_path, monkeypatch):
    # /skills 的允许根来自 phase27_root()（每次调用现读 env）：指到 tmp，
    # 免得单测往生产证据根里造目录。
    monkeypatch.setenv("SIMPRAXIS_PHASE27_ROOT", str(tmp_path / "p27"))
    base = _argv_base(tmp_path)
    zoo = str(mf.zoo_root())
    # 合法：/assets 指向资产库根。
    argv = build_docker_create_argv(
        **base, extra_ro_mounts=((zoo, "/assets"),))
    assert f"{zoo}:/assets:ro" in " ".join(argv), argv

    # 合法：/skills 指向 skills_stage/<sha12>（目录先造出来）。
    staged = tmp_path / "p27" / "skills_stage" / ("a" * 12)
    ensure_private_dir(staged)
    argv2 = build_docker_create_argv(
        **base, extra_ro_mounts=((str(staged), "/skills"),))
    assert f"{staged}:/skills:ro" in " ".join(argv2), argv2

    bad_cases = [
        dict(extra_ro_mounts=(("/tmp", "/assets"),)),                     # 源越界
        dict(extra_ro_mounts=((zoo, "/data"),)),                          # dst 非白名单
        dict(extra_ro_mounts=((zoo, "/assets"), (zoo, "/assets"))),       # 重复
        dict(extra_ro_mounts=((str(Path(zoo) / "nope"), "/assets"),)),    # 源不存在
        dict(extra_ro_mounts=((str(tmp_path / "p27" / "skills_stage" / "short"),
                              "/skills"),)),                              # sha 前缀非法
        dict(extra_ro_mounts=(("/mnt/venvs/simpraxis", "/skills"),)),     # /skills 错根
        dict(gpus="device=abc"),                                          # 非法值
        dict(gpus="all,device=0"),                                        # 非法值
        dict(gpus=["all"]),                                               # 非字符串
    ]
    for bad in bad_cases:
        with pytest.raises(ValueError):
            build_docker_create_argv(**base, **bad)

    good = build_docker_create_argv(**base, gpus="device=0")
    idx = good.index("--gpus")
    assert good[idx + 1] == "device=0"
    assert build_docker_create_argv(**base, gpus="all").count("--gpus") == 1


def test_ac_t1_7_no_production_gpus_call_site():
    """全仓 `gpus=` 关键字实参只允许 None / 透传（BR-11 未授权，无生产调用点）。

    锁内适配（RSI-R0 T1）：R0 自带容器/可信服务 API 把 ``gpus`` 定成 bool 显式
    开关（``matrix.py`` 权限事实用 ``gpus=False`` 数隔离旗标、``trusted.py``
    渲染真验用 ``gpus=True``），这是本期新引入的授权面，不是资产动物园的
    ``docker create`` 接线。保留全仓扫描的强度：具名白名单之外任何常量
    ``gpus=`` 仍判红（下方不放水对照），白名单文件本身也必须仍在真实命中
    （否则白名单陈旧即失败）。
    """
    import ast  # noqa: PLC0415

    allowed_files = {  # 具名白名单：仅这两个 R0 文件，且须各有真实命中
        "orchestrator/rsi_r0/matrix.py",
        "orchestrator/rsi_r0/trusted.py",
    }

    def _allowed(node: ast.AST) -> bool:
        if isinstance(node, ast.Constant) and node.value is None:
            return True
        if isinstance(node, ast.Name) and node.id == "gpus":
            return True
        return isinstance(node, ast.Attribute) and node.attr == "gpus"

    offenders = []
    allowlisted_hits = set()
    for path in sorted((REPO_ROOT / "orchestrator").rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "gpus" or _allowed(kw.value):
                    continue
                if rel in allowed_files:
                    allowlisted_hits.add(rel)
                    continue
                offenders.append((rel, node.lineno, ast.dump(kw.value)[:80]))
    assert not offenders, f"出现非 None/非透传的 gpus= 生产实参：{offenders}"
    # 白名单陈腐即红：两条豁免必须各自仍有真实命中。
    assert allowlisted_hits == allowed_files, allowlisted_hits
    # 构造默认必须是 None（不改默认就等于没接线）。
    import inspect  # noqa: PLC0415

    sig = inspect.signature(build_docker_create_argv)
    assert sig.parameters["gpus"].default is None
    backend_sig = inspect.signature(DockerSiblingBackend.__init__)
    assert backend_sig.parameters["gpus"].default is None


# ---------------------------------------------------------------------------
# AC-T1-8：默认 argv 与改前 golden 逐元素相等
# ---------------------------------------------------------------------------

GOLDEN_DEFAULT_ARGV = [
    "docker", "create", "--rm", "-i", "--network=none",
    "--user", "1000:1000",
    "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges",
    "--read-only",
    "--pids-limit", "256",
    "--memory", "4g",
    "--tmpfs", "/tmp:rw,size=256m,mode=1777",
    "-w", "/work",
    "--label", "simpraxis-agentic-run=golden",
    "-v", "/WD:/work:rw",
    "-v", "/mnt/venvs/simpraxis:/opt/venv:ro",
    "-e", "PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin",
    "-e", "PYTHONUNBUFFERED=1",
    "-e", "HF_HOME=/opt/hf",
    "-e", "TRANSFORMERS_OFFLINE=1",
    "-e", "HF_HUB_OFFLINE=1",
    "sim-praxis/x@sha256:" + "0" * 64,
    "--",
    "python", "-c", "print(1)",
]


def test_ac_t1_8_default_argv_golden(tmp_path):
    base = _argv_base(tmp_path)
    argv = build_docker_create_argv(**base)
    workdir_mount = str(Path(base["workdir"]).resolve()) + ":/work:rw"
    golden = [workdir_mount if seg == "/WD:/work:rw" else seg
              for seg in GOLDEN_DEFAULT_ARGV]
    assert argv == golden, (
        "默认 argv 与改前 golden 不逐元素相等：\n"
        f"actual={argv}\ngolden={golden}")


# ---------------------------------------------------------------------------
# AC-T1-9：来源可核 + 禁止拼接 + probe_report 探测路径
# ---------------------------------------------------------------------------

def test_ac_t1_9_provenance_and_no_concat():
    _zoo_required()
    zoo = mf.load_zoo_manifest()
    # venv 根与构建脚本同一口径（$SIMPRAXIS_VENV 优先，缺省解释器前缀）；
    # 不能用 Path(sys.executable).resolve()——venv 的 python 是指向系统
    # 解释器的软链，resolve 后会把根指到 /usr。
    venv = Path(os.environ.get("SIMPRAXIS_VENV") or sys.prefix)
    for name, entry in zoo["assets"].items():
        assert entry.origin in mf.ORIGINS, f"{name} origin 非法：{entry.origin}"
        assert isinstance(entry.source, str) and entry.source, f"{name} source 缺失"
        if entry.origin == "vendored":
            assert entry.source_sha256, f"{name} vendored 必须登记 source_sha256"
            src = venv / entry.source
            assert src.is_file(), f"{name} 源文件不存在：{src}"
            assert _sha256_file(src) == entry.source_sha256, f"{name} 源文件 sha256 不符"
        else:
            assert entry.source_sha256 is None, (
                f"{name} origin={entry.origin} 时 source_sha256 必须为 None")
            assert entry.source in ("revolve",) or entry.source.startswith(
                "trimesh.creation."), f"{name} 生成器描述可疑：{entry.source}"

    # ② 构建脚本本身过引擎层扫描（0 deny）。
    findings = cad_guard.scan_source(BUILD_SCRIPT.read_text(encoding="utf-8"),
                                     tier=cad_guard.TIER_ENGINE)
    assert not findings, f"build_asset_zoo.py 引擎层违规：{findings}"

    # ③ probe_report.json 存在且列出逐件探测路径与结果。
    report = _load(EVIDENCE / "probe_report.json")
    assert report["schema"] == "simpraxis.asset_build_report.v1"
    rows = {row["name"]: row for row in report["assets"]}
    assert sorted(rows) == sorted(ASSETS), sorted(rows)
    for name, row in rows.items():
        # 收割件必须有「候选探测链」并落在 hit（来源可核）；revolved/primitive
        # 是本地生成器产物（无外部源文件），probes 为空是诚实形态，其来源由
        # manifest 的 source 生成器描述 + AC-T1-4 的确定性重建共同背书。
        if row["origin"] == "vendored":
            assert row["probes"], f"{name} 没有探测记录（来源不可核）"
        for probe in row["probes"]:
            assert probe["result"] in ("hit", "missing", "load_error",
                                       "gate_reject", "bbox_reject"), probe
        assert row["origin"] == zoo["assets"][name].origin
        if row["origin"] == "vendored":
            assert row["probes"][-1]["result"] == "hit", row
    # cad_guard 自检结果一并登记（0 引擎层违规 + 原语使用点可审计）。
    assert report["cad_guard"]["engine_tier_violations"] == 0, report["cad_guard"]


def test_ac_t1_9_manifest_origin_branch_fail_closed():
    """manifest 入口的 origin 分支规则本身必须 fail-closed（L-14）。"""
    root = Path("/nonexistent-zoo-root")
    good = {"relpath": "a.obj", "sha256": "0" * 64, "size_bytes": 1, "faces": 12,
            "watertight": True, "collision_proxy": "coacd", "origin": "vendored",
            "source": "x.glb", "source_sha256": "1" * 64,
            "keypoints": {"schema": "rigid", "bottom_center": [0, 0, 0],
                          "rim_center": None, "pour_edge": None,
                          "handle_grip": None, "spout_defined": False}}
    mf._entry_from_json("a", dict(good), root)   # 正例不抛
    for patch in ({"source_sha256": None}, {"origin": "weird"},
                  {"collision_proxy": "nope"}, {"source": ""},
                  {"relpath": "../escape.obj"}, {"relpath": "/abs.obj"}):
        doc = dict(good)
        doc.update(patch)
        with pytest.raises(mf.AssetError):
            mf._entry_from_json("a", doc, root)
    for origin in ("revolved", "primitive"):
        doc = dict(good, origin=origin, source_sha256=None, source="revolve")
        mf._entry_from_json("a", doc, root)
        with pytest.raises(mf.AssetError):
            mf._entry_from_json("a", dict(doc, source_sha256="1" * 64), root)


# ---------------------------------------------------------------------------
# AC-T1-10：keypoints 按 schema 分支 + 两次构建逐位相同
# ---------------------------------------------------------------------------

def test_ac_t1_10_keypoints_deterministic(tmp_path):
    _zoo_required()
    zoo = mf.load_zoo_manifest()
    manifest_doc = _load(mf.manifest_path())
    by_name = {row["name"]: row for row in manifest_doc["assets"]}

    for name, entry in zoo["assets"].items():
        kp = entry.keypoints
        assert kp.schema in mf.KEYPOINT_SCHEMAS, f"{name} schema 非法：{kp.schema}"
        assert isinstance(kp.spout_defined, bool), name
        mesh = trimesh.load(entry.path, process=False, force="mesh")
        lo, hi = mesh.bounds
        half_z = float((hi[2] - lo[2]) / 2.0)
        assert kp.bottom_center is not None, name
        for axis in range(3):
            assert lo[axis] - 1e-6 <= kp.bottom_center[axis] <= hi[axis] + 1e-6, (
                f"{name} bottom_center 越出 bbox：{kp.bottom_center} vs {mesh.bounds}")
        assert kp.bottom_center[2] - lo[2] <= half_z + 1e-6, f"{name} bottom_center.z 过高"
        if kp.schema == "rigid":
            assert kp.rim_center is None and kp.pour_edge is None
            assert kp.handle_grip is None and kp.spout_defined is False, name
            assert by_name[name]["keypoint_schema"] == "rigid", name
        else:
            assert kp.rim_center is not None, f"{name} vessel 缺 rim_center"
            for axis in range(3):
                assert lo[axis] - 1e-6 <= kp.rim_center[axis] <= hi[axis] + 1e-6, name
            if kp.spout_defined:
                assert kp.pour_edge is not None
                assert float(kp.pour_edge[2]) >= float(kp.rim_center[2])
            else:
                assert kp.pour_edge is None and kp.handle_grip is None, (
                    f"{name} 轴对称件被硬造出口点")
            assert by_name[name]["keypoint_schema"] == "vessel", name

    # 两次构建的 keypoints 逐位相同（确定性；AC-T1-10 ④）。
    kps = []
    for run in (1, 2):
        zoo_dir = tmp_path / f"zoo{run}"
        _run_build(zoo_dir, tmp_path / f"ev{run}", tmp_path / f"h{run}.sha256")
        doc = _load(zoo_dir / "zoo_manifest.json")
        kps.append({row["name"]: row["keypoints"] for row in doc["assets"]})
    assert kps[0] == kps[1], "两次构建的 keypoints 不同（非确定性）"
    for name in ASSETS:
        assert kps[0][name] == by_name[name]["keypoints"], (
            f"{name} 生产 manifest 的 keypoints 与重跑结果不同")


def test_ac_t1_10_keypoints_honesty_gates():
    """合成文档验证诚实性闸本身（RV-05/RV-10）：不得靠真资产全 False 而空转。"""
    base = {"schema": "vessel", "bottom_center": [0.0, 0.0, 0.0],
            "rim_center": [0.0, 0.0, 0.1], "pour_edge": None,
            "handle_grip": None, "spout_defined": False}
    kp = mf.Keypoints.from_json(dict(base))          # 轴对称件合法
    assert kp.spout_defined is False and kp.pour_edge is None
    with pytest.raises(mf.AssetError):               # False 时不得硬造出口点
        mf.Keypoints.from_json(dict(base, pour_edge=[0.0, 0.0, 0.1]))
    with pytest.raises(mf.AssetError):               # False 时不得造把手
        mf.Keypoints.from_json(dict(base, handle_grip=[0.0, 0.0, 0.0]))
    kp_true = mf.Keypoints.from_json(dict(base, spout_defined=True,
                                          pour_edge=[0.0, 0.05, 0.1]))
    assert kp_true.pour_edge == (0.0, 0.05, 0.1)
    with pytest.raises(mf.AssetError):               # vessel 缺 rim_center
        mf.Keypoints.from_json(dict(base, rim_center=None))
    rigid = {"schema": "rigid", "bottom_center": [0.0, 0.0, 0.0],
             "rim_center": None, "pour_edge": None, "handle_grip": None,
             "spout_defined": False}
    assert mf.Keypoints.from_json(dict(rigid)).schema == "rigid"
    with pytest.raises(mf.AssetError):               # 方块不得有口沿
        mf.Keypoints.from_json(dict(rigid, rim_center=[0.0, 0.0, 0.05]))
    with pytest.raises(mf.AssetError):               # rigid 不得 spout_defined=True
        mf.Keypoints.from_json(dict(rigid, spout_defined=True))
    with pytest.raises(mf.AssetError):               # schema 必须显式
        mf.Keypoints.from_json({k: v for k, v in rigid.items() if k != "schema"})


def test_task1_evidence_layout():
    """T1.7 证据树：文件名与 schema 逐项在位（缺件即红）。"""
    expected = ("build_run1.sha256", "build_run2.sha256", "probe_report.json",
                "validate_report.json", "scene_probe_empty.log",
                "scene_probe_pitcher_cup.log", "cad_guard_cases.jsonl")
    for name in expected:
        assert (EVIDENCE / name).is_file(), f"缺证据件：{EVIDENCE / name}"
    validate = _load(EVIDENCE / "validate_report.json")
    assert validate["schema"] == "simpraxis.asset_validate.v1"
    names = {row["name"] for row in validate["assets"]}
    assert names == set(ASSETS), sorted(names)
    # 单真值：收尾命令仍然只有两排除项（不得为本 Task 新增第三项）。
    assert TWO_EXCLUSIONS == "-m 'not paid and not kb_integration'"
    assert "AGENTIC_IMAGE" and AGENTIC_IMAGE.startswith("sim-praxis/agentic:")
    assert SCENE_BUILD_IMAGE.startswith("sim-praxis/sandbox:phase1-task8@sha256:")
    assert SCENE_BUILD_PYTHON == "/usr/bin/python"
