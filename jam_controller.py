import asyncio
import json
import threading
import time
import websockets
from websockets.exceptions import ConnectionClosed

RELAY_URL = "wss://lyric-overlay-relay-production.up.railway.app"

class JamController:
    def __init__(self, on_sync=None, on_host_left=None, on_peer_joined=None, on_peer_left=None, on_room_not_found=None):
        """
        on_sync(position, at_utc)   called when host broadcasts a position
        on_host_left()              called when host disconnects
        on_peer_joined(count)       called when someone joins the room
        on_peer_left(count)         called when someone leaves
        """
        self.on_room_not_found = on_room_not_found
        self.on_sync = on_sync
        self.on_host_left = on_host_left
        self.on_peer_joined = on_peer_joined
        self.on_peer_left = on_peer_left

        self.room_code = None
        self.is_host = False
        self.connected = False
        self._ws = None
        self._loop = None
        self._thread = None
        self._running = False

    def host(self, room_code):
        self.room_code = room_code
        self.is_host = True
        self._start(room_code, is_host=True)

    def join(self, room_code):
        self.room_code = room_code
        self.is_host = False
        self._start(room_code, is_host=False)

    def broadcast(self, position, at_utc, title="", artist=""):
        if not self.is_host or not self.connected:
            return
        self._send({
            "type": "sync",
            "position": position,
            "at_utc": at_utc,
            "title": title,
            "artist": artist
        })

    def disconnect(self):
        self._running = False
        if self._loop and self._ws:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)

    def _start(self, room_code, is_host):
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connect_loop())

    async def _connect_loop(self):
        while self._running:
            try:
                print(f"[Jam] connecting to relay server")
                async with websockets.connect(RELAY_URL) as ws:
                    self._ws = ws

                    # send join message
                    await ws.send(json.dumps({
                        "room": self.room_code,
                        "host": self.is_host
                    }))

                    # wait for server confirmation
                    raw = await ws.recv()
                    response = json.loads(raw)

                    if response.get("error") == "room_not_found":
                        print(f"[Jam] Room {self.room_code} not found")
                        self.connected = False
                        if self.on_room_not_found:
                            self.on_room_not_found()
                        return  # dont reconnect

                    if response.get("error") == "no_room":
                        print("[Jam] no room code provided dummy")
                        return

                    # successful join
                    self.connected = True
                    print(f"[Jam] Connected. room={self.room_code} host={self.is_host}")
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(raw)
                            self._handle_message(data)
                        except Exception as e:
                            print(f"[Jam] Message error: {e}")

            except ConnectionClosed:
                print("[Jam] Connection closed")
            except Exception as e:
                print(f"[Jam] Connection error: {e}")
            finally:
                self.connected = False
                self._ws = None

            if self._running:
                print("[Jam] reconnecting")
                await asyncio.sleep(3)

    def _handle_message(self, data):
        msg_type = data.get("type")

        if msg_type == "sync" and not self.is_host:
            if self.on_sync:
                self.on_sync(data["position"], data["at_utc"], data.get("title", ""), data.get("artist", ""))

        elif msg_type == "peer_joined":
            if self.on_peer_joined:
                self.on_peer_joined(data.get("count", 0))

        elif msg_type == "peer_left":
            if self.on_peer_left:
                self.on_peer_left(data.get("count", 0))

        elif data.get("event") == "host_left":
            print("[Jam] Host left")
            if self.on_host_left:
                self.on_host_left()

    def _send(self, data):
        if self._loop and self._ws and self.connected:
            asyncio.run_coroutine_threadsafe(
                self._ws.send(json.dumps(data)),
                self._loop
            )

if __name__ == "__main__":
    import sys

    mode = input("host or join (h/j)").strip().lower()
    room = input("Room code: ").strip().upper()

    if mode == "h":
        jam = JamController()
        jam.host(room)
        print(f"Hosting room {room}")
        pos = 0.0
        try:
            while True:
                if jam.connected:
                    jam.broadcast(pos, time.time())
                    print(f"[Host] Broadcast pos={pos:.1f}")
                pos += 2.0
                time.sleep(2)
        except KeyboardInterrupt:
            print("disconnecting")
            jam.disconnect()

    elif mode == "j":
        def on_sync(position, at_utc):
            target = position + (time.time() - at_utc)
            print(f"[Client] Received sync: position={position:.2f} at_utc={at_utc:.3f} -> target={target:.2f}")

        def on_host_left():
            print("[Client] Host left the room.")

        def on_peer_joined(count):
            print(f"[Client] Someone joined. {count} in room")

        def on_peer_left(count):
            print(f"[Client] Someone left. {count} in room")

        jam = JamController(
            on_sync=on_sync,
            on_host_left=on_host_left,
            on_peer_joined=on_peer_joined,
            on_peer_left=on_peer_left
        )
        jam.join(room)
        print(f"Joined room {room}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("disconnecting")
            jam.disconnect()

    else:
        print("Invalid choice")
        sys.exit(1)