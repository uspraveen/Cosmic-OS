import asyncio
import json
import sys
import threading
import subprocess
import time
import base64
import ctypes
from ctypes import wintypes

# Try to import requests for iTunes fallback
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

DEBUG = True

def dlog(*args):
    if DEBUG:
        print("[bridge]", *args, file=sys.stderr, flush=True)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---------------- MEDIA (winsdk) ----------------
HAS_MEDIA = False
try:
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus,
    )
    from winsdk.windows.storage.streams import DataReader
    HAS_MEDIA = True
except Exception as e:
    dlog("winsdk import failed (Media features disabled):", repr(e))
    HAS_MEDIA = False

# ---------------- VOLUME BACKENDS ----------------
HAS_PYCAW = False
volume_interface = None
audio_device_object = None 

try:
    import comtypes
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from comtypes import CLSCTX_ALL
    from ctypes import cast, POINTER
    HAS_PYCAW = True
except Exception:
    HAS_PYCAW = False

winmm = ctypes.WinDLL("winmm")
waveOutGetVolume = winmm.waveOutGetVolume
waveOutSetVolume = winmm.waveOutSetVolume

def _coinit():
    if not HAS_PYCAW: return
    try: comtypes.CoInitialize()
    except: pass

def init_volume_pycaw():
    global volume_interface, audio_device_object
    if not HAS_PYCAW: return False
    _coinit()
    try:
        audio_device_object = AudioUtilities.GetSpeakers()
        interface = audio_device_object.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume_interface = cast(interface, POINTER(IAudioEndpointVolume))
        return True
    except:
        volume_interface = None
        audio_device_object = None
        return False

def get_volume_pycaw():
    global volume_interface
    if not HAS_PYCAW: return None
    _coinit()
    if volume_interface is None and not init_volume_pycaw(): return None
    try: return int(round(volume_interface.GetMasterVolumeLevelScalar() * 100))
    except:
        volume_interface = None
        return None

def get_audio_device_name():
    global audio_device_object
    if not HAS_PYCAW: return "System"
    _coinit()
    if audio_device_object is None:
        init_volume_pycaw()
    try:
        if hasattr(audio_device_object, 'FriendlyName'):
            return audio_device_object.FriendlyName
        dev = AudioUtilities.GetSpeakers()
        return dev.FriendlyName if dev else "Speaker"
    except:
        return "Speaker"

def set_volume_pycaw(val: int):
    global volume_interface
    if not HAS_PYCAW: return False
    _coinit()
    
    # Logic: Try setting. If fail, re-init and try once more.
    try:
        if volume_interface is None: init_volume_pycaw()
        if volume_interface:
            v = max(0.0, min(1.0, val / 100.0))
            volume_interface.SetMasterVolumeLevelScalar(v, None)
            return True
    except:
        # Retry mechanism
        try:
            if init_volume_pycaw():
                 v = max(0.0, min(1.0, val / 100.0))
                 volume_interface.SetMasterVolumeLevelScalar(v, None)
                 return True
        except: pass
        
    return False

def get_volume_winmm():
    try:
        vol = wintypes.DWORD()
        res = waveOutGetVolume(0, ctypes.byref(vol))
        if res != 0: return None
        left = vol.value & 0xFFFF
        right = (vol.value >> 16) & 0xFFFF
        return int(round(((left + right) / 2.0) / 0xFFFF * 100))
    except: return None

def set_volume_winmm(val: int):
    try:
        v = int(max(0, min(100, val)) / 100 * 0xFFFF)
        res = waveOutSetVolume(0, (v << 16) | v)
        return res == 0
    except: return False

def get_volume():
    v = get_volume_pycaw()
    return v if v is not None else get_volume_winmm()

def set_volume(val: int):
    # Try Pycaw first, fall back to WinMM
    if set_volume_pycaw(val): return True
    return set_volume_winmm(val)

# ---------------- THUMB FETCH (SYSTEM + ITUNES) ----------------
THUMB_SCRIPT = r'''
import sys, asyncio, base64
try:
    from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager
    from winsdk.windows.storage.streams import DataReader
except:
    sys.exit(0)

def sniff_mime(buf: bytes) -> str:
    if len(buf) >= 2 and buf[0] == 0xFF and buf[1] == 0xD8: return "image/jpeg"
    if len(buf) >= 8 and buf[:8] == b"\x89PNG\r\n\x1a\n": return "image/png"
    return "image/png"

async def main():
    try:
        manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
        session = manager.get_current_session()
        
        if not session:
            sessions = manager.get_sessions()
            for s in sessions:
                try:
                    info = s.get_playback_info()
                    if info.playback_status in [4, 5]:
                        session = s
                        break
                except: pass

        if not session: return

        props = await session.try_get_media_properties_async()
        if not props or not props.thumbnail: return

        stream = await props.thumbnail.open_read_async()
        if not stream: return
        
        size = stream.size
        if size == 0: return

        reader = DataReader(stream.get_input_stream_at(0))
        await reader.load_async(size)
        buf = bytearray(size)
        reader.read_bytes(buf)
        
        if buf:
            mime = sniff_mime(bytes(buf))
            b64 = base64.b64encode(bytes(buf)).decode("utf-8")
            print("THUMB:data:" + mime + ";base64," + b64)
    except: pass

asyncio.run(main())
'''

_thumb_lock = threading.Lock()
_thumb_cache = {}       
_thumb_inflight = set()

def _fetch_thumb_system_sync():
    try:
        result = subprocess.run(
            [sys.executable, "-c", THUMB_SCRIPT],
            capture_output=True, text=True, timeout=1.5, 
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        )
        out = (result.stdout or "").strip()
        if out.startswith("THUMB:"): return out[6:]
    except: pass
    return None

