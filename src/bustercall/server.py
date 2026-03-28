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
_turn_state: dict[str, dict] = {}  # room_id -> turn tracking state


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


# -- Discussion start (turn-based) --

async def start_discussion(request: Request) -> JSONResponse:
    room_id = request.path_params["room_id"]
    body = await request.json()
    topic = body.get("topic", "")
    first_speaker = body.get("first_speaker")
    turn_order = body.get("turn_order", [])

    if not topic:
        return JSONResponse({"error": "topic is required"}, status_code=400)

    # Auto-build turn_order from online AI participants if not provided
    if not turn_order:
        participants = get_store().list_participants(room_id)
        turn_order = [
            p["participant_id"] for p in participants
            if p.get("online") and p.get("type") == "ai" and p["participant_id"] != "system"
        ]

    if not turn_order:
        return JSONResponse({"error": "No AI participants in room"}, status_code=400)

    # If first_speaker is a display_name, resolve to participant_id
    if first_speaker:
        participants = get_store().list_participants(room_id)
        for p in participants:
            if p["display_name"] == first_speaker or p["participant_id"] == first_speaker:
                first_speaker = p["participant_id"]
                break
        # Reorder turn_order so first_speaker is first
        if first_speaker in turn_order:
            idx = turn_order.index(first_speaker)
            turn_order = turn_order[idx:] + turn_order[:idx]
        else:
            return JSONResponse(
                {"error": f"first_speaker '{first_speaker}' not found in turn_order"},
                status_code=400,
            )

    # Set turn state
    _turn_state[room_id] = {
        "active": True,
        "topic": topic,
        "turn_order": turn_order,
        "current_index": 0,
    }

    current_speaker = turn_order[0]

    # Resolve display names for the announcement
    participants = get_store().list_participants(room_id)
    name_map = {p["participant_id"]: p["display_name"] for p in participants}
    speaker_name = name_map.get(current_speaker, current_speaker)
    order_names = [name_map.get(pid, pid) for pid in turn_order]

    # Broadcast start message
    start_content = f"Discussion started!\nTopic: {topic}\nTurn order: {' → '.join(order_names)}\n@{speaker_name}, you go first."
    msg = get_store().add_message(
        room_id=room_id,
        participant_id="system",
        content=start_content,
        metadata={
            "content_type": "system",
            "action": "DISCUSSION_START",
            "topic": topic,
            "turn_order": turn_order,
            "current_speaker": current_speaker,
        },
    )
    _broadcast(room_id, "message", msg.to_dict())
    _broadcast(room_id, "turn", {
        "current_speaker": current_speaker,
        "display_name": speaker_name,
        "turn_order": turn_order,
        "topic": topic,
    })

    return JSONResponse({
        "status": "started",
        "room_id": room_id,
        "topic": topic,
        "turn_order": turn_order,
        "current_speaker": current_speaker,
    })


async def get_turn(request: Request) -> JSONResponse:
    room_id = request.path_params["room_id"]
    state = _turn_state.get(room_id)
    if not state or not state["active"]:
        return JSONResponse({"active": False, "message": "No active discussion"})

    current_speaker = state["turn_order"][state["current_index"]]
    participants = get_store().list_participants(room_id)
    name_map = {p["participant_id"]: p["display_name"] for p in participants}

    return JSONResponse({
        "active": True,
        "topic": state["topic"],
        "current_speaker": current_speaker,
        "display_name": name_map.get(current_speaker, current_speaker),
        "turn_order": state["turn_order"],
        "turn_index": state["current_index"],
    })


