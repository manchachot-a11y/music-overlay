import asyncio, websockets, json

async def test_host():
    async with websockets.connect("ws://localhost:8765") as ws:
        # Join as host
        await ws.send(json.dumps({"room": "abc123", "host": True}))
        # Broadcast position every second
        for i in range(30):
            await ws.send(json.dumps({"position": i, "at_utc": __import__('time').time()}))
            await asyncio.sleep(1)
    print("left")
asyncio.run(test_host())