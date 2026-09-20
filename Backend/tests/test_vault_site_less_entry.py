"""A vault entry saved with a title and no site must stay usable and must not
be duplicated.

On 2026-09-17 the user's 'ycombinator' entry (no site URL) was approved,
resolved, and injected into a browser run — then dropped by the run because
it had no domain to key on, so the run asked for the password by hand, and the
save-back offer could not see the existing entry and created a second one.
These pin the two gateway halves of the fix: the lookup says the site is
missing where the model can act on it, and the save-back offer recognises the
login it already holds.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.browser.routes import _vault_already_holds_login  # noqa: E402
from gateway.credentials.encryption import Fernet  # noqa: E402
from gateway.vault.routes import LookupRequest, internal_lookup  # noqa: E402
from gateway.vault.store import POLICY_ALWAYS_ALLOW, VaultStore  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", key)
    from gateway.credentials import encryption

    encryption._CIPHER = None
    vault = VaultStore(tmp_path / "vault.db")
    vault.initialize()
    yield vault
    encryption._CIPHER = None


def _add(store: VaultStore, **overrides):
    item = {
        "title": "ycombinator",
        "site_url": "",
        "username": "usp@example.com",
        "password": "hunter2",
        "totp_seed": "",
        "notes": "",
    }
    item.update(overrides)
    entry = store.add_entry(item)
    store.set_policy(entry["entry_id"], POLICY_ALWAYS_ALLOW)
    return entry


def _lookup(store: VaultStore, query: str):
    runtime = SimpleNamespace(vault_store=store, config=SimpleNamespace(internal_token=""))
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(gateway_runtime=runtime)),
        headers={},
    )
    return asyncio.run(
        internal_lookup(LookupRequest(query=query, task_id="task-1", session_id="s1", channel="desktop:x"), request)
    )


# ── lookup ─────────────────────────────────────────────────────────────────


def test_lookup_warns_when_the_matched_entry_has_no_site(store):
    _add(store)  # title only, matched by title
    result = _lookup(store, "ycombinator")
    assert result["status"] == "ok"
    assert result["site_domain"] == ""
    assert "initial_url" in result["site_warning"]
    assert "Do not ask the user to type the password" in result["site_warning"]


def test_lookup_stays_quiet_when_the_entry_names_its_site(store):
    _add(store, site_url="https://account.ycombinator.com/")
    result = _lookup(store, "ycombinator.com")
    assert result["status"] == "ok"
    assert result["site_domain"] == "account.ycombinator.com"
    assert "site_warning" not in result


# ── save-back dedupe ───────────────────────────────────────────────────────


def test_save_back_recognises_a_title_only_entry_for_the_same_account(store):
    _add(store)  # 'ycombinator', no site, usp@example.com
    assert _vault_already_holds_login(store, domain="account.ycombinator.com", username="usp@example.com") is True


def test_save_back_recognises_a_title_that_is_the_domain_itself(store):
    _add(store, title="ycombinator.com")  # still no site URL
    assert _vault_already_holds_login(store, domain="account.ycombinator.com", username="usp@example.com") is True


def test_save_back_does_not_suppress_a_genuinely_new_login(store):
    _add(store)  # 'ycombinator', usp@example.com, no site
    # Different account on the same site: a real second login, offer it.
    assert _vault_already_holds_login(store, domain="account.ycombinator.com", username="other@example.com") is False
    # Same account, unrelated site: the site-less YC entry must NOT be read
    # as "this account's login everywhere" — that would lose the GitLab save.
    assert _vault_already_holds_login(store, domain="gitlab.com", username="usp@example.com") is False


def test_save_back_needs_an_account_to_compare(store):
    _add(store)
    # With no username on the card there is nothing to match an entry by;
    # refusing to guess is the safe side (an extra offer beats a lost login).
    assert _vault_already_holds_login(store, domain="account.ycombinator.com", username="") is False
