import asyncio, websockets, json

async def test_client():
    async with websockets.connect("ws://localhost:8765") as ws:
        await ws.send(json.dumps({"room": "abc123", "host": False}))
        async for msg in ws:
            print("Received:", json.loads(msg))

asyncio.run(test_client())