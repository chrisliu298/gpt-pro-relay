"""Regression coverage for the default generation lifetime."""

import math

import pytest

from gpt_pro import cli


def test_default_generation_timeout_is_disabled():
    assert math.isinf(cli.DEFAULT_GENERATION_TIMEOUT)


def test_ask_help_describes_generation_timeout_as_opt_in(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["gpt-pro-relay", "ask", "--help"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert "Default: wait indefinitely" in capsys.readouterr().out
