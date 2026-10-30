"""Device-wide display theme shared by Web previews and the physical panel."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re


THEME_FILE = Path("/var/lib/r1-web/theme.json")
DEFAULT = "current"
CHOICES = ("current", "mocha")

# Match the already approved Mocha preview. Keep warning and status roles distinct.
MOCHA = {
    "#0b121b": "#11111b", "#15232e": "#1e1e2e",
    "#17382f": "#2b3c37", "#244354": "#313244",
    "#263845": "#45475a", "#2a4050": "#45475a",
    "#327861": "#a6e3a1", "#344b57": "#45475a",
    "#3a5260": "#45475a", "#6bbfe5": "#89b4fa",
    "#71d4b7": "#94e2d5", "#74bfff": "#89b4fa",
    "#75c4dc": "#89dceb", "#8fa6b4": "#a6adc8",
    "#9ab2c0": "#bac2de", "#9bb1be": "#a6adc8",
    "#9ee8ce": "#a6e3a1", "#edf6f8": "#cdd6f4",
    "#f4b46f": "#fab387", "#f5ad75": "#fab387",
}
HEX = re.compile(rb"#[0-9a-fA-F]{6}\b")


def load_theme(path: Path | None = None) -> str:
    """An absent or damaged setting always falls back to the proven palette."""
    try:
        value = json.loads((path or THEME_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return DEFAULT
    return value["theme"] if isinstance(value, dict) and value.get("theme") in CHOICES else DEFAULT


def save_theme(name: str, path: Path | None = None) -> None:
    if type(name) is not str or name not in CHOICES:
        raise ValueError("invalid theme")
    target = path or THEME_FILE
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"theme": name}, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def theme_svg(svg: bytes, name: str | None = None) -> bytes:
    choice = load_theme() if name is None else name
    if choice not in CHOICES:
        raise ValueError("invalid theme")
    if choice == DEFAULT:
        return svg
    colors = {match.group().lower() for match in HEX.finditer(svg)}
    missing = colors - {key.encode() for key in MOCHA}
    if missing:
        raise ValueError(f"unmapped SVG colors: {sorted(missing)}")
    return HEX.sub(lambda match: MOCHA[match.group().decode().lower()].encode(), svg)
