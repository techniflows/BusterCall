"""BusterCall Terminal UI - rich terminal interface for human participants."""
from __future__ import annotations

import json
import sys
import threading
import time

import httpx
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.layout import Layout

from bustercall.client import BusterCallClient

console = Console()

# Color map for participants
_COLORS = [
    "cyan", "green", "yellow", "magenta", "blue",
    "bright_red", "bright_green", "bright_cyan", "bright_magenta", "bright_yellow",
]
_color_map: dict[str, str] = {}
_color_idx = 0


def _get_color(participant_id: str) -> str:
    global _color_idx
    if participant_id == "system":
        return "dim white"
    if participant_id not in _color_map:
        _color_map[participant_id] = _COLORS[_color_idx % len(_COLORS)]
        _color_idx += 1
    return _color_map[participant_id]


def _format_message(msg: dict) -> Text:
    pid = msg.get("participant_id", "unknown")
    name = msg.get("display_name", pid)
    content = msg.get("content", "")
    ptype = msg.get("participant_type", "ai")
    meta = msg.get("metadata", {})
    color = _get_color(pid)

    text = Text()

    if meta.get("content_type") == "system":
        text.append(f"  --- {content} ---", style="dim italic")
    else:
        badge = "[AI]" if ptype == "ai" else "[ME]" if pid != "system" else ""
        text.append(f"{name}", style=f"bold {color}")
        if badge:
            text.append(f" {badge}", style="dim")
        text.append(f": {content}")

    return text


def run_tui(
    server_url: str,
    room_id: str,
    participant_id: str,
    display_name: str,
) -> None:
    """Run the interactive terminal UI for human chat participation."""

    client = BusterCallClient(server_url)

    # Join room
    try:
        result = client.join(room_id, participant_id, display_name, "human")
    except httpx.ConnectError:
        console.print(f"[red]Cannot connect to server at {server_url}[/red]")
        console.print("Start the server first: [bold]bustercall serve[/bold]")
        sys.exit(1)

    console.clear()
    console.print(Panel(
        f"[bold cyan]BusterCall[/bold cyan] - Room: [bold]{room_id}[/bold]\n"
        f"You are: [bold green]{display_name}[/bold green]\n"
        f"Server: {server_url}\n"
        f"Type your message and press Enter. Type [bold]/quit[/bold] to leave.",
        title="[bold]BUSTER CALL[/bold]",
        border_style="cyan",
    ))

    # Show existing participants
    try:
        participants = client.list_participants(room_id)
        online = [p for p in participants if p.get("online") and p["participant_id"] != "system"]
        if online:
            console.print(f"\n[dim]Online ({len(online)}):[/dim] ", end="")
            names = [f"[{_get_color(p['participant_id'])}]{p['display_name']}[/]" for p in online]
            console.print(", ".join(names))
            console.print()
    except Exception:
        pass

    # Load and display message history
    messages_lock = threading.Lock()

    try:
        page = client.get_messages(room_id, after=0, limit=50)
        if page["messages"]:
            console.print("[dim]--- Message History ---[/dim]")
            for msg in page["messages"]:
                console.print(_format_message(msg))
            console.print("[dim]--- End History ---[/dim]\n")
            cursor = page["next_cursor"]
        else:
            cursor = 0
    except Exception:
        cursor = 0

    # SSE subscription for real-time messages
    def on_event(event_type: str, data: dict):
        with messages_lock:
            if event_type == "message":
                # Don't display our own messages (we echo them locally)
                if data.get("participant_id") == participant_id:
                    return
                console.print(_format_message(data))
            elif event_type == "join":
                name = data.get("display_name", data.get("participant_id", "?"))
                console.print(f"  [dim italic]--- {name} joined ---[/dim italic]")
            elif event_type == "leave":
                name = data.get("display_name", data.get("participant_id", "?"))
                console.print(f"  [dim italic]--- {name} left ---[/dim italic]")

    client.subscribe(
        room_id,
        participant_id,
        on_message=lambda msg: None,  # handled by on_event
        after=cursor if cursor > 0 else 0,
        on_event=on_event,
    )

    # Input loop
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.formatted_text import HTML

        session = PromptSession()

        while True:
            try:
                user_input = session.prompt(
                    HTML(f"<ansicyan><b>{display_name}</b></ansicyan>> "),
                )
            except (EOFError, KeyboardInterrupt):
                break

            user_input = user_input.strip()
            if not user_input:
                continue

            if user_input.lower() in ("/quit", "/exit", "/q"):
                break

            if user_input.lower() == "/who":
                try:
                    participants = client.list_participants(room_id)
                    online = [p for p in participants if p.get("online") and p["participant_id"] != "system"]
                    console.print(f"\n[bold]Online ({len(online)}):[/bold]")
                    for p in online:
                        badge = "AI" if p.get("type") == "ai" else "Human"
                        color = _get_color(p["participant_id"])
                        console.print(f"  [{color}]{p['display_name']}[/] ({badge})")
                    console.print()
                except Exception as e:
                    console.print(f"[red]Error: {e}[/red]")
                continue

            if user_input.lower() == "/help":
                console.print(Panel(
                    "/quit  - Leave the room\n"
                    "/who   - Show online participants\n"
                    "/help  - Show this help",
                    title="Commands",
                    border_style="dim",
                ))
                continue

            # Send message
            try:
                result = client.send(room_id, participant_id, user_input)
                # Echo own message locally
                own_msg = {
                    "participant_id": participant_id,
                    "display_name": display_name,
                    "participant_type": "human",
                    "content": user_input,
                    "metadata": {},
                }
                with messages_lock:
                    console.print(_format_message(own_msg))
            except Exception as e:
                console.print(f"[red]Failed to send: {e}[/red]")

    finally:
        # Cleanup
        console.print("\n[dim]Leaving room...[/dim]")
        try:
            client.leave(room_id, participant_id)
        except Exception:
            pass
        client.close()
        console.print("[dim]Goodbye![/dim]")
