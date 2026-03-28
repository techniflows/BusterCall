from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from bustercall.store import MessageStore
from bustercall.models import Message

logger = logging.getLogger("bustercall")

# Global state
_store: MessageStore | None = None
_subscribers: dict[str, list[asyncio.Queue]] = {}  # room_id -> list of queues


def get_store() -> MessageStore:
    assert _store is not None, "Store not initialized"
    return _store


def _broadcast(room_id: str, event: str, data: dict) -> None:
    queues = _subscribers.get(room_id, [])
    dead: list[asyncio.Queue] = []
    for q in queues:
        try:
            q.put_nowait({"event": event, "data": data})
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        queues.remove(q)


# -- Room endpoints --

async def create_room(request: Request) -> JSONResponse:
    body = await request.json()
    room_id = body.get("name") or body.get("room_id")
    if not room_id:
        return JSONResponse({"error": "name is required"}, status_code=400)
    description = body.get("description", "")
    room = get_store().create_room(room_id, description)
    return JSONResponse(room.to_dict(), status_code=201)


async def list_rooms(request: Request) -> JSONResponse:
    rooms = get_store().list_rooms()
    return JSONResponse(rooms)


# -- Participant endpoints --

async def join_room(request: Request) -> JSONResponse:
    room_id = request.path_params["room_id"]
    body = await request.json()
    participant_id = body.get("participant_id")
    display_name = body.get("display_name", participant_id)
    ptype = body.get("type", "ai")

    if not participant_id:
        return JSONResponse({"error": "participant_id is required"}, status_code=400)

    participant = get_store().join_room(room_id, participant_id, display_name, ptype)

    # Broadcast join event
    _broadcast(room_id, "join", {
        "participant_id": participant_id,
        "display_name": display_name,
        "type": ptype,
    })

    # Add system message
    get_store().add_message(
        room_id=room_id,
        participant_id="system",
        content=f"{display_name} joined the room",
        metadata={"content_type": "system"},
    )

    cursor = get_store().get_latest_message_id(room_id)
    result = participant.to_dict()
    result["cursor"] = cursor
    return JSONResponse(result)


async def leave_room(request: Request) -> JSONResponse:
    room_id = request.path_params["room_id"]
    body = await request.json()
    participant_id = body.get("participant_id")
    if not participant_id:
        return JSONResponse({"error": "participant_id is required"}, status_code=400)

    # Get display name before leaving
    participants = get_store().list_participants(room_id)
    display_name = participant_id
    for p in participants:
        if p["participant_id"] == participant_id:
            display_name = p["display_name"]
            break

    get_store().leave_room(room_id, participant_id)

    # Broadcast leave event
    _broadcast(room_id, "leave", {
        "participant_id": participant_id,
        "display_name": display_name,
    })

    # Add system message
    get_store().add_message(
        room_id=room_id,
        participant_id="system",
        content=f"{display_name} left the room",
        metadata={"content_type": "system"},
    )

    return JSONResponse({"left_at": get_store()._now()})


async def list_participants(request: Request) -> JSONResponse:
    room_id = request.path_params["room_id"]
    participants = get_store().list_participants(room_id)
    return JSONResponse(participants)


# -- Message endpoints --

async def send_message(request: Request) -> JSONResponse:
    room_id = request.path_params["room_id"]
    body = await request.json()
    participant_id = body.get("participant_id")
    content = body.get("content")
    metadata = body.get("metadata", {})

    if not participant_id or not content:
        return JSONResponse(
            {"error": "participant_id and content are required"}, status_code=400
        )

    msg = get_store().add_message(
        room_id=room_id,
        participant_id=participant_id,
        content=content,
        metadata=metadata,
    )

    # Broadcast to SSE subscribers
    _broadcast(room_id, "message", msg.to_dict())

    # Update heartbeat
    get_store().update_heartbeat(room_id, participant_id)

    return JSONResponse(
        {"message_id": msg.message_id, "timestamp": msg.timestamp, "sequence": msg.sequence},
        status_code=201,
    )


async def get_messages(request: Request) -> JSONResponse:
    room_id = request.path_params["room_id"]
    after = int(request.query_params.get("after", "0"))
    limit = min(int(request.query_params.get("limit", "100")), 500)

    page = get_store().get_messages(room_id, after=after, limit=limit)
    return JSONResponse(page.to_dict())


