import anthropic
import pytest

from grc_agent import cli


class RaisingAgent:
    def __init__(self, exc):
        self.exc = exc

    def ask(self, prompt):
        raise self.exc


def run_with(monkeypatch, exc):
    monkeypatch.setattr(cli, "Agent", lambda: RaisingAgent(exc))
    return cli.main(["hello"])


def test_missing_credentials_prints_help(monkeypatch, capsys):
    exc = TypeError('"Could not resolve authentication method. Expected one of api_key..."')
    assert run_with(monkeypatch, exc) == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_missing_profile_prints_help(monkeypatch, capsys):
    assert run_with(monkeypatch, anthropic.CredentialsError("Config file not found")) == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_unrelated_type_errors_are_not_swallowed(monkeypatch):
    with pytest.raises(TypeError, match="bug"):
        run_with(monkeypatch, TypeError("a real bug"))
