#!/usr/bin/env python3
"""
Voice Bridge for Cosmic Dynamic Island (Windows Version)
Handles real-time voice transcription using Deepgram via direct WebSocket
and types transcribed text into the active window.
"""

import asyncio
import json
import sys
import threading
import subprocess
import time
import os
import websockets
from dotenv import load_dotenv

load_dotenv()

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")

DEBUG = True

def dlog(*args):
    if DEBUG:
        print("[voice]", *args, file=sys.stderr, flush=True)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    import pyaudio
    HAS_PYAUDIO = True
except ImportError:
    HAS_PYAUDIO = False

try:
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    HAS_WIN32 = True
except Exception:
    HAS_WIN32 = False

CHUNK_SIZE = 2560
SAMPLE_RATE = 16000
CHANNELS = 1

audio_stream = None
websocket_conn = None
voice_active = False
loop = None
voice_thread = None

def type_text_windows(text: str):
    if not HAS_WIN32:
        dlog("Win32 not available for typing")
        return
    
    # Proper Win32 SendInput with KEYEVENTF_UNICODE
    KEYEVENTF_UNICODE = 0x0004
    KEYEVENTF_KEYUP = 0x0002
    INPUT_KEYBOARD = 1
    
    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", ctypes.c_ushort),
            ("wScan", ctypes.c_ushort),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]
    
    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouseData", ctypes.c_ulong),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]
    
    class _INPUTunion(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]
    
    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("union", _INPUTunion)]
    
    inputs = []
    for char in text:
        # Key down
        down = INPUT()
        down.type = INPUT_KEYBOARD
        down.union.ki.wVk = 0
        down.union.ki.wScan = ord(char)
        down.union.ki.dwFlags = KEYEVENTF_UNICODE
        inputs.append(down)
        
        # Key up
        up = INPUT()
        up.type = INPUT_KEYBOARD
        up.union.ki.wVk = 0
        up.union.ki.wScan = ord(char)
        up.union.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
        inputs.append(up)
    
    if inputs:
        arr = (INPUT * len(inputs))(*inputs)
        user32.SendInput(len(inputs), ctypes.pointer(arr), ctypes.sizeof(INPUT))

def type_text_mac(text: str):
    def run_applescript():
        script = f'''
        tell application "System Events"
            keystroke "{text.replace('"', '\\"')}"
        end tell
        '''
        subprocess.run(["osascript", "-e", script], capture_output=True)
    
    threading.Thread(target=run_applescript, daemon=True).start()

def send_transcript(text: str, is_final: bool = False):
    payload = json.dumps({
        "text": text,
        "is_final": is_final,
        "timestamp": time.time()
    }, ensure_ascii=False)
    print(f"<<VOICE_TRANSCRIPT>>{payload}<<END>>", flush=True)

def send_status(status: str, error: str = None):
    payload = json.dumps({
        "status": status,
        "error": error,
        "timestamp": time.time()
    }, ensure_ascii=False)
    print(f"<<VOICE_STATUS>>{payload}<<END>>", flush=True)

async def listen_to_deepgram(audio_queue):
    global websocket_conn, voice_active
    
    if not DEEPGRAM_API_KEY:
        send_status("error", "DEEPGRAM_API_KEY not set in environment")
        dlog("Error: DEEPGRAM_API_KEY is missing")
        return
    
    dlog(f"Connecting to Deepgram with API Key: {DEEPGRAM_API_KEY[:4]}...{DEEPGRAM_API_KEY[-4:] if len(DEEPGRAM_API_KEY) > 8 else ''}")
    
    url = f"wss://api.deepgram.com/v1/listen?model=nova-2&encoding=linear16&sample_rate={SAMPLE_RATE}&channels=1&interim_results=true&smart_format=true"
    
    headers = {
        "Authorization": f"Token {DEEPGRAM_API_KEY}"
    }
    
    try:
        async with websockets.connect(url, extra_headers=headers) as ws:
            websocket_conn = ws
            send_status("connected")
            
            async def send_audio():
                while voice_active:
                    try:
                        data = await asyncio.wait_for(audio_queue.get(), timeout=1.0)
                        await ws.send(data)
                    except asyncio.TimeoutError:
                        continue
                    except Exception as e:
                        dlog("Send error:", e)
                        break
            
            async def recv_audio():
                global voice_active
                while voice_active:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        data = json.loads(msg)
                        
                        if data.get("channel"):
                            alternative = data["channel"]["alternatives"][0]
                            transcript = alternative.get("transcript", "")
                            if transcript:
                                is_final = data.get("is_final", False)
                                send_transcript(transcript, is_final)
                                
                                if is_final:
                                    if sys.platform == 'darwin':
                                        type_text_mac(transcript)
                                    else:
                                        type_text_windows(transcript)
                                
                    except asyncio.TimeoutError:
                        continue
                    except Exception as e:
                        dlog("Recv error:", e)
                        break
                
                voice_active = False
            
            sender = asyncio.create_task(send_audio())
            receiver = asyncio.create_task(recv_audio())
            
            await asyncio.gather(sender, receiver)
            
    except Exception as e:
        send_status("error", str(e))
        dlog("WebSocket error:", e)
    finally:
        websocket_conn = None
        voice_active = False

