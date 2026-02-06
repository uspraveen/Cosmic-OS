import sys
import json
import logging
from database import db

# Configure logging
logging.basicConfig(level=logging.ERROR)

def main():
    """
    Simple bridge to handle settings requests:
    - GET_ALL_SETTINGS -> returns JSON of all settings
    - SAVE_SETTING:key:value -> saves a setting
    """
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            if line == "GET_ALL_SETTINGS":
                settings = db.get_all_settings()
                print(f"<<SETTINGS>>{json.dumps(settings)}<<END>>")
                sys.stdout.flush()

            elif line.startswith("SAVE_SETTING:"):
                # format: SAVE_SETTING:key:value
                _, key, value = line.split(":", 2)
                db.set_setting(key, value)
                # echo back the updated settings to keep frontend in sync?
                # or just acknowledge? For now, let's auto-push all settings back
                # so the frontend definitely has the latest state.
                settings = db.get_all_settings()
                print(f"<<SETTINGS>>{json.dumps(settings)}<<END>>")
                sys.stdout.flush()

        except Exception as e:
            logging.error(f"Error processing line '{line}': {e}")

if __name__ == "__main__":
    main()
