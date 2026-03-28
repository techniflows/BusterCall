"""BusterCall AI Agent SDK - simple HTTP client for AI agents."""
from __future__ import annotations

import json
import threading
import time
from typing import Callable

import httpx


class BusterCallClient:
    """Simple HTTP client for AI agents to interact with BusterCall server.

    Usage:
        client = BusterCallClient("http://localhost:7777")
        client.join("war-room", "agent-01", "Claude", "ai")

        # Send a message
        client.send("war-room", "agent-01", "Hello everyone!")

        # Get messages (polling)
        page = client.get_messages("war-room", after=0)
        for msg in page["messages"]:
            print(f"{msg['display_name']}: {msg['content']}")

        # Subscribe with callback (background thread)
        def on_message(msg):
            print(f"{msg['display_name']}: {msg['content']}")

        client.subscribe("war-room", "agent-01", on_message)
    """

    def __init__(self, server_url: str = "http://localhost:7777", timeout: float = 30.0):
        self.server_url = server_url.rstrip("/")
        self._http = httpx.Client(timeout=timeout)
        self._subscribe_thread: threading.Thread | None = None
        self._subscribe_stop = threading.Event()

    def health(self) -> dict:
        resp = self._http.get(f"{self.server_url}/health")
        resp.raise_for_status()
        return resp.json()

    # -- Rooms --

    def create_room(self, name: str, description: str = "") -> dict:
        resp = self._http.post(
            f"{self.server_url}/rooms",
            json={"name": name, "description": description},
        )
        resp.raise_for_status()
        return resp.json()

    def list_rooms(self) -> list[dict]:
        resp = self._http.get(f"{self.server_url}/rooms")
        resp.raise_for_status()
        return resp.json()

    # -- Participants --

    def join(
        self, room_id: str, participant_id: str, display_name: str, participant_type: str = "ai"
    ) -> dict:
        resp = self._http.post(
            f"{self.server_url}/rooms/{room_id}/join",
            json={
                "participant_id": participant_id,
                "display_name": display_name,
                "type": participant_type,
            },
        )
        resp.raise_for_status()
        return resp.json()

    def leave(self, room_id: str, participant_id: str) -> dict:
        resp = self._http.post(
            f"{self.server_url}/rooms/{room_id}/leave",
            json={"participant_id": participant_id},
        )
        resp.raise_for_status()
        return resp.json()

    def list_participants(self, room_id: str) -> list[dict]:
        resp = self._http.get(f"{self.server_url}/rooms/{room_id}/participants")
        resp.raise_for_status()
        return resp.json()

    def start_discussion(
        self,
        room_id: str,
        topic: str,
        first_speaker: str | None = None,
        turn_order: list[str] | None = None,
    ) -> dict:
        body: dict = {"topic": topic}
        if first_speaker:
            body["first_speaker"] = first_speaker
        if turn_order:
            body["turn_order"] = turn_order
        resp = self._http.post(f"{self.server_url}/rooms/{room_id}/start", json=body)
        resp.raise_for_status()
        return resp.json()

    def get_turn(self, room_id: str) -> dict:
        resp = self._http.get(f"{self.server_url}/rooms/{room_id}/turn")
        resp.raise_for_status()
        return resp.json()

    def clear_room(self, room_id: str) -> dict:
        resp = self._http.post(f"{self.server_url}/rooms/{room_id}/clear", json={})
        resp.raise_for_status()
        return resp.json()

    def end_room(self, room_id: str, message: str | None = None) -> dict:
        body = {}
        if message:
            body["message"] = message
        resp = self._http.post(f"{self.server_url}/rooms/{room_id}/end", json=body)
        resp.raise_for_status()
        return resp.json()

    # -- Messages --

    def send(
        self,
        room_id: str,
        participant_id: str,
        content: str,
        metadata: dict | None = None,
    ) -> dict:
        resp = self._http.post(
            f"{self.server_url}/rooms/{room_id}/messages",
            json={
                "participant_id": participant_id,
                "content": content,
                "metadata": metadata or {},
            },
        )
        resp.raise_for_status()
        return resp.json()

    def get_context(self, room_id: str, recent: int = 20) -> dict:
        resp = self._http.get(
            f"{self.server_url}/rooms/{room_id}/context",
            params={"recent": recent},
        )
        resp.raise_for_status()
        return resp.json()

    def get_messages(self, room_id: str, after: int = 0, limit: int = 100) -> dict:
        resp = self._http.get(
            f"{self.server_url}/rooms/{room_id}/messages",
            params={"after": after, "limit": limit},
        )
        resp.raise_for_status()
        return resp.json()

    # -- SSE Subscription --

    def subscribe(
        self,
        room_id: str,
        participant_id: str,
        on_message: Callable[[dict], None],
        after: int = 0,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        """Subscribe to room messages via SSE in a background thread.

        Args:
            room_id: Room to subscribe to
            participant_id: Your participant ID
            on_message: Callback for new messages
            after: Message ID cursor to start from (0 = all history)
            on_event: Optional callback for all event types (join, leave, heartbeat)
        """
        self._subscribe_stop.clear()

        def _run():
            url = f"{self.server_url}/rooms/{room_id}/stream"
            params = {"participant_id": participant_id, "after": after}

            while not self._subscribe_stop.is_set():
                try:
                    with httpx.stream("GET", url, params=params, timeout=None) as resp:
                        for line in resp.iter_lines():
                            if self._subscribe_stop.is_set():
                                break
                            if not line:
                                continue

                            if line.startswith("event: "):
                                current_event = line[7:]
                            elif line.startswith("data: "):
                                try:
                                    data = json.loads(line[6:])
                                except json.JSONDecodeError:
                                    continue

                                if on_event:
                                    on_event(current_event, data)

                                if current_event == "message":
                                    on_message(data)
                                    # Update cursor for reconnection
                                    if "message_id" in data:
                                        params["after"] = data["message_id"]
                except (httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError):
                    if not self._subscribe_stop.is_set():
                        time.sleep(2)  # Reconnect after brief pause
                except Exception:
                    if not self._subscribe_stop.is_set():
                        time.sleep(5)

        self._subscribe_thread = threading.Thread(target=_run, daemon=True)
        self._subscribe_thread.start()

    def unsubscribe(self) -> None:
        self._subscribe_stop.set()
        if self._subscribe_thread:
            self._subscribe_thread.join(timeout=5)
            self._subscribe_thread = None

    # -- Polling helper --

    def poll_loop(
        self,
        room_id: str,
        participant_id: str,
        on_message: Callable[[dict], None],
        interval: float = 1.0,
        after: int = 0,
    ) -> None:
        """Simple polling loop for agents that prefer pull-based consumption.

        This blocks the current thread. Use in a background thread or as main loop.
        """
        cursor = after
        while not self._subscribe_stop.is_set():
            try:
                page = self.get_messages(room_id, after=cursor)
                for msg in page["messages"]:
                    if msg["participant_id"] != participant_id:
                        on_message(msg)
                cursor = page["next_cursor"]
            except Exception:
                pass
            time.sleep(interval)

    def close(self) -> None:
        self.unsubscribe()
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
