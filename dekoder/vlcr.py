#!/usr/bin/env python3
"""VLCR container format v1.0 - pack arbitrary files into .vlcr containers."""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import struct
import sys
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAGIC = b"VLCR"
FORMAT_MAJOR = 1
FORMAT_MINOR = 0

# Header (64 bytes), all multi-byte numbers are big-endian:
# magic[4], major[1], minor[1], flags[2], type_code[4], metadata_len[4],
# stored_payload_len[8], original_payload_len[8], sha256[32]
HEADER = struct.Struct(">4sBBH4sIQQ32s")
HEADER_SIZE = HEADER.size

FLAG_ZLIB = 0x0001
KNOWN_FLAGS = FLAG_ZLIB
MAX_METADATA_SIZE = 1 * 1024 * 1024  # 1 MiB
MAX_DECODED_SIZE = 512 * 1024 * 1024  # 512 MiB safety limit for v1 reference reader

TYPE_CODES: dict[str, bytes] = {
    "image/png": b"PNG ",
    "image/jpeg": b"JPEG",
    "image/gif": b"GIF ",
    "image/webp": b"WEBP",
    "image/bmp": b"BMP ",
    "image/svg+xml": b"SVG ",
    "text/html": b"HTML",
    "text/plain": b"TEXT",
    "text/css": b"CSS ",
    "text/javascript": b"JS  ",
    "application/javascript": b"JS  ",
    "application/json": b"JSON",
    "application/pdf": b"PDF ",
    "application/zip": b"ZIP ",
    "application/octet-stream": b"DATA",
}
CODE_TO_TYPE = {value: key for key, value in TYPE_CODES.items()}


class VLCRException(Exception):
    """Base exception for VLCR format errors."""


class VLCRFormatError(VLCRException):
    """The file is not a valid or supported VLCR container."""


class VLCRIntegrityError(VLCRException):
    """The decoded payload does not match its SHA-256 digest."""


@dataclass(frozen=True)
class VLCRDocument:
    """Decoded VLCR content. ``data`` contains the original, uncompressed bytes."""

    media_type: str
    original_name: str
    data: bytes
    metadata: dict[str, Any]
    major: int = FORMAT_MAJOR
    minor: int = FORMAT_MINOR
    compressed: bool = False
    type_code: str = "DATA"


def _fourcc(media_type: str) -> bytes:
    return TYPE_CODES.get(media_type.lower(), b"DATA")


def _guess_media_type(path: Path, data: bytes) -> str:
    """Detect common formats from signatures, then fall back to the filename."""
    signatures: tuple[tuple[bytes, str], ...] = (
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
        (b"RIFF", "image/webp"),  # confirmed below using WEBP marker
        (b"%PDF-", "application/pdf"),
        (b"PK\x03\x04", "application/zip"),
    )
    for signature, media_type in signatures:
        if data.startswith(signature):
            if media_type == "image/webp" and data[8:12] != b"WEBP":
                continue
            return media_type

    suffix = path.suffix.lower()
    if suffix == ".svg":
        return "image/svg+xml"
    if suffix in {".html", ".htm"}:
        return "text/html"
    if suffix == ".json":
        return "application/json"

    guessed, _encoding = mimetypes.guess_type(path.name)
    if guessed:
        return guessed

    # Heuristic for otherwise unknown UTF-8 text files.
    try:
        text = data[:8192].decode("utf-8")
        if "\x00" not in text:
            return "text/plain"
    except UnicodeDecodeError:
        pass
    return "application/octet-stream"


def encode_bytes(
    data: bytes,
    *,
    original_name: str = "content.bin",
    media_type: str = "application/octet-stream",
    compress: bool = False,
    extra_metadata: dict[str, Any] | None = None,
) -> bytes:
    """Return a complete VLCR container as bytes."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("data must be bytes-like")
    raw = bytes(data)
    if len(raw) > MAX_DECODED_SIZE:
        raise ValueError(f"Obsah překračuje limit {MAX_DECODED_SIZE} bajtů referenční verze.")
    name = Path(original_name).name or "content.bin"
    media_type = (media_type or "application/octet-stream").strip().lower()
    if "/" not in media_type or any(ch.isspace() for ch in media_type):
        raise ValueError("media_type must look like a MIME type, e.g. image/png")

    metadata: dict[str, Any] = {
        "media_type": media_type,
        "original_name": name,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "format": "VLCR",
        "metadata_version": 1,
    }
    if extra_metadata:
        # Reserved fields cannot be overwritten by callers.
        for key, value in extra_metadata.items():
            if key not in metadata:
                metadata[key] = value

    metadata_bytes = json.dumps(
        metadata, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(metadata_bytes) > MAX_METADATA_SIZE:
        raise ValueError("metadata is too large")

    flags = 0
    stored_payload = raw
    if compress and raw:
        compressed = zlib.compress(raw, level=6)
        # Only store compressed data if this actually saves space.
        if len(compressed) < len(raw):
            stored_payload = compressed
            flags |= FLAG_ZLIB

    digest = hashlib.sha256(raw).digest()
    header = HEADER.pack(
        MAGIC,
        FORMAT_MAJOR,
        FORMAT_MINOR,
        flags,
        _fourcc(media_type),
        len(metadata_bytes),
        len(stored_payload),
        len(raw),
        digest,
    )
    return header + metadata_bytes + stored_payload


def decode_bytes(container: bytes | bytearray | memoryview) -> VLCRDocument:
    """Decode and validate a complete VLCR container."""
    blob = bytes(container)
    if len(blob) < HEADER_SIZE:
        raise VLCRFormatError("Soubor je kratší než hlavička VLCR.")

    (magic, major, minor, flags, code, metadata_len, stored_len,
     original_len, expected_digest) = HEADER.unpack_from(blob, 0)

    if magic != MAGIC:
        raise VLCRFormatError("Chybí signatura VLCR.")
    if major != FORMAT_MAJOR:
        raise VLCRFormatError(
            f"Nepodporovaná hlavní verze formátu {major}; tato knihovna podporuje {FORMAT_MAJOR}."
        )
    if minor > FORMAT_MINOR:
        raise VLCRFormatError(
            f"Soubor používá novější vedlejší verzi {minor}; tato knihovna podporuje {FORMAT_MINOR}."
        )
    if flags & ~KNOWN_FLAGS:
        raise VLCRFormatError(f"Neznámé příznaky v hlavičce: 0x{flags:04x}.")
    if metadata_len > MAX_METADATA_SIZE:
        raise VLCRFormatError("Metadata překračují povolenou velikost.")

    expected_total = HEADER_SIZE + metadata_len + stored_len
    if len(blob) != expected_total:
        raise VLCRFormatError(
            f"Nesouhlasí délka souboru (očekáváno {expected_total} B, nalezeno {len(blob)} B)."
        )

    metadata_start = HEADER_SIZE
    payload_start = metadata_start + metadata_len
    metadata_bytes = blob[metadata_start:payload_start]
    stored_payload = blob[payload_start:]

    try:
        metadata = json.loads(metadata_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VLCRFormatError("Metadata nejsou platný UTF-8 JSON.") from exc
    if not isinstance(metadata, dict):
        raise VLCRFormatError("Kořen metadata musí být JSON objekt.")

    media_type = metadata.get("media_type", "application/octet-stream")
    original_name = metadata.get("original_name", "content.bin")
    if not isinstance(media_type, str) or "/" not in media_type:
        raise VLCRFormatError("V metadatech chybí platný MIME typ.")
    if not isinstance(original_name, str) or not original_name:
        raise VLCRFormatError("V metadatech chybí původní název souboru.")

    if original_len > MAX_DECODED_SIZE:
        raise VLCRFormatError(
            f"Dekódovaný obsah překračuje limit {MAX_DECODED_SIZE} bajtů."
        )

    try:
        if flags & FLAG_ZLIB:
            decompressor = zlib.decompressobj()
            # Limit output to the declared size + 1, so a forged header cannot
            # trigger an unbounded decompression allocation.
            raw = decompressor.decompress(stored_payload, original_len + 1)
            if decompressor.unconsumed_tail or len(raw) > original_len:
                raise VLCRFormatError("Komprimovaný obsah překračuje deklarovanou velikost.")
            raw += decompressor.flush()
            if not decompressor.eof or decompressor.unused_data:
                raise VLCRFormatError("Komprimovaný obsah nemá platný konec zlib streamu.")
        else:
            raw = stored_payload
    except zlib.error as exc:
        raise VLCRFormatError("Komprimovaný obsah je poškozený.") from exc

    if len(raw) != original_len:
        raise VLCRFormatError(
            f"Nesouhlasí rozbalená velikost (očekáváno {original_len} B, nalezeno {len(raw)} B)."
        )
    if hashlib.sha256(raw).digest() != expected_digest:
        raise VLCRIntegrityError("Kontrola SHA-256 selhala: obsah byl změněn nebo poškozen.")

    # Keep only the base filename during extraction; never trust paths from metadata.
    safe_name = Path(original_name.replace("\\", "/")).name or "content.bin"
    try:
        code_text = code.decode("ascii").rstrip()
    except UnicodeDecodeError:
        code_text = "????"

    return VLCRDocument(
        media_type=media_type,
        original_name=safe_name,
        data=raw,
        metadata=metadata,
        major=major,
        minor=minor,
        compressed=bool(flags & FLAG_ZLIB),
        type_code=code_text,
    )


def pack_file(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str] | None = None,
    *,
    media_type: str | None = None,
    compress: bool = False,
    overwrite: bool = False,
) -> Path:
    """Pack a file into .vlcr. Returns the output path."""
    src = Path(source)
    if not src.is_file():
        raise FileNotFoundError(src)
    out = Path(destination) if destination else src.with_suffix(".vlcr")
    if out.exists() and not overwrite:
        raise FileExistsError(f"Výstup už existuje: {out} (použij --overwrite)")

    data = src.read_bytes()
    detected_type = media_type or _guess_media_type(src, data)
    container = encode_bytes(
        data,
        original_name=src.name,
        media_type=detected_type,
        compress=compress,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(container)
    return out


def unpack_file(
    source: str | os.PathLike[str],
    output_dir: str | os.PathLike[str] = ".",
    *,
    output_name: str | None = None,
    overwrite: bool = False,
) -> Path:
    """Verify and extract a .vlcr container. Returns the extracted path."""
    src = Path(source)
    doc = decode_bytes(src.read_bytes())
    out_dir = Path(output_dir)
    name = Path(output_name).name if output_name else doc.original_name
    out = out_dir / (name or "content.bin")
    if out.exists() and not overwrite:
        raise FileExistsError(f"Cílový soubor už existuje: {out} (použij --overwrite)")
    out_dir.mkdir(parents=True, exist_ok=True)
    out.write_bytes(doc.data)
    return out


def inspect_file(source: str | os.PathLike[str]) -> VLCRDocument:
    return decode_bytes(Path(source).read_bytes())


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{size} B"
        value /= 1024
    return f"{size} B"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vlcr",
        description="VLCR – nástroj pro balení a čtení univerzálních kontejnerů .vlcr",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_pack = sub.add_parser("pack", help="Zabalí běžný soubor do .vlcr")
    p_pack.add_argument("source", help="Vstupní soubor")
    p_pack.add_argument("-o", "--output", help="Výstupní .vlcr soubor")
    p_pack.add_argument("--type", dest="media_type", help="Vynutí MIME typ, např. image/png")
    p_pack.add_argument("--compress", action="store_true", help="Zkusí zlib kompresi")
    p_pack.add_argument("--overwrite", action="store_true", help="Povolí přepsání výstupu")

    p_unpack = sub.add_parser("unpack", help="Ověří a rozbalí .vlcr")
    p_unpack.add_argument("source", help="Vstupní .vlcr soubor")
    p_unpack.add_argument("-d", "--directory", default=".", help="Adresář pro rozbalení")
    p_unpack.add_argument("--name", help="Volitelný název výstupního souboru")
    p_unpack.add_argument("--overwrite", action="store_true", help="Povolí přepsání souboru")

    p_info = sub.add_parser("info", help="Zobrazí informace o .vlcr")
    p_info.add_argument("source", help="Vstupní .vlcr soubor")

    p_verify = sub.add_parser("verify", help="Ověří strukturu a SHA-256")
    p_verify.add_argument("source", help="Vstupní .vlcr soubor")

    args = parser.parse_args(argv)
    try:
        if args.command == "pack":
            output = pack_file(
                args.source,
                args.output,
                media_type=args.media_type,
                compress=args.compress,
                overwrite=args.overwrite,
            )
            print(f"Vytvořeno: {output}")
            return 0

        if args.command == "unpack":
            output = unpack_file(
                args.source,
                args.directory,
                output_name=args.name,
                overwrite=args.overwrite,
            )
            print(f"Rozbaleno: {output}")
            return 0

        doc = inspect_file(args.source)
        if args.command == "verify":
            print("OK – struktura i SHA-256 jsou v pořádku.")
            return 0

        if args.command == "info":
            file_size = Path(args.source).stat().st_size
            print(f"Formát:       VLCR {doc.major}.{doc.minor}")
            print(f"Typ obsahu:   {doc.media_type}")
            print(f"Typový kód:   {doc.type_code}")
            print(f"Původní název:{' '}{doc.original_name}")
            print(f"Velikost:     {_human_size(len(doc.data))}")
            print(f"Soubor VLCR:  {_human_size(file_size)}")
            print(f"Komprese:     {'zlib' if doc.compressed else 'žádná'}")
            print(f"SHA-256:      ověřeno")
            if doc.metadata.get("created_utc"):
                print(f"Vytvořeno:    {doc.metadata['created_utc']}")
            return 0

    except (OSError, ValueError, VLCRException) as exc:
        print(f"Chyba: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
