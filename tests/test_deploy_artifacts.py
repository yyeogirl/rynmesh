import configparser
import os
import plistlib
import re
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENV_DIR = REPO / "deploy" / "env"


def parse_env(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def test_env_examples_exist_and_parse():
    for name in ("office-mac", "hk-server", "sz-node", "sz-tunnel"):
        p = ENV_DIR / f"{name}.env.example"
        assert p.exists(), f"missing {p}"
        assert parse_env(p), f"{p} has no KEY=VALUE lines"


def test_peer_env_required_keys():
    common = {"RYNMESH_REGISTRY_URL", "RYNMESH_AUTO_REGISTER", "RYNMESH_PEER_PORT", "RYNMESH_NODE_NAME"}
    for name in ("office-mac", "hk-server", "sz-node"):
        env = parse_env(ENV_DIR / f"{name}.env.example")
        assert common <= set(env), f"{name} missing one of {common - set(env)}"
        assert env["RYNMESH_REGISTRY_URL"] == "https://registry.rynmesh.ai"
        assert env["RYNMESH_AUTO_REGISTER"] == "1"
        assert env["RYNMESH_NETWORK_ID"] == "rynmesh-main"


def test_role_specific_env():
    hk = parse_env(ENV_DIR / "hk-server.env.example")
    assert hk["RYNMESH_PEER_ENDPOINT"] == "http://203.0.113.10:8791"
    assert hk["RYNMESH_NODE_NAME"] == "hk-server"
    assert "RYNMESH_NETWORK_KEY" in hk  # required on public node

    sz = parse_env(ENV_DIR / "sz-node.env.example")
    assert sz["RYNMESH_PEER_HOST"] == "127.0.0.1"
    assert sz["RYNMESH_PEER_ENDPOINT"] == "http://203.0.113.10:8792"
    assert sz["RYNMESH_NODE_NAME"] == "sz-egress"
    assert sz["RYNMESH_PEER_PORT"] == "8791"      # local bind; tunnel maps 8791->8792 on HK
    assert "RYNMESH_NETWORK_KEY" in sz             # SZ is a public-reachable P2P node

    tun = parse_env(ENV_DIR / "sz-tunnel.env.example")
    assert {"HK_PUBLIC_HOST", "REMOTE_PORT", "LOCAL_PORT", "HK_SSH_TARGET", "SSH_KEY"} <= set(tun)
    assert tun["REMOTE_PORT"] == "8792" and tun["LOCAL_PORT"] == "8791"


SYSTEMD_DIR = REPO / "deploy" / "systemd"


def _load_unit(path: Path) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str  # preserve key case
    cp.read_string(path.read_text(encoding="utf-8"))
    return cp


def test_peer_unit_is_well_formed():
    u = _load_unit(SYSTEMD_DIR / "rynmesh-peer.service")
    assert {"Unit", "Service", "Install"} <= set(u.sections())
    assert "rynmesh-peer" in u["Service"]["ExecStart"]
    assert u["Service"]["EnvironmentFile"] == "/etc/rynmesh/rynmesh-peer.env"
    assert u["Install"]["WantedBy"] == "multi-user.target"
    assert u["Service"]["Restart"] == "on-failure"


def test_sz_tunnel_unit_is_well_formed():
    u = _load_unit(SYSTEMD_DIR / "rynmesh-sz-tunnel.service")
    assert {"Unit", "Service", "Install"} <= set(u.sections())
    exec_start = u["Service"]["ExecStart"]
    assert "autossh" in exec_start
    assert "-R ${HK_PUBLIC_HOST}:${REMOTE_PORT}:127.0.0.1:${LOCAL_PORT}" in exec_start
    assert u["Service"]["EnvironmentFile"] == "/etc/rynmesh/rynmesh-sz-tunnel.env"
    assert "AUTOSSH_GATETIME=0" in u["Service"]["Environment"]
    assert "rynmesh-peer.service" in u["Unit"]["After"]
    assert u["Service"]["Restart"] == "always"
    assert u["Service"]["User"] == "ops"


LAUNCHD_DIR = REPO / "deploy" / "launchd"


def test_plist_parses_and_runs_peer():
    p = LAUNCHD_DIR / "ai.rynmesh.peer.plist"
    data = plistlib.loads(p.read_bytes())
    assert data["Label"] == "ai.rynmesh.peer"
    args = data["ProgramArguments"]
    assert any("rynmesh-peer" in a for a in args)
    assert data.get("RunAtLoad") is True
    assert data.get("KeepAlive") is True


def test_plist_passes_plutil_lint_when_available():
    plutil = shutil.which("plutil")
    if not plutil:  # non-macOS CI
        return
    p = LAUNCHD_DIR / "ai.rynmesh.peer.plist"
    r = subprocess.run([plutil, "-lint", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


BIN_DIR = REPO / "deploy" / "bin"


def test_bringup_scripts_exist_and_are_executable():
    for name in ("bringup-office-mac.zsh", "bringup-linux-node.sh"):
        p = BIN_DIR / name
        assert p.exists(), f"missing {p}"
        if os.name != "nt":
            assert p.stat().st_mode & 0o111, f"{p} not executable"


def test_bringup_office_mac_syntax():
    zsh = shutil.which("zsh")
    if not zsh:
        return
    r = subprocess.run([zsh, "-n", str(BIN_DIR / "bringup-office-mac.zsh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_bringup_linux_node_syntax():
    bash = shutil.which("bash")
    if not bash or os.name == "nt":
        return
    r = subprocess.run([bash, "-n", str(BIN_DIR / "bringup-linux-node.sh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_download_installer_marks_managed_desktop_daemon():
    text = (REPO / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert "RYNMESH_DESKTOP_MODE" in text
    assert "Environment=RYNMESH_DESKTOP_MODE=1" in text


def test_desktop_sidecar_is_signed_and_executed_during_verification():
    build_script = (REPO / "webapp" / "src-tauri" / "scripts" / "build-sidecar.sh").read_text(encoding="utf-8")
    verifier = (REPO / "webapp" / "src-tauri" / "scripts" / "verify-sidecar.sh").read_text(encoding="utf-8")
    ci_workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release_workflow = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    tauri_config = (REPO / "webapp" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")

    assert "--codesign-identity -" in build_script
    assert "--osx-entitlements-file" in build_script
    assert "/health" in verifier
    assert "grep -q 'peer_id'" in verifier
    assert "verify-sidecar.sh" in release_workflow
    assert "rynmesh-peer.entitlements.plist" in tauri_config
    for workflow in (ci_workflow, release_workflow):
        assert "runner: macos-15-intel" in workflow
        assert "runner: macos-15" in workflow
        assert "runner: macos-15-arm64" not in workflow


def test_verify_mesh_syntax_and_expected_nodes():
    p = REPO / "scripts" / "verify_mesh.sh"
    assert p.exists(), "verify_mesh.sh missing"
    if os.name != "nt":
        assert p.stat().st_mode & 0o111, "verify_mesh.sh not executable"
    bash = shutil.which("bash")
    if bash and os.name != "nt":
        r = subprocess.run([bash, "-n", str(p)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    text = p.read_text(encoding="utf-8")
    for n in ("your-mac", "ms-2", "m4-mini", "m2-mini", "hk-server", "sz-egress"):
        assert n in text, f"verify_mesh.sh does not check for {n}"


def test_env_templates_have_update_publisher_key_slot():
    for name in ("office-mac", "hk-server", "sz-node"):
        text = (ENV_DIR / f"{name}.env.example").read_text(encoding="utf-8")
        assert "RYNMESH_UPDATE_PUBLISHER_KEYS" in text, f"{name} missing publisher-key slot"


def test_runbook_exists_and_references_real_artifacts():
    doc = REPO / "docs" / "RYNMESH_MESH_DEPLOYMENT.md"
    assert doc.exists(), "runbook missing"
    text = doc.read_text(encoding="utf-8")
    for ref in (
        "deploy/bin/bringup-office-mac.zsh",
        "deploy/bin/bringup-linux-node.sh",
        "scripts/verify_mesh.sh",
        "GatewayPorts clientspecified",
        "registry.rynmesh.ai",
    ):
        assert ref in text, f"runbook missing reference: {ref}"
    for m in re.findall(r"(?:deploy|scripts)/[\w./-]+", text):
        assert (REPO / m).exists(), f"runbook references missing file: {m}"
