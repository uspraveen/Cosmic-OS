import sys
import json
import asyncio
# Ensure database.py is in the same folder
from database import db

DEBUG = True

def dlog(*args):
    if DEBUG:
        print("[gemini]", *args, file=sys.stderr, flush=True)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    from google import genai
    HAS_GEMINI = True
except ImportError:
    HAS_GEMINI = False

# Global State
CURRENT_SESSION_ID = None

async def stream_gemini_response(prompt: str):
    global CURRENT_SESSION_ID
    
    api_key = db.get_api_key("gemini")
    
    if not HAS_GEMINI:
        err = json.dumps({"chunk": "Error: google-genai not installed.", "done": True})
        print(f"<<CHUNK>>{err}<<END>>", flush=True)
        return

    if not api_key:
        err = json.dumps({"chunk": "Error: No API Key found. Please check Settings.", "done": True})
        print(f"<<CHUNK>>{err}<<END>>", flush=True)
        return
    
    # 1. Initialize Session
    if not CURRENT_SESSION_ID:
        title = (prompt[:30] + '..') if len(prompt) > 30 else prompt
        CURRENT_SESSION_ID = db.create_session(title=title)
        # Broadcasting the new session ID to sync with other models
        print(f"<<SESSION_SET>>{json.dumps(CURRENT_SESSION_ID)}<<END>>", flush=True)
    
    # 2. Save User Msg
    db.add_message(CURRENT_SESSION_ID, "user", prompt)

    # NEW: Fetch Pruned Context
    history_msgs = db.get_pruned_history(CURRENT_SESSION_ID)
    contents = []
    
    for msg in history_msgs:
        role = "model" if msg["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})
    
    # 3. Stream
    full_response = ""
    try:
        client = genai.Client(api_key=api_key)
        # Using 2.0-flash-exp or similar
        for chunk in client.models.generate_content_stream(
            model="gemini-3-flash-preview", 
            contents=contents,
            
        ):
            if chunk.text:
                data = json.dumps({"chunk": chunk.text, "done": False}, ensure_ascii=False)
                print(f"<<CHUNK>>{data}<<END>>", flush=True)
                full_response += chunk.text
        
        # 4. Save Assistant Msg
        if full_response:
            db.add_message(CURRENT_SESSION_ID, "assistant", full_response)

        data = json.dumps({"chunk": "", "done": True}, ensure_ascii=False)
        print(f"<<CHUNK>>{data}<<END>>", flush=True)
    
    except Exception as e:
        error_msg = f"API Error: {str(e)}"
        db.add_message(CURRENT_SESSION_ID, "assistant", error_msg)
        error_data = json.dumps({"chunk": error_msg, "done": True}, ensure_ascii=False)
        print(f"<<CHUNK>>{error_data}<<END>>", flush=True)

async def handle_command(cmd: str):
    global CURRENT_SESSION_ID
    
    if cmd.startswith("PROMPT:"):
        await stream_gemini_response(cmd[7:].strip())

    elif cmd == "CHECK_KEYS":
        gemini = db.get_api_key("gemini")
        pplx = db.get_api_key("perplexity")
        status = {
            "hasKeys": (gemini is not None) or (pplx is not None),
            "gemini": bool(gemini),
            "perplexity": bool(pplx)
        }
        print(f"<<KEY_STATUS>>{json.dumps(status)}<<END>>", flush=True)

    elif cmd.startswith("SAVE_KEYS:"):
        try:
            payload = json.loads(cmd[10:])
            if payload.get("gemini"): db.set_api_key("gemini", payload["gemini"])
            if payload.get("perplexity"): db.set_api_key("perplexity", payload["perplexity"])
            print("<<KEY_SAVED>>true<<END>>", flush=True)
            
            # Send updated status immediately to unlock UI
            await handle_command("CHECK_KEYS")
        except: pass

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
        # Confirm clearing
        print(f"<<HISTORY>>[]<<END>>", flush=True)

    elif cmd.startswith("DELETE_SESSION:"):
        sess_id = cmd.split(":", 1)[1]
        db.delete_session(sess_id)
        # Refresh the list for the UI immediately
        sessions = db.list_sessions()
        print(f"<<SESSIONS>>{json.dumps(sessions)}<<END>>", flush=True)

def input_listener(loop):
    for line in sys.stdin:
        asyncio.run_coroutine_threadsafe(handle_command(line.strip()), loop)

def logout_google():
    db.conn.execute("DELETE FROM config WHERE key IN ('google_calendar_token', 'user_gmail')")
    db.conn.commit()

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    import threading
    threading.Thread(target=input_listener, args=(loop,), daemon=True).start()
    loop.run_forever()