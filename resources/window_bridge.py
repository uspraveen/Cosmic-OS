#!/usr/bin/env python3
"""
Window Detection Bridge for Cosmic Dynamic Island
Detects active window/application and sends to Electron via stdout
"""

import sys
import json
import time
import asyncio

try:
    import win32gui
    import win32process
    import psutil
    WIN32_AVAILABLE = True
except ImportError:
    print("⚠️ WindowBridge: win32gui/psutil not available", file=sys.stderr)
    WIN32_AVAILABLE = False

DEBUG = False

def dlog(*args):
    if DEBUG:
        print("[window_bridge]", *args, file=sys.stderr, flush=True)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

class WindowMonitor:
    """Monitors the currently focused window."""
    
    def __init__(self):
        self._pid_cache = {}  # PID -> process_name cache
        self._last_hwnd = None
        self._last_title = None
    
    def get_active_window_info(self):
        """Gets information about the currently focused window."""
        if not WIN32_AVAILABLE:
            return {
                'title': 'Desktop',
                'process': 'explorer.exe',
                'appName': 'Windows'
            }
        
        try:
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return None
            
            # Get window title
            title = win32gui.GetWindowText(hwnd)
            if not title:
                title = "Desktop"
            
            # Get process ID
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            
            # Check cache first for process name
            if pid in self._pid_cache:
                process_name = self._pid_cache[pid]
            else:
                try:
                    proc = psutil.Process(pid)
                    process_name = proc.name()
                    self._pid_cache[pid] = process_name
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    process_name = "Unknown"
            
            # Get friendly app name
            app_name = self._get_friendly_name(process_name, title)
            
            return {
                'title': title,
                'process': process_name,
                'appName': app_name
            }
        
        except Exception as e:
            dlog("Error getting window info:", repr(e))
            return None
    
    def _get_friendly_name(self, process_name, title):
        """Convert process name to friendly app name."""
        if not process_name:
            return "Unknown"
        
        p = process_name.lower()
        
        # Browsers
        if 'chrome' in p:
            return 'Chrome'
        if 'firefox' in p:
            return 'Firefox'
        if 'msedge' in p or 'edge' in p:
            return 'Edge'
        if 'brave' in p:
            return 'Brave'
        if 'opera' in p:
            return 'Opera'
        
        # IDEs
        if 'code' in p:
            return 'VS Code'
        if 'pycharm' in p:
            return 'PyCharm'
        if 'intellij' in p:
            return 'IntelliJ'
        if 'sublime' in p:
            return 'Sublime Text'
        
        # Communication
        if 'slack' in p:
            return 'Slack'
        if 'teams' in p:
            return 'Teams'
        if 'discord' in p:
            return 'Discord'
        if 'zoom' in p:
            return 'Zoom'
        
        # Office
        if 'outlook' in p:
            return 'Outlook'
        if 'word' in p:
            return 'Word'
        if 'excel' in p:
            return 'Excel'
        if 'powerpoint' in p:
            return 'PowerPoint'
        
        # Terminal
        if 'windowsterminal' in p or 'wt.exe' in p:
            return 'Terminal'
        if 'powershell' in p:
            return 'PowerShell'
        if 'cmd' in p:
            return 'Command Prompt'
        
        # System
        if 'explorer' in p:
            return 'File Explorer'
        if 'notepad' in p:
            return 'Notepad'
        
        # Default: Clean up the process name
        name = process_name.replace('.exe', '')
        return name.capitalize()
    
    def has_changed(self, current_info):
        """Check if window has changed."""
        if current_info is None:
            return False
        
        title = current_info.get('title', '')
        changed = title != self._last_title
        
        if changed:
            self._last_title = title
        
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
                    'process': 'explorer.exe',
                    'appName': 'Windows'
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
    dlog("Window bridge starting...")
    
    try:
        asyncio.run(update_loop())
    except KeyboardInterrupt:
        dlog("Window bridge stopped")