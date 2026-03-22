import json
import logging
import os
import sys
import threading
import time

from database import db
from google_integration import connect_google_account, disconnect_google_account, get_google_calendar_agenda_snapshot

logging.basicConfig(level=logging.ERROR)

PRINT_LOCK = threading.Lock()
GOOGLE_AUTH_LOCK = threading.Lock()
CALENDAR_FETCH_LOCK = threading.Lock()


def emit(tag, payload):
    with PRINT_LOCK:
        print(f"<<{tag}>>{json.dumps(payload)}<<END>>")
        sys.stdout.flush()


def emit_settings():
    emit("SETTINGS", db.get_all_settings())


def emit_integrations():
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
        emit_calendar_agenda(get_google_calendar_agenda_snapshot())
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
        saved_account = db.save_integration_account(payload)
        account_id = saved_account["account_id"]
        account_details = account_event_details(saved_account)
        emit_integrations()
        emit_event(
            "auth_started",
            account_id=account_id,
            message="Opening Google sign-in in your browser.",
            extra=account_details,
        )
        connected_account = connect_google_account(saved_account)
        emit_event(
            "auth_success",
            account_id=account_id,
            message="Google account connected.",
            extra=account_event_details(connected_account),
        )
        emit_integrations()
        threading.Thread(target=run_calendar_agenda_fetch, daemon=True).start()
    except Exception as exc:
        if account_id:
            db.update_integration_account_auth(
                account_id,
                status="needs_auth",
                metadata_patch={"last_auth_error": str(exc)},
            )
        emit_event(
            "auth_error",
            account_id=account_id,
            message=str(exc),
            extra=account_event_details(saved_account if "saved_account" in locals() else payload),
        )
        emit_integrations()
    finally:
        GOOGLE_AUTH_LOCK.release()


def run_google_disconnect(account_id):
    try:
        account = db.get_integration_account(account_id)
        emit_event(
            "disconnect_started",
            account_id=account_id,
            message="Disconnecting Google account.",
            extra=account_event_details(account),
        )
        disconnected_account = disconnect_google_account(account_id)
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
                db.save_integration_account(payload)
                emit_integrations()
                threading.Thread(target=run_calendar_agenda_fetch, daemon=True).start()

            elif line.startswith("DELETE_INTEGRATION_ACCOUNT:"):
                account_id = line.split(":", 1)[1]
                db.delete_integration_account(account_id)
                emit_integrations()
                threading.Thread(target=run_calendar_agenda_fetch, daemon=True).start()

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
