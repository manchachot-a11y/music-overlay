import asyncio
import websockets
import json

async def handler(ws):
    print("connected")
    # seek to 30s
    await asyncio.sleep(2)
    await ws.send(json.dumps({"seek": 30.0}))
    print("seek to 30s")
    await asyncio.sleep(999)

async def main():
    async with websockets.serve(handler, "localhost", 9001):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())