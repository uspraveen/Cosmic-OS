import json
import os
import sys
from typing import Any

import requests

from database import db


DEBUG = True
CURRENT_SESSION_ID = None
ANTHROPIC_API_VERSION = "2023-06-01"
LOCAL_HAIKU_MODEL = os.getenv("LOCAL_HAIKU_MODEL", "claude-haiku-4-5")
LOCAL_HAIKU_MAX_TOKENS = int(os.getenv("LOCAL_HAIKU_MAX_TOKENS", "16000"))
LOCAL_HAIKU_THINKING_BUDGET = int(os.getenv("LOCAL_HAIKU_THINKING_BUDGET_TOKENS", "10000"))
DIRECT_ASSISTANT_SYSTEM_PROMPT = (
    "You are Cosmic, a helpful, accurate personal AI assistant for a single user.\n"
    "Give direct, high-signal answers. Use Markdown when it improves readability.\n"
    "Stay practical and concise unless the user explicitly asks for depth.\n"
)


def dlog(*args: Any):
    if DEBUG:
        print("[haiku]", *args, file=sys.stderr, flush=True)


try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def emit_chunk(chunk: str, *, done: bool) -> None:
    data = json.dumps({"chunk": chunk, "done": done}, ensure_ascii=False)
    print(f"<<CHUNK>>{data}<<END>>", flush=True)


def build_messages(history_msgs: list[dict[str, Any]]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for msg in history_msgs:
        role = str(msg.get("role") or "").strip()
        content = str(msg.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n\n" + content
            continue
        messages.append({"role": role, "content": content})
    return messages


def iter_sse_events(response: requests.Response):
    event_name = "message"
    data_lines: list[str] = []
    for raw_line in response.iter_lines(decode_unicode=True):
        if raw_line is None:
            continue
        line = raw_line.rstrip("\r")
        if not line:
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip() or "message"
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if data_lines:
        yield event_name, "\n".join(data_lines)


def stream_haiku_response(prompt: str):
    global CURRENT_SESSION_ID

    api_key = db.get_api_key("anthropic") or os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        emit_chunk("Error: No Anthropic API key found. Please check Settings.", done=True)
        return

    if not CURRENT_SESSION_ID:
        title = (prompt[:30] + "..") if len(prompt) > 30 else prompt
        CURRENT_SESSION_ID = db.create_session(title=title)
        print(f"<<SESSION_SET>>{json.dumps(CURRENT_SESSION_ID)}<<END>>", flush=True)

    db.add_message(CURRENT_SESSION_ID, "user", prompt)
    history_msgs = db.get_pruned_history(CURRENT_SESSION_ID)
    messages = build_messages(history_msgs)

    payload = {
        "model": LOCAL_HAIKU_MODEL,
        "max_tokens": LOCAL_HAIKU_MAX_TOKENS,
        "stream": True,
        "thinking": {"type": "enabled", "budget_tokens": LOCAL_HAIKU_THINKING_BUDGET},
        "system": DIRECT_ASSISTANT_SYSTEM_PROMPT,
        "messages": messages,
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_API_VERSION,
        "content-type": "application/json",
        "accept": "text/event-stream",
    }

    full_response = ""
    try:
        with requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=payload,
            stream=True,
            timeout=90,
        ) as response:
            response.raise_for_status()
            for event_name, data in iter_sse_events(response):
                if event_name == "ping" or not data:
                    continue
                parsed = json.loads(data)
                payload_type = str(parsed.get("type") or "").strip()
                if payload_type == "error":
                    error = parsed.get("error")
                    if isinstance(error, dict):
                        message = str(error.get("message") or "").strip()
                    else:
                        message = "Anthropic stream error"
                    raise RuntimeError(message)
                if payload_type != "content_block_delta":
                    continue
                delta = parsed.get("delta")
                if not isinstance(delta, dict):
                    continue
                if str(delta.get("type") or "").strip() != "text_delta":
                    continue
                text = str(delta.get("text") or "")
                if not text:
                    continue
                emit_chunk(text, done=False)
                full_response += text

        if full_response:
            db.add_message(CURRENT_SESSION_ID, "assistant", full_response)
        emit_chunk("", done=True)
    except Exception as exc:
        error_msg = f"API Error: {exc}"
        db.add_message(CURRENT_SESSION_ID, "assistant", error_msg)
        emit_chunk(error_msg, done=True)


def handle_command(cmd: str):
    global CURRENT_SESSION_ID

    if cmd.startswith("PROMPT:"):
        stream_haiku_response(cmd[7:].strip())
    elif cmd == "CHECK_KEYS":
        haiku = db.get_api_key("anthropic")
        pplx = db.get_api_key("perplexity")
        deepgram = db.get_api_key("deepgram")
        groq = db.get_api_key("groq")
        status = {
            "hasKeys": bool(haiku or pplx or deepgram or groq),
            "haiku": bool(haiku),
            "perplexity": bool(pplx),
            "deepgram": bool(deepgram),
            "groq": bool(groq),
            "anthropic": bool(haiku),
        }
        print(f"<<KEY_STATUS>>{json.dumps(status)}<<END>>", flush=True)
    elif cmd.startswith("SAVE_KEYS:"):
        try:
            payload = json.loads(cmd[10:])
            if payload.get("anthropic"):
                db.set_api_key("anthropic", payload["anthropic"])
            if payload.get("perplexity"):
                db.set_api_key("perplexity", payload["perplexity"])
            if payload.get("deepgram"):
                db.set_api_key("deepgram", payload["deepgram"])
            if payload.get("groq"):
                db.set_api_key("groq", payload["groq"])
            print("<<KEY_SAVED>>true<<END>>", flush=True)
            handle_command("CHECK_KEYS")
        except Exception:
            return
    elif cmd == "LIST_SESSIONS":
        sessions = db.list_sessions()
        print(f"<<SESSIONS>>{json.dumps(sessions)}<<END>>", flush=True)
    elif cmd.startswith("LOAD_SESSION:"):
        sess_id = cmd.split(":", 1)[1]
        CURRENT_SESSION_ID = sess_id
        history = db.get_chat_history(sess_id)
        print(f"<<HISTORY>>{json.dumps(history)}<<END>>", flush=True)
    elif cmd == "NEW_CHAT":
        CURRENT_SESSION_ID = None
        print("<<HISTORY>>[]<<END>>", flush=True)
    elif cmd.startswith("DELETE_SESSION:"):
        sess_id = cmd.split(":", 1)[1]
        db.delete_session(sess_id)
        sessions = db.list_sessions()
        print(f"<<SESSIONS>>{json.dumps(sessions)}<<END>>", flush=True)


def main():
    for line in sys.stdin:
        handle_command(line.strip())


if __name__ == "__main__":
    main()
