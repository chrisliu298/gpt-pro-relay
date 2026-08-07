"""Chrome app isolation and launch regression tests.

The relay must not share Stable Chrome's macOS bundle identity. A dedicated
user-data directory isolates profile data, but LaunchServices still routes Dock
activation by bundle ID; launching ``com.google.Chrome`` for the relay can make
the user's normal Chrome icon target the automation process.
"""

import plistlib

import pytest

from gpt_pro import cli


def _app(tmp_path, name: str, bundle_id: str, executable: str = "Google Chrome Beta"):
    app = tmp_path / f"{name}.app"
    contents = app / "Contents"
    contents.mkdir(parents=True)
    with (contents / "Info.plist").open("wb") as f:
        plistlib.dump({
            "CFBundleIdentifier": bundle_id,
            "CFBundleExecutable": executable,
        }, f)
    binary = contents / "MacOS" / executable
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    return app


def test_chrome_app_defaults_to_side_by_side_beta(monkeypatch):
    monkeypatch.delenv("GPT_PRO_CHROME_APP", raising=False)
    assert cli.chrome_app_path() == cli.DEFAULT_CHROME_APP


def test_chrome_app_path_accepts_explicit_override(tmp_path, monkeypatch):
    app = tmp_path / "Custom Chrome.app"
    monkeypatch.setenv("GPT_PRO_CHROME_APP", str(app))
    assert cli.chrome_app_path() == app


def test_validate_chrome_app_accepts_side_by_side_beta(tmp_path):
    app = _app(tmp_path, "Google Chrome Beta", "com.google.Chrome.beta")
    assert cli.validate_chrome_app(app) == app


def test_validate_chrome_app_rejects_stable_bundle_identity(tmp_path):
    app = _app(tmp_path, "Google Chrome", "com.google.Chrome")
    with pytest.raises(RuntimeError, match=r"com\.google\.Chrome.*Dock"):
        cli.validate_chrome_app(app)


def test_validate_chrome_app_rejects_missing_app(tmp_path):
    app = tmp_path / "Missing Chrome Beta.app"
    with pytest.raises(RuntimeError, match="does not exist"):
        cli.validate_chrome_app(app)


def test_validate_chrome_app_rejects_missing_executable(tmp_path):
    app = _app(tmp_path, "Google Chrome Beta", "com.google.Chrome.beta")
    (app / "Contents" / "MacOS" / "Google Chrome Beta").unlink()
    with pytest.raises(RuntimeError, match="executable is missing or not runnable"):
        cli.validate_chrome_app(app)


def test_healthy_cdp_rejects_wrong_running_chrome_app(tmp_path, monkeypatch):
    beta = _app(tmp_path, "Google Chrome Beta", "com.google.Chrome.beta")
    stable_executable = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    monkeypatch.setattr(cli, "chrome_app_path", lambda: beta)
    monkeypatch.setattr(cli, "probe_cdp", lambda *_a, **_k: True)
    monkeypatch.setattr(
        cli,
        "_find_chrome_browser_process",
        lambda: (123, f"{stable_executable} --user-data-dir={cli.PROFILE}"),
    )

    with pytest.raises(RuntimeError, match="close-chrome"):
        cli.ensure_shared_chrome_running()


def test_running_beta_must_own_cdp_listener(tmp_path, monkeypatch):
    beta = _app(tmp_path, "Google Chrome Beta", "com.google.Chrome.beta")
    executable = beta / "Contents" / "MacOS" / "Google Chrome Beta"
    monkeypatch.setattr(
        cli,
        "_find_chrome_browser_process",
        lambda: (123, f"{executable} --user-data-dir={cli.PROFILE}"),
    )
    monkeypatch.setattr(cli, "_tcp_listener_pids", lambda _port: {999})

    with pytest.raises(RuntimeError, match="does not own.*19222"):
        cli._require_running_chrome_app(beta, 19222)


