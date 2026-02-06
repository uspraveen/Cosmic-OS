import sys
import json
import time
import threading
import requests
import datetime
from icalendar import Calendar
from database import db

# Force UTF-8
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

def dlog(*args):
    print("[calendar]", *args, file=sys.stderr, flush=True)

def get_local_tz():
    """Get the local system timezone safely."""
    return datetime.datetime.now().astimezone().tzinfo

def to_local_datetime(dt):
    """
    Converts any date/datetime object to a SYSTEM LOCAL datetime.
    This ensures 'Jan 26 00:00 UTC' becomes 'Jan 25 18:00 CST'.
    """
    if dt is None: return None
    
    local_tz = get_local_tz()

    # 1. Handle "All Day" events (date only)
    if not isinstance(dt, datetime.datetime):
        # Treat all-day events as starting at Midnight LOCAL time
        dt = datetime.datetime.combine(dt, datetime.time.min)
        return dt.replace(tzinfo=local_tz)
    
    # 2. Handle Naive datetimes (missing timezone) -> Assume Local
    if dt.tzinfo is None:
        return dt.replace(tzinfo=local_tz)
        
    # 3. Handle Aware datetimes (UTC, etc) -> Convert to Local
    return dt.astimezone(local_tz)

def fetch_ical_data():
    ical_url = db.get_api_key("calendar_ical_url")
    
    if not ical_url:
        return {"email": "", "events": [], "error": "No URL Set"}

    try:
        # dlog("Fetching URL...")
        response = requests.get(ical_url, timeout=15)
        response.raise_for_status()
        
        cal = Calendar.from_ical(response.content)
        
        upcoming_events = []
        now = datetime.datetime.now().astimezone() # Now in Local Time
        
        for component in cal.walk('vevent'):
            try:
                dtstart_raw = component.get('dtstart').dt
                
                # Get Title
                summary = component.get('summary')
                summary = str(summary) if summary else "No Title"

                # VITAL FIX: Convert to Local Time immediately
                start_dt = to_local_datetime(dtstart_raw)
                
                # Filter: Keep events from 1 hour ago onwards
                cutoff = now - datetime.timedelta(hours=1)
                
                if start_dt >= cutoff:
                    upcoming_events.append({
                        "id": str(component.get('uid', '')),
                        "summary": summary,
                        # Send ISO format with the Offset (e.g. -06:00) so JS handles it right
                        "start": start_dt.isoformat(),
                        "colorId": "1",
                        "raw_obj": start_dt
                    })
            except Exception as e:
                continue

        # Sort by Date
        upcoming_events.sort(key=lambda x: x['raw_obj'])
        
        # Clean up
        final_events = []
        for e in upcoming_events[:10]:
            del e['raw_obj']
            final_events.append(e)
        
        return {
            "email": "Connected via Link", 
            "events": final_events,
            "updated": time.time()
        }

    except Exception as e:
        dlog(f"Fetch Error: {e}")
        return {"email": "", "events": [], "error": "Failed to load"}

def handle_input():
    while True:
        try:
            line = sys.stdin.readline()
            if not line: break
            cmd = line.strip()
            
            if cmd.startswith("SAVE_URL:"):
                url = cmd[9:].strip()
                if "calendar.google.com" in url:
                    db.set_api_key("calendar_ical_url", url)
                    dlog("URL saved, fetching...")
                    data = fetch_ical_data()
                    print(f"<<CALENDAR>>{json.dumps(data)}<<END>>", flush=True)

            elif cmd == "REFRESH":
                data = fetch_ical_data()
                print(f"<<CALENDAR>>{json.dumps(data)}<<END>>", flush=True)
                
        except: pass

def main():
    threading.Thread(target=handle_input, daemon=True).start()
    while True:
        try:
            if db.get_api_key("calendar_ical_url"):
                data = fetch_ical_data()
                print(f"<<CALENDAR>>{json.dumps(data)}<<END>>", flush=True)
        except: pass
        
        # CHANGED: 300 -> 120 seconds (2 minutes)
        time.sleep(120)

if __name__ == "__main__":
    main()