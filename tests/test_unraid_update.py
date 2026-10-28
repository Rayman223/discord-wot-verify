"""Exercise deployment failures without accessing Docker or the user's server."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/update-unraid.sh"

FAKE = r"""
import json, os, pathlib, sys
tool = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
root = pathlib.Path(os.environ["PROJECT_DIR"])
state_path = root / "state.json"
state = json.loads(state_path.read_text())
state["calls"].append([tool, *args])
mode = os.environ.get("FAIL_MODE", "")
code = 0
output = ""
if tool == "git":
    if args[0] == "branch":
        output = "main"
    elif args[0] == "pull" and mode == "pull":
        code = 1
    elif args[0] == "pull":
        (root / "wot_verify").mkdir(exist_ok=True)
        (root / "wot_verify" / "updated.py").write_text("VALUE = 1\n")
elif args[:2] == ["container", "ls"]:
    if mode == "docker_unknown" and any(c["version"] == "new" for c in state["containers"].values()):
        code = 1
    else:
        output = "\n".join(state["containers"])
elif args[:2] == ["container", "inspect"]:
    code = 0 if args[-1] in state["containers"] else 1
elif args[0] == "inspect":
    if ".Source" in args[2]:
        output = "/different/data" if mode == "mount" else str(root / "data")
    else:
        output = "true" if state["containers"][args[-1]]["running"] else "false"
elif args[0] == "build":
    code = 1 if mode == "build" else 0
elif args[0] == "stop":
    state["containers"][args[-1]]["running"] = False
elif args[0] == "rename":
    if args[2] in state["containers"]:
        code = 1
    else:
        state["containers"][args[2]] = state["containers"].pop(args[1])
elif args[0] == "run":
    if mode == "run":
        code = 1
    else:
        name = args[args.index("--name") + 1]
        state["containers"][name] = {"running": mode != "health", "version": "new"}
        (root / "data" / "schema").write_text("new schema")
elif args[0] == "exec":
    code = 1 if mode in {"health", "docker_unknown", "remove_failure"} else 0
elif args[0] == "rm":
    name = args[-1]
    if name not in state["containers"] or mode == "remove_failure":
        code = 1
    else:
        del state["containers"][name]
elif args[0] == "start":
    state["containers"][args[-1]]["running"] = True
state_path.write_text(json.dumps(state))
if output:
    print(output)
sys.exit(code)
"""


@pytest.fixture
def deployment(tmp_path):
    if not shutil.which("flock"):
        pytest.skip("flock unavailable")
    project = tmp_path / "project with spaces"
    project.mkdir()
    (project / ".env").write_text("DISCORD_TOKEN=private-token\n")
    (project / "data").mkdir()
    (project / "data/schema").write_text("old schema")
    (project / "state.json").write_text(
        json.dumps(
            {
                "containers": {"wot-verify": {"running": True, "version": "old"}},
                "calls": [],
            }
        )
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in ("git", "docker"):
        executable = binaries / name
        executable.write_text(f"#!{sys.executable}\n" + FAKE)
        executable.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{binaries}:{os.environ['PATH']}",
        "PROJECT_DIR": str(project),
        "HEALTH_TIMEOUT": "1",
    }
    return project, env


def run(deployment, mode=""):
    project, env = deployment
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        check=False,
        env={**env, "FAIL_MODE": mode},
        text=True,
        capture_output=True,
        timeout=20,
    )
    state = json.loads((project / "state.json").read_text())
    assert "private-token" not in result.stdout + result.stderr
    return result, state


def test_update_keeps_data_backup_and_replaces_only_after_build(deployment):
    project, _ = deployment
    result, state = run(deployment)
    assert result.returncode == 0, result.stderr
    assert state["containers"] == {"wot-verify": {"running": True, "version": "new"}}
    calls = [call[1] for call in state["calls"] if call[0] == "docker"]
    assert calls.index("build") < calls.index("stop") < calls.index("run") < calls.index("exec")
    assert list((project / ".deploy-backups").glob("*/data.tar"))
    assert (project / "data/schema").read_text() == "new schema"
    assert (project / ".env").read_text() == "DISCORD_TOKEN=private-token\n"
    assert (project / "wot_verify/updated.py").stat().st_mode & 0o777 == 0o644
    assert (project / "wot_verify").stat().st_mode & 0o777 == 0o755
    assert (project / ".unraid-update.lock").stat().st_mode & 0o777 == 0o600
    archive = next((project / ".deploy-backups").glob("*/data.tar"))
    assert archive.stat().st_mode & 0o777 == 0o600
    assert archive.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("mode", ["pull", "build", "mount"])
def test_preparation_failure_never_stops_existing_bot(deployment, mode):
    project, _ = deployment
    result, state = run(deployment, mode)
    assert result.returncode != 0
    assert state["containers"]["wot-verify"] == {"running": True, "version": "old"}
    assert not any(call[:2] == ["docker", "stop"] for call in state["calls"])
    assert (project / "data/schema").read_text() == "old schema"


@pytest.mark.parametrize("mode", ["health", "run"])
def test_failed_replacement_restores_old_container_and_database(deployment, mode):
    project, _ = deployment
    result, state = run(deployment, mode)
    assert result.returncode != 0
    assert state["containers"] == {"wot-verify": {"running": True, "version": "old"}}
    assert (project / "data/schema").read_text() == "old schema"
    assert list((project / ".deploy-backups").glob("*/failed-data"))


def test_second_update_is_rejected_by_lock(deployment):
    import fcntl

    project, _ = deployment
    with (project / ".unraid-update.lock").open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result, state = run(deployment)
    assert result.returncode != 0
    assert "déjà en cours" in result.stderr
    assert not state["calls"]


def test_incomplete_backup_restarts_old_bot_without_restoring_partial_archive(deployment):
    project, env = deployment
    tar = Path(env["PATH"].split(os.pathsep)[0]) / "tar"
    tar.write_text(
        f"#!{sys.executable}\nimport pathlib, sys\n"
        "pathlib.Path(sys.argv[2]).write_text('incomplete archive')\n"
        "sys.exit(1)\n"
    )
    tar.chmod(0o755)
    result, state = run(deployment)
    assert result.returncode != 0
    assert state["containers"] == {"wot-verify": {"running": True, "version": "old"}}
    assert (project / "data/schema").read_text() == "old schema"
    assert list((project / ".deploy-backups").glob("*/data.tar.partial"))
    assert not list((project / ".deploy-backups").glob("*/failed-data"))


@pytest.mark.parametrize("mode", ["docker_unknown", "remove_failure"])
def test_uncertain_stop_never_restores_data(deployment, mode):
    project, _ = deployment
    result, state = run(deployment, mode)
    assert result.returncode != 0
    assert state["containers"]["wot-verify"]["version"] == "new"
    assert (project / "data/schema").read_text() == "new schema"
    assert not list((project / ".deploy-backups").glob("*/failed-data"))
    assert not any(c[:2] == ["docker", "start"] for c in state["calls"])
    assert "Aucune restauration des données effectuée" in result.stderr