def test_running_beta_with_flags_and_owned_listener_is_accepted(tmp_path, monkeypatch):
    beta = _app(tmp_path, "Google Chrome Beta", "com.google.Chrome.beta")
    executable = beta / "Contents" / "MacOS" / "Google Chrome Beta"
    monkeypatch.setattr(
        cli,
        "_find_chrome_browser_process",
        lambda: (123, f"{executable} --restart --user-data-dir={cli.PROFILE}"),
    )
    monkeypatch.setattr(cli, "_tcp_listener_pids", lambda _port: {123})

    cli._require_running_chrome_app(beta, 19222)


def test_invalid_app_fails_before_orphan_kill(tmp_path, monkeypatch):
    missing = tmp_path / "Missing Chrome Beta.app"
    calls = {"kill": 0, "popen": 0}
    monkeypatch.setattr(cli, "chrome_app_path", lambda: missing)
    monkeypatch.setattr(cli, "probe_cdp", lambda *_a, **_k: False)
    monkeypatch.setattr(
        cli,
        "_kill_chrome_orphans",
        lambda: calls.__setitem__("kill", calls["kill"] + 1),
    )
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda *_a, **_k: calls.__setitem__("popen", calls["popen"] + 1),
    )

    with pytest.raises(RuntimeError, match="does not exist"):
        cli.ensure_shared_chrome_running()

    assert calls["kill"] == 0
    assert calls["popen"] == 0


def test_launch_uses_validated_beta_app(tmp_path, monkeypatch):
    beta = _app(tmp_path, "Google Chrome Beta", "com.google.Chrome.beta")
    calls = {"probe": 0, "popen": None}
    monkeypatch.setattr(cli, "chrome_app_path", lambda: beta)
    monkeypatch.setattr(cli, "LaunchLock", _DummyLock)
    monkeypatch.setattr(cli, "_slots_held", lambda **_k: False)
    monkeypatch.setattr(cli, "_kill_chrome_orphans", lambda: None)
    monkeypatch.setattr(cli, "bind_chrome_compositor_surface", lambda: None)
    monkeypatch.setattr(cli.time, "sleep", lambda *_a: None)
    executable = beta / "Contents" / "MacOS" / "Google Chrome Beta"
    monkeypatch.setattr(
        cli,
        "_find_chrome_browser_process",
        lambda: (123, f"{executable} --user-data-dir={cli.PROFILE}"),
    )
    monkeypatch.setattr(cli, "_tcp_listener_pids", lambda _port: {123})

    def probe(*_a, **_k):
        calls["probe"] += 1
        return calls["probe"] > 3

    def popen(argv, **_kwargs):
        calls["popen"] = argv

    monkeypatch.setattr(cli, "probe_cdp", probe)
    monkeypatch.setattr(cli.subprocess, "Popen", popen)

    assert cli.ensure_shared_chrome_running() is True
    assert calls["popen"][:5] == [
        "/usr/bin/open", "-n", "-a", str(beta), "--args",
    ]
    assert f"--user-data-dir={cli.PROFILE}" in calls["popen"]


def test_post_launch_readiness_rejects_wrong_app(tmp_path, monkeypatch):
    beta = _app(tmp_path, "Google Chrome Beta", "com.google.Chrome.beta")
    stable_executable = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    calls = {"probe": 0}
    monkeypatch.setattr(cli, "chrome_app_path", lambda: beta)
    monkeypatch.setattr(cli, "LaunchLock", _DummyLock)
    monkeypatch.setattr(cli, "_slots_held", lambda **_k: False)
    monkeypatch.setattr(cli, "_kill_chrome_orphans", lambda: None)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *_a, **_k: None)
    monkeypatch.setattr(cli.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(
        cli,
        "_find_chrome_browser_process",
        lambda: (456, f"{stable_executable} --user-data-dir={cli.PROFILE}"),
    )

    def probe(*_a, **_k):
        calls["probe"] += 1
        return calls["probe"] > 3

    monkeypatch.setattr(cli, "probe_cdp", probe)

    with pytest.raises(RuntimeError, match="does not match"):
        cli.ensure_shared_chrome_running()


class _DummyLock:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False
