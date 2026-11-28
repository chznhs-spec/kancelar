"""Čtení a zápis pracovních souborů VLČR pro aplikaci Poscilogramář.

Kontejner odpovídá formátu použitému ve VLČR Průzkumníku:
8 B magic + 2 B verze (big endian) + 4 B délka JSON metadat + zlib(JSON payload).

Časy hran jsou ukládány v absolutních nanosekundách. Starší soubory,
které ukládaly pouze index dílku, se při načtení automaticky převedou.
Pracovní soubor obsahuje také textové komentáře, šipky a nastavení
neviditelné zarovnávací mřížky anotací.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import struct
import zlib
from typing import Any, Optional

MAGIC = b"VLCRPOSC"  # přesně 8 bajtů
CONTAINER_VERSION = 1
MAX_CHANNELS = 10
MAX_ANNOTATIONS = 10_000
MAX_ANNOTATION_TEXT = 500
MAX_ANNOTATION_DESCRIPTION = 5_000
MAX_ANNOTATION_Y = 10_000_000
MIN_TIMELINE_DIVISIONS = 200
MAX_TIMELINE_NS = 10**15
DEFAULT_ANNOTATION_GRID_PX = 19

# Stejná posloupnost jako v GUI: 1–2–5 × 10⁻⁹ až 10⁻¹ s.
TIME_UNITS_NS = [
    mantissa * (10 ** (exponent + 9))
    for exponent in range(-9, 0)
    for mantissa in (1, 2, 5)
]
DEFAULT_TIME_UNIT_INDEX = 9  # 1 µs/dílek


def _now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def default_channels() -> list[dict[str, Any]]:
    return [{"name": "CH 1", "color": "#00FF66", "transitions": []}]


def default_workspace(title: str = "Nový oscilogram") -> dict[str, Any]:
    now = _now_iso()
    return {
        "title": title,
        "time_unit_index": DEFAULT_TIME_UNIT_INDEX,
        "transition_unit": "ns",
        "channels": default_channels(),
        "annotations": [],
        "annotation_grid_px": DEFAULT_ANNOTATION_GRID_PX,
        "created": now,
        "modified": now,
    }


def _metadata(title: str, workspace: dict[str, Any]) -> dict[str, Any]:
    return {
        "app": "poscilogramar",
        "application": "logic_analyzer",
        "aplikace": "logický analyzátor",
        "editor": "poscilogramar",
        "type": "logic_analyzer",
        "format": "vlcr",
        "container_format": "VLCRPOSC",
        "container_version": CONTAINER_VERSION,
        "schema_version": 3,
        "generator": "VLČR Poscilogramář",
        "title": title,
        "created": workspace.get("created") or _now_iso(),
        "modified": _now_iso(),
        "channel_count": len(workspace.get("channels", [])),
        "content_encoding": "zlib+json",
    }


def _normalise_color(value: Any, default: str = "#00FF66") -> str:
    color = str(value or default)
    if (
        len(color) != 7
        or not color.startswith("#")
        or any(ch not in "0123456789abcdefABCDEF" for ch in color[1:])
    ):
        return default
    return color.upper()


def _to_int(value: Any) -> int:
    """Převede čas na celé nanosekundy a odmítne NaN/nekonečno."""
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Časová poloha hrany není platné číslo.")
    if not math.isfinite(numeric):
        raise ValueError("Časová poloha hrany není platné číslo.")
    number = int(round(numeric))
    if number < 0 or number > MAX_TIMELINE_NS:
        raise ValueError("Časová poloha hrany je mimo podporovaný rozsah.")
    return number


def _annotation_y(value: Any) -> int:
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Svislá souřadnice anotace není platná.")
    if not math.isfinite(numeric):
        raise ValueError("Svislá souřadnice anotace není platná.")
    number = int(round(numeric))
    if number < 0 or number > MAX_ANNOTATION_Y:
        raise ValueError("Svislá souřadnice anotace je mimo podporovaný rozsah.")
    return number


def _normalise_annotations(value: Any) -> list[dict[str, Any]]:
    """Normalizuje textové poznámky a šipky včetně rozbalovacích popisů."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Seznam anotací má neplatnou strukturu.")
    if len(value) > MAX_ANNOTATIONS:
        raise ValueError(f"Soubor obsahuje více než {MAX_ANNOTATIONS} anotací.")

    result: list[dict[str, Any]] = []
    for number, item in enumerate(value, 1):
        if not isinstance(item, dict):
            raise ValueError(f"Anotace {number} má neplatnou strukturu.")

        kind = item.get("kind")
        if kind == "text":
            title = str(item.get("text", ""))[:MAX_ANNOTATION_TEXT]
            if not title.strip():
                continue
            description = str(item.get("description", ""))[:MAX_ANNOTATION_DESCRIPTION]
            # Nový formát rozlišuje prostý text a dlouhý komentář.
            # Zachováváme také starší záznamy, které používaly expandable.
            requested_type = str(item.get("comment_type", item.get("mode", ""))).lower()
            is_long_comment = (
                requested_type == "long"
                or bool(item.get("expandable", False))
                or bool(description.strip())
            )
            comment_type = "long" if is_long_comment else "plain"
            expandable = is_long_comment
            expanded = bool(item.get("expanded", False)) and expandable
            try:
                font_size = int(round(float(item.get("font_size", 10))))
            except (TypeError, ValueError, OverflowError):
                font_size = 10
            font_size = max(1, min(96, font_size))
            result.append({
                "kind": "text",
                "x_ns": _to_int(item.get("x_ns", 0)),
                "y": _annotation_y(item.get("y", 28)),
                "text": title,
                "description": description,
                "comment_type": comment_type,
                "expandable": expandable,
                "expanded": expanded,
                "color": _normalise_color(item.get("color"), "#FFFFFF"),
                "background": _normalise_color(item.get("background"), "#FFFFFF"),
                "font_size": font_size,
            })

        elif kind == "arrow":
            try:
                width = float(item.get("width", 2.2))
            except (TypeError, ValueError, OverflowError):
                width = 2.2
            if not math.isfinite(width) or width <= 0 or width > 100:
                width = 2.2
            result.append({
                "kind": "arrow",
                "x1_ns": _to_int(item.get("x1_ns", 0)),
                "y1": _annotation_y(item.get("y1", 28)),
                "x2_ns": _to_int(item.get("x2_ns", 0)),
                "y2": _annotation_y(item.get("y2", 28)),
                "color": _normalise_color(item.get("color"), "#FFFFFF"),
                "width": width,
            })

    return result


