import io
import json
import types
from pathlib import Path

import pytest

from gpt_pro import cli


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(cli, "RUNS", runs)
    monkeypatch.setattr(cli, "CLAIMS", tmp_path / "claims")
    monkeypatch.setattr(cli, "ACCOUNT_ROUTER_LOCK", tmp_path / "account-router.lock")
    monkeypatch.setattr(cli, "ACCOUNT_ROUTER_STATE", tmp_path / "account-router.json")
    monkeypatch.setattr(cli, "stderr_jsonl", lambda _obj: None)
    spawned = []
    monkeypatch.setattr(cli, "_spawn_worker", lambda rid, rd: spawned.append((rid, rd)))
    return types.SimpleNamespace(runs=runs, spawned=spawned)


def ask_args(run_id, account="auto"):
    return types.SimpleNamespace(
        run_id=run_id,
        account=account,
        no_wait=True,
        generation_timeout=1.0,
        output=None,
    )


def test_account_config_preserves_account_one_profile_and_isolates_runtime_paths():
    one = cli.account_config(1)
    two = cli.account_config(2)
    three = cli.account_config(3)

    assert one.profile == Path.home() / ".gpt-pro-profile"
    assert two.profile == Path.home() / ".gpt-pro-profile-2"
    assert three.profile == Path.home() / ".gpt-pro-profile-3"
    assert {one.port, two.port, three.port} == {19222, 19223, 19224}
    assert len({one.launch_lock, two.launch_lock, three.launch_lock}) == 3
    assert len({one.activity_lock, two.activity_lock, three.activity_lock}) == 3
    assert len({one.slot_dir, two.slot_dir, three.slot_dir}) == 3


def test_configure_account_switches_the_process_browser_resources(monkeypatch):
    original = {
        name: getattr(cli, name)
        for name in ("PROFILE", "LAUNCH_DEBUG_PORT", "LAUNCH_LOCK", "CHROME_ACTIVITY_LOCK", "SLOT_LOCK_DIR")
    }
    for name, value in original.items():
        monkeypatch.setattr(cli, name, value)

    config = cli.configure_account(3)
    assert cli.PROFILE == config.profile
    assert cli.LAUNCH_DEBUG_PORT == 19224
    assert cli.LAUNCH_LOCK == config.launch_lock
    assert cli.CHROME_ACTIVITY_LOCK == config.activity_lock
    assert cli.SLOT_LOCK_DIR == config.slot_dir


def test_chrome_helpers_resolve_the_configured_port_at_call_time(monkeypatch, tmp_path):
    for name in ("PROFILE", "LAUNCH_DEBUG_PORT", "LAUNCH_LOCK", "CHROME_ACTIVITY_LOCK", "SLOT_LOCK_DIR"):
        monkeypatch.setattr(cli, name, getattr(cli, name))
    monkeypatch.setattr(cli, "STATE", tmp_path / "state")
    cli.configure_account(2)
    seen_ports = []
    monkeypatch.setattr(cli, "chrome_app_path", lambda: tmp_path / "Chrome Beta.app")
    monkeypatch.setattr(cli, "validate_chrome_app", lambda app: app)
    monkeypatch.setattr(cli, "probe_cdp", lambda port, **_kwargs: seen_ports.append(port) or True)
    monkeypatch.setattr(cli, "_require_running_chrome_app", lambda _app, _port: None)

    assert cli.ensure_shared_chrome_running() is False
    assert seen_ports == [19223]


def test_round_robin_is_persistent_and_uniform(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "ACCOUNT_ROUTER_LOCK", tmp_path / "account-router.lock")
    monkeypatch.setattr(cli, "ACCOUNT_ROUTER_STATE", tmp_path / "account-router.json")

    assert [cli.allocate_account() for _ in range(7)] == [1, 2, 3, 1, 2, 3, 1]


@pytest.mark.asyncio
async def test_new_runs_round_robin_and_record_account(isolated, monkeypatch):
    for i in range(1, 7):
        monkeypatch.setattr("sys.stdin", io.StringIO(f"prompt {i}"))
        assert await cli.cmd_ask(ask_args(f"run-{i}")) == 0

    accounts = [
        json.loads((isolated.runs / f"run-{i}" / "meta.json").read_text())["account"]
        for i in range(1, 7)
    ]
    assert accounts == [1, 2, 3, 1, 2, 3]


@pytest.mark.asyncio
async def test_reattach_does_not_advance_round_robin(isolated, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("same"))
    assert await cli.cmd_ask(ask_args("same-run")) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("same"))
    assert await cli.cmd_ask(ask_args("same-run")) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("new"))
    assert await cli.cmd_ask(ask_args("new-run")) == 0

    first = json.loads((isolated.runs / "same-run" / "meta.json").read_text())
    second = json.loads((isolated.runs / "new-run" / "meta.json").read_text())
    assert first["account"] == 1
    assert second["account"] == 2
    assert len(isolated.spawned) == 2


@pytest.mark.asyncio
async def test_explicit_account_bypasses_round_robin(isolated, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("manual"))
    assert await cli.cmd_ask(ask_args("manual", account="3")) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("auto"))
    assert await cli.cmd_ask(ask_args("auto")) == 0

    manual = json.loads((isolated.runs / "manual" / "meta.json").read_text())
    auto = json.loads((isolated.runs / "auto" / "meta.json").read_text())
    assert manual["account"] == 3
    assert auto["account"] == 1


@pytest.mark.asyncio
async def test_worker_uses_account_recorded_in_meta(isolated, monkeypatch):
    run_dir = isolated.runs / "worker"
    run_dir.mkdir()
    (run_dir / "prompt.md").write_text("hello")
    (run_dir / "meta.json").write_text(json.dumps({"account": 2}))
    configured = []
    monkeypatch.setattr(cli, "configure_account", lambda account: configured.append(account))

    async def fake_browser_run(_run_id, _run_dir, _prompt_text):
        return {"status": "ok", "exit_code": 0}

    monkeypatch.setattr(cli, "_browser_run", fake_browser_run)
    assert await cli._run_claimed("worker", run_dir) == 0
    assert configured == [2]
    result = json.loads((run_dir / "result.json").read_text())
    assert result["account"] == 2
