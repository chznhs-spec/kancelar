
"""Čtení a zápis pracovních souborů VLČR pro aplikaci Poscilogramář.

Kontejner odpovídá formátu použitému ve VLČR Průzkumníku:
8 B magic + 2 B verze (big endian) + 4 B délka JSON metadat + zlib(JSON payload).

Nová verze ukládá časy hran v absolutních nanosekundách. Starší soubory,
které ukládaly pouze index dílku, se při načtení automaticky převedou.

Podporuje také ukládání textových poznámek a šipek (anotací).
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
MAX_ANNOTATION_Y = 10_000_000

MIN_TIMELINE_DIVISIONS = 200
MAX_TIMELINE_NS = 10**15

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
    return [
        {
            "name": "CH 1",
            "color": "#00FF66",
            "transitions": []
        }
    ]


def default_workspace(title: str = "Nový oscilogram") -> dict[str, Any]:
    now = _now_iso()

    return {
        "title": title,
        "time_unit_index": DEFAULT_TIME_UNIT_INDEX,
        "transition_unit": "ns",
        "channels": default_channels(),
        "annotations": [],
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
        "schema_version": 2,
        "generator": "VLČR Poscilogramář 1.1.0",
        "title": title,
        "created": workspace.get("created") or _now_iso(),
        "modified": _now_iso(),
        "channel_count": len(workspace.get("channels", [])),
        "content_encoding": "zlib+json",
    }


def _normalise_color(value: Any) -> str:
    color = str(value or "#00FF66")

    if (
        len(color) != 7
        or not color.startswith("#")
        or any(char not in "0123456789abcdefABCDEF" for char in color[1:])
    ):
        return "#00FF66"

    return color.upper()


def _normalise_annotation_color(value: Any, default: str) -> str:
    """Kontrola barvy anotace ve formátu #RRGGBB."""
    color = str(value or default)

    if (
        len(color) != 7
        or not color.startswith("#")
        or any(char not in "0123456789abcdefABCDEF" for char in color[1:])
    ):
        return default

    return color.upper()


def _to_int(value: Any) -> int:
    """Převede čas na celé nanosekundy a ověří jeho rozsah."""
    number = float(value)

    if not math.isfinite(number):
        raise ValueError("Časová poloha hrany není platné číslo.")

    number = int(round(number))

    if number < 0 or number > MAX_TIMELINE_NS:
        raise ValueError(
            "Časová poloha hrany je mimo podporovaný rozsah."
        )

    return number


