import json
import os
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = {
    "http11probe": ROOT / "scripts/run-http11probe.sh",
    "ab-verify": ROOT / "scripts/run-ab-verify.sh",
    "bench-compare": ROOT / "scripts/run-bench-compare.sh",
    "httparena-bench": ROOT / "scripts/run-httparena-bench.sh",
}


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def plan(name: str, *args: str) -> dict:
    result = run(str(SCRIPTS[name]), "--plan", *args)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_workflow_scripts_are_executable_shell() -> None:
    for script in SCRIPTS.values():
        assert script.stat().st_mode & 0o111
        result = run("bash", "-n", str(script))
        assert result.returncode == 0, result.stderr


def test_http11probe_plan_pins_upstream_and_runs_both_lanes() -> None:
    result = plan("http11probe", "--lane", "both")
    assert result["workflow"] == "http11probe"
    assert result["upstream_commit"] == "d4bc93f2843ac77fcaae71f25069ce1534952e1a"
    assert result["lanes"] == ["native", "compat"]


def test_ab_plan_is_one_approved_self_driving_command() -> None:
    result = plan("ab-verify")
    assert result == {
        "workflow": "ab-verify",
        "requires_approval": True,
        "steps": ["up", "failsafe", "install", "launch", "finish", "marker"],
    }


def test_bench_compare_delegates_defaults_to_authoritative_driver() -> None:
    result = plan("bench-compare")
    assert result == {
        "workflow": "bench-compare",
        "command": ["bash", "bench/peers/compare_servers.sh"],
    }


def test_httparena_plan_reads_profiles_from_metadata() -> None:
    result = plan("httparena-bench")
    metadata = json.loads((ROOT / "bench/httparena/meta.json").read_text())
    assert result["workflow"] == "httparena-bench"
    assert result["requires_approval"] is True
    assert result["profiles"] == metadata["tests"]


def test_cloud_wrappers_stop_before_mutation_without_approval() -> None:
    env = os.environ.copy()
    env.pop("APPROVE_CLOUD", None)
    for name in ("ab-verify", "httparena-bench"):
        result = run(str(SCRIPTS[name]), env=env)
        assert result.returncode != 0
        assert "APPROVE_CLOUD=1" in result.stderr


def test_cloud_wrapper_preflight_is_non_mutating() -> None:
    for name in ("ab-verify", "httparena-bench"):
        result = run(str(SCRIPTS[name]), "--preflight")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"workflow": name, "ready": True}


def test_just_recipes_delegate_to_workflow_scripts() -> None:
    justfile = (ROOT / "justfile").read_text()
    recipes = {
        "http11probe lane='both':": 'scripts/run-http11probe.sh --lane "{{lane}}"',
        "bench-compare:": "scripts/run-bench-compare.sh",
        "ab-verify:": "scripts/run-ab-verify.sh",
        "httparena-bench:": "scripts/run-httparena-bench.sh",
    }
    for header, command in recipes.items():
        pattern = rf"(?m)^{re.escape(header)}\n    {re.escape(command)}\n(?!(?:    |\t))"
        assert re.search(pattern, justfile), header
