"""RSI-G2.1 隔离：argv 硬化、探针判定语义、负控与 closeout 不 vacuous pass。"""
from __future__ import annotations

from orchestrator.rsi_r1 import g21_bridge as G21


def _checks(probe: dict, *, uid: int = 10002, canary: str = "c",
            extra: dict | None = None, forbid: bool = True) -> dict:
    spec = {"uid": str(uid), "canary": canary, "forbid": forbid,
            "extra": extra or {}}
    forbidden = ["private/expected.json"] if forbid else []
    return G21._isolation_checks("candidate", spec, probe, forbidden)


def _good_probe(**overrides) -> dict:
    canary_sha = G21.sha_bytes(b"c")
    probe = {
        "uid": 10002, "gid": 10002,
        "allowed_read": {"ok": True, "value": canary_sha},
        "forbidden_read": {"private/expected.json": {"ok": False, "errno": 13,
                                                     "strerror": "EACCES"}},
        "write_data": {"ok": False, "errno": 30, "strerror": "EROFS"},
        "write_out": {"ok": True, "value": 2},
        "network": {"ok": False, "errno": 101, "strerror": "ENETUNREACH"},
        "routes": "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT",
        "env_keys": ["G21_FORBIDDEN", "HOME", "HOSTNAME", "PATH"],
        "host_env_leak": [], "fd_count": 4,
    }
    probe.update(overrides)
    return probe


def test_docker_argv_is_hardened_and_cpu_only(tmp_path):
    view = tmp_path / "view"
    out = tmp_path / "out"
    view.mkdir()
    out.mkdir()
    argv = G21._docker_argv(image="img@sha256:" + "0" * 64,
                            container="c1", view=view, out=out, uid="10002",
                            forbidden=["private/expected.json"],
                            read_ok=["raw"])
    text = " ".join(argv)
    for flag in ("--network none", "--read-only", "--cap-drop ALL",
                 "--security-opt no-new-privileges", "--user 10002:10002",
                 "--pids-limit", "--memory", f"{view}:/data:ro",
                 f"{out}:/out:rw"):
        assert flag in text
    assert "--gpus" not in text and "nvidia" not in text
    assert "/var/run/docker.sock" not in text
    assert "-e G21_FORBIDDEN=private/expected.json" in text


def test_probe_semantics_pass_and_fail():
    checks = _checks(_good_probe())
    assert all(checks.values()), {k: v for k, v in checks.items() if not v}
    # 私件可读 ⇒ 该条必须失败（AC G21-23）
    leak = _good_probe(forbidden_read={"private/expected.json":
                                       {"ok": True, "value": "NEG"}})
    assert _checks(leak)["forbidden_blocked:private/expected.json"] is False
    # 网络可通 ⇒ 失败
    assert _checks(_good_probe(network={"ok": True}))["network_blocked"] is False
    # 宿主 env 泄入 ⇒ 失败
    assert _checks(_good_probe(host_env_leak=["G21_HOST_CANARY"])
                   )["host_env_not_leaked"] is False
    # 有默认路由 ⇒ 失败
    route = ("Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
             "eth0\t00000000\t0100007F\t0003\t0\t0\t0\t00000000")
    assert _checks(_good_probe(routes=route))["no_default_route"] is False
    # uid 不符 ⇒ 失败
    assert _checks(_good_probe(uid=10005))["uid_applied"] is False


def test_positive_control_reads_authorized_files(tmp_path):
    extra = {"raw": "raw-canary", "expected": "expected-canary"}
    probe = _good_probe(positive_read={"raw": {"ok": True, "value": "raw-cana"},
                                       "expected": {"ok": True,
                                                    "value": "expecte"}})
    checks = _checks(probe, extra=extra)
    assert checks["positive_control:raw"] is True
    assert checks["positive_control:expected"] is True
    probe["positive_read"]["raw"] = {"ok": False, "errno": 13}
    assert _checks(probe, extra=extra)["positive_control:raw"] is False


def test_closeout_never_claims_ready_without_evidence(tmp_path):
    root = tmp_path / "diag"
    (root / "decision").mkdir(parents=True)
    report = G21.closeout(root)
    assert report["overall"] != "G2.1_ENGINEERING_READY_FOR_NEW_DESIGN"
    assert report["future_paid_ready"] is False
    assert report["gaps"]
    assert report["scientific_claim"].startswith("NONE")