# -- SSE Stream --

async def stream_messages(request: Request) -> Response:
    room_id = request.path_params["room_id"]
    participant_id = request.query_params.get("participant_id", "anonymous")
    after = int(request.query_params.get("after", "0"))

    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

    if room_id not in _subscribers:
        _subscribers[room_id] = []
    _subscribers[room_id].append(queue)

    async def event_generator():
        try:
            # First, send any missed messages (catch-up)
            page = get_store().get_messages(room_id, after=after, limit=500)
            for msg in page.messages:
                yield f"event: message\ndata: {msg.to_json()}\n\n"

            # Then stream live events
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    event_type = event["event"]
                    data = json.dumps(event["data"], ensure_ascii=False)
                    yield f"event: {event_type}\ndata: {data}\n\n"
                except asyncio.TimeoutError:
                    # Send heartbeat
                    latest_id = get_store().get_latest_message_id(room_id)
                    hb = json.dumps({"timestamp": get_store()._now(), "last_message_id": latest_id})
                    yield f"event: heartbeat\ndata: {hb}\n\n"
                    # Update heartbeat for this participant
                    get_store().update_heartbeat(room_id, participant_id)
        except asyncio.CancelledError:
            pass
        finally:
            if queue in _subscribers.get(room_id, []):
                _subscribers[room_id].remove(queue)

    from starlette.responses import StreamingResponse
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# -- Room End (shutdown signal) --

async def end_room(request: Request) -> JSONResponse:
    room_id = request.path_params["room_id"]
    body = await request.json() if await request.body() else {}
    message = body.get("message", "The host has ended the discussion. Please say your final words and leave the room.")

    # Send DISCUSSION_END system message
    msg = get_store().add_message(
        room_id=room_id,
        participant_id="system",
        content=message,
        metadata={"content_type": "system", "action": "DISCUSSION_END"},
    )

    # Broadcast to SSE subscribers
    _broadcast(room_id, "message", msg.to_dict())
    _broadcast(room_id, "end", {"room_id": room_id, "message": message})

    return JSONResponse({
        "status": "ending",
        "room_id": room_id,
        "message": message,
        "message_id": msg.message_id,
    })


# -- Health --

async def health(request: Request) -> JSONResponse:
    rooms = get_store().list_rooms()
    total_participants = sum(r.get("participant_count", 0) for r in rooms)
    return JSONResponse({
        "status": "ok",
        "rooms": len(rooms),
        "participants": total_participants,
        "version": "0.1.0",
    })


# -- System message for registering system participant --

def _ensure_system_participant(store: MessageStore, room_id: str) -> None:
    store.join_room(room_id, "system", "System", "ai")


# -- App factory --

def create_app(db_path: str | Path | None = None) -> Starlette:
    global _store

    if db_path is None:
        data_dir = Path.home() / ".bustercall"
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / "bustercall.db"

    _store = MessageStore(db_path)

    routes = [
        Route("/health", health, methods=["GET"]),
        # Rooms
        Route("/rooms", create_room, methods=["POST"]),
        Route("/rooms", list_rooms, methods=["GET"]),
        # Participants
        Route("/rooms/{room_id}/join", join_room, methods=["POST"]),
        Route("/rooms/{room_id}/leave", leave_room, methods=["POST"]),
        Route("/rooms/{room_id}/participants", list_participants, methods=["GET"]),
        # Messages
        Route("/rooms/{room_id}/messages", send_message, methods=["POST"]),
        Route("/rooms/{room_id}/messages", get_messages, methods=["GET"]),
        # Room control
        Route("/rooms/{room_id}/end", end_room, methods=["POST"]),
        # SSE Stream
        Route("/rooms/{room_id}/stream", stream_messages, methods=["GET"]),
    ]

    middleware = [
        Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]),
    ]

    app = Starlette(routes=routes, middleware=middleware)
    return app


def run_server(host: str = "0.0.0.0", port: int = 7777, db_path: str | Path | None = None) -> None:
    import uvicorn
    app = create_app(db_path)
    logger.info(f"BusterCall server starting on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")
