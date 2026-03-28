"""BusterCall CLI - command-line interface for server and client operations."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import click


@click.group()
@click.version_option(version="0.1.0", prog_name="bustercall")
def main():
    """BusterCall - Local chat server for AI agents and humans."""
    pass


@main.command()
@click.option("--host", default="0.0.0.0", help="Bind host")
@click.option("--port", "-p", default=7777, help="Bind port")
@click.option("--db", default=None, help="SQLite database path")
def serve(host: str, port: int, db: str | None):
    """Start the BusterCall chat server."""
    from rich.console import Console
    from rich.panel import Panel

    console = Console()
    console.print(Panel(
        f"[bold cyan]BUSTER CALL[/bold cyan] Server\n\n"
        f"  URL:  [bold]http://{host}:{port}[/bold]\n"
        f"  DB:   [dim]{db or '~/.bustercall/bustercall.db'}[/dim]\n\n"
        f"  API Docs:\n"
        f"    POST /rooms                    - Create room\n"
        f"    GET  /rooms                    - List rooms\n"
        f"    POST /rooms/{{id}}/join          - Join room\n"
        f"    POST /rooms/{{id}}/messages      - Send message\n"
        f"    GET  /rooms/{{id}}/messages      - Get messages\n"
        f"    GET  /rooms/{{id}}/stream        - SSE stream\n\n"
        f"  Press Ctrl+C to stop.",
        title="[bold red]BUSTER CALL[/bold red]",
        border_style="red",
    ))

    db_path = Path(db) if db else None
    from bustercall.server import run_server
    run_server(host=host, port=port, db_path=db_path)


@main.command()
@click.argument("room_id")
@click.option("--name", "-n", default=None, help="Display name")
@click.option("--server", "-s", default="http://localhost:7777", help="Server URL")
@click.option("--ai", is_flag=True, help="Join as AI agent (stdin/stdout JSON mode)")
def join(room_id: str, name: str | None, server: str, ai: bool):
    """Join a chat room."""
    if ai:
        _run_ai_mode(server, room_id, name)
    else:
        _run_human_mode(server, room_id, name)


def _run_human_mode(server_url: str, room_id: str, name: str | None):
    """Run interactive TUI for human users."""
    if name is None:
        import getpass
        name = getpass.getuser()

    participant_id = f"human-{name}-{uuid.uuid4().hex[:6]}"

    from bustercall.tui import run_tui
    run_tui(server_url, room_id, participant_id, name)


def _run_ai_mode(server_url: str, room_id: str, name: str | None):
    """Run stdin/stdout JSON mode for AI agents.

    Input (stdin, one JSON per line):
        {"content": "Hello everyone!"}
        {"content": "What do you think?", "metadata": {"reply_to": 42}}

    Output (stdout, one JSON per line):
        {"event": "message", "data": {...}}
        {"event": "join", "data": {...}}
    """
    import json
    import threading

    from bustercall.client import BusterCallClient

    if name is None:
        name = f"agent-{uuid.uuid4().hex[:8]}"

    participant_id = f"ai-{name}"
    client = BusterCallClient(server_url)

    try:
        client.join(room_id, participant_id, name, "ai")
    except Exception as e:
        print(json.dumps({"error": f"Failed to join: {e}"}), flush=True)
        sys.exit(1)

    # Output welcome
    print(json.dumps({
        "event": "connected",
        "data": {
            "room_id": room_id,
            "participant_id": participant_id,
            "display_name": name,
        }
    }), flush=True)

    # Subscribe to messages and output to stdout
    def on_event(event_type: str, data: dict):
        # Skip own messages
        if event_type == "message" and data.get("participant_id") == participant_id:
            return
        print(json.dumps({"event": event_type, "data": data}, ensure_ascii=False), flush=True)

    # Get full history first
    page = client.get_messages(room_id, after=0, limit=500)
    for msg in page["messages"]:
        if msg["participant_id"] != participant_id:
            print(json.dumps({"event": "history", "data": msg}, ensure_ascii=False), flush=True)

    cursor = page["next_cursor"]

    client.subscribe(
        room_id,
        participant_id,
        on_message=lambda msg: None,
        after=cursor,
        on_event=on_event,
    )

    # Read from stdin and send
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                content = data.get("content", "")
                metadata = data.get("metadata", {})
                if content:
                    result = client.send(room_id, participant_id, content, metadata)
                    print(json.dumps({"event": "sent", "data": result}, ensure_ascii=False), flush=True)
            except json.JSONDecodeError:
                # Treat plain text as message content
                result = client.send(room_id, participant_id, line)
                print(json.dumps({"event": "sent", "data": result}, ensure_ascii=False), flush=True)
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        try:
            client.leave(room_id, participant_id)
        except Exception:
            pass
        client.close()


@main.command()
@click.option("--server", "-s", default="http://localhost:7777", help="Server URL")
def rooms(server: str):
    """List active chat rooms."""
    from rich.console import Console
    from rich.table import Table
    from bustercall.client import BusterCallClient

    console = Console()
    client = BusterCallClient(server)

    try:
        room_list = client.list_rooms()
    except Exception as e:
        console.print(f"[red]Cannot connect to server: {e}[/red]")
        sys.exit(1)

    if not room_list:
        console.print("[dim]No active rooms.[/dim]")
        return

    table = Table(title="Active Rooms")
    table.add_column("Room ID", style="cyan bold")
    table.add_column("Participants", justify="center")
    table.add_column("Description")
    table.add_column("Created", style="dim")

    for room in room_list:
        table.add_row(
            room["room_id"],
            str(room.get("participant_count", 0)),
            room.get("description", ""),
            room.get("created_at", "")[:19],
        )

    console.print(table)
    client.close()


@main.command()
@click.argument("room_id")
@click.option("--topic", "-t", required=True, help="Discussion topic")
@click.option("--first", "-f", default=None, help="Who speaks first (display name or participant ID)")
@click.option("--order", "-o", default=None, help="Turn order, comma-separated (display names or IDs)")
@click.option("--server", "-s", default="http://localhost:7777", help="Server URL")
def start(room_id: str, topic: str, first: str | None, order: str | None, server: str):
    """Start a turn-based discussion in a room."""
    from rich.console import Console
    from bustercall.client import BusterCallClient

    console = Console()
    client = BusterCallClient(server)

    turn_order = [x.strip() for x in order.split(",")] if order else None

    try:
        result = client.start_discussion(room_id, topic, first_speaker=first, turn_order=turn_order)
        console.print(f"[bold green]Discussion started in '{room_id}'[/bold green]")
        console.print(f"  Topic: [bold]{result['topic']}[/bold]")
        order_display = " -> ".join(result["turn_order"])
        console.print(f"  Turn order: {order_display}")
        console.print(f"  First speaker: [bold cyan]{result['current_speaker']}[/bold cyan]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)
    finally:
        client.close()


@main.command()
@click.argument("room_id")
@click.option("--server", "-s", default="http://localhost:7777", help="Server URL")
@click.option("--message", "-m", default=None, help="Custom shutdown message")
def end(room_id: str, server: str, message: str | None):
    """End a discussion. Signals all agents to say final words and leave."""
    from rich.console import Console
    from bustercall.client import BusterCallClient

    console = Console()
    client = BusterCallClient(server)

    try:
        result = client.end_room(room_id, message)
        console.print(f"[bold yellow]Discussion ending signal sent to room '{room_id}'[/bold yellow]")
        console.print(f"[dim]{result.get('message', '')}[/dim]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)
    finally:
        client.close()


@main.command()
@click.argument("room_id")
@click.option("--server", "-s", default="http://localhost:7777", help="Server URL")
@click.option("--after", default=0, help="Show messages after this ID")
@click.option("--json-output", "--json", is_flag=True, help="Output as JSON")
@click.option("--limit", "-l", default=100, help="Max messages to show")
def history(room_id: str, server: str, after: int, json_output: bool, limit: int):
    """Show message history for a room."""
    import json as json_mod
    from rich.console import Console
    from bustercall.client import BusterCallClient

    console = Console()
    client = BusterCallClient(server)

    try:
        page = client.get_messages(room_id, after=after, limit=limit)
    except Exception as e:
        console.print(f"[red]Cannot connect to server: {e}[/red]")
        sys.exit(1)

    if json_output:
        for msg in page["messages"]:
            print(json_mod.dumps(msg, ensure_ascii=False))
    else:
        if not page["messages"]:
            console.print(f"[dim]No messages in room '{room_id}'.[/dim]")
            return

        for msg in page["messages"]:
            pid = msg.get("participant_id", "?")
            name = msg.get("display_name", pid)
            content = msg.get("content", "")
            ts = msg.get("timestamp", "")[:19]
            meta = msg.get("metadata", {})

            if meta.get("content_type") == "system":
                console.print(f"  [dim]{ts} --- {content} ---[/dim]")
            else:
                ptype = msg.get("participant_type", "ai")
                badge = "AI" if ptype == "ai" else "Human"
                console.print(f"  [dim]{ts}[/dim] [bold]{name}[/bold] ({badge}): {content}")

    if page.get("has_more"):
        console.print(f"\n[dim]More messages available. Use --after {page['next_cursor']}[/dim]")

    client.close()


if __name__ == "__main__":
    main()