def _advance_turn(room_id: str) -> str | None:
    state = _turn_state.get(room_id)
    if not state or not state["active"]:
        return None
    state["current_index"] = (state["current_index"] + 1) % len(state["turn_order"])
    next_speaker = state["turn_order"][state["current_index"]]

    participants = get_store().list_participants(room_id)
    name_map = {p["participant_id"]: p["display_name"] for p in participants}

    _broadcast(room_id, "turn", {
        "current_speaker": next_speaker,
        "display_name": name_map.get(next_speaker, next_speaker),
        "turn_order": state["turn_order"],
        "topic": state["topic"],
    })
    return next_speaker


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

    # Turn-based enforcement: check if it's this participant's turn
    state = _turn_state.get(room_id)
    if state and state["active"] and participant_id != "system":
        # Humans (host) can always speak
        participants = get_store().list_participants(room_id)
        ptype = "ai"
        for p in participants:
            if p["participant_id"] == participant_id:
                ptype = p.get("type", "ai")
                break

        if ptype == "ai" and participant_id in state["turn_order"]:
            current_speaker = state["turn_order"][state["current_index"]]
            if participant_id != current_speaker:
                # Resolve names for error message
                name_map = {p["participant_id"]: p["display_name"] for p in participants}
                return JSONResponse({
                    "error": "not_your_turn",
                    "message": f"It's {name_map.get(current_speaker, current_speaker)}'s turn. Please wait.",
                    "current_speaker": current_speaker,
                }, status_code=403)

    # Auto-tag human messages with from_host
    all_participants = get_store().list_participants(room_id)
    for p in all_participants:
        if p["participant_id"] == participant_id and p.get("type") == "human":
            metadata["from_host"] = True
            break

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

    # Advance turn if this was a turn-order participant
    next_speaker = None
    if state and state["active"] and participant_id in state.get("turn_order", []):
        next_speaker = _advance_turn(room_id)

    result = {
        "message_id": msg.message_id,
        "timestamp": msg.timestamp,
        "sequence": msg.sequence,
    }
    if next_speaker:
        result["next_speaker"] = next_speaker

    return JSONResponse(result, status_code=201)


async def get_messages(request: Request) -> JSONResponse:
    room_id = request.path_params["room_id"]
    after = int(request.query_params.get("after", "0"))
    limit = min(int(request.query_params.get("limit", "100")), 500)

    page = get_store().get_messages(room_id, after=after, limit=limit)
    return JSONResponse(page.to_dict())


async def get_context(request: Request) -> JSONResponse:
    """Return recent messages + turn state for AI agent context building.

    Designed to give agents just enough context to respond intelligently
    without flooding their context window with full history.
    """
    room_id = request.path_params["room_id"]
    recent = min(int(request.query_params.get("recent", "20")), 100)

    messages = get_store().get_recent_messages(room_id, limit=recent)

    # Include turn state
    state = _turn_state.get(room_id)
    turn_info = None
    if state and state["active"]:
        current_speaker = state["turn_order"][state["current_index"]]
        participants = get_store().list_participants(room_id)
        name_map = {p["participant_id"]: p["display_name"] for p in participants}
        turn_info = {
            "topic": state["topic"],
            "current_speaker": current_speaker,
            "display_name": name_map.get(current_speaker, current_speaker),
            "turn_order": state["turn_order"],
        }

    # Extract host (human) messages that agents should prioritize
    host_messages = [
        m.to_dict() for m in messages
        if m.metadata.get("from_host")
    ]

    # Cursor for incremental polling after this
    cursor = messages[-1].message_id if messages else 0

    return JSONResponse({
        "messages": [m.to_dict() for m in messages],
        "host_messages": host_messages,
        "turn": turn_info,
        "cursor": cursor,
        "total_messages": get_store().get_latest_message_id(room_id),
    })


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


# -- Clear room history --

async def clear_room(request: Request) -> JSONResponse:
    room_id = request.path_params["room_id"]
    count = get_store().clear_messages(room_id)

    # Reset turn state if active
    if room_id in _turn_state:
        del _turn_state[room_id]

    _broadcast(room_id, "clear", {"room_id": room_id, "deleted": count})

    return JSONResponse({
        "status": "cleared",
        "room_id": room_id,
        "deleted": count,
    })


# -- Room End (shutdown signal) --

async def end_room(request: Request) -> JSONResponse:
    room_id = request.path_params["room_id"]
    body = await request.json() if await request.body() else {}
    message = body.get("message", "The host has ended the discussion. Please say your final words and leave the room.")

    # Deactivate turn state
    if room_id in _turn_state:
        _turn_state[room_id]["active"] = False

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
        Route("/rooms/{room_id}/context", get_context, methods=["GET"]),
        # Discussion control
        Route("/rooms/{room_id}/start", start_discussion, methods=["POST"]),
        Route("/rooms/{room_id}/turn", get_turn, methods=["GET"]),
        Route("/rooms/{room_id}/clear", clear_room, methods=["POST"]),
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
