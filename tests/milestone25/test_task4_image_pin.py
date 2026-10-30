"""Phase 2.5 Task4 AC-T4-18：兄弟容器镜像来源精确 pin + 直连构建证据（0 付费）。

不依赖 docker daemon（hermetic）：四方静态对账——
Dockerfile.agentic FROM digest == wheel manifest base_image ==
image_pin.BASE_IMAGE == 证据 base_ref；产物 digest == image_pin.AGENTIC_IMAGE；
构建网络面（--network=none/--pull=false/SIMPRAXIS_USE_PROXY=0/零 wheel）
与运行网络面（--network=none/只读/cap-drop/禁挂 sock）分别断言。
真实离线构建 + 烟囱已由 scripts/phase25/build_agentic_image.sh 执行，
输出落证据件；网络阻断时脚本非零退出（STOP 报 LEAD），禁止松 pin。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from orchestrator.agentic.image_pin import AGENTIC_IMAGE, BASE_IMAGE, SMOLAGENTS_PIN
from orchestrator.agentic.tools.run import build_docker_create_argv

REPO = Path(__file__).resolve().parents[2]
DOCKER_DIR = REPO / "sandbox" / "docker"
DOCKERFILE = DOCKER_DIR / "Dockerfile.agentic"
MANIFEST = DOCKER_DIR / "agentic_wheel_manifest.json"
EVIDENCE = DOCKER_DIR / "agentic_build_evidence.json"
BUILD_SCRIPT = REPO / "scripts" / "phase25" / "build_agentic_image.sh"

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
# M4：docker 引用必须带显式算法前缀；tag@<裸 64hex> 是 invalid reference。
_REF_PIN_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
_OLD_WEAK_PIN_RE = re.compile(r"@[0-9a-f]{64}$")


def _dockerfile_from() -> str:
    m = re.search(r"^FROM\s+(\S+)", DOCKERFILE.read_text(encoding="utf-8"), re.M)
    assert m, "Dockerfile.agentic 必须有 FROM"
    ref = m.group(1)
    assert _DIGEST_RE.search(ref), "FROM 必须 @sha256 digest 精确 pin（禁止浮动 tag）"
    return ref


def test_four_way_digest_consistency():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    from_ref = _dockerfile_from()
    # 基础镜像四方一致。
    assert from_ref == BASE_IMAGE
    assert manifest["base_image"] == BASE_IMAGE
    assert evidence["base_ref"] == BASE_IMAGE
    assert evidence["base_image_id"] == "sha256:" + BASE_IMAGE.split("@sha256:")[1]
    # 产物 digest 与单一源常量一致。
    assert evidence["image_ref_digest"] == AGENTIC_IMAGE
    assert evidence["image_id"] == "sha256:" + AGENTIC_IMAGE.rsplit(
        "@", 1)[1].removeprefix("sha256:")
    assert evidence["image_tag"] == "sim-praxis/agentic:phase25-t4"


def test_dockerfile_has_no_floating_tag_or_install_step():
    text = DOCKERFILE.read_text(encoding="utf-8")
    # 先按反斜杠续行合并 LABEL，再剔除注释/空行。
    joined = re.sub(r"\\\n\s*", " ", text)
    directives = [ln.strip() for ln in joined.splitlines()
                  if ln.strip() and not ln.lstrip().startswith("#")]
    # 零 wheel 形态：禁任何安装/拷贝指令（只允许 FROM/LABEL/USER/WORKDIR）。
    allowed = {"FROM", "LABEL", "USER", "WORKDIR"}
    for line in directives:
        instr = line.split(None, 1)[0].upper()
        assert instr in allowed, f"Dockerfile 出现非白名单指令：{instr}"
        if instr == "FROM":
            assert "@sha256:" in line, "FROM 必须 digest pin，禁止浮动 tag"


def test_wheel_manifest_zero_wheels_with_rationale_and_mount_plane():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["schema"] == "simpraxis.agentic_wheel_manifest.v1"
    assert manifest["wheels"] == [], "D-3 主 venv：镜像内零 wheel"
    assert manifest["zero_wheels_rationale"].strip()
    assert "主 venv" in manifest["zero_wheels_rationale"]
    mp = manifest["mounted_python"]
    assert mp["host_path"] == "/mnt/venvs/simpraxis"
    assert mp["container_path"] == "/opt/venv" and mp["mount_mode"] == "ro"
    assert mp["smolagents_version"] == SMOLAGENTS_PIN == "1.26.0"
    assert "--no-deps" in mp["smolagents_install_mode"]
    assert manifest["build"]["network"] == "none"
    assert manifest["runtime"]["network_default"] == "--network=none"


def test_build_plane_direct_offline_and_enforced_by_script():
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    build_plane = evidence["build_plane"]
    assert build_plane["simpraxis_use_proxy"] == "0"
    assert build_plane["network"] == "none"
    assert build_plane["pull"].startswith("false")
    assert build_plane["wheels_installed"] == 0
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    # 脚本必须强制三个直连/禁网条件（缺项测试必红）。
    assert 'SIMPRAXIS_USE_PROXY' in script and '!= "0"' in script
    assert "--network=none" in script and "--pull=false" in script
    # 证据文件自身记录 Dockerfile/manifest 的 sha256，防替换不被发现。
    assert evidence["dockerfile_sha256"] == hashlib.sha256(
        DOCKERFILE.read_bytes()).hexdigest()
    assert evidence["wheel_manifest_sha256"] == hashlib.sha256(
        MANIFEST.read_bytes()).hexdigest()


def test_runtime_plane_isolated_and_smoke_green():
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    runtime = evidence["runtime_plane"]
    assert runtime["network"] == "--network=none"
    assert runtime["read_only_root"] is True
    assert runtime["cap_drop"] == "ALL"
    assert runtime["no_new_privileges"] is True
    assert runtime["venv_mount"] == "/mnt/venvs/simpraxis:/opt/venv:ro"
    smoke = evidence["runtime_smoke"]
    assert smoke["rc"] == 0
    assert "PY 3.10.12" in smoke["output"]
    assert "SM 1.26.0" in smoke["output"]
    assert evidence["schema"] == "simpraxis.agentic_image_evidence.v1"


def test_build_argv_uses_pinned_image_and_blocks_docker_sock(tmp_path):
    # 生产 argv 构造器：digest 镜像可用、默认禁网、sock 禁挂、venv 只读。
    argv = build_docker_create_argv(
        image=AGENTIC_IMAGE, argv=["python", "-V"], workdir=tmp_path,
        kb_path=None, probes_path=None)
    joined = " ".join(argv)
    assert "--network=none" in argv
    assert "--read-only" in argv
    assert "--cap-drop" in argv and "ALL" in argv
    assert any(a.endswith(":/opt/venv:ro") for a in argv if ":ro" in a)
    assert "/var/run/docker.sock" not in joined
    with pytest.raises(ValueError, match="docker.sock"):
        build_docker_create_argv(
            image=AGENTIC_IMAGE, argv=["x"], workdir=tmp_path,
            kb_path="/var/run/docker.sock")
    # M4：禁止浮动 tag，也禁止 tag@<裸 hex>（invalid reference）；
    # 单一源常量必须锁死 @sha256:<64hex> 终形。
    assert _REF_PIN_RE.search(AGENTIC_IMAGE)


def test_m4_every_image_ref_requires_explicit_sha256_prefix():
    # 常量、证据、Dockerfile、wheel manifest 中所有镜像引用统一终形。
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    refs = [
        AGENTIC_IMAGE, BASE_IMAGE,
        evidence["image_ref_digest"], evidence["base_ref"],
        manifest["base_image"], _dockerfile_from(),
    ]
    for ref in refs:
        assert _REF_PIN_RE.search(ref), f"缺 @sha256: 前缀：{ref}"
        tag, _, tail = ref.rpartition("@")
        rawhash_form = tag + "@" + tail.removeprefix("sha256:")
        # 旧弱正则 @<64hex>$ 会误放裸 hash 形——它必须是红的。
        assert _OLD_WEAK_PIN_RE.search(rawhash_form)
        assert not _REF_PIN_RE.search(rawhash_form)
    # 构建脚本证据写出处必须发 @sha256: 形（M4 返修点）。
    assert '"@sha256:"' in BUILD_SCRIPT.read_text(encoding="utf-8")


@pytest.mark.docker_integration
def test_m4_pinned_ref_resolves_and_runs_offline():
    """M4 硬门：tag@sha256:<id> 引用本机可 inspect 且 --network=none 真跑 true rc0。"""
    import subprocess
    from tests.docker_support import require_docker_integration
    require_docker_integration()
    inspect = subprocess.run(
        ["docker", "image", "inspect", AGENTIC_IMAGE],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    assert inspect.returncode == 0, inspect.stderr.decode("utf-8", "replace")
    # 裸 hash 形必须仍被 docker 拒（invalid reference）。
    tag, _, tail = AGENTIC_IMAGE.rpartition("@")
    rawhash = tag + "@" + tail.removeprefix("sha256:")
    bad = subprocess.run(
        ["docker", "image", "inspect", rawhash],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    assert bad.returncode != 0
    assert b"invalid reference" in bad.stderr
    run = subprocess.run(
        ["docker", "run", "--rm", "--network=none", AGENTIC_IMAGE, "true"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    assert run.returncode == 0, run.stderr.decode("utf-8", "replace")