def _annotation_y(value: Any) -> int:
    """Ověří svislou souřadnici anotace."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Svislá souřadnice anotace není platná.")

    if not math.isfinite(number):
        raise ValueError("Svislá souřadnice anotace není platná.")

    number = int(round(number))

    if number < 0 or number > MAX_ANNOTATION_Y:
        raise ValueError(
            "Svislá souřadnice anotace je mimo podporovaný rozsah."
        )

    return number


def _annotation_font_size(value: Any) -> int:
    """Ověří velikost písma textové poznámky."""
    try:
        size = int(round(float(value)))
    except (TypeError, ValueError, OverflowError):
        size = 10

    return max(1, min(96, size))


def _normalise_annotations(
    annotations: Any
) -> list[dict[str, Any]]:
    """Zkontroluje a normalizuje textové a šipkové anotace."""

    if annotations is None:
        return []

    if not isinstance(annotations, list):
        raise ValueError("Seznam anotací má neplatnou strukturu.")

    if len(annotations) > MAX_ANNOTATIONS:
        raise ValueError(
            f"Pracovní soubor obsahuje více než "
            f"{MAX_ANNOTATIONS} anotací."
        )

    normalized = []

    for number, annotation in enumerate(annotations, 1):
        if not isinstance(annotation, dict):
            raise ValueError(
                f"Anotace {number} má neplatnou strukturu."
            )

        kind = annotation.get("kind")

        # Textová poznámka.
        if kind == "text":
            text = str(annotation.get("text", ""))[:MAX_ANNOTATION_TEXT]

            if not text.strip():
                continue

            normalized.append({
                "kind": "text",
                "x_ns": _to_int(annotation.get("x_ns", 0)),
                "y": _annotation_y(annotation.get("y", 28)),
                "text": text,
                "color": _normalise_annotation_color(
                    annotation.get("color"),
                    "#202020"
                ),
                "background": _normalise_annotation_color(
                    annotation.get("background"),
                    "#FFFFFF"
                ),
                "font_size": _annotation_font_size(
                    annotation.get("font_size", 10)
                )
            })

        # Šipka.
        elif kind == "arrow":
            try:
                width = float(annotation.get("width", 2.2))
            except (TypeError, ValueError, OverflowError):
                width = 2.2

            if not math.isfinite(width) or width <= 0 or width > 100:
                width = 2.2

            normalized.append({
                "kind": "arrow",
                "x1_ns": _to_int(annotation.get("x1_ns", 0)),
                "y1": _annotation_y(annotation.get("y1", 28)),
                "x2_ns": _to_int(annotation.get("x2_ns", 0)),
                "y2": _annotation_y(annotation.get("y2", 28)),
                "color": _normalise_annotation_color(
                    annotation.get("color"),
                    "#FFFFFF"
                ),
                "width": width
            })

        # Neznámé druhy anotací se nepřenášejí do aktuálního formátu.

    return normalized


def encode_workspace(
    workspace: dict[str, Any],
    title: Optional[str] = None
) -> bytes:
    """Převede pracovní prostor do binárního kontejneru VLCR."""

    channels = workspace.get("channels")

    if (
        not isinstance(channels, list)
        or not 1 <= len(channels) <= MAX_CHANNELS
    ):
        raise ValueError(
            f"Pracovní soubor musí obsahovat 1 až {MAX_CHANNELS} kanálů."
        )

    clean_channels = []

    for number, channel in enumerate(channels, 1):
        if not isinstance(channel, dict):
            raise ValueError(
                f"Kanál {number} má neplatnou strukturu."
            )

        name = str(channel.get("name") or f"CH {number}")[:80]
        color = _normalise_color(channel.get("color"))

        transitions = channel.get("transitions", [])

        if not isinstance(transitions, list):
            transitions = []

        clean_times = sorted({
            _to_int(value) for value in transitions
        })

        clean_channels.append({
            "name": name,
            "color": color,
            "transitions": clean_times
        })

    # NOVÉ: Uložení textových poznámek a šipek.
    clean_annotations = _normalise_annotations(
        workspace.get("annotations", [])
    )

    payload = {
        "time_unit_index": int(
            workspace.get(
                "time_unit_index",
                DEFAULT_TIME_UNIT_INDEX
            )
        ),
        "max_ticks": MIN_TIMELINE_DIVISIONS,
        "transition_unit": "ns",
        "channels": clean_channels,

        # Anotace jsou součástí komprimovaného JSON payloadu.
        "annotations": clean_annotations,
    }

    title = title or str(
        workspace.get("title") or "Oscilogram"
    )

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


def decode_workspace_bytes(
    raw: bytes
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Načte a zkontroluje binární kontejner VLCR."""

    if len(raw) < 14 or raw[:8] != MAGIC:
        raise ValueError(
            "Soubor není pracovní soubor Poscilogramáře (.vlcr)."
        )

    version, meta_len = struct.unpack(">HI", raw[8:14])

    if version != CONTAINER_VERSION:
        raise ValueError(
            f"Nepodporovaná verze souboru: {version}."
        )

    if (
        not 0 < meta_len <= 1024 * 1024
        or len(raw) < 14 + meta_len
    ):
        raise ValueError(
            "Metadata souboru mají neplatnou délku."
        )

    metadata = json.loads(
        raw[14:14 + meta_len].decode("utf-8-sig")
    )

    payload = json.loads(
        zlib.decompress(raw[14 + meta_len:]).decode("utf-8-sig")
    )

    if not isinstance(metadata, dict) or not isinstance(payload, dict):
        raise ValueError(
            "Obsah pracovního souboru má neplatnou strukturu."
        )

    unit_index = int(
        payload.get(
            "time_unit_index",
            DEFAULT_TIME_UNIT_INDEX
        )
    )

    unit_index = max(
        0,
        min(len(TIME_UNITS_NS) - 1, unit_index)
    )

    is_absolute_ns = (
        payload.get("transition_unit") == "ns"
    )

    legacy_unit_ns = TIME_UNITS_NS[unit_index]

    channels = payload.get("channels")

    if (
        not isinstance(channels, list)
        or not 1 <= len(channels) <= MAX_CHANNELS
    ):
        raise ValueError(
            f"Soubor musí obsahovat 1 až {MAX_CHANNELS} kanálů."
        )

    normalized = []

    for number, channel in enumerate(channels, 1):
        if not isinstance(channel, dict):
            raise ValueError(
                f"Kanál {number} má neplatnou strukturu."
            )

        name = str(
            channel.get("name") or f"CH {number}"
        )[:80]

        color = _normalise_color(
            channel.get("color")
        )

        values = channel.get("transitions", [])

        if not isinstance(values, list):
            values = []

        if is_absolute_ns:
            transitions = sorted({
                _to_int(value) for value in values
            })
        else:
            # Migrace staré verze:
            # čísla byla indexy dílků, nikoli časy.
            migrated = set()

            for value in values:
                tick = int(value)

                if tick < 0 or tick > 10**9:
                    continue

                migrated.add(
                    _to_int(tick * legacy_unit_ns)
                )

            transitions = sorted(migrated)

        normalized.append({
            "name": name,
            "color": color,
            "transitions": transitions
        })

    # NOVÉ: Načtení textových poznámek a šipek.
    # Starší soubory, které anotace neobsahují, dostanou prázdný seznam.
    annotations = _normalise_annotations(
        payload.get("annotations", [])
    )

    payload["channels"] = normalized
    payload["annotations"] = annotations
    payload["time_unit_index"] = unit_index
    payload["transition_unit"] = "ns"

    return metadata, payload


def load_workspace(
    path: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Načte pracovní soubor z disku."""

    with open(path, "rb") as file:
        return decode_workspace_bytes(file.read())


def save_workspace(
    path: str,
    workspace: dict[str, Any],
    title: Optional[str] = None
) -> None:
    """Bezpečně uloží pracovní soubor včetně anotací."""

    data = encode_workspace(workspace, title)

    folder = os.path.dirname(
        os.path.abspath(path)
    )

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
