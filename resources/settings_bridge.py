import json
import logging
import os
import sys
import threading
import time
import webbrowser

import requests
from database import db

logging.basicConfig(level=logging.ERROR)

PRINT_LOCK = threading.Lock()
GOOGLE_AUTH_LOCK = threading.Lock()
CALENDAR_FETCH_LOCK = threading.Lock()
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8080"
DEFAULT_GOOGLE_CONNECT_TIMEOUT_SECONDS = 120


def emit(tag, payload):
    with PRINT_LOCK:
        print(f"<<{tag}>>{json.dumps(payload)}<<END>>")
        sys.stdout.flush()


def emit_settings():
    emit("SETTINGS", db.get_all_settings())


def _get_gateway_url():
    value = str(
        db.get_setting("gatewayUrl", "") or os.getenv("GATEWAY_URL", DEFAULT_GATEWAY_URL)
    ).strip()
    return value.rstrip("/") or DEFAULT_GATEWAY_URL


def _get_gateway_local_token():
    return str(
        db.get_setting("gatewayLocalApiToken", "") or os.getenv("GATEWAY_LOCAL_API_TOKEN", "")
    ).strip()


def _gateway_headers():
    headers = {"Content-Type": "application/json"}
    token = _get_gateway_local_token()
    if token:
        headers["X-Local-Token"] = token
    return headers


def _gateway_request(method, path, *, payload=None, params=None, timeout=30):
    response = requests.request(
        method.upper(),
        f"{_get_gateway_url()}{path}",
        headers=_gateway_headers(),
        json=payload,
        params=params,
        timeout=timeout,
    )
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except Exception:
            detail = response.text
        if isinstance(detail, dict):
            message = str(detail.get("message") or detail.get("error") or detail)
        elif isinstance(detail, list):
            message = "; ".join(str(item) for item in detail)
        else:
            message = str(detail or response.text or f"Gateway HTTP {response.status_code}")
        raise RuntimeError(message.strip() or f"Gateway HTTP {response.status_code}")
    if response.content:
        return response.json()
    return {}


def _get_gateway_integrations_snapshot():
    return _gateway_request("GET", "/internal/credentials/google/snapshot", timeout=20)


def _get_gateway_calendar_agenda_snapshot():
    return _gateway_request("GET", "/internal/google/calendar/agenda", timeout=45)


def _list_gateway_google_accounts():
    payload = _gateway_request(
        "GET",
        "/internal/credentials/accounts",
        params={"provider": "google"},
        timeout=20,
    )
    accounts = payload.get("accounts") if isinstance(payload, dict) else []
    return accounts if isinstance(accounts, list) else []


def emit_integrations():
    try:
        emit("INTEGRATIONS", _get_gateway_integrations_snapshot())
    except Exception:
        emit("INTEGRATIONS", db.get_integrations_snapshot())


def build_key_status():
    deepgram = bool(db.get_api_key("deepgram") or os.getenv("DEEPGRAM_API_KEY"))
    anthropic = bool(db.get_api_key("anthropic") or os.getenv("ANTHROPIC_API_KEY"))
    groq = bool(db.get_api_key("groq") or os.getenv("GROQ_API_KEY"))
    return {
        "hasKeys": deepgram or anthropic or groq,
        "haiku": False,
        "perplexity": False,
        "deepgram": deepgram,
        "anthropic": anthropic,
        "groq": groq,
    }


def emit_key_status():
    emit("KEY_STATUS", build_key_status())


def emit_event(event_type, account_id="", provider="google", message="", extra=None):
    payload = {
        "type": event_type,
        "provider": provider,
        "account_id": account_id,
        "message": message,
    }
    if isinstance(extra, dict):
        payload.update(extra)
    emit("INTEGRATION_EVENT", payload)


def emit_calendar_agenda(snapshot):
    emit("CALENDAR_AGENDA", snapshot)


def account_event_details(account):
    if not isinstance(account, dict):
        return {}
    return {
        "account_label": str(account.get("account_label") or ""),
        "email": str(account.get("email") or ""),
        "display_name": str(account.get("display_name") or ""),
    }


def run_calendar_agenda_fetch():
    if not CALENDAR_FETCH_LOCK.acquire(blocking=False):
        return

    try:
        emit_calendar_agenda(_get_gateway_calendar_agenda_snapshot())
    except Exception as exc:
        emit_calendar_agenda(
            {
                "state": "error",
                "generated_at": time.time(),
                "message": str(exc),
                "accounts": [],
                "events": [],
            }
        )
    finally:
        CALENDAR_FETCH_LOCK.release()


