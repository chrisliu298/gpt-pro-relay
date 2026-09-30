"""Recovery of a killed collector must never submit another prompt."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from gpt_pro import cli


@pytest.fixture
def run(monkeypatch, tmp_path):
    rd = tmp_path / "runs" / "r1"
    rd.mkdir(parents=True)
    (rd / "meta.json").write_text(json.dumps({"account": 1}))
    (rd / "conversation.json").write_text(json.dumps({"url": "https://chatgpt.com/c/owned"}))
    monkeypatch.setattr(cli, "RUNS", rd.parent)
    monkeypatch.setattr(cli, "CLAIMS", tmp_path / "claims")
    monkeypatch.setattr(cli, "WORKER_START_GRACE_SECS", 0, raising=False)
    monkeypatch.setattr(cli, "_worker_process_alive", lambda rid, **kw: False)
    return rd


def args(**kw):
    return SimpleNamespace(run_id="r1", timeout=1, poll_interval=0.001, output=None,
                           recover=False, **kw)


async def test_dead_collector_returns_diagnostic_without_terminal_artifact(run, capsys):
    assert await cli.cmd_fetch(args()) == 6
    result = json.loads(capsys.readouterr().err)
    assert result["reason"] == "no_live_worker"
    assert result["recoverable"] is True
    assert not (run / "result.json").exists()


async def test_recovery_spawns_only_collector_and_publishes_answer(run, monkeypatch, capsys):
    spawned = []

    def spawn(rid, rd, *, recover=False):
        spawned.append((rid, recover))
        (rd / "response.md").write_text("existing answer")
        (rd / "result.json").write_text(json.dumps({"status": "ok", "exit_code": 0}))

    monkeypatch.setattr(cli, "_spawn_worker", spawn)
    monkeypatch.setattr(cli, "RunClaim", lambda *a, **kw: pytest.fail("reader must not steal collector claim"))
    a = args()
    a.recover = True
    assert await cli.cmd_fetch(a) == 0
    assert spawned == [("r1", True)]
    assert capsys.readouterr().out == "existing answer"


@pytest.mark.parametrize("url", [None, "https://chatgpt.com/", "https://evil.com/c/owned"])
async def test_unknown_or_foreign_url_never_spawns(run, monkeypatch, url):
    (run / "conversation.json").write_text(json.dumps({"url": url}))
    monkeypatch.setattr(cli, "_spawn_worker", lambda *a, **kw: pytest.fail("must not submit or recover"))
    a = args()
    a.recover = True
    assert await cli.cmd_fetch(a) == 6
    assert not (run / "result.json").exists()


async def test_startup_grace_allows_worker_to_publish(run, monkeypatch):
    monkeypatch.setattr(cli, "WORKER_START_GRACE_SECS", 0.1, raising=False)

    async def finish():
        await asyncio.sleep(0.01)
        (run / "result.json").write_text('{"status":"ok"}')

    task = asyncio.create_task(finish())
    result = await cli._wait_for_result(run, poll_interval=0.001, timeout=1, detect_orphan=True)
    await task
    assert result["status"] == "ok"


async def test_terminal_fetch_never_starts_recovery(run, monkeypatch):
    (run / "response.md").write_text("done")
    (run / "result.json").write_text('{"status":"ok"}')
    monkeypatch.setattr(cli, "_spawn_worker", lambda *a, **kw: pytest.fail("terminal run"))
    a = args()
    a.recover = True
    assert await cli.cmd_fetch(a) == 0


async def test_live_worker_is_only_waited_on(run, monkeypatch):
    monkeypatch.setattr(cli, "_worker_process_alive", lambda rid, **kw: True)
    monkeypatch.setattr(cli, "_spawn_worker", lambda *a, **kw: pytest.fail("live worker"))
    a = args()
    a.timeout = 0
    a.recover = True
    assert await cli.cmd_fetch(a) == 124


async def test_expired_wait_never_spawns_recovery(run, monkeypatch):
    import time

    def slow_probe(rid, **kw):
        time.sleep(0.02)
        return False

    monkeypatch.setattr(cli, "_worker_process_alive", slow_probe)
    monkeypatch.setattr(cli, "_spawn_worker", lambda *a, **kw: pytest.fail("expired fetch must not spawn"))
    a = args()
    a.timeout = 0.01
    a.recover = True
    assert await cli.cmd_fetch(a) == 124


async def test_recovery_worker_does_not_read_or_submit_prompt(run, monkeypatch):
    (run / "prompt.md").write_text("DO NOT SEND THIS")
    monkeypatch.setattr(cli, "configure_account", lambda n: None)
    monkeypatch.setattr(cli, "_browser_run", lambda *a: pytest.fail("must not call submit path"))

    async def recover(rid, rd):
        assert rid == "r1"
        return {"status": "ok", "exit_code": 0}

    monkeypatch.setattr(cli, "_browser_recover", recover)
    assert await cli.cmd_run(SimpleNamespace(run_id="r1"), recover=True) == 0
    assert json.loads((run / "result.json").read_text())["account"] == 1


def test_liveness_recognizes_recovery_worker(monkeypatch):
    seen = []
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **kw: seen.append(argv) or SimpleNamespace(returncode=0))
    assert cli._worker_process_alive("r1")
    assert "(_run|_recover)" in seen[0][2]
