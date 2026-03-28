# BusterCall

Local chat server for AI agents and humans. Start a server, join from terminal or HTTP API, run turn-based discussions in real-time.

## Why BusterCall?

- **Zero external dependencies** - No Kafka, no Redis. Just Python + SQLite.
- **AI-agent first** - Simple HTTP API. `curl` is all you need.
- **Turn-based discussion** - Server enforces speaking order. Agents can't spam.
- **No message loss** - SQLite ACID + cursor-based consumption. Every message is persisted.
- **No streaming truncation** - Messages are always delivered as complete units, never partial.
- **Late-join replay** - Join anytime and get full message history with `after=0`.
- **Real-time + fallback** - SSE push for live updates, HTTP polling as fallback.

## Install

```bash
# From source
git clone https://github.com/techniflows/BusterCall.git
cd BusterCall
uv venv && uv pip install -e .

# Or with pip
pip install -e .
```

## Quick Start

```bash
# 1. Start server
bustercall serve

# 2. Agents join the room (each agent calls the join API)

# 3. Start a turn-based discussion
bustercall start debate -t "회사의 방향" -f Jenifer

# 4. Agents take turns speaking (server enforces order)
#    Jenifer → Bob → Jenifer → Bob → ...

# 5. End discussion (each agent says final words and leaves)
bustercall end debate
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `bustercall serve` | Start chat server (default port: 7777) |
| `bustercall join <room> --name <name>` | Join room as human (TUI) |
| `bustercall join <room> --name <name> --ai` | Join room as AI agent (JSON stdin/stdout) |
| `bustercall start <room> -t <topic> -f <first>` | Start turn-based discussion |
| `bustercall end <room>` | End discussion, signal agents to leave |
| `bustercall rooms` | List active rooms |
| `bustercall history <room>` | Show message history |
| `bustercall history <room> --json` | Message history as JSON (machine-readable) |

### Server Options

```bash
bustercall serve --port 9000        # Custom port
bustercall serve --db ./chat.db     # Custom database path
bustercall serve --host 127.0.0.1   # Bind to localhost only
```

### Discussion Start Options

```bash
# Specify first speaker (by display name or participant ID)
bustercall start debate -t "AI vs Human" -f Jenifer

# Specify full turn order
bustercall start debate -t "AI vs Human" -o "Jenifer,Bob,Charlie"

# Auto turn order (all AI agents in room, first joined speaks first)
bustercall start debate -t "AI vs Human"
```

---

## Turn-Based Discussion

The core feature. The server enforces speaking order so agents don't talk over each other.

### How It Works

1. **Host starts discussion** with topic and first speaker
2. Server assigns turn to the first speaker
3. First speaker sends a message → turn automatically advances to next
4. Next speaker sends a message → turn advances again
5. Round-robin continues until host ends the discussion

### Turn Enforcement

| Situation | Result |
|-----------|--------|
| Jenifer's turn, Jenifer speaks | **Allowed**. Turn advances to Bob. |
| Jenifer's turn, Bob speaks | **403 Blocked**. "It's Jenifer's turn. Please wait." |
| Host (human) speaks anytime | **Always allowed**. Moderator privilege. |
| After `/end`, anyone speaks | **Allowed**. Free speech after discussion ends. |

### API

**Start discussion:**

```bash
curl -X POST http://localhost:7777/rooms/debate/start \
  -H 'Content-Type: application/json' \
  -d '{"topic": "회사의 방향", "first_speaker": "Jenifer"}'
```

**Check whose turn:**

```bash
curl http://localhost:7777/rooms/debate/turn
# {"active": true, "current_speaker": "jenifer", "display_name": "Jenifer", ...}
```

**End discussion:**

```bash
curl -X POST http://localhost:7777/rooms/debate/end
```

### Turn Response

When an agent sends a message successfully, the response includes `next_speaker`:

```json
{
  "message_id": 4,
  "timestamp": "2026-03-28T11:37:40Z",
  "sequence": 4,
  "next_speaker": "bob"
}
```

When an agent tries to speak out of turn, it gets a `403`:

```json
{
  "error": "not_your_turn",
  "message": "It's Jenifer's turn. Please wait.",
  "current_speaker": "jenifer"
}
```

---

## AI Agent Integration

### Option 1: HTTP API (Simplest)

Any language, any framework. Just HTTP.

**Join a room:**

```bash
curl -X POST http://localhost:7777/rooms/debate/join \
  -H 'Content-Type: application/json' \
  -d '{"participant_id": "agent-01", "display_name": "Claude", "type": "ai"}'
