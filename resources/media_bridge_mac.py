#!/usr/bin/env python3
"""
Media Bridge for Cosmic Dynamic Island (macOS Version)
Controls Media (Music/Spotify) via AppleScript and sends to Electron
"""

import asyncio
import json
import sys
import subprocess
import threading
import time

DEBUG = True

def dlog(*args):
    if DEBUG:
        print("[bridge]", *args, file=sys.stderr, flush=True)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Helper to run AppleScript
def run_osascript(script):
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=1
        )
        return result.stdout.strip()
    except Exception as e:
        return None

def get_volume_mac():
    try:
        vol = run_osascript("output volume of (get volume settings)")
        return int(vol) if vol else 0
    except:
        return 0

def set_volume_mac(val: int):
    try:
        # val is 0-100
        run_osascript(f"set volume output volume {val}")
        return True
    except:
        return False

def get_media_info_mac():
    # Try Music (iTunes) first, then Spotify
    # This is a simplified check. A robust one would check which app is running first.
    
    # Check Music
    script_music = '''
    if application "Music" is running then
        tell application "Music"
            if player state is playing then
                return "Music||" & name of current track & "||" & artist of current track & "||" & player position & "||" & duration of current track
            else if player state is paused then
                return "Music||" & name of current track & "||" & artist of current track & "||" & player position & "||" & duration of current track & "||paused"
            end if
        end tell
    end if
    '''
    
    res = run_osascript(script_music)
    
    if not res:
        # Check Spotify
        script_spotify = '''
        if application "Spotify" is running then
            tell application "Spotify"
                if player state is playing then
                    return "Spotify||" & name of current track & "||" & artist of current track & "||" & player position & "||" & duration of current track
                else if player state is paused then
                    return "Spotify||" & name of current track & "||" & artist of current track & "||" & player position & "||" & duration of current track & "||paused"
                end if
            end tell
        end if
        '''
        res = run_osascript(script_spotify)

    if res:
        try:
            parts = res.split("||")
            source = parts[0]
            title = parts[1]
            artist = parts[2]
            # AppleScript duration is often in seconds (Music) or ms (Spotify?) - actually Spotify is ms, Music is seconds
            # Need to verify per app.
            
            position = float(parts[3])
            duration = float(parts[4])
            is_paused = len(parts) > 5 and parts[5] == "paused"
            
            # Spotify duration/position is in milliseconds usually?
            if source == "Spotify":
                # Spotify AppleScript usually returns seconds for position? No, apparently it's inconsistent or needs check.
                # Actually Spotify `duration` is usually ms, `player position` is seconds. 
                # Let's assume seconds for now or safe check.
                # If duration > 10000, probably ms.
                 if duration > 10000: duration = duration / 1000
                 # Setup
            
            return {
                "title": title,
                "artist": artist,
                "source": source,
                "appId": f"com.apple.{source}", # Mock ID
                "isPlaying": not is_paused,
                "position": int(position),
                "duration": int(duration),
                "trackKey": f"{source}::{title}::{artist}"
            }
        except:
            pass
            
    return None

async def update_loop():
    dlog("Bridge Loop Starting (macOS)...")
    
    last_sent = None
    
    while True:
        try:
            media_info = get_media_info_mac()
            
            if media_info:
                data = media_info
                data["volume"] = get_volume_mac()
                data["device"] = "System Output"
                data["thumbnail"] = None # Thumbnails via AppleScript is hard/slow, skip for now
            else:
                data = {
                    "title": "Not Playing", "artist": "System Audio", "source": "System",
                    "appId": "System", "isPlaying": False, "thumbnail": None, 
                    "volume": get_volume_mac(),
                    "device": "System Output", 
                    "position": 0, "duration": 0,      
                    "trackKey": "System::Not Playing::System Audio",
                }

            payload = json.dumps(data, ensure_ascii=False)
            
            if payload != last_sent:
                print(f"<<START>>{payload}<<END>>")
                sys.stdout.flush()
                last_sent = payload
        
        except Exception as e:
            dlog("Loop Error:", e)
        
        await asyncio.sleep(0.5)

async def handle_command(cmd: str):
    try:
        if cmd.startswith("setvol:"):
            set_volume_mac(int(cmd.split(":")[1]))
            return
        
        # Determine which app to control
        script_play = ""
        
        # Simple Logic: send command to both or just try one
        # Ideally we know which one is active from get_media_info
        
        cmd_map = {
            "playpause": "playpause",
            "next": "next track",
            "prev": "previous track"
        }
        
        action = cmd_map.get(cmd)
        if not action: return

        script = f'''
        if application "Music" is running then
            tell application "Music" to {action}
        end if
        if application "Spotify" is running then
            tell application "Spotify" to {action}
        end if
        '''
        run_osascript(script)

    except: pass

def input_listener(loop):
    for line in sys.stdin:
        asyncio.run_coroutine_threadsafe(handle_command(line.strip()), loop)

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    threading.Thread(target=input_listener, args=(loop,), daemon=True).start()
    loop.run_until_complete(update_loop())
