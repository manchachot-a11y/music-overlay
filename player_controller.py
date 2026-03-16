import asyncio
import json
import threading
import time
import websockets
from websockets.exceptions import ConnectionClosed

SPICETIFY_WS_PORT = 9001

class SpicetifyController:
    def __init__(self, on_connected=None, on_disconnected=None):
        """
        on_connected()     called when Spotify extension connects
        on_disconnected()  called when it disconnects
        """
        self.on_connected = on_connected
        self.on_disconnected = on_disconnected

        self.connected = False
        self._ws = None
        self._loop = None
        self._thread = None
        self._running = False
        self._pending = []  # queue commands while disconnected


    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def seek(self, position_seconds):
        self._send({"seek": round(position_seconds, 3)})

    def toggle_play(self):
        self._send({"toggle_play": True})


    def _send(self, data):
        if self._loop and self._ws and self.connected:
            asyncio.run_coroutine_threadsafe(
                self._ws.send(json.dumps(data)),
                self._loop
            )
        else:
            # queue
            self._pending.append(data)
            print(f"[Spicetify] queued bc not connected: {data}")

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self):
        print(f"[Spicetify] listening on ws://localhost:{SPICETIFY_WS_PORT}")
        async with websockets.serve(self._handle_extension, "localhost", SPICETIFY_WS_PORT):
            while self._running:
                await asyncio.sleep(0.1)

    async def _handle_extension(self, ws):
        self._ws = ws
        self.connected = True
        print("[Spicetify] connected")

        if self.on_connected:
            self.on_connected()

        # flush queued commands
        for data in self._pending:
            try:
                await ws.send(json.dumps(data))
                print(f"[Spicetify] flushed: {data}")
            except Exception:
                pass
        self._pending.clear()

        try:
            #keep connection alive
            async for raw in ws:
                try:
                    data = json.loads(raw)
                    self._handle_incoming(data)
                except Exception as e:
                    print(f"[Spicetify] Parse error: {e}")
        except ConnectionClosed:
            pass
        finally:
            self.connected = False
            self._ws = None
            print("[Spicetify] disconnected")
            if self.on_disconnected:
                self.on_disconnected()

    def _handle_incoming(self, data):
        print(f"[Spicetify] Incoming: {data}")


class YTMusicController:
    def __init__(self, on_connected=None, on_disconnected=None):
        self.on_connected = on_connected
        self.on_disconnected = on_disconnected
        self.connected = False
        self._ws = None
        self._loop = None
        self._thread = None
        self._running = False
        self._pending = []

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def seek(self, position_seconds):
        self._send({"seek": round(position_seconds, 3)})

    def toggle_play(self):
        self._send({"toggle_play": True})

    def _send(self, data):
        if self._loop and self._ws and self.connected:
            asyncio.run_coroutine_threadsafe(
                self._ws.send(json.dumps(data)),
                self._loop
            )
        else:
            self._pending.append(data)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self):
        print(f"[YTMusic] Listening on ws://localhost:9002")
        async with websockets.serve(self._handle_extension, "localhost", 9002):
            while self._running:
                await asyncio.sleep(0.1)

    async def _handle_extension(self, ws):
        self._ws = ws
        self.connected = True
        print("[YTMusic] Extension connected")
        if self.on_connected:
            self.on_connected()

        for data in self._pending:
            try:
                await ws.send(json.dumps(data))
            except Exception:
                pass
        self._pending.clear()

        try:
            async for raw in ws:
                pass  # no incoming messages needed yet
        except ConnectionClosed:
            pass
        finally:
            self.connected = False
            self._ws = None
            print("[YTMusic] extension disconnected")
            if self.on_disconnected:
                self.on_disconnected()

class PlayerRouter:
    def __init__(self):
        self.spicetify = SpicetifyController(
            on_connected=lambda: print("[Player] Spotify connected"),
            on_disconnected=lambda: print("[Player] Spotify disconnected")
        )
        self.ytmusic = YTMusicController(
            on_connected=lambda: print("[Player] YTM connected"),
            on_disconnected=lambda: print("[Player] YTM disconnected")
        )
        self._current_app_id = ""

    def start(self):
        self.spicetify.start()
        self.ytmusic.start()

    def stop(self):
        self.spicetify.stop()
        self.ytmusic.stop()

    def set_app_id(self, app_id):
        """Call on new app id in MediaThread"""
        self._current_app_id = app_id.lower()

    def seek(self, position_seconds):
        controller = self._get_controller()
        if controller:
            controller.seek(position_seconds)
        else:
            print(f"[Player] No controller for app_id: {self._current_app_id}")

    def toggle_play(self):
        controller = self._get_controller()
        if controller:
            controller.toggle_play()

    @property
    def connected(self):
        controller = self._get_controller()
        return controller.connected if controller else False

    def _get_controller(self):
        if "spotify" in self._current_app_id and "chrome" not in self._current_app_id and "msedge" not in self._current_app_id:
            return self.spicetify
        elif "chrome" in self._current_app_id or "msedge" in self._current_app_id:
            return self.ytmusic
        return None
    

if __name__ == "__main__":
    controller = YTMusicController(
        on_connected=lambda: print("YTMusic ready"),
        on_disconnected=lambda: print("YTMusic disconnected")
    )
    controller.start()

    try:
        while True:
            cmd = input("> ").strip().lower()
            if cmd.startswith("s "):
                try:
                    pos = float(cmd[2:])
                    controller.seek(pos)
                    print(f"Seeking to {pos}s")
                except ValueError:
                    print("Usage: s 30.5")
            elif cmd == "p":
                controller.toggle_play()
            elif cmd == "q":
                break
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop()