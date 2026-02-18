# resources/thumb_reader.py
# Standalone thumbnail fetcher - runs in separate process
import sys
import asyncio
import base64

async def main():
    try:
        from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager
        from winsdk.windows.storage.streams import DataReader
        
        manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
        session = manager.get_current_session()
        
        if not session:
            print("NO_SESSION")
            return
        
        props = await session.try_get_media_properties_async()
        
        if not props.thumbnail:
            print("NO_THUMB")
            return
        
        stream = await props.thumbnail.open_read_async()
        
        if not stream or stream.size == 0:
            print("EMPTY_STREAM")
            return
        
        reader = DataReader(stream.get_input_stream_at(0))
        await reader.load_async(stream.size)
        
        buffer = bytearray(stream.size)
        reader.read_bytes(buffer)
        
        if len(buffer) > 0:
            b64 = base64.b64encode(buffer).decode('utf-8')
            print(f"DATA:{b64}")
        else:
            print("EMPTY_BUFFER")
            
    except Exception as e:
        print(f"ERROR:{e}")

if __name__ == "__main__":
    asyncio.run(main())