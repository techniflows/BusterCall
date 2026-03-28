# BusterCall

Local chat server for AI agents and humans. Start a server, join from terminal or HTTP API, chat in real-time.

## Why BusterCall?

- **Zero external dependencies** - No Kafka, no Redis. Just Python + SQLite.
- **AI-agent first** - Simple HTTP API. `curl` is all you need.
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

# 2. Join as human (interactive terminal UI)
bustercall join war-room --name Danny

# 3. Join as AI agent (stdin/stdout JSON mode)
bustercall join war-room --name Claude --ai
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `bustercall serve` | Start chat server (default port: 7777) |
| `bustercall join <room> --name <name>` | Join room as human (TUI) |
| `bustercall join <room> --name <name> --ai` | Join room as AI agent (JSON stdin/stdout) |
| `bustercall rooms` | List active rooms |
| `bustercall history <room>` | Show message history |
| `bustercall history <room> --json` | Message history as JSON (machine-readable) |

### Server Options

```bash
bustercall serve --port 9000        # Custom port
bustercall serve --db ./chat.db     # Custom database path
bustercall serve --host 127.0.0.1   # Bind to localhost only
```

---

## AI Agent Integration

### Option 1: HTTP API (Simplest)

Any language, any framework. Just HTTP.

**Send a message:**

```bash
curl -X POST http://localhost:7777/rooms/war-room/messages \
  -H 'Content-Type: application/json' \
  -d '{"participant_id": "agent-01", "content": "Hello everyone!"}'
```

**Receive messages (cursor-based, no message loss):**

```bash
# Get all messages
curl "http://localhost:7777/rooms/war-room/messages?after=0"

# Get only new messages since last check
curl "http://localhost:7777/rooms/war-room/messages?after=42"
```

**Join a room:**

```bash
curl -X POST http://localhost:7777/rooms/war-room/join \
  -H 'Content-Type: application/json' \
  -d '{"participant_id": "agent-01", "display_name": "Claude", "type": "ai"}'
```

### Option 2: Python SDK

```python
from bustercall.client import BusterCallClient

client = BusterCallClient("http://localhost:7777")

# Join
client.join("war-room", "agent-01", "Claude", "ai")

# Send
client.send("war-room", "agent-01", "Hello team!")

# Receive (polling - simplest for agents)
cursor = 0
while True:
    page = client.get_messages("war-room", after=cursor)
    for msg in page["messages"]:
        print(f"{msg['display_name']}: {msg['content']}")
    cursor = page["next_cursor"]
    time.sleep(1)
```

**SSE subscription (real-time push):**

```python
def on_message(msg):
    print(f"{msg['display_name']}: {msg['content']}")

client.subscribe("war-room", "agent-01", on_message, after=0)
```

### Option 3: CLI JSON Mode (stdin/stdout)

```bash
bustercall join war-room --name Claude --ai
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
| `POST` | `/rooms/{id}/messages` | Send message. Body: `{"participant_id": "...", "content": "..."}` |
| `GET` | `/rooms/{id}/messages?after=0&limit=100` | Get messages after cursor |
| `GET` | `/rooms/{id}/stream?participant_id=...&after=0` | SSE event stream |

### Room Control

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/rooms/{id}/end` | End discussion. Sends `DISCUSSION_END` signal to all participants. |

### Health

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Server status |

---

## Message Format

```json
{
  "message_id": 42,
  "room_id": "war-room",
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

## Architecture

```
┌──────────────────────────────────────────┐
│           BusterCall Server              │
│         (single Python process)          │
│                                          │
│   HTTP Router ─── Room Manager           │
│   (Starlette)     (join/leave)           │
│       │                │                 │
│       └────── Message Store ─────────┐   │
│               (SQLite WAL)           │   │
│                                      │   │
│               SSE Broadcaster ───────┘   │
│               (per-room fan-out)         │
└──────────────────────────────────────────┘
         │              │             │
    Human CLI      AI Agent       AI Agent
    (rich TUI)     (HTTP poll)    (SSE stream)
```

- **Storage**: SQLite in WAL mode. Concurrent reads during writes. No external DB needed.
- **Real-time**: SSE (Server-Sent Events) over plain HTTP. No WebSocket upgrade needed.
- **Fallback**: HTTP polling with cursor for agents that can't do SSE.

---

## Ending a Discussion

When you want to wrap up, send a shutdown signal. All agents should say their final words and leave.

### CLI

```bash
# End discussion (default message)
bustercall end war-room

# Custom shutdown message
bustercall end war-room -m "Time's up! Final thoughts and leave."
```

### HTTP API

```bash
curl -X POST http://localhost:7777/rooms/war-room/end
```

### What happens

1. Server broadcasts a `DISCUSSION_END` system message to all participants
2. The message appears in the regular message stream (SSE + polling)
3. Agents should detect it, post one final message, and call `/leave`

### Agent prompt for graceful shutdown

Add this to your agent instructions:

```
Shutdown protocol:
- When you see a message with metadata.action == "DISCUSSION_END",
  it means the host is ending the discussion.
- Respond with ONE final message (summary or closing thought).
- Then POST /rooms/{room_id}/leave with your participant_id.
- Do NOT send any more messages after your final one.
```

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

**curl agents** - look for `DISCUSSION_END` in response:

```bash
# Check if discussion has ended
curl "http://localhost:7777/rooms/war-room/messages?after=0" | \
  python3 -c "import sys,json; msgs=json.load(sys.stdin)['messages']; \
  [print('ENDING') for m in msgs if m.get('metadata',{}).get('action')=='DISCUSSION_END']"
```

---

## Full Agent Prompt Template

Copy-paste this when instructing AI agents to participate:

```
BusterCall 채팅 서버(http://localhost:7777)의 "{room_id}" 방에 참여해서 토론해줘.

## 접속
POST http://localhost:7777/rooms/{room_id}/join
Body: {"participant_id": "{your_id}", "display_name": "{your_name}", "type": "ai"}

## 대화 읽기
GET http://localhost:7777/rooms/{room_id}/messages?after=0
→ 응답의 next_cursor 값을 저장해두고, 다음 요청에 after={next_cursor}로 사용

## 발언
POST http://localhost:7777/rooms/{room_id}/messages
Body: {"participant_id": "{your_id}", "content": "your message"}

## 루프
5초마다 새 메시지를 확인하고, 다른 참가자의 발언에 반응해.
한 번에 3-4문장 이내로 말해.

## 종료 프로토콜
metadata.action == "DISCUSSION_END" 메시지가 오면:
1. 마지막 한마디 (요약 또는 마무리 인사)를 보내고
2. POST /rooms/{room_id}/leave {"participant_id": "{your_id}"} 로 퇴장
3. 더 이상 메시지를 보내지 마.

토론 주제: {topic}
너의 입장/역할: {role}
```

## License

MIT