def _fetch_thumb_itunes_sync(title, artist):
    if not HAS_REQUESTS: return None
    if not title or title == "Unknown" or not artist or artist == "Unknown": return None
    try:
        query = f"{title} {artist}"
        params = {"term": query, "media": "music", "entity": "song", "limit": 1}
        resp = requests.get("https://itunes.apple.com/search", params=params, timeout=2)
        if resp.status_code != 200: return None
        data = resp.json()
        if data.get("resultCount", 0) == 0: return None
        track = data["results"][0]
        artwork_url = track.get("artworkUrl100")
        if not artwork_url: return None
        return artwork_url.replace("100x100bb", "1000x1000bb")
    except Exception as e:
        dlog("iTunes Fetch Error:", e)
    return None

def request_thumb_async(track_key: str, title: str, artist: str):
    with _thumb_lock:
        if track_key in _thumb_cache: return _thumb_cache[track_key]
        if track_key in _thumb_inflight: return None
        if len(_thumb_cache) > 50:
            keys = list(_thumb_cache.keys())[:25]
            for k in keys: del _thumb_cache[k]
        _thumb_inflight.add(track_key)

    def worker():
        thumb = _fetch_thumb_system_sync()
        if not thumb:
            thumb = _fetch_thumb_itunes_sync(title, artist)
        with _thumb_lock:
            if thumb: _thumb_cache[track_key] = thumb
            _thumb_inflight.discard(track_key)

    threading.Thread(target=worker, daemon=True).start()
    return None

# ---------------- MAIN LOOP ----------------
async def pick_best_session(manager):
    try:
        sessions = manager.get_sessions()
        for s in sessions:
            try:
                if s.get_playback_info().playback_status == GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING:
                    return s
            except: pass
        return manager.get_current_session()
    except: return None

def get_timeline(session):
    try:
        timeline = session.get_timeline_properties()
        if not timeline: return 0, 0
        start = timeline.start_time.total_seconds() if timeline.start_time else 0
        end = timeline.end_time.total_seconds() if timeline.end_time else 0
        pos = timeline.position.total_seconds() if timeline.position else 0
        return int(pos), int(end - start)
    except:
        return 0, 0

async def update_loop():
    dlog("Bridge Loop Starting...")
    
    manager = None
    
    last_sent = None
    track_stable_start = 0
    current_track_key_candidate = None

    while True:
        try:
            # 1. Try to acquire/re-acquire manager if we have media support
            if HAS_MEDIA and manager is None:
                try:
                    manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
                except Exception as e:
                    # Don't spam logs every 0.25s, maybe just debug
                    # dlog("Manager init error (retrying):", e)
                    pass

            # 2. Base Data (always sent, even if no media session)
            data = {
                "title": "Not Playing", "artist": "System Audio", "source": "System",
                "appId": "System", "isPlaying": False, "thumbnail": None, 
                "volume": get_volume(),
                "device": get_audio_device_name(), 
                "position": 0, "duration": 0,      
                "trackKey": "System::Not Playing::System Audio",
            }

            # 3. If we have a manager, try to get media info
            if manager:
                try:
                    session = await pick_best_session(manager)
                    if session:
                        props = await session.try_get_media_properties_async()
                        info = session.get_playback_info()
                        
                        title = props.title if props else "Unknown"
                        artist = props.artist if props else "Unknown"
                        app_id = session.source_app_user_model_id or "System"
                        source = app_id.split(".")[0].capitalize() if "." in app_id else app_id
                        track_key = f"{app_id}::{title}::{artist}"
                        
                        pos, dur = get_timeline(session)

                        if track_key != current_track_key_candidate:
                            current_track_key_candidate = track_key
                            track_stable_start = time.time()
                        
                        thumb = None
                        with _thumb_lock:
                            thumb = _thumb_cache.get(track_key)

                        if not thumb:
                            if (time.time() - track_stable_start) > 0.5:
                                request_thumb_async(track_key, title, artist)

                        data.update({
                            "title": title, "artist": artist, "source": source, "appId": app_id,
                            "isPlaying": info.playback_status == GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING,
                            "thumbnail": thumb, 
                            "volume": get_volume(), 
                            "device": get_audio_device_name(),
                            "position": pos, 
                            "duration": dur,
                            "trackKey": track_key
                        })
                except Exception as ex:
                    # If session interaction fails, force re-acquire manager
                    # dlog("Session Error:", ex)
                    manager = None

            payload = json.dumps(data, ensure_ascii=False)
            
            if payload != last_sent:
                print(f"<<START>>{payload}<<END>>")
                sys.stdout.flush()
                last_sent = payload

        except Exception as e:
            dlog("Loop Error:", e)
        
        await asyncio.sleep(0.25)

async def handle_command(cmd: str):
    try:
        if cmd.startswith("setvol:"):
            set_volume(int(cmd.split(":")[1]))
            return
        
        # Only try media controls if we have a valid manager
        if HAS_MEDIA:
            manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
            session = manager.get_current_session()
            if session:
                if cmd == "playpause": await session.try_toggle_play_pause_async()
                elif cmd == "next": await session.try_skip_next_async()
                elif cmd == "prev": await session.try_skip_previous_async()
    except: pass

def input_listener(loop):
    for line in sys.stdin:
        asyncio.run_coroutine_threadsafe(handle_command(line.strip()), loop)

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    threading.Thread(target=input_listener, args=(loop,), daemon=True).start()
    loop.run_until_complete(update_loop())