async def audio_capture_task():
    global audio_stream, voice_active
    
    if not HAS_PYAUDIO:
        send_status("error", "PyAudio not installed")
        return
    
    p = pyaudio.PyAudio()
    audio_queue = asyncio.Queue()
    
    try:
        audio_stream = p.open(
            format=pyaudio.paInt16,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            input=True,
            frames_per_buffer=CHUNK_SIZE
        )
        
        send_status("listening")
        
        async def capture():
            ev_loop = asyncio.get_running_loop()
            while voice_active:
                try:
                    # Use a short read so we can check voice_active frequently
                    data = await ev_loop.run_in_executor(
                        None,
                        lambda: audio_stream.read(CHUNK_SIZE, exception_on_overflow=False) if voice_active else b''
                    )
                    if data and voice_active:
                        await audio_queue.put(data)
                except OSError:
                    # Stream was closed — exit cleanly
                    break
                except Exception as e:
                    dlog("Capture error:", e)
                    break
        
        capture_task = asyncio.create_task(capture())
        deepgram_task = asyncio.create_task(listen_to_deepgram(audio_queue))
        
        await asyncio.gather(capture_task, deepgram_task)
        
    except Exception as e:
        send_status("error", str(e))
    finally:
        # Only close audio resources HERE, never from stop_voice()
        stream = audio_stream
        audio_stream = None
        if stream:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
        try:
            p.terminate()
        except Exception:
            pass

def start_voice():
    global voice_active, loop, voice_thread
    
    if voice_active:
        dlog("Voice already active")
        return
    
    # Wait for previous voice thread to fully finish before starting a new one
    if voice_thread and voice_thread.is_alive():
        dlog("Waiting for previous session to finish...")
        voice_thread.join(timeout=3)
    
    voice_active = True
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    def run():
        global loop
        try:
            loop.run_until_complete(audio_capture_task())
        except Exception as e:
            dlog("Loop error:", e)
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            loop = None
    
    voice_thread = threading.Thread(target=run, daemon=True)
    voice_thread.start()

def stop_voice():
    global voice_active, websocket_conn
    
    if not voice_active:
        return
    
    # Signal all loops to exit — do NOT touch audio_stream here!
    voice_active = False
    
    # Schedule websocket close on the event loop's thread
    if websocket_conn and loop and loop.is_running():
        ws = websocket_conn
        websocket_conn = None
        try:
            asyncio.run_coroutine_threadsafe(ws.close(), loop)
        except Exception:
            pass
    else:
        websocket_conn = None
    
    send_status("stopped")

def input_listener():
    for line in sys.stdin:
        line = line.strip()
        
        if line == "START":
            start_voice()
        elif line == "STOP":
            stop_voice()
        elif line.startswith("SET_KEY:"):
            key = line[8:].strip()
            if key:
                DEEPGRAM_API_KEY = key
                dlog("API key updated")

if __name__ == "__main__":
    dlog("Voice Bridge starting...")
    
    if not HAS_PYAUDIO:
        dlog("WARNING: PyAudio not installed - audio capture will not work")
        send_status("error", "pyaudio not installed")
    
    send_status("ready")
    
    input_thread = threading.Thread(target=input_listener, daemon=True)
    input_thread.start()
    input_thread.join()