def run_google_connect(payload):
    account_id = str(payload.get("account_id") or "").strip()
    if not GOOGLE_AUTH_LOCK.acquire(blocking=False):
        emit_event(
            "auth_error",
            account_id=account_id,
            message="Another Google connection flow is already in progress.",
        )
        return

    try:
        requested_scopes = [
            str(item).strip()
            for item in (payload.get("required_scopes") or [])
            if str(item).strip()
        ]
        selected_tools = [
            str(item).strip()
            for item in (payload.get("selected_tools") or [])
            if str(item).strip()
        ]
        requested_label = str(payload.get("account_label") or "").strip()
        requested_primary = bool(payload.get("is_primary"))
        platform_key = str(payload.get("platform_key") or "workspace").strip() or "workspace"

        existing_accounts = _list_gateway_google_accounts()
        existing_ids = {
            str(item.get("account_id") or "").strip()
            for item in existing_accounts
            if isinstance(item, dict) and str(item.get("account_id") or "").strip()
        }

        start_payload = _gateway_request(
            "POST",
            "/auth/connect/google",
            payload={
                "scopes": requested_scopes,
                "account_label": requested_label or None,
                "selected_tools": selected_tools,
                "is_primary": requested_primary,
                "platform_key": platform_key,
            },
            timeout=20,
        )
        authorize_url = str(start_payload.get("authorize_url") or "").strip()
        if not authorize_url:
            raise RuntimeError("Gateway did not return a Google authorize URL.")

        emit_integrations()
        emit_event(
            "auth_started",
            account_id=account_id,
            message="Opening Google sign-in in your browser.",
            extra=account_event_details(payload),
        )
        webbrowser.open(authorize_url, new=1, autoraise=True)

        deadline = time.time() + max(30, DEFAULT_GOOGLE_CONNECT_TIMEOUT_SECONDS)
        connected_account = None
        while time.time() < deadline:
            time.sleep(1.0)
            accounts = _list_gateway_google_accounts()
            if account_id and not account_id.startswith("draft-"):
                connected_account = next(
                    (
                        item
                        for item in accounts
                        if str(item.get("account_id") or "").strip() == account_id
                        and str(item.get("status") or "").strip() == "active"
                        and bool(item.get("has_refresh_token"))
                    ),
                    None,
                )
            if connected_account is None:
                connected_account = next(
                    (
                        item
                        for item in accounts
                        if str(item.get("account_id") or "").strip() not in existing_ids
                        and str(item.get("status") or "").strip() == "active"
                        and bool(item.get("has_refresh_token"))
                    ),
                    None,
                )
            if connected_account is None and requested_label:
                connected_account = next(
                    (
                        item
                        for item in accounts
                        if str(item.get("account_label") or "").strip() == requested_label
                        and str(item.get("status") or "").strip() == "active"
                        and bool(item.get("has_refresh_token"))
                    ),
                    None,
                )
            if connected_account is not None:
                break

        if connected_account is None:
            raise RuntimeError("Google sign-in did not finish before the timeout window closed.")

        account_id = str(connected_account.get("account_id") or account_id).strip()
        emit_event(
            "auth_success",
            account_id=account_id,
            message="Google account connected.",
            extra=account_event_details(connected_account),
        )
        emit_integrations()
        threading.Thread(target=run_calendar_agenda_fetch, daemon=True).start()
    except Exception as exc:
        emit_event(
            "auth_error",
            account_id=account_id,
            message=str(exc),
            extra=account_event_details(payload),
        )
        emit_integrations()
    finally:
        GOOGLE_AUTH_LOCK.release()


def run_google_disconnect(account_id):
    try:
        account = next(
            (
                item
                for item in _list_gateway_google_accounts()
                if str(item.get("account_id") or "").strip() == str(account_id or "").strip()
            ),
            None,
        )
        emit_event(
            "disconnect_started",
            account_id=account_id,
            message="Disconnecting Google account.",
            extra=account_event_details(account),
        )
        payload = _gateway_request(
            "DELETE",
            f"/internal/credentials/accounts/{account_id}",
            timeout=20,
        )
        disconnected_account = payload.get("account") if isinstance(payload, dict) else {}
        emit_event(
            "disconnect_success",
            account_id=account_id,
            message="Google account disconnected.",
            extra=account_event_details(disconnected_account),
        )
        emit_integrations()
        threading.Thread(target=run_calendar_agenda_fetch, daemon=True).start()
    except Exception as exc:
        emit_event(
            "disconnect_error",
            account_id=account_id,
            message=str(exc),
            extra=account_event_details(locals().get("account")),
        )
        emit_integrations()