def _normalise_grid_px(value: Any) -> int:
    try:
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError
        grid = int(round(numeric))
    except (TypeError, ValueError, OverflowError):
        grid = DEFAULT_ANNOTATION_GRID_PX
    return max(2, min(152, grid))


def encode_workspace(
    workspace: dict[str, Any],
    title: Optional[str] = None
) -> bytes:
    channels = workspace.get("channels")
    if not isinstance(channels, list) or not 1 <= len(channels) <= MAX_CHANNELS:
        raise ValueError(f"Pracovní soubor musí obsahovat 1 až {MAX_CHANNELS} kanálů.")

    clean_channels = []
    for number, channel in enumerate(channels, 1):
        if not isinstance(channel, dict):
            raise ValueError(f"Kanál {number} má neplatnou strukturu.")
        name = str(channel.get("name") or f"CH {number}")[:80]
        color = _normalise_color(channel.get("color"))
        transitions = channel.get("transitions", [])
        if not isinstance(transitions, list):
            transitions = []
        clean_times = sorted({_to_int(v) for v in transitions})
        clean_channels.append({
            "name": name,
            "color": color,
            "transitions": clean_times
        })

    clean_annotations = _normalise_annotations(workspace.get("annotations", []))
    grid_px = _normalise_grid_px(
        workspace.get("annotation_grid_px", DEFAULT_ANNOTATION_GRID_PX)
    )

    payload = {
        "time_unit_index": int(workspace.get("time_unit_index", DEFAULT_TIME_UNIT_INDEX)),
        "max_ticks": MIN_TIMELINE_DIVISIONS,
        "transition_unit": "ns",
        "channels": clean_channels,
        "annotations": clean_annotations,
        "annotation_grid_px": grid_px,
    }

    title = title or str(workspace.get("title") or "Oscilogram")
    meta_raw = json.dumps(
        _metadata(title, workspace),
        ensure_ascii=False,
        separators=(",", ":")
    ).encode("utf-8")
    body_raw = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":")
    ).encode("utf-8")

    return (
        MAGIC
        + struct.pack(">HI", CONTAINER_VERSION, len(meta_raw))
        + meta_raw
        + zlib.compress(body_raw, 8)
    )