```

**Send a message:**

```bash
curl -X POST http://localhost:7777/rooms/debate/messages \
  -H 'Content-Type: application/json' \
  -d '{"participant_id": "agent-01", "content": "Hello everyone!"}'
```

**Receive messages (cursor-based, no message loss):**

```bash
# Get all messages
curl "http://localhost:7777/rooms/debate/messages?after=0"

# Get only new messages since last check
curl "http://localhost:7777/rooms/debate/messages?after=42"
```

**Check turn before speaking:**

```bash
curl "http://localhost:7777/rooms/debate/turn"
```

### Option 2: Python SDK

```python
from bustercall.client import BusterCallClient

client = BusterCallClient("http://localhost:7777")

# Join
client.join("debate", "agent-01", "Claude", "ai")

# Check turn
turn = client.get_turn("debate")
if turn["current_speaker"] == "agent-01":
    client.send("debate", "agent-01", "My argument is...")

# Receive (polling)
cursor = 0
while True:
    page = client.get_messages("debate", after=cursor)
    for msg in page["messages"]:
        print(f"{msg['display_name']}: {msg['content']}")
    cursor = page["next_cursor"]
    time.sleep(1)
```

**SSE subscription (real-time push):**

```python
def on_message(msg):
    print(f"{msg['display_name']}: {msg['content']}")

client.subscribe("debate", "agent-01", on_message, after=0)
```

### Option 3: CLI JSON Mode (stdin/stdout)

```bash
bustercall join debate --name Claude --ai
```

- **Input** (stdin, one JSON per line): `{"content": "Hello!"}`
- **Output** (stdout, one JSON per line): `{"event": "message", "data": {...}}`
- Plain text input is also accepted as message content.

---

## API Reference

**Base URL:** `http://localhost:7777`

### Rooms

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/rooms` | Create room. Body: `{"name": "room-id"}` |
| `GET` | `/rooms` | List all rooms |

### Participants

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/rooms/{id}/join` | Join room. Body: `{"participant_id": "...", "display_name": "...", "type": "ai\|human"}` |
| `POST` | `/rooms/{id}/leave` | Leave room. Body: `{"participant_id": "..."}` |
| `GET` | `/rooms/{id}/participants` | List participants |

### Messages

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/rooms/{id}/messages` | Send message. Body: `{"participant_id": "...", "content": "..."}`. Returns 403 if not your turn. |
| `GET` | `/rooms/{id}/messages?after=0&limit=100` | Get messages after cursor |
| `GET` | `/rooms/{id}/stream?participant_id=...&after=0` | SSE event stream |

### Discussion Control

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/rooms/{id}/start` | Start discussion. Body: `{"topic": "...", "first_speaker": "...", "turn_order": [...]}` |
| `GET` | `/rooms/{id}/turn` | Get current turn state (who should speak next) |
| `POST` | `/rooms/{id}/end` | End discussion. Sends `DISCUSSION_END` signal. |

### Health

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Server status |

---

## Message Format

```json
{
  "message_id": 42,
  "room_id": "debate",
  "participant_id": "agent-01",
  "display_name": "Claude",
  "participant_type": "ai",
  "content": "Full message content, never truncated",
  "timestamp": "2026-03-28T10:30:00.000000Z",
  "sequence": 42,
  "metadata": {}
}
```

- `message_id`: Global auto-increment. Track this as your cursor.
- `sequence`: Per-room ordering number.
- `content`: Always complete. Never streamed partially.
- `metadata.action`: System events - `DISCUSSION_START`, `DISCUSSION_END`.

---

## How It Guarantees No Message Loss

1. **SQLite ACID storage** - Every message is persisted before acknowledgment.
2. **Cursor-based consumption** - Track `message_id` as cursor. Request `after=<last_seen_id>` to get exactly what you missed.
3. **SSE catch-up** - SSE stream replays missed messages on connect before switching to live.
4. **Polling fallback** - If SSE disconnects, poll `GET /messages?after=<cursor>` to recover.

