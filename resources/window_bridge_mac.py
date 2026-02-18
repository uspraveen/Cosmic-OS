#!/usr/bin/env python3
"""
Window Detection Bridge for Cosmic Dynamic Island (macOS Version)
Detects active window/application using AppleScript and sends to Electron via stdout
"""

import sys
import json
import time
import subprocess
import asyncio

DEBUG = False

def dlog(*args):
    if DEBUG:
        print("[window_bridge]", *args, file=sys.stderr, flush=True)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

class WindowMonitor:
    """Monitors the currently focused window using AppleScript."""
    
    def __init__(self):
        self._last_title = None
        self._last_app = None
    
    def get_active_window_info(self):
        """Gets information about the currently focused window."""
        
        script = '''
        global frontApp, frontAppName, windowTitle
        set windowTitle to ""
        tell application "System Events"
            set frontApp to first application process whose frontmost is true
            set frontAppName to name of frontApp
            tell process frontAppName
                try
                    set windowTitle to name of front window
                end try
            end tell
        end tell
        return "{" & "\\"process\\": \\"" & frontAppName & "\\", \\"title\\": \\"" & windowTitle & "\\", \\"appName\\": \\"" & frontAppName & "\\"}"
        '''
        
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=1
            )
            
            output = result.stdout.strip()
            if not output:
                return None
                
            # Parse the JSON returned by AppleScript
            # Note: AppleScript output might need manual parsing if simple json load fails due to quotes
            # But the script constructs a JSON string, so we should be okay if we escape correctly in AppleScript
            
            # Sanitization in case AppleScript returns "missing value" or quotes in title break it
            # A safer way might be to ask for specific fields separated by a delimiter
            
            return json.loads(output)
            
        except Exception as e:
            # Fallback for simpler script if manual JSON construction is fragile
            return self._fallback_script()

    def _fallback_script(self):
        try:
            # Get App Name
            res_app = subprocess.run(["osascript", "-e", 'tell application "System Events" to get name of first application process whose frontmost is true'], capture_output=True, text=True)
            app_name = res_app.stdout.strip()
            
            # Get Window Title
            res_title = subprocess.run(["osascript", "-e", 'tell application "System Events" to tell process "' + app_name + '" to get name of front window'], capture_output=True, text=True)
            title = res_title.stdout.strip()
            
            return {
                'title': title,
                'process': app_name,
                'appName': app_name
            }
        except:
            return None

    def has_changed(self, current_info):
        """Check if window has changed."""
        if current_info is None:
            return False
        
        title = current_info.get('title', '')
        app_name = current_info.get('appName', '')
        
        changed = (title != self._last_title) or (app_name != self._last_app)
        
        if changed:
            self._last_title = title
            self._last_app = app_name
        
        return changed

async def update_loop():
    """Main loop that sends window updates."""
    monitor = WindowMonitor()
    last_sent = None
    
    while True:
        try:
            window_info = monitor.get_active_window_info()
            
            if window_info is None:
                window_info = {
                    'title': 'Desktop',
                    'process': 'Finder',
                    'appName': 'macOS'
                }
            
            # Only send if changed
            current = json.dumps(window_info, ensure_ascii=False)
            if current != last_sent:
                print(f"<<WINDOW>>{current}<<END>>")
                sys.stdout.flush()
                last_sent = current
                dlog("Window changed:", window_info['appName'], "-", window_info['title'][:50])
        
        except Exception as e:
            dlog("Loop error:", repr(e))
        
        await asyncio.sleep(0.5)

if __name__ == "__main__":
    dlog("Window bridge starting (macOS)...")
    try:
        asyncio.run(update_loop())
    except KeyboardInterrupt:
        dlog("Window bridge stopped")