def decode_workspace_bytes(raw: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
    if len(raw) < 14 or raw[:8] != MAGIC:
        raise ValueError("Soubor není pracovní soubor Poscilogramáře (.vlcr).")

    version, meta_len = struct.unpack(">HI", raw[8:14])
    if version != CONTAINER_VERSION:
        raise ValueError(f"Nepodporovaná verze souboru: {version}.")
    if not 0 < meta_len <= 1024 * 1024 or len(raw) < 14 + meta_len:
        raise ValueError("Metadata souboru mají neplatnou délku.")

    metadata = json.loads(raw[14:14 + meta_len].decode("utf-8-sig"))
    payload = json.loads(zlib.decompress(raw[14 + meta_len:]).decode("utf-8-sig"))
    if not isinstance(metadata, dict) or not isinstance(payload, dict):
        raise ValueError("Obsah pracovního souboru má neplatnou strukturu.")

    unit_index = int(payload.get("time_unit_index", DEFAULT_TIME_UNIT_INDEX))
    unit_index = max(0, min(len(TIME_UNITS_NS) - 1, unit_index))
    is_absolute_ns = payload.get("transition_unit") == "ns"
    legacy_unit_ns = TIME_UNITS_NS[unit_index]

    channels = payload.get("channels")
    if not isinstance(channels, list) or not 1 <= len(channels) <= MAX_CHANNELS:
        raise ValueError(f"Soubor musí obsahovat 1 až {MAX_CHANNELS} kanálů.")

    normalized = []
    for number, channel in enumerate(channels, 1):
        if not isinstance(channel, dict):
            raise ValueError(f"Kanál {number} má neplatnou strukturu.")
        name = str(channel.get("name") or f"CH {number}")[:80]
        color = _normalise_color(channel.get("color"))
        values = channel.get("transitions", [])
        if not isinstance(values, list):
            values = []

        if is_absolute_ns:
            transitions = sorted({_to_int(value) for value in values})
        else:
            migrated = set()
            for value in values:
                tick = int(value)
                if tick < 0 or tick > 10**9:
                    continue
                migrated.add(_to_int(tick * legacy_unit_ns))
            transitions = sorted(migrated)

        normalized.append({
            "name": name,
            "color": color,
            "transitions": transitions
        })

    payload["channels"] = normalized
    payload["annotations"] = _normalise_annotations(payload.get("annotations", []))
    payload["annotation_grid_px"] = _normalise_grid_px(
        payload.get("annotation_grid_px", DEFAULT_ANNOTATION_GRID_PX)
    )
    payload["time_unit_index"] = unit_index
    payload["transition_unit"] = "ns"
    return metadata, payload


def load_workspace(path: str) -> tuple[dict[str, Any], dict[str, Any]]:
    with open(path, "rb") as file:
        return decode_workspace_bytes(file.read())


def save_workspace(
    path: str,
    workspace: dict[str, Any],
    title: Optional[str] = None
) -> None:
    data = encode_workspace(workspace, title)
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    temporary = path + ".tmp"
    try:
        with open(temporary, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
