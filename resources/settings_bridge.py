import html
import json
import logging
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests
from database import db

logging.basicConfig(level=logging.ERROR)

PRINT_LOCK = threading.Lock()
GOOGLE_AUTH_LOCK = threading.Lock()
CALENDAR_FETCH_LOCK = threading.Lock()
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8080"
DEFAULT_GOOGLE_CONNECT_TIMEOUT_SECONDS = 120
DEFAULT_GOOGLE_REDIRECT_URI = "http://localhost:8085/"


def emit(tag, payload):
    with PRINT_LOCK:
        print(f"<<{tag}>>{json.dumps(payload)}<<END>>")
        sys.stdout.flush()


def emit_settings():
    emit("SETTINGS", db.get_all_settings())


def _get_saved_setting(*keys):
    for key in keys:
        value = str(db.get_setting(key, "") or "").strip()
        if value:
            return value
    return ""


def _get_saved_cosmic_auth():
    raw = str(db.get_setting("cosmicAuth", "") or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _get_gateway_url():
    auth_payload = _get_saved_cosmic_auth()
    value = (
        _get_saved_setting("gatewayBaseUrl", "gatewayUrl")
        or str(auth_payload.get("gatewayUrl") or "").strip()
        or os.getenv("GATEWAY_URL", DEFAULT_GATEWAY_URL)
    ).strip()
    return value.rstrip("/") or DEFAULT_GATEWAY_URL


def _get_google_redirect_uri():
    value = str(
        _get_saved_setting("googleRedirectUri")
        or os.getenv("GOOGLE_REDIRECT_URI", DEFAULT_GOOGLE_REDIRECT_URI)
    ).strip()
    if value and not value.endswith("/"):
        value = f"{value}/"
    return value or DEFAULT_GOOGLE_REDIRECT_URI


def _get_gateway_local_token():
    auth_payload = _get_saved_cosmic_auth()
    return (
        _get_saved_setting("gatewayApiToken", "gatewayLocalApiToken")
        or str(auth_payload.get("gatewayApiToken") or "").strip()
        or os.getenv("GATEWAY_LOCAL_API_TOKEN", "")
        or os.getenv("GATEWAY_API_TOKEN", "")
    ).strip()


def _parse_redirect_uri(redirect_uri):
    parsed = urlparse(str(redirect_uri or "").strip() or DEFAULT_GOOGLE_REDIRECT_URI)
    host = parsed.hostname or "localhost"
    port = parsed.port or 8085
    path = parsed.path or "/"
    return host, port, path


def _normalize_callback_path(path):
    value = str(path or "").strip() or "/"
    if value != "/" and value.endswith("/"):
        value = value.rstrip("/")
    return value or "/"


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


def _html_page(title, message, *, badge="COSMIC", accent="#8aa7ff"):
    safe_title = html.escape(str(title or "COSMIC").strip() or "COSMIC")
    safe_message = html.escape(str(message or "").strip() or "You can return to the app.")
    safe_badge = html.escape(str(badge or "COSMIC").strip() or "COSMIC")
    return f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>{safe_title}</title>
    <style>
      body {{ background: #050607; color: #f5f7fb; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; min-height: 100vh; display: grid; place-items: center; }}
      .card {{ width: min(92vw, 520px); padding: 32px 28px; border-radius: 24px; background: linear-gradient(180deg, rgba(22,26,34,.96), rgba(10,12,16,.98)); border: 1px solid rgba(255,255,255,.08); box-shadow: 0 24px 80px rgba(0,0,0,.45); }}
      h1 {{ margin: 0 0 8px; font-size: 28px; }}
      p {{ margin: 0; color: rgba(235,240,248,.74); line-height: 1.6; }}
      .badge {{ display: inline-block; margin-bottom: 16px; padding: 6px 10px; border-radius: 999px; background: {accent}; color: #f5f7fb; font-size: 12px; letter-spacing: .04em; text-transform: uppercase; }}
    </style>
  </head>
  <body>
    <div class="card">
      <div class="badge">{safe_badge}</div>
      <h1>{safe_title}</h1>
      <p>{safe_message}</p>
    </div>
  </body>
</html>"""


def _start_google_callback_bridge():
    gateway_url = _get_gateway_url()
    redirect_uri = _get_google_redirect_uri()
    host, port, expected_path = _parse_redirect_uri(redirect_uri)
    expected_path = _normalize_callback_path(expected_path)
    result = {"ok": False, "error": "", "status_code": None}
    completed = threading.Event()
    server = None

    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def _finish(self, status_code, body, *, content_type="text/html; charset=utf-8"):
            body_bytes = body if isinstance(body, bytes) else str(body).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)

        def do_GET(self):
            parsed = urlparse(self.path)
            request_path = _normalize_callback_path(parsed.path)
            if request_path != expected_path:
                self._finish(404, _html_page("Callback Not Found", "This page is reserved for Google sign-in.", badge="404", accent="#4c5a78"))
                return

            params = parse_qs(parsed.query)
            code = str((params.get("code") or [""])[0] or "").strip()
            state = str((params.get("state") or [""])[0] or "").strip()
            error = str((params.get("error") or [""])[0] or "").strip()

            try:
                response = requests.get(
                    f"{gateway_url}/auth/callback/google",
                    params={"code": code, "state": state, "error": error},
                    timeout=30,
                )
                result["status_code"] = response.status_code
                result["ok"] = response.status_code < 400
                if not result["ok"]:
                    detail = response.text
                    try:
                        payload = response.json()
                    except Exception:
                        payload = {}
                    if isinstance(payload, dict):
                        detail = str(payload.get("detail") or payload.get("message") or detail)
                    result["error"] = detail.strip() or f"Gateway HTTP {response.status_code}"
                self._finish(
                    response.status_code,
                    response.content or _html_page("Google Connected", "You can return to the app.").encode("utf-8"),
                    content_type=response.headers.get("Content-Type", "text/html; charset=utf-8"),
                )
            except Exception as exc:
                result["status_code"] = 502
                result["ok"] = False
                result["error"] = f"Failed to complete Google sign-in with COSMIC Gateway: {exc}"
                self._finish(
                    502,
                    _html_page(
                        "COSMIC Couldn't Finish Sign-In",
                        result["error"],
                        badge="Error",
                        accent="#7b3c44",
                    ),
                )
            finally:
                completed.set()
                if server is not None:
                    threading.Thread(target=server.shutdown, daemon=True).start()

    try:
        server = ThreadingHTTPServer((host, port), CallbackHandler)
    except OSError as exc:
        raise RuntimeError(
            f"Could not start the local Google sign-in listener on {host}:{port}. "
            f"Close any other process using that port and try again."
        ) from exc

    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()

    def shutdown():
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass

    return completed, result, shutdown


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


def _coerce_timestamp(value):
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _is_active_google_account(account):
    if not isinstance(account, dict):
        return False
    metadata = account.get("metadata") if isinstance(account.get("metadata"), dict) else {}
    status = str(account.get("status") or "").strip()
    return (
        status in {"active", "connected"}
        and bool(account.get("has_refresh_token") or metadata.get("has_refresh_token"))
    )


def _selected_tools_match(account, selected_tools):
    if not isinstance(account, dict):
        return False
    if not selected_tools:
        return True
    account_tools = {
        str(item).strip()
        for item in (account.get("selected_tools") or [])
        if str(item).strip()
    }
    if not account_tools and isinstance(account.get("metadata"), dict):
        account_tools = {
            str(item).strip()
            for item in (account["metadata"].get("selected_tools") or [])
            if str(item).strip()
        }
    if not account_tools:
        # Some gateway responses can lag tool metadata immediately after OAuth.
        # The callback success is authoritative; do not let delayed metadata keep
        # the desktop progress indicator stuck.
        return True
    return set(selected_tools).issubset(account_tools)


def _find_oauth_completed_account(
    accounts,
    *,
    account_id,
    existing_ids,
    selected_tools,
    started_at,
):
    active_accounts = [account for account in accounts if _is_active_google_account(account)]
    if account_id and not account_id.startswith("draft-"):
        for account in active_accounts:
            if str(account.get("account_id") or "").strip() != account_id:
                continue
            if not _selected_tools_match(account, selected_tools):
                continue
            if _coerce_timestamp(account.get("last_connected_at")) >= started_at - 5:
                return account
            return account
        return None

    for account in active_accounts:
        current_id = str(account.get("account_id") or "").strip()
        if current_id and current_id not in existing_ids and _selected_tools_match(account, selected_tools):
            return account

    updated_accounts = [
        account
        for account in active_accounts
        if _selected_tools_match(account, selected_tools)
        and _coerce_timestamp(account.get("last_connected_at")) >= started_at - 5
    ]
    if updated_accounts:
        return max(updated_accounts, key=lambda item: _coerce_timestamp(item.get("last_connected_at")))
    if len(active_accounts) == 1:
        return active_accounts[0]
    return None


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
        callback_done = None
        callback_result = None
        callback_shutdown = None
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

        flow_started_at = time.time()
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
        callback_done, callback_result, callback_shutdown = _start_google_callback_bridge()

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
        callback_completed = False
        while time.time() < deadline:
            if callback_done is None or not callback_done.is_set():
                time.sleep(0.5)
                continue
            callback_completed = True
            if callback_result and (callback_result.get("error") or not callback_result.get("ok")):
                raise RuntimeError(str(callback_result.get("error") or "Google sign-in failed."))
            time.sleep(1.0)
            accounts = _list_gateway_google_accounts()
            connected_account = _find_oauth_completed_account(
                accounts,
                account_id=account_id,
                existing_ids=existing_ids,
                selected_tools=selected_tools,
                started_at=flow_started_at,
            )
            if connected_account is not None:
                break

        if connected_account is None:
            if not callback_completed:
                if callback_result and callback_result.get("error"):
                    raise RuntimeError(str(callback_result.get("error") or "Google sign-in failed."))
                raise RuntimeError("Google sign-in did not finish before the timeout window closed.")
            if callback_result and callback_result.get("error"):
                raise RuntimeError(str(callback_result.get("error") or "Google sign-in failed."))
            connected_account = payload

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
        callback_shutdown = locals().get("callback_shutdown")
        if callable(callback_shutdown):
            callback_shutdown()
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
            "delete_started",
            account_id=account_id,
            message="Removing Google account from Cosmic.",
            extra=account_event_details(account),
        )
        _gateway_request(
            "DELETE",
            f"/internal/credentials/accounts/{account_id}/purge",
            timeout=20,
        )
        emit_event(
            "delete_success",
            account_id=account_id,
            message="Google account removed.",
            extra=account_event_details(account),
        )
        emit_integrations()
        threading.Thread(target=run_calendar_agenda_fetch, daemon=True).start()
    except Exception as exc:
        emit_event(
            "delete_error",
            account_id=account_id,
            message=str(exc),
            extra=account_event_details(locals().get("account")),
        )
        emit_integrations()


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
