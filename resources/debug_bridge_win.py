# resources/debug_bridge.py
import sys
import asyncio

print("--- DEBUG START ---")
sys.stdout.flush()

try:
    print("1. Importing libraries...")
    from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager
    from pycaw.pycaw import AudioUtilities
    import comtypes
    print("   -> Libraries Imported.")
except Exception as e:
    print(f"   -> IMPORT ERROR: {e}")

async def test_media():
    print("2. Testing Media Manager (Async)...")
    try:
        # Add a timeout so it doesn't hang forever
        mgr = await asyncio.wait_for(GlobalSystemMediaTransportControlsSessionManager.request_async(), timeout=3.0)
        print(f"   -> Media Manager Got: {mgr}")
    except asyncio.TimeoutError:
        print("   -> TIMEOUT: Media Manager took too long.")
    except Exception as e:
        print(f"   -> MEDIA ERROR: {e}")

def test_volume():
    print("3. Testing Volume (COM)...")
    try:
        # Force COM initialization
        comtypes.CoInitialize() 
        devices = AudioUtilities.GetSpeakers()
        print(f"   -> Speakers Found: {devices}")
    except Exception as e:
        print(f"   -> VOLUME ERROR: {e}")

async def main():
    await test_media()
    test_volume()
    print("--- DEBUG FINISHED ---")

if __name__ == "__main__":
    asyncio.run(main())