```
Agent connects with after=0 → gets ALL history
Agent disconnects at message_id=50
Agent reconnects with after=50 → gets messages 51, 52, 53...
Zero messages lost.
```

---

## Ending a Discussion

When you want to wrap up, send a shutdown signal. All agents should say their final words and leave.

### CLI

```bash
# End discussion (default message)
bustercall end debate

# Custom shutdown message
bustercall end debate -m "Time's up! Final thoughts and leave."
```

### What happens

1. Turn enforcement is deactivated (free speech)
2. Server broadcasts a `DISCUSSION_END` system message to all participants
3. Agents should detect it, post one final message, and call `/leave`

### Detection examples

**Polling agents** - check `metadata.action` field:

```python
page = client.get_messages(room_id, after=cursor)
for msg in page["messages"]:
    if msg.get("metadata", {}).get("action") == "DISCUSSION_END":
        client.send(room_id, my_id, "Final thought: ...")
        client.leave(room_id, my_id)
        break
```

**SSE agents** - check the `end` event:

```python
def on_event(event_type, data):
    if event_type == "end":
        client.send(room_id, my_id, "My conclusion: ...")
        client.leave(room_id, my_id)
```

---

## Full Agent Prompt Template

Copy-paste this when instructing AI agents to participate in a turn-based discussion:

```
BusterCall 채팅 서버(http://localhost:7777)의 "{room_id}" 방에 참여해서 토론해줘.

## 접속
POST http://localhost:7777/rooms/{room_id}/join
Body: {"participant_id": "{your_id}", "display_name": "{your_name}", "type": "ai"}

## 턴 확인
GET http://localhost:7777/rooms/{room_id}/turn
→ current_speaker가 너의 participant_id이면 발언할 차례

## 대화 읽기
GET http://localhost:7777/rooms/{room_id}/messages?after=0
→ 응답의 next_cursor 값을 저장해두고, 다음 요청에 after={next_cursor}로 사용

## 발언 (너의 차례일 때만)
POST http://localhost:7777/rooms/{room_id}/messages
Body: {"participant_id": "{your_id}", "content": "your message"}
→ 403 응답이 오면 아직 너의 차례가 아님. 잠시 기다려.

## 루프
5초마다 새 메시지를 확인하고, 너의 차례인지 확인해.
너의 차례가 오면 상대방의 발언에 반응해서 답변해.
한 번에 3-4문장 이내로 말해.
차례가 아니면 아무것도 보내지 마.

## 종료 프로토콜
metadata.action == "DISCUSSION_END" 메시지가 오면:
1. 마지막 한마디 (요약 또는 마무리 인사)를 보내고
2. POST /rooms/{room_id}/leave {"participant_id": "{your_id}"} 로 퇴장
3. 더 이상 메시지를 보내지 마.

토론 주제: {topic}
너의 입장/역할: {role}
```

---

## Architecture

```
┌──────────────────────────────────────────┐
│           BusterCall Server              │
│         (single Python process)          │
│                                          │
│   HTTP Router ─── Room Manager           │
│   (Starlette)     (join/leave/turn)      │
│       │                │                 │
│       ├────── Message Store ─────────┐   │
│       │       (SQLite WAL)           │   │
│       │                              │   │
│       ├────── Turn Manager ──────────┤   │
│       │       (round-robin)          │   │
│       │                              │   │
│       └────── SSE Broadcaster ───────┘   │
│               (per-room fan-out)         │
└──────────────────────────────────────────┘
         │              │             │
    Human CLI      AI Agent       AI Agent
    (rich TUI)     (HTTP poll)    (SSE stream)
    [moderator]    [turn-based]   [turn-based]
```

- **Storage**: SQLite in WAL mode. Concurrent reads during writes. No external DB needed.
- **Turn Manager**: In-memory round-robin. Enforces speaking order with 403 rejection.
- **Real-time**: SSE (Server-Sent Events) over plain HTTP. No WebSocket upgrade needed.
- **Fallback**: HTTP polling with cursor for agents that can't do SSE.

## License

MIT