def run_save_google_account(payload):
    account_id = str(payload.get("account_id") or "").strip()
    if not account_id or account_id.startswith("draft-"):
        emit_integrations()
        return
    _gateway_request(
        "PATCH",
        f"/internal/credentials/accounts/{account_id}",
        payload={
            "account_label": str(payload.get("account_label") or "").strip() or None,
            "is_primary": bool(payload.get("is_primary")),
            "selected_tools": [
                str(item).strip()
                for item in (payload.get("selected_tools") or [])
                if str(item).strip()
            ],
            "required_scopes": [
                str(item).strip()
                for item in (payload.get("required_scopes") or [])
                if str(item).strip()
            ],
            "platform_key": str(payload.get("platform_key") or "workspace").strip() or "workspace",
        },
        timeout=20,
    )
    emit_integrations()
    threading.Thread(target=run_calendar_agenda_fetch, daemon=True).start()


def run_delete_google_account(account_id):
    _gateway_request(
        "DELETE",
        f"/internal/credentials/accounts/{account_id}/purge",
        timeout=20,
    )
    emit_integrations()
    threading.Thread(target=run_calendar_agenda_fetch, daemon=True).start()


def main():
    """
    Settings + integrations bridge.
    """
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            if line == "GET_ALL_SETTINGS":
                emit_settings()

            elif line == "GET_KEY_STATUS":
                emit_key_status()

            elif line == "GET_ALL_INTEGRATIONS":
                emit_integrations()

            elif line == "GET_CALENDAR_AGENDA":
                threading.Thread(target=run_calendar_agenda_fetch, daemon=True).start()

            elif line.startswith("SAVE_SETTING:"):
                _, key, value = line.split(":", 2)
                db.set_setting(key, value)
                emit_settings()

            elif line.startswith("SAVE_API_KEYS:"):
                payload = json.loads(line.split(":", 1)[1])
                if payload.get("deepgram") is not None:
                    db.set_api_key("deepgram", str(payload.get("deepgram") or "").strip())
                if payload.get("anthropic") is not None:
                    db.set_api_key("anthropic", str(payload.get("anthropic") or "").strip())
                if payload.get("groq") is not None:
                    db.set_api_key("groq", str(payload.get("groq") or "").strip())
                emit_key_status()

            elif line.startswith("SAVE_INTEGRATION_ACCOUNT:"):
                payload = json.loads(line.split(":", 1)[1])
                threading.Thread(target=run_save_google_account, args=(payload,), daemon=True).start()

            elif line.startswith("DELETE_INTEGRATION_ACCOUNT:"):
                account_id = line.split(":", 1)[1]
                threading.Thread(target=run_delete_google_account, args=(account_id,), daemon=True).start()

            elif line.startswith("CONNECT_GOOGLE_ACCOUNT:"):
                payload = json.loads(line.split(":", 1)[1])
                threading.Thread(target=run_google_connect, args=(payload,), daemon=True).start()

            elif line.startswith("DISCONNECT_GOOGLE_ACCOUNT:"):
                account_id = line.split(":", 1)[1]
                threading.Thread(target=run_google_disconnect, args=(account_id,), daemon=True).start()

            elif line.startswith("COSMIC_MAIL_DB:"):
                payload = json.loads(line[len("COSMIC_MAIL_DB:") :])
                request_id = str(payload.get("requestId") or "")
                op = str(payload.get("op") or "")
                reply = {"requestId": request_id, "ok": False, "error": None, "result": None}
                try:
                    if op == "is_baseline_done":
                        reply["ok"] = True
                        reply["result"] = db.cosmic_mail_is_baseline_done(str(payload.get("mailboxId") or ""))
                    elif op == "set_baseline_done":
                        db.cosmic_mail_set_baseline_done(str(payload.get("mailboxId") or ""))
                        reply["ok"] = True
                    elif op == "seed_seen":
                        db.cosmic_mail_seed_inbound_seen(
                            str(payload.get("mailboxId") or ""),
                            payload.get("messageIds") or [],
                        )
                        reply["ok"] = True
                    elif op == "try_mark_seen":
                        reply["ok"] = True
                        reply["result"] = db.cosmic_mail_try_mark_inbound_seen(
                            str(payload.get("mailboxId") or ""),
                            str(payload.get("messageId") or ""),
                        )
                    else:
                        reply["error"] = f"unknown op: {op}"
                except Exception as exc:
                    reply["error"] = str(exc)
                emit("COSMIC_MAIL_DB_REPLY", reply)

        except Exception as exc:
            logging.error(f"Error processing line '{line}': {exc}")


if __name__ == "__main__":
    main()
