"""Alpha's git credentials must be resolved per call, never baked in.

The previous setup used a personal access token. It expired in late July, the
uspraveen.github.io Pages deploy broke, and COSMIC spent days asking a human to
paste a new one. GitHub App user tokens expire every 8 hours, so anything
written into a file or an env var at startup is wrong before the day is out.

Git therefore asks the Gateway at push time, through this helper.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from agents.alpha_agent import git_credentials  # noqa: E402
from shared.cursor_cli import apply_git_credentials, cursor_cli_env  # noqa: E402


def _stdin(text: str):
    return io.StringIO(text)


class TestHelperProtocol:
    def test_it_answers_github_with_a_live_token(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(git_credentials, "resolve_github_token", lambda: "ghu_live")
        monkeypatch.setattr(sys, "stdin", _stdin("protocol=https\nhost=github.com\n\n"))

        assert git_credentials.main(["get"]) == 0
        out = capsys.readouterr().out
        assert "username=x-access-token" in out
        assert "password=ghu_live" in out

    def test_it_stays_silent_for_other_hosts(self, monkeypatch, capsys) -> None:
        """Answering here would hand a GitHub token to whoever owns that remote."""
        monkeypatch.setattr(git_credentials, "resolve_github_token", lambda: "ghu_live")
        monkeypatch.setattr(sys, "stdin", _stdin("protocol=https\nhost=gitlab.com\n\n"))

        assert git_credentials.main(["get"]) == 0
        assert capsys.readouterr().out == ""

    def test_no_token_produces_silence_not_a_crash(self, monkeypatch, capsys) -> None:
        """Git should report a normal auth failure, not a helper traceback."""
        monkeypatch.setattr(git_credentials, "resolve_github_token", lambda: "")
        monkeypatch.setattr(sys, "stdin", _stdin("protocol=https\nhost=github.com\n\n"))

        assert git_credentials.main(["get"]) == 0
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize("action", ["store", "erase"])
    def test_store_and_erase_exit_cleanly(self, action: str, monkeypatch, capsys) -> None:
        """A non-zero exit from these makes git abort the whole operation."""
        monkeypatch.setattr(sys, "stdin", _stdin("protocol=https\nhost=github.com\n\n"))
        assert git_credentials.main([action]) == 0
        assert capsys.readouterr().out == ""

    def test_a_request_with_no_host_is_still_answered(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(git_credentials, "resolve_github_token", lambda: "ghu_live")
        monkeypatch.setattr(sys, "stdin", _stdin("protocol=https\n\n"))
        assert git_credentials.main(["get"]) == 0
        assert "ghu_live" in capsys.readouterr().out


class TestResolve:
    def test_it_asks_the_gateway_for_a_github_credential(self, monkeypatch) -> None:
        seen: dict[str, object] = {}

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def read(self):
                return json.dumps({"access_token": "ghu_from_gateway"}).encode()

        def fake_urlopen(request, timeout=None):
            seen["url"] = request.full_url
            seen["body"] = json.loads(request.data.decode())
            seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
            seen["timeout"] = timeout
            return _Response()

        monkeypatch.setattr(git_credentials.urllib.request, "urlopen", fake_urlopen)
        token = git_credentials.resolve_github_token(
            gateway_url="http://gw:8080", internal_token="internal"
        )

        assert token == "ghu_from_gateway"
        assert seen["url"] == "http://gw:8080/internal/credentials/resolve"
        assert seen["body"]["provider"] == "github"
        assert seen["headers"]["X-internal-token".lower()] == "internal"

    def test_it_fails_fast_rather_than_hanging_a_push(self, monkeypatch) -> None:
        captured: dict[str, object] = {}

        def fake_urlopen(request, timeout=None):
            captured["timeout"] = timeout
            raise TimeoutError("slow")

        monkeypatch.setattr(git_credentials.urllib.request, "urlopen", fake_urlopen)
        assert git_credentials.resolve_github_token(internal_token="x") == ""
        assert 0 < float(captured["timeout"]) <= 15

    def test_a_gateway_error_returns_empty(self, monkeypatch) -> None:
        def fake_urlopen(request, timeout=None):
            raise OSError("connection refused")

        monkeypatch.setattr(git_credentials.urllib.request, "urlopen", fake_urlopen)
        assert git_credentials.resolve_github_token(internal_token="x") == ""

    def test_no_internal_token_means_no_call_at_all(self, monkeypatch) -> None:
        def explode(*_a, **_k):
            raise AssertionError("must not call the gateway without a token")

        monkeypatch.setattr(git_credentials.urllib.request, "urlopen", explode)
        monkeypatch.delenv("GATEWAY_INTERNAL_TOKEN", raising=False)
        assert git_credentials.resolve_github_token() == ""


class TestEnvironmentInjection:
    def test_cursor_env_installs_the_helper(self, tmp_path) -> None:
        env = cursor_cli_env(tmp_path / "cursor-home", base_env={"PATH": "/usr/bin"})
        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
        assert "git_credentials.py" in env["GIT_CONFIG_VALUE_0"]

    def test_terminal_prompt_is_disabled(self, tmp_path) -> None:
        """Without this a failed credential blocks forever instead of erroring."""
        env = cursor_cli_env(tmp_path / "cursor-home", base_env={})
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_it_does_not_clobber_existing_git_config_entries(self) -> None:
        env = apply_git_credentials(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "user.name",
                "GIT_CONFIG_VALUE_0": "Alpha",
            }
        )
        assert env["GIT_CONFIG_KEY_0"] == "user.name"
        assert env["GIT_CONFIG_KEY_1"] == "credential.helper"
        assert env["GIT_CONFIG_COUNT"] == "2"

    def test_a_corrupt_count_does_not_lose_the_helper(self) -> None:
        env = apply_git_credentials({"GIT_CONFIG_COUNT": "not-a-number"})
        assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
        assert env["GIT_CONFIG_COUNT"] == "1"

    def test_the_users_own_git_config_is_never_written(self, tmp_path) -> None:
        """Everything is env-scoped, so nothing leaks into ~/.gitconfig."""
        env = cursor_cli_env(tmp_path / "cursor-home", base_env={})
        assert not (tmp_path / "cursor-home" / ".gitconfig").exists()
        assert "credential.helper" not in env.get("HOME", "")
