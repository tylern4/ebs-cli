import json
from typing import Any

import typer
from rich.console import Console, RenderableType
from rich.text import Text

console = Console(highlight=False, soft_wrap=True)
error_console = Console(stderr=True, highlight=False, soft_wrap=True)


def safe_text(value: Any, *, limit: int = 240) -> str:
    """Bound display text and neutralize terminal control characters."""
    text = str(value)
    cleaned = "".join(character if character.isprintable() else " " for character in text)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > limit:
        return cleaned[: limit - 1] + "…"
    return cleaned


def text(value: Any, *, limit: int = 240) -> Text:
    return Text(safe_text(value, limit=limit))


def print_json(value: Any) -> None:
    typer.echo(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True))


def print_panel(renderable: RenderableType, *, title: str) -> None:
    from rich.panel import Panel

    if isinstance(renderable, str):
        renderable = Text(renderable)
    console.print(Panel(renderable, title=Text(title), border_style="blue", expand=False))


def print_error(message: str) -> None:
    error_console.print(f"Error: {message}", style="red", markup=False, highlight=False)
