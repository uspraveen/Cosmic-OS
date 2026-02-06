import sys
import json
import requests
import asyncio
import threading
from database import db # Ensure database.py is present

URL = "https://api.perplexity.ai/chat/completions"

# Global current session
CURRENT_SESSION_ID = None
DEBUG = True

def dlog(*args):
    if DEBUG:
        print("[perplexity]", *args, file=sys.stderr, flush=True)

def enrich_and_send_sources(urls):
    """
    Fetches page titles for the URLs and sends the enriched list.
    """
    import re
    enriched = []
    
    for url in urls:
        item = {"url": url, "title": None, "domain": ""}
        try:
            from urllib.parse import urlparse
            item["domain"] = urlparse(url).netloc.replace("www.", "")
            
            # Fetch Title with timeout
            try:
                resp = requests.get(url, timeout=1.5, headers={"User-Agent": "CosmicBrowser/1.0"})
                if resp.status_code == 200:
                    # Simple regex for title
                    match = re.search(r'<title>(.*?)</title>', resp.text, re.IGNORECASE | re.DOTALL)
                    if match:
                        item["title"] = match.group(1).strip()
            except:
                pass # Title fetch failed, use domain/url

        except: pass
        enriched.append(item)
    
    print(f"<<SOURCES>>{json.dumps(enriched)}<<END>>", flush=True)

async def stream_perplexity_response(prompt: str):
    global CURRENT_SESSION_ID
    dlog(f"Received prompt: {prompt[:50]}...")

    # 1. CHECK FOR KEY
    api_key = db.get_api_key("perplexity")
    
    # --- MISSING KEY HANDLER ---
    if not api_key:
        dlog("Error: Missing API Key")
        err_msg = "⚠️ **Perplexity Key Missing**\n\nPlease open Settings (gear icon) and enter your Perplexity API key to use this search mode."
        
        # If we have a session, save the error so it persists
        if CURRENT_SESSION_ID:
             db.add_message(CURRENT_SESSION_ID, "assistant", err_msg)
             
        # Stream the error to the UI immediately
        print(f"<<CHUNK>>{json.dumps({'chunk': err_msg, 'done': True})}<<END>>", flush=True)
        return
    # ---------------------------

    # 2. Session & History
    if not CURRENT_SESSION_ID:
        title = (prompt[:30] + '..') if len(prompt) > 30 else prompt
        CURRENT_SESSION_ID = db.create_session(title=title, model="perplexity")
        dlog(f"Created new session: {CURRENT_SESSION_ID}")
        # Broadcasting the new session ID to sync with other models
        print(f"<<SESSION_SET>>{json.dumps(CURRENT_SESSION_ID)}<<END>>", flush=True)
    
    db.add_message(CURRENT_SESSION_ID, "user", prompt)

    # NEW: Fetch Pruned Context
    history_msgs = db.get_pruned_history(CURRENT_SESSION_ID)
    
    api_messages = [{"role": "system", "content": "You are a helpful, accurate AI assistant."}]
    for msg in history_msgs:
        api_messages.append({"role": msg["role"], "content": msg["content"]})
    
    # 3. Request
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "sonar", 
        "messages": api_messages,
        "stream": True
    }

    dlog(f"Sending request to {URL}...")
    full_response = ""
    
    try:
        response = requests.post(URL, json=payload, headers=headers, stream=True)
        dlog(f"Response status: {response.status_code}")
        
        if response.status_code != 200:
            err_msg = f"Error: {response.status_code} - {response.text}"
            dlog(err_msg)
            db.add_message(CURRENT_SESSION_ID, "assistant", err_msg)
            print(f"<<CHUNK>>{json.dumps({'chunk': err_msg, 'done':True})}<<END>>", flush=True)
            return

        sources_sent = False

        for chunk in response.iter_lines():
            if chunk:
                chunk_str = chunk.decode('utf-8')
                if chunk_str.startswith('data: '):
                    data_str = chunk_str[6:]
                    if data_str == '[DONE]': break
                    
                    try:
                        data = json.loads(data_str)
                        
                        # Handle Sources (Enriched)
                        if not sources_sent and 'citations' in data and data['citations']:
                             sources_sent = True
                             raw_sources = data['citations']
                             # Spawn a thread to fetch titles so we don't block streaming
                             threading.Thread(target=enrich_and_send_sources, args=(raw_sources,)).start()
                        
                        if 'choices' in data and len(data['choices']) > 0:
                            delta = data['choices'][0].get('delta', {})
                            content = delta.get('content', '')
                            if content:
                                full_response += content
                                print(f"<<CHUNK>>{json.dumps({'chunk': content})}<<END>>", flush=True)

                    except Exception as e:
                        dlog(f"JSON Parse Error: {e}")
        
        if full_response:
             dlog(f"Full response length: {len(full_response)}")
             db.add_message(CURRENT_SESSION_ID, "assistant", full_response)
             
        print(f"<<CHUNK>>{json.dumps({'chunk': '', 'done': True})}<<END>>", flush=True)
 


    except Exception as e:
        dlog(f"Connection Exception: {e}")
        err = f"Connection Error: {str(e)}"
        db.add_message(CURRENT_SESSION_ID, "assistant", err)
        print(f"<<CHUNK>>{json.dumps({'chunk': err, 'done':True})}<<END>>", flush=True)


async def handle_command(cmd: str):
    global CURRENT_SESSION_ID
    
    if cmd.startswith("PROMPT:"):
        await stream_perplexity_response(cmd[7:].strip())

    elif cmd == "LIST_SESSIONS":
        sessions = db.list_sessions()
        dlog("Listing sessions")
        print(f"<<SESSIONS>>{json.dumps(sessions)}<<END>>", flush=True)

    elif cmd.startswith("LOAD_SESSION:"):
        sess_id = cmd.split(":", 1)[1]
        CURRENT_SESSION_ID = sess_id
        dlog(f"Loading session: {sess_id}")
        history = db.get_chat_history(sess_id)
        print(f"<<HISTORY>>{json.dumps(history)}<<END>>", flush=True)

    elif cmd == "NEW_CHAT":
        dlog("Starting new chat")
        CURRENT_SESSION_ID = None
        # We don't print <<NEW_CHAT>> because the frontend handles the UI reset
        
    elif cmd.startswith("DELETE_SESSION:"):
        sess_id = cmd.split(":", 1)[1]
        db.delete_session(sess_id)
        # No need to send session list, Gemini bridge usually handles the UI list update


def input_listener(loop):
    dlog("Perplexity Bridge Started")
    for line in sys.stdin:
        asyncio.run_coroutine_threadsafe(handle_command(line.strip()), loop)

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    threading.Thread(target=input_listener, args=(loop,), daemon=True).start()
    loop.run_forever()