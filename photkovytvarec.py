#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Photkovytvářeč — grafický editor ze sady VLČR Kancelář.

Vrstvy (rastrové, textové, tvarové), režimy prolnutí, výběry, štětce, výplně,
barevné úpravy a filtry s náhledem, historie změn a vlastní formát .vlcr (VLCRGRAF).

Požadavky: Python 3.9+, PyQt5 >= 5.15, numpy
Spuštění:  python photkovytvarec.py [soubor.vlcr | obrázek.png]
"""
from __future__ import annotations

import base64
import datetime as dt
import importlib.util
import json
import math
import os
import struct
import sys
import tempfile
import zlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# Informace o aplikaci a příprava na GitHub releases
# ---------------------------------------------------------------------------
APP_NAME = "Photkovytvářeč"
APP_ID = "photkovytvarec"
APP_VERSION = "0.1.0"                    # při releasu se přepisuje z tagu
GITHUB_REPO = "chznhs-spec/kancelar"
UPDATES_ENABLED = False                  # aktualizace řeší rozcestník (main.py + updater.py)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOGO_PATH = os.path.join(APP_DIR, "loga", "photkovytvarec.png")


# ==== JÁDRO ZPRACOVÁNÍ OBRAZU (numpy, bez Qt) ===============================
# Pole jsou uint8 RGBA s PŘÍMÝM (nepředmnoženým) alfa kanálem, tvar (výška, šířka, 4).
def _clamp8(a):
    return np.clip(np.rint(a), 0, 255).astype(np.uint8)


def _join(rgb, alpha):
    out = np.empty(rgb.shape[:2] + (4,), np.uint8)
    out[..., :3] = _clamp8(rgb)
    out[..., 3] = alpha
    return out


def brightness_contrast(arr, brightness=0.0, contrast=0.0):
    """brightness a contrast v rozsahu -100..100."""
    rgb = arr[..., :3].astype(np.float32)
    c = float(contrast) * 2.55
    factor = (259.0 * (c + 255.0)) / (255.0 * (259.0 - c))
    rgb = factor * (rgb - 128.0) + 128.0 + float(brightness) * 2.55
    return _join(rgb, arr[..., 3])


def _rgb_to_hsv(rgb):
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx, mn = rgb.max(-1), rgb.min(-1)
    d = mx - mn
    h = np.zeros_like(mx)
    m = d > 1e-6
    rc = (mx == r) & m
    gc = (mx == g) & m & ~rc
    bc = m & ~rc & ~gc
    h[rc] = ((g - b)[rc] / d[rc]) % 6.0
    h[gc] = (b - r)[gc] / d[gc] + 2.0
    h[bc] = (r - g)[bc] / d[bc] + 4.0
    s = np.where(mx > 1e-6, d / np.maximum(mx, 1e-6), 0.0)
    return h / 6.0, s, mx


def _hsv_to_rgb(h, s, v):
    h6 = (h % 1.0) * 6.0
    i = np.floor(h6).astype(np.int64) % 6
    f = h6 - np.floor(h6)
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    r = np.choose(i, [v, q, p, p, t, v])
    g = np.choose(i, [t, v, v, q, p, p])
    b = np.choose(i, [p, p, t, v, v, q])
    return np.stack([r, g, b], axis=-1)


def hue_saturation(arr, hue=0.0, saturation=0.0, lightness=0.0):
    """hue -180..180 (stupně), saturation a lightness -100..100."""
    rgb = arr[..., :3].astype(np.float32) / 255.0
    h, s, v = _rgb_to_hsv(rgb)
    h = (h + float(hue) / 360.0) % 1.0
    s = np.clip(s * (1.0 + float(saturation) / 100.0), 0.0, 1.0)
    out = _hsv_to_rgb(h, s, v)
    lt = float(lightness) / 100.0
    out = out + (1.0 - out) * lt if lt > 0 else out * (1.0 + lt)
    return _join(np.clip(out, 0, 1) * 255.0, arr[..., 3])


def levels(arr, black=0, white=255, gamma=1.0):
    black, white = int(black), max(int(white), int(black) + 1)
    x = (np.arange(256, dtype=np.float32) - black) / float(white - black)
    lut = _clamp8(np.clip(x, 0, 1) ** (1.0 / max(0.05, float(gamma))) * 255.0)
    out = arr.copy()
    out[..., :3] = lut[arr[..., :3]]
    return out


def invert(arr):
    out = arr.copy()
    out[..., :3] = 255 - arr[..., :3]
    return out


def grayscale(arr):
    rgb = arr[..., :3].astype(np.float32)
    g = rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114
    return _join(np.stack([g, g, g], -1), arr[..., 3])


def sepia(arr):
    rgb = arr[..., :3].astype(np.float32)
    m = np.array([[0.393, 0.769, 0.189], [0.349, 0.686, 0.168], [0.272, 0.534, 0.131]], np.float32)
    return _join(rgb @ m.T, arr[..., 3])


def threshold(arr, level=128):
    g = grayscale(arr)[..., 0]
    v = np.where(g >= int(level), 255, 0).astype(np.uint8)
    out = arr.copy()
    out[..., 0] = out[..., 1] = out[..., 2] = v
    return out


def posterize(arr, levels_count=4):
    n = max(2, int(levels_count))
    step = 255.0 / (n - 1)
    out = arr.copy()
    out[..., :3] = _clamp8(np.rint(arr[..., :3] / step) * step)
    return out


def _box_blur_axis(a, r, axis):
    if r <= 0:
        return a
    a = np.moveaxis(a, axis, 0)
    p = np.pad(a, [(r, r)] + [(0, 0)] * (a.ndim - 1), mode="edge")
    c = np.cumsum(p, axis=0, dtype=np.float64)
    c = np.concatenate([np.zeros((1,) + c.shape[1:]), c], axis=0)
    k = 2 * r + 1
    out = (c[k:] - c[:-k]) / k
    return np.moveaxis(out.astype(np.float32), 0, axis)


def gaussian_blur(arr, radius=4.0):
    """Přibližný Gaussův rozptyl (3x box blur) s ohledem na průhlednost."""
    if radius <= 0:
        return arr.copy()
    r = max(1, int(round(float(radius) * 0.58)))
    alpha = arr[..., 3:4].astype(np.float32) / 255.0
    pre = np.concatenate([arr[..., :3].astype(np.float32) * alpha, alpha], axis=-1)
    for _ in range(3):
        pre = _box_blur_axis(_box_blur_axis(pre, r, 0), r, 1)
    a = pre[..., 3:4]
    rgb = np.where(a > 1e-4, pre[..., :3] / np.maximum(a, 1e-4), 0.0)
    return _join(rgb, _clamp8(a[..., 0] * 255.0))


def sharpen(arr, amount=1.0, radius=2.0):
    """Maskování neostrosti (unsharp mask)."""
    blurred = gaussian_blur(arr, radius).astype(np.float32)
    rgb = arr[..., :3].astype(np.float32)
    return _join(rgb + float(amount) * (rgb - blurred[..., :3]), arr[..., 3])


def add_noise(arr, amount=20.0, mono=True, seed=None):
    rng = np.random.default_rng(seed)
    shape = arr.shape[:2] + ((1,) if mono else (3,))
    noise = rng.normal(0.0, float(amount), shape).astype(np.float32)
    return _join(arr[..., :3].astype(np.float32) + noise, arr[..., 3])


def pixelate(arr, size=8):
    size = max(1, int(size))
    h, w = arr.shape[:2]
    ph, pw = (-h) % size, (-w) % size
    p = np.pad(arr.astype(np.float32), ((0, ph), (0, pw), (0, 0)), mode="edge")
    hh, ww = p.shape[0] // size, p.shape[1] // size
    small = p.reshape(hh, size, ww, size, 4).mean(axis=(1, 3))
    big = np.repeat(np.repeat(small, size, axis=0), size, axis=1)[:h, :w]
    return _clamp8(big)


def vignette(arr, strength=50.0):
    h, w = arr.shape[:2]
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    d = np.sqrt(((x - w / 2.0) / (w / 2.0)) ** 2 + ((y - h / 2.0) / (h / 2.0)) ** 2) / math.sqrt(2.0)
    f = 1.0 - np.clip(d, 0, 1) ** 2 * (float(strength) / 100.0)
    return _join(arr[..., :3].astype(np.float32) * f[..., None], arr[..., 3])


def convolve3(arr, kernel, bias=0.0):
    k = np.asarray(kernel, np.float32).reshape(3, 3)
    rgb = np.pad(arr[..., :3].astype(np.float32), ((1, 1), (1, 1), (0, 0)), mode="edge")
    h, w = arr.shape[:2]
    out = np.zeros((h, w, 3), np.float32)
    for dy in range(3):
        for dx in range(3):
            out += k[dy, dx] * rgb[dy:dy + h, dx:dx + w]
    return _join(out + bias, arr[..., 3])


def emboss(arr):
    return convolve3(arr, [-2, -1, 0, -1, 1, 1, 0, 1, 2], 0.0)


def edge_detect(arr):
    return convolve3(arr, [-1, -1, -1, -1, 8, -1, -1, -1, -1], 0.0)


def flood_fill_mask(arr, x, y, tolerance=32, contiguous=True):
    """Maska (bool) pixelů podobných pixelu [y, x]; contiguous = jen souvislá oblast."""
    h, w = arr.shape[:2]
    if not (0 <= x < w and 0 <= y < h):
        return np.zeros((h, w), bool)
    seed = arr[y, x].astype(np.int16)
    match = np.abs(arr.astype(np.int16) - seed).max(axis=2) <= int(tolerance)
    if not contiguous:
        return match
    visited = np.zeros((h, w), bool)
    out = np.zeros((h, w), bool)
    stack = [(x, y)]
    while stack:
        cx, cy = stack.pop()
        if visited[cy, cx] or not match[cy, cx]:
            continue
        row = match[cy]
        f = np.flatnonzero(~row[:cx])
        left = int(f[-1]) + 1 if f.size else 0
        f = np.flatnonzero(~row[cx:])
        right = cx + int(f[0]) - 1 if f.size else w - 1
        visited[cy, left:right + 1] = True
        out[cy, left:right + 1] = True
        for ny in (cy - 1, cy + 1):
            if 0 <= ny < h:
                seg = match[ny, left:right + 1] & ~visited[ny, left:right + 1]
                idx = np.flatnonzero(seg)
                if idx.size:
                    starts = idx[np.r_[0, np.flatnonzero(np.diff(idx) != 1) + 1]]
                    stack.extend((left + int(s), ny) for s in starts)
    return out


def blend_with_mask(original, result, mask):
    """Výsledek se použije jen tam, kde je maska (0..1, tvar výška x šířka)."""
    if mask is None:
        return result
    m = np.clip(mask.astype(np.float32), 0, 1)[..., None]
    return _clamp8(original.astype(np.float32) * (1 - m) + result.astype(np.float32) * m)


def fill_mask_with_color(arr, mask, rgba):
    out = arr.copy()
    out[mask] = np.array(rgba, np.uint8)
    return out


# ==== FORMÁT SOUBORU .vlcr (VLCRGRAF) =======================================
MAGIC = b"VLCRGRAF"
FORMAT_VERSION = 1
HEADER_SIZE = 14
MAX_FILE_BYTES = 500 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def container_bytes(title: str, payload: dict, created: Optional[str] = None) -> bytes:
    meta = {
        "app": APP_ID, "application": "graphics", "aplikace": "grafika",
        "type": "image", "format": "raster", "container_format": MAGIC.decode("ascii"),
        "container_version": FORMAT_VERSION, "generator": f"{APP_NAME} {APP_VERSION}",
        "title": (title or "Nepojmenovaná grafika").strip(), "created": created or _now(),
        "modified": _now(), "width": payload.get("width"), "height": payload.get("height"),
        "layer_count": len(payload.get("layers", [])), "content_encoding": "zlib+json",
    }
    meta_raw = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    body = zlib.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)
    return MAGIC + struct.pack(">HI", FORMAT_VERSION, len(meta_raw)) + meta_raw + body


def parse_container(data: bytes):
    """-> (metadata, payload). Při chybě ValueError."""
    if len(data) < HEADER_SIZE or data[:8] != MAGIC:
        raise ValueError("Toto není soubor Photkovytvářeče (.vlcr).")
    version, meta_len = struct.unpack(">HI", data[8:14])
    if version != FORMAT_VERSION:
        raise ValueError(f"Nepodporovaná verze souboru: {version}.")
    if not 0 < meta_len <= MAX_METADATA_BYTES or HEADER_SIZE + meta_len > len(data):
        raise ValueError("Hlavička souboru je poškozená.")
    meta = json.loads(data[14:14 + meta_len].decode("utf-8"))
    if str(meta.get("app", "")).lower() != APP_ID:
        raise ValueError("Soubor nepatří Photkovytvářeči.")
    dec = zlib.decompressobj()
    try:
        raw = dec.decompress(data[14 + meta_len:], MAX_FILE_BYTES + 1) + dec.flush()
    except zlib.error as e:
        raise ValueError(f"Obsah souboru je poškozený ({e}).")
    if len(raw) > MAX_FILE_BYTES or not dec.eof:
        raise ValueError("Obsah souboru je poškozený nebo příliš velký.")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("layers"), list):
        raise ValueError("Soubor neobsahuje vrstvy.")
    return meta, payload
# ==== KONEC JÁDRA ===========================================================