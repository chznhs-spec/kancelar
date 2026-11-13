#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Počítář — tabulkový procesor ze sady VLČR Kancelář.

Sešity se ukládají do vlastního binárního formátu .vlcr (VLCRTABL),
nikoli jako ZIP nebo XLSX.

Požadavky: Python 3.9+ a PyQt5 >= 5.15
Spuštění: python pocitar.py [sesit.vlcr]
"""
from __future__ import annotations

import copy
import csv
import datetime as dt
import importlib.util
import json
import math
import os
import random
import re
import statistics
import struct
import sys
import tempfile
import unicodedata
import zlib
from collections import Counter
from contextlib import contextmanager
from decimal import Decimal, ROUND_HALF_UP, ROUND_UP, ROUND_DOWN
from html import escape
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Informace o aplikaci a příprava na GitHub releases
# ---------------------------------------------------------------------------
APP_NAME = "Počítář"
APP_ID = "pocitar"
APP_VERSION = "0.1.0"                    # při každém releasu zvyšuj (shodně s tagem, např. v0.1.0)
GITHUB_REPO = "uzivatel/vlcr-kancelar"   # TODO: "vlastnik/repozitar" na GitHubu
UPDATES_ENABLED = False                  # zapneš, až budou releasy existovat


def _parse_version(text):
    """'v1.2.3' -> (1, 2, 3) pro porovnávání verzí."""
    text = text.strip().lstrip("vV")
    parts = []
    for p in text.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def check_for_updates():
    """
    Vrací dict {"version", "url", "notes"}, pokud existuje novější release, jinak None.
    Dokud je UPDATES_ENABLED False, nic nedělá (žádný síťový přístup).
    """
    if not UPDATES_ENABLED:
        return None
    import urllib.request
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.load(resp)
    except Exception as e:
        print(f"Kontrola aktualizací selhala: {e}")
        return None
    latest = data.get("tag_name", "")
    if _parse_version(latest) > _parse_version(APP_VERSION):
        return {"version": latest, "url": data.get("html_url", ""), "notes": data.get("body", "")}
    return None


# ==== ENGINE ===============================================================
# Datový model, vzorce a formát souboru. Neobsahuje žádný kód PyQt.
# ===========================================================================
DEFAULT_ROWS = 1000
DEFAULT_COLS = 52
MAX_ROWS = 100000
MAX_COLS = 702

E_DIV0 = "#DĚLENÍ_NULOU!"
E_VALUE = "#HODNOTA!"
E_NAME = "#NÁZEV?"
E_REF = "#ODKAZ!"
E_NA = "#N/A"
E_NUM = "#ČÍSLO!"
E_CYCLE = "#CYKLUS!"
E_SYNTAX = "#CHYBA!"


class ExcelError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code

    def __eq__(self, other):
        return isinstance(other, ExcelError) and other.code == self.code

    def __hash__(self):
        return hash(self.code)

    def __str__(self):
        return self.code


# --- adresy ----------------------------------------------------------------
def col_name(c: int) -> str:
    s, c = "", c + 1
    while c:
        c, rem = divmod(c - 1, 26)
        s = chr(65 + rem) + s
    return s


def col_index(name: str) -> int:
    n = 0
    for ch in name.upper():
        n = n * 26 + ord(ch) - 64
    return n - 1


def cell_name(r: int, c: int) -> str:
    return f"{col_name(c)}{r + 1}"


_ADDR = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d+)$")


def parse_address(text: str):
    """'B5' -> (4, 1); neplatné -> None."""
    m = _ADDR.match(text.strip())
    if not m:
        return None
    r, c = int(m.group(2)) - 1, col_index(m.group(1))
    if 0 <= r < MAX_ROWS and 0 <= c < MAX_COLS:
        return r, c
    return None


# --- data / formát čísel -----------------------------------------------------
EPOCH = dt.date(1899, 12, 30)


def date_to_serial(d: dt.date) -> float:
    return float((d - EPOCH).days)


def serial_to_date(v: float) -> dt.date:
    try:
        return EPOCH + dt.timedelta(days=int(math.floor(v)))
    except (OverflowError, ValueError):
        raise ExcelError(E_NUM)


_DATE_PATTERNS = (
    (re.compile(r"^(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})$"), (2, 1, 0)),
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"), (0, 1, 2)),
    (re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$"), (2, 1, 0)),
)


def parse_date(s: str):
    s = s.strip()
    for rx, (iy, im, idd) in _DATE_PATTERNS:
        m = rx.match(s)
        if m:
            try:
                g = m.groups()
                return date_to_serial(dt.date(int(g[iy]), int(g[im]), int(g[idd])))
            except ValueError:
                return None
    return None


_NUM_LIT = re.compile(r"^[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:[eE][+-]?\d+)?$")
_NUM_GROUPED = re.compile(r"^[+-]?\d{1,3}(?:[\s\u00a0]\d{3})+(?:[.,]\d+)?$")


def parse_literal(raw: str):
    """Text zadaný do buňky -> None / bool / float / datum (serial) / str."""
    s = raw.strip()
    if not s:
        return None
    up = s.upper()
    if up in ("TRUE", "PRAVDA"):
        return True
    if up in ("FALSE", "NEPRAVDA"):
        return False
    if _NUM_LIT.match(s):
        try:
            return float(s.replace(",", "."))
        except ValueError:
            pass
    if _NUM_GROUPED.match(s):
        return float(re.sub(r"[\s\u00a0]", "", s).replace(",", "."))
    if s.endswith("%") and _NUM_LIT.match(s[:-1].strip()):
        return float(s[:-1].strip().replace(",", ".")) / 100
    d = parse_date(s)
    if d is not None:
        return d
    return raw


def is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def fmt_general(v) -> str:
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return E_NUM
        if v == int(v) and abs(v) < 1e15:
            return str(int(v))
        return format(v, ".10g").replace(".", ",")
    return str(v)


def fmt_number(v: float, decimals: int, thousands: bool = True) -> str:
    r = round(v, decimals)
    s = format(abs(r), (",.%df" if thousands else ".%df") % decimals)
    s = s.replace(",", "\u00a0").replace(".", ",")
    return ("-" if r < 0 else "") + s


def format_value(v, f=None) -> str:
    """Hodnota buňky -> text podle číselného formátu."""
    if v is None:
        return ""
    if isinstance(v, ExcelError):
        return v.code
    if isinstance(v, bool):
        return "PRAVDA" if v else "NEPRAVDA"
    if isinstance(v, str):
        return v
    f = f or {}
    nf, d = f.get("nf", "general"), f.get("dec")
    try:
        if nf == "number":
            return fmt_number(v, 2 if d is None else d)
        if nf == "currency":
            return fmt_number(v, 2 if d is None else d) + "\u00a0Kč"
        if nf == "percent":
            return fmt_number(v * 100, 0 if d is None else d, False) + "\u00a0%"
        if nf == "date":
            dd = serial_to_date(v)
            return f"{dd.day}.{dd.month}.{dd.year}"
        if d is not None and nf != "text":
            return fmt_number(v, d, False)
    except ExcelError as e:
        return e.code
    return fmt_general(v)


# --- konverze hodnot ----------------------------------------------------------
class RangeValue:
    """Odkaz na obdélník buněk (hodnoty se čtou líně)."""

    def __init__(self, wb, sheet, r1, c1, r2, c2):
        self.wb, self.sheet = wb, sheet
        self.r1, self.c1, self.r2, self.c2 = r1, c1, r2, c2
        self.rows, self.cols = r2 - r1 + 1, c2 - c1 + 1

    def get(self, i, j):
        if not (0 <= i < self.rows and 0 <= j < self.cols):
            raise ExcelError(E_REF)
        return self.wb.value(self.sheet, self.r1 + i, self.c1 + j)

    def positions(self):
        """(i, j, hodnota) jen pro neprázdné buňky, po řádcích."""
        keys = sorted(k for k in self.sheet.cells
                      if self.r1 <= k[0] <= self.r2 and self.c1 <= k[1] <= self.c2)
        for r, c in keys:
            v = self.wb.value(self.sheet, r, c)
            if v is not None:
                yield r - self.r1, c - self.c1, v

    def values(self):
        for _, _, v in self.positions():
            yield v


def scalar(v):
    return v.get(0, 0) if isinstance(v, RangeValue) else v


def to_num(v) -> float:
    v = scalar(v)
    if isinstance(v, ExcelError):
        raise v
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace("\u00a0", "").replace(" ", "")
        try:
            return float(s.replace(",", "."))
        except ValueError:
            pass
        lit = parse_literal(v)
        if is_number(lit):
            return float(lit)
    raise ExcelError(E_VALUE)


def to_bool(v) -> bool:
    v = scalar(v)
    if isinstance(v, ExcelError):
        raise v
    if v is None:
        return False
    if isinstance(v, str):
        up = v.strip().upper()
        if up in ("TRUE", "PRAVDA"):
            return True
        if up in ("FALSE", "NEPRAVDA"):
            return False
        raise ExcelError(E_VALUE)
    return bool(v)


def to_text(v) -> str:
    v = scalar(v)
    if isinstance(v, ExcelError):
        raise v
    if v is None:
        return ""
    if isinstance(v, bool):
        return "PRAVDA" if v else "NEPRAVDA"
    if isinstance(v, (int, float)):
        return fmt_general(float(v))
    return str(v)


def _cmp_key(v):
    if isinstance(v, bool):
        return (2, v)
    if is_number(v):
        return (0, float(v))
    return (1, str(v).lower())


def compare_values(a, b) -> int:
    a, b = scalar(a), scalar(b)
    for x in (a, b):
        if isinstance(x, ExcelError):
            raise x
    if a is None:
        a = "" if isinstance(b, str) else (False if isinstance(b, bool) else 0.0)
    if b is None:
        b = "" if isinstance(a, str) else (False if isinstance(a, bool) else 0.0)
    ka, kb = _cmp_key(a), _cmp_key(b)
    return (ka > kb) - (ka < kb)


# --- tokenizer a parser vzorců -----------------------------------------------
_FUNC_RE = re.compile(r"[A-Za-z_À-ž][A-Za-z0-9_.À-ž]*(?=\s*\()")
_SHEETREF_RE = re.compile(r"(?:'(?:[^']|'')+'|[A-Za-z_À-ž][\wÀ-ž.]*)!\$?[A-Za-z]{1,3}\$?\d+(?![\wÀ-ž(.])")
_REF_RE = re.compile(r"\$?[A-Za-z]{1,3}\$?\d+(?![\wÀ-ž(.])")
_NAME_RE = re.compile(r"[A-Za-z_À-ž][A-Za-z0-9_.À-ž]*")
_NUM_DOT = re.compile(r"(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?")
_NUM_COMMA = re.compile(r"(?:\d+(?:[.,]\d+)?|[.,]\d+)(?:[eE][+-]?\d+)?")
_REF_PARTS = re.compile(r"^(?:(?P<sheet>'(?:[^']|'')+'|[^!]+)!)?\$?(?P<col>[A-Za-z]{1,3})\$?(?P<row>\d+)$")


def tokenize(src: str):
    # Desetinná čárka: když se ve vzorci používá ";" jako oddělovač argumentů,
    # nebo když vzorec neobsahuje žádnou funkci (čárka tedy nemůže oddělovat argumenty).
    num_re = _NUM_COMMA if (";" in src or not _FUNC_RE.search(src)) else _NUM_DOT
    toks, i, n = [], 0, len(src)
    while i < n:
        ch = src[i]
        if ch.isspace():
            i += 1
            continue
        if ch == '"':
            j, buf = i + 1, []
            while True:
                if j >= n:
                    raise ExcelError(E_SYNTAX)
                if src[j] == '"':
                    if j + 1 < n and src[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    break
                buf.append(src[j])
                j += 1
            toks.append(("str", "".join(buf)))
            i = j + 1
            continue
        m = _FUNC_RE.match(src, i)
        if m:
            toks.append(("func", m.group(0)))
            i = m.end()
            continue
        m = _SHEETREF_RE.match(src, i) or _REF_RE.match(src, i)
        if m:
            toks.append(("ref", m.group(0)))
            i = m.end()
            continue
        m = num_re.match(src, i)
        if m:
            toks.append(("num", float(m.group(0).replace(",", "."))))
            i = m.end()
            continue
        m = _NAME_RE.match(src, i)
        if m:
            toks.append(("name", m.group(0)))
            i = m.end()
            continue
        two = src[i:i + 2]
        if two in ("<>", "<=", ">="):
            toks.append(("op", two))
            i += 2
            continue
        if ch in "+-*/^&=<>%:":
            toks.append(("op", ch))
        elif ch == "(":
            toks.append(("lp", ch))
        elif ch == ")":
            toks.append(("rp", ch))
        elif ch in ";,":
            toks.append(("sep", ch))
        else:
            raise ExcelError(E_SYNTAX)
        i += 1
    return toks


def _unquote_sheet(text: str) -> str:
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        return text[1:-1].replace("''", "'")
    return text


def split_ref(text: str):
    m = _REF_PARTS.match(text)
    if not m:
        raise ExcelError(E_REF)
    sheet = m.group("sheet")
    if sheet:
        sheet = _unquote_sheet(sheet)
    r, c = int(m.group("row")) - 1, col_index(m.group("col"))
    if not (0 <= r < MAX_ROWS and 0 <= c < MAX_COLS):
        raise ExcelError(E_REF)
    return sheet, r, c


class Parser:
    def __init__(self, tokens):
        self.t, self.i = tokens, 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def next(self):
        tok = self.peek()
        self.i += 1
        return tok

    def parse(self):
        node = self.compare()
        if self.i < len(self.t):
            raise ExcelError(E_SYNTAX)
        return node

    def _binary(self, sub, ops):
        left = sub()
        while self.peek()[0] == "op" and self.peek()[1] in ops:
            op = self.next()[1]
            left = ("bin", op, left, sub())
        return left

    def compare(self):
        return self._binary(self.concat, ("=", "<>", "<", ">", "<=", ">="))

    def concat(self):
        return self._binary(self.additive, ("&",))

    def additive(self):
        return self._binary(self.term, ("+", "-"))

    def term(self):
        return self._binary(self.power, ("*", "/"))

    def power(self):
        return self._binary(self.unary, ("^",))

    def unary(self):
        if self.peek()[0] == "op" and self.peek()[1] in ("+", "-"):
            op = self.next()[1]
            return ("un", op, self.unary())
        return self.postfix()

    def postfix(self):
        node = self.primary()
        while self.peek() == ("op", "%"):
            self.next()
            node = ("pct", node)
        return node

    def args(self):
        out = []
        if self.peek()[0] == "rp":
            self.next()
            return out
        while True:
            if self.peek()[0] in ("sep", "rp"):
                out.append(("empty",))
            else:
                out.append(self.compare())
            kind = self.next()[0]
            if kind == "rp":
                return out
            if kind != "sep":
                raise ExcelError(E_SYNTAX)

    def primary(self):
        kind, val = self.next()
        if kind == "num":
            return ("num", val)
        if kind == "str":
            return ("str", val)
        if kind == "lp":
            node = self.compare()
            if self.next()[0] != "rp":
                raise ExcelError(E_SYNTAX)
            return node
        if kind == "func":
            if self.next()[0] != "lp":
                raise ExcelError(E_SYNTAX)
            return ("call", val, self.args())
        if kind == "ref":
            sheet, r, c = split_ref(val)
            if self.peek() == ("op", ":"):
                self.next()
                k2, t2 = self.next()
                if k2 != "ref":
                    raise ExcelError(E_SYNTAX)
                s2, r2, c2 = split_ref(t2)
                return ("range", sheet or s2, min(r, r2), min(c, c2), max(r, r2), max(c, c2))
            return ("ref", sheet, r, c)
        if kind == "name":
            up = val.upper()
            if up in ("TRUE", "PRAVDA"):
                return ("bool", True)
            if up in ("FALSE", "NEPRAVDA"):
                return ("bool", False)
            raise ExcelError(E_NAME)
        raise ExcelError(E_SYNTAX)


def parse_formula(body: str):
    toks = tokenize(body)
    if not toks:
        raise ExcelError(E_SYNTAX)
    return Parser(toks).parse()


# --- přepis odkazů (kopírování, vkládání řádků, přejmenování listů) ---------
_TOKEN_SCAN = re.compile(
    r'"(?:[^"]|"")*"|(?<![A-Za-z0-9_.À-ž$])'
    r"(?P<sheet>(?:'(?:[^']|'')+'|[A-Za-z_À-ž][\wÀ-ž.]*)!)?"
    r"(?P<ac>\$?)(?P<col>[A-Za-z]{1,3})(?P<ar>\$?)(?P<row>\d+)(?![\wÀ-ž(.])"
)


def rewrite_refs(body: str, fn) -> str:
    def repl(m):
        if m.group(0).startswith('"'):
            return m.group(0)
        return fn(m, body)
    return _TOKEN_SCAN.sub(repl, body)


def _ref_text(m, r, c):
    return (m.group("sheet") or "") + m.group("ac") + col_name(c) + m.group("ar") + str(r + 1)


def shift_formula(raw: str, dr: int, dc: int) -> str:
    """Posune relativní odkazy (kopírování vzorce o dr řádků a dc sloupců)."""
    def fn(m, body):
        r, c = int(m.group("row")) - 1, col_index(m.group("col"))
        if not m.group("ar"):
            r += dr
        if not m.group("ac"):
            c += dc
        if not (0 <= r < MAX_ROWS and 0 <= c < MAX_COLS):
            return E_REF
        return _ref_text(m, r, c)
    return "=" + rewrite_refs(raw[1:], fn)


def adjust_formula(raw, own, target, axis, at, count):
    """Upraví odkazy po vložení (count>0) nebo smazání (count<0) řádků/sloupců."""
    def fn(m, body):
        sh = m.group("sheet")
        name = own if not sh else _unquote_sheet(sh[:-1])
        if name.lower() != target.lower():
            return m.group(0)
        r, c = int(m.group("row")) - 1, col_index(m.group("col"))
        idx = (r, c)[axis]
        is_start = m.end() < len(body) and body[m.end()] == ":"
        is_end = m.start() > 0 and body[m.start() - 1] == ":"
        if count > 0:
            if idx >= at:
                idx += count
        else:
            n = -count
            if at <= idx < at + n:
                if is_start:
                    idx = at
                elif is_end:
                    idx = at - 1
                else:
                    return E_REF
            elif idx >= at + n:
                idx -= n
        if idx < 0:
            return E_REF
        r, c = (idx, c) if axis == 0 else (r, idx)
        return _ref_text(m, r, c)
    return "=" + rewrite_refs(raw[1:], fn)


def rename_refs(raw, old, new):
    quoted = new if re.fullmatch(r"[A-Za-z_À-ž][\wÀ-ž.]*", new) else "'" + new.replace("'", "''") + "'"

    def fn(m, body):
        sh = m.group("sheet")
        if sh and _unquote_sheet(sh[:-1]).lower() == old.lower():
            return quoted + "!" + m.group("ac") + m.group("col") + m.group("ar") + m.group("row")
        return m.group(0)
    return "=" + rewrite_refs(raw[1:], fn)


# --- vyhodnocení ---------------------------------------------------------------
class Ctx:
    def __init__(self, wb, sheet, row, col):
        self.wb, self.sheet, self.row, self.col = wb, sheet, row, col

    def sheet_for(self, name):
        if name is None:
            return self.sheet
        sh = self.wb.sheet_by_name(name)
        if sh is None:
            raise ExcelError(E_REF)
        return sh


def eval_bin(op, a, b):
    if op in ("+", "-", "*", "/", "^"):
        x, y = to_num(a), to_num(b)
        if op == "+":
            return x + y
        if op == "-":
            return x - y
        if op == "*":
            return x * y
        if op == "/":
            if y == 0:
                raise ExcelError(E_DIV0)
            return x / y
        try:
            r = x ** y
        except ZeroDivisionError:
            raise ExcelError(E_DIV0)
        except OverflowError:
            raise ExcelError(E_NUM)
        if isinstance(r, complex):
            raise ExcelError(E_NUM)
        return r
    if op == "&":
        return to_text(a) + to_text(b)
    c = compare_values(a, b)
    return {"=": c == 0, "<>": c != 0, "<": c < 0, ">": c > 0, "<=": c <= 0, ">=": c >= 0}[op]


def ev(node, ctx):
    t = node[0]
    if t in ("num", "str", "bool"):
        return node[1]
    if t == "empty":
        return None
    if t == "ref":
        return ctx.wb.value(ctx.sheet_for(node[1]), node[2], node[3])
    if t == "range":
        return RangeValue(ctx.wb, ctx.sheet_for(node[1]), node[2], node[3], node[4], node[5])
    if t == "un":
        v = to_num(ev(node[2], ctx))
        return -v if node[1] == "-" else v
    if t == "pct":
        return to_num(ev(node[1], ctx)) / 100
    if t == "bin":
        return eval_bin(node[1], ev(node[2], ctx), ev(node[3], ctx))
    if t == "call":
        return call_function(node, ctx)
    raise ExcelError(E_SYNTAX)


def canon_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name.upper())
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).replace(".", "")
    return ALIASES.get(s, s)


def _is_err(v) -> bool:
    return isinstance(v, ExcelError)


def call_function(node, ctx):
    name, args = canon_name(node[1]), node[2]
    if name == "IF":
        if not args:
            raise ExcelError(E_VALUE)
        branch = 1 if to_bool(ev(args[0], ctx)) else 2
        return ev(args[branch], ctx) if len(args) > branch else (branch == 1)
    if name in ("IFERROR", "IFNA"):
        if len(args) != 2:
            raise ExcelError(E_VALUE)
        try:
            v = scalar(ev(args[0], ctx))
            if not _is_err(v) or (name == "IFNA" and v.code != E_NA):
                return v
            if name == "IFNA":
                raise v
        except ExcelError as e:
            if name == "IFNA" and e.code != E_NA:
                raise
        return ev(args[1], ctx)
    if name in ("ISERROR", "ISNA"):
        try:
            v = scalar(ev(args[0], ctx)) if args else None
        except ExcelError as e:
            v = e
        if name == "ISNA":
            return _is_err(v) and v.code == E_NA
        return _is_err(v)
    if name in ("ROW", "COLUMN"):
        if not args:
            return float((ctx.row if name == "ROW" else ctx.col) + 1)
        n = args[0]
        if n[0] == "ref":
            return float((n[2] if name == "ROW" else n[3]) + 1)
        if n[0] == "range":
            return float((n[2] if name == "ROW" else n[3]) + 1)
        raise ExcelError(E_VALUE)
    fn = FUNCS.get(name)
    if fn is None:
        raise ExcelError(E_NAME)
    if name in _AGGREGATES:
        vals = [RangeValue(ctx.wb, ctx.sheet_for(n[1]), n[2], n[3], n[2], n[3]) if n[0] == "ref" else ev(n, ctx)
                for n in args]
    else:
        vals = [ev(a, ctx) for a in args]
    return fn(ctx, vals)


FUNCS: Dict[str, Any] = {}
_AGGREGATES = {"SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA", "PRODUCT", "MEDIAN", "MODE", "STDEV", "VAR"}
ALIASES = {
    "SUMA": "SUM", "PRUMER": "AVERAGE", "KDYZ": "IF", "POCET": "COUNT", "POCET2": "COUNTA",
    "COUNTBLANK": "COUNTBLANK", "ZAOKROUHLIT": "ROUND", "SVYHLEDAT": "VLOOKUP", "VVYHLEDAT": "HLOOKUP",
    "A": "AND", "NEBO": "OR", "NE": "NOT", "DNES": "TODAY", "NYNI": "NOW", "DELKA": "LEN",
    "MALA": "LOWER", "VELKA": "UPPER", "VELKA2": "PROPER", "ZLEVA": "LEFT", "ZPRAVA": "RIGHT",
    "CAST": "MID", "SPOJIT": "CONCATENATE", "JEPRAZDNE": "ISBLANK", "JECISLO": "ISNUMBER",
    "JETEXT": "ISTEXT", "JECHYBA": "ISERROR", "KDYZCHYBA": "IFERROR", "POZVYHLEDAT": "MATCH",
    "ODMOCNINA": "SQRT", "MOCNINA": "POWER", "ZBYTEK": "MOD", "CELACAST": "INT", "SOUCIN": "PRODUCT",
    "DEN": "DAY", "MESIC": "MONTH", "ROK": "YEAR", "DATUM": "DATE", "SUMIF": "SUMIF",
    "COUNTIF": "COUNTIF", "COUNTIFS": "COUNTIFS", "SUMIFS": "SUMIFS", "ZAOKR.NAHORU": "ROUNDUP",
    "ZAOKROUHLITNAHORU": "ROUNDUP", "ZAOKROUHLITDOLU": "ROUNDDOWN", "NAHODNE": "RAND", "MEDIAN": "MEDIAN",
    "OPAKOVAT": "REPT", "DOSADIT": "SUBSTITUTE", "NAJÍT": "FIND", "NAJIT": "FIND", "HLEDAT": "SEARCH",
    "TEXT": "TEXT", "HODNOTA": "VALUE", "VYBRAT": "CHOOSE", "PLATBA": "PMT", "BUDHODNOTA": "FV",
    "SOUCASHODNOTA": "PV", "MAX": "MAX", "MIN": "MIN", "ABS": "ABS", "LARGE": "LARGE", "SMALL": "SMALL",
    "POZNAMKA": "N", "SPOJIT.TEXT": "TEXTJOIN", "SPOJITTEXT": "TEXTJOIN",
}


def func(*names):
    def deco(f):
        for n in names:
            FUNCS[n] = f
        return f
    return deco


def nums(args):
    """Čísla pro agregační funkce: z oblastí jen číselné buňky, přímé argumenty se převádějí."""
    out = []
    for a in args:
        if isinstance(a, RangeValue):
            for v in a.values():
                if _is_err(v):
                    raise v
                if is_number(v):
                    out.append(float(v))
        else:
            out.append(to_num(a))
    return out


def flat_values(args):
    for a in args:
        if isinstance(a, RangeValue):
            for v in a.values():
                yield v
        elif a is not None:
            yield a


def _round_dec(x, n, mode):
    x = to_num(x)
    n = int(to_num(n))
    q = Decimal(1).scaleb(-n)
    try:
        return float(Decimal(repr(x)).quantize(q, rounding=mode))
    except Exception:
        raise ExcelError(E_NUM)


@func("SUM")
def f_sum(ctx, a):
    return math.fsum(nums(a))


@func("AVERAGE")
def f_avg(ctx, a):
    xs = nums(a)
    if not xs:
        raise ExcelError(E_DIV0)
    return math.fsum(xs) / len(xs)


@func("MIN")
def f_min(ctx, a):
    xs = nums(a)
    return min(xs) if xs else 0.0


@func("MAX")
def f_max(ctx, a):
    xs = nums(a)
    return max(xs) if xs else 0.0


@func("COUNT")
def f_count(ctx, a):
    n = 0
    for x in a:
        if isinstance(x, RangeValue):
            n += sum(1 for v in x.values() if is_number(v))
        else:
            try:
                to_num(x)
                n += 1
            except ExcelError:
                pass
    return float(n)


@func("COUNTA")
def f_counta(ctx, a):
    return float(sum(1 for _ in flat_values(a)))


@func("COUNTBLANK")
def f_countblank(ctx, a):
    total = 0
    for x in a:
        if isinstance(x, RangeValue):
            total += x.rows * x.cols - sum(1 for v in x.values() if v != "")
    return float(total)


@func("PRODUCT")
def f_product(ctx, a):
    return math.prod(nums(a))


@func("MEDIAN")
def f_median(ctx, a):
    xs = nums(a)
    if not xs:
        raise ExcelError(E_NUM)
    return float(statistics.median(xs))


@func("MODE")
def f_mode(ctx, a):
    xs = nums(a)
    if not xs:
        raise ExcelError(E_NA)
    return Counter(xs).most_common(1)[0][0]


@func("STDEV")
def f_stdev(ctx, a):
    xs = nums(a)
    if len(xs) < 2:
        raise ExcelError(E_DIV0)
    return statistics.stdev(xs)


@func("VAR")
def f_var(ctx, a):
    xs = nums(a)
    if len(xs) < 2:
        raise ExcelError(E_DIV0)
    return statistics.variance(xs)


@func("LARGE", "SMALL")
def _noop(ctx, a):  # přepsáno níže
    raise ExcelError(E_NAME)


def _kth(a, large):
    if len(a) != 2:
        raise ExcelError(E_VALUE)
    xs = sorted(nums([a[0]]), reverse=large)
    k = int(to_num(a[1]))
    if not 1 <= k <= len(xs):
        raise ExcelError(E_NUM)
    return xs[k - 1]


FUNCS["LARGE"] = lambda ctx, a: _kth(a, True)
FUNCS["SMALL"] = lambda ctx, a: _kth(a, False)


@func("ROUND")
def f_round(ctx, a):
    return _round_dec(a[0], a[1] if len(a) > 1 else 0, ROUND_HALF_UP)


@func("ROUNDUP")
def f_roundup(ctx, a):
    return _round_dec(a[0], a[1] if len(a) > 1 else 0, ROUND_UP)


@func("ROUNDDOWN")
def f_rounddown(ctx, a):
    return _round_dec(a[0], a[1] if len(a) > 1 else 0, ROUND_DOWN)


@func("INT")
def f_int(ctx, a):
    return float(math.floor(to_num(a[0])))


@func("ABS")
def f_abs(ctx, a):
    return abs(to_num(a[0]))


@func("SQRT")
def f_sqrt(ctx, a):
    x = to_num(a[0])
    if x < 0:
        raise ExcelError(E_NUM)
    return math.sqrt(x)


@func("POWER")
def f_power(ctx, a):
    return eval_bin("^", a[0], a[1])


@func("MOD")
def f_mod(ctx, a):
    y = to_num(a[1])
    if y == 0:
        raise ExcelError(E_DIV0)
    return to_num(a[0]) % y


@func("PI")
def f_pi(ctx, a):
    return math.pi


@func("EXP")
def f_exp(ctx, a):
    try:
        return math.exp(to_num(a[0]))
    except OverflowError:
        raise ExcelError(E_NUM)


@func("LN")
def f_ln(ctx, a):
    x = to_num(a[0])
    if x <= 0:
        raise ExcelError(E_NUM)
    return math.log(x)


@func("LOG")
def f_log(ctx, a):
    x = to_num(a[0])
    base = to_num(a[1]) if len(a) > 1 else 10.0
    if x <= 0 or base <= 0 or base == 1:
        raise ExcelError(E_NUM)
    return math.log(x, base)


@func("LOG10")
def f_log10(ctx, a):
    x = to_num(a[0])
    if x <= 0:
        raise ExcelError(E_NUM)
    return math.log10(x)


@func("SIGN")
def f_sign(ctx, a):
    x = to_num(a[0])
    return float((x > 0) - (x < 0))


@func("CEILING")
def f_ceiling(ctx, a):
    sig = to_num(a[1]) if len(a) > 1 else 1.0
    if sig == 0:
        return 0.0
    return math.ceil(to_num(a[0]) / sig) * sig


@func("FLOOR")
def f_floor(ctx, a):
    sig = to_num(a[1]) if len(a) > 1 else 1.0
    if sig == 0:
        raise ExcelError(E_DIV0)
    return math.floor(to_num(a[0]) / sig) * sig


@func("RAND")
def f_rand(ctx, a):
    return random.random()


@func("RANDBETWEEN")
def f_randbetween(ctx, a):
    lo, hi = int(to_num(a[0])), int(to_num(a[1]))
    if lo > hi:
        raise ExcelError(E_NUM)
    return float(random.randint(lo, hi))


@func("SUMPRODUCT")
def f_sumproduct(ctx, a):
    rs = [x for x in a if isinstance(x, RangeValue)]
    if not rs:
        return to_num(a[0]) if a else 0.0
    if any((x.rows, x.cols) != (rs[0].rows, rs[0].cols) for x in rs):
        raise ExcelError(E_VALUE)
    total = 0.0
    for i in range(rs[0].rows):
        for j in range(rs[0].cols):
            p = 1.0
            for x in rs:
                v = x.get(i, j)
                if _is_err(v):
                    raise v
                p *= float(v) if is_number(v) else 0.0
            total += p
    return total


def _wild_regex(pattern: str):
    out = []
    for ch in pattern:
        out.append(".*" if ch == "*" else "." if ch == "?" else re.escape(ch))
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE | re.DOTALL)


def make_criteria(c):
    c = scalar(c)
    if _is_err(c):
        raise c
    op, val = "=", c
    if isinstance(c, str):
        m = re.match(r"^(<=|>=|<>|<|>|=)(.*)$", c, re.DOTALL)
        if m:
            op, val = m.group(1), m.group(2)
        lit = parse_literal(val)
        if lit is not None and not isinstance(lit, str):
            val = lit
    if isinstance(val, str):
        rx = _wild_regex(val)

        def pred(x):
            if x is None:
                return False
            if op in ("=", "<>"):
                hit = isinstance(x, str) and bool(rx.match(x))
                return hit if op == "=" else not hit
            if not isinstance(x, str):
                return False
            k = (x.lower() > val.lower()) - (x.lower() < val.lower())
            return {"<": k < 0, ">": k > 0, "<=": k <= 0, ">=": k >= 0}[op]
        return pred

    def pred_num(x):
        if x is None:
            return False
        if isinstance(val, bool) or not is_number(x):
            same = isinstance(x, bool) == isinstance(val, bool) and x == val
            return same if op == "=" else (not same if op == "<>" else False)
        k = (float(x) > float(val)) - (float(x) < float(val))
        return {"=": k == 0, "<>": k != 0, "<": k < 0, ">": k > 0, "<=": k <= 0, ">=": k >= 0}[op]
    return pred_num


def _matches(pairs):
    """pairs: [(RangeValue, kritérium)] -> množina (i, j) vyhovující všem kritériím."""
    result = None
    for rng, crit in pairs:
        if not isinstance(rng, RangeValue):
            raise ExcelError(E_VALUE)
        pred = make_criteria(crit)
        hits = {(i, j) for i, j, v in rng.positions() if pred(v)}
        result = hits if result is None else result & hits
    return result or set()


@func("SUMIF")
def f_sumif(ctx, a):
    hits = _matches([(a[0], a[1])])
    target = a[2] if len(a) > 2 else a[0]
    return math.fsum(float(v) for i, j in hits
                     for v in [target.get(i, j)] if is_number(v))


@func("COUNTIF")
def f_countif(ctx, a):
    return float(len(_matches([(a[0], a[1])])))


@func("AVERAGEIF")
def f_averageif(ctx, a):
    hits = _matches([(a[0], a[1])])
    target = a[2] if len(a) > 2 else a[0]
    xs = [float(v) for i, j in hits for v in [target.get(i, j)] if is_number(v)]
    if not xs:
        raise ExcelError(E_DIV0)
    return math.fsum(xs) / len(xs)


@func("COUNTIFS")
def f_countifs(ctx, a):
    return float(len(_matches([(a[k], a[k + 1]) for k in range(0, len(a) - 1, 2)])))


@func("SUMIFS")
def f_sumifs(ctx, a):
    hits = _matches([(a[k], a[k + 1]) for k in range(1, len(a) - 1, 2)])
    return math.fsum(float(v) for i, j in hits for v in [a[0].get(i, j)] if is_number(v))


def _bools(a):
    out = []
    for x in a:
        if isinstance(x, RangeValue):
            for v in x.values():
                if _is_err(v):
                    raise v
                if not isinstance(v, str):
                    out.append(bool(v))
        else:
            out.append(to_bool(x))
    return out


@func("AND")
def f_and(ctx, a):
    vals = _bools(a)
    if not vals:
        raise ExcelError(E_VALUE)
    return all(vals)


@func("OR")
def f_or(ctx, a):
    vals = _bools(a)
    if not vals:
        raise ExcelError(E_VALUE)
    return any(vals)


@func("NOT")
def f_not(ctx, a):
    return not to_bool(a[0])


@func("TRUE")
def f_true(ctx, a):
    return True


@func("FALSE")
def f_false(ctx, a):
    return False


@func("ISBLANK")
def f_isblank(ctx, a):
    return scalar(a[0]) is None


@func("ISNUMBER")
def f_isnumber(ctx, a):
    return is_number(scalar(a[0]))


@func("ISTEXT")
def f_istext(ctx, a):
    return isinstance(scalar(a[0]), str)


@func("N")
def f_n(ctx, a):
    v = scalar(a[0])
    return float(v) if is_number(v) or isinstance(v, bool) else 0.0


@func("CONCATENATE", "CONCAT")
def f_concat(ctx, a):
    return "".join(to_text(v) for v in flat_values(a))


@func("TEXTJOIN")
def f_textjoin(ctx, a):
    delim, skip = to_text(a[0]), to_bool(a[1])
    parts = []
    for x in a[2:]:
        if isinstance(x, RangeValue):
            for i in range(x.rows):
                for j in range(x.cols):
                    v = x.get(i, j)
                    if v is None and skip:
                        continue
                    parts.append(to_text(v))
        else:
            if x is None and skip:
                continue
            parts.append(to_text(x))
    return delim.join(parts)


@func("LEFT")
def f_left(ctx, a):
    n = int(to_num(a[1])) if len(a) > 1 else 1
    if n < 0:
        raise ExcelError(E_VALUE)
    return to_text(a[0])[:n]


@func("RIGHT")
def f_right(ctx, a):
    n = int(to_num(a[1])) if len(a) > 1 else 1
    if n < 0:
        raise ExcelError(E_VALUE)
    return to_text(a[0])[-n:] if n else ""


@func("MID")
def f_mid(ctx, a):
    start, n = int(to_num(a[1])), int(to_num(a[2]))
    if start < 1 or n < 0:
        raise ExcelError(E_VALUE)
    return to_text(a[0])[start - 1:start - 1 + n]


@func("LEN")
def f_len(ctx, a):
    return float(len(to_text(a[0])))


@func("UPPER")
def f_upper(ctx, a):
    return to_text(a[0]).upper()


@func("LOWER")
def f_lower(ctx, a):
    return to_text(a[0]).lower()


@func("PROPER")
def f_proper(ctx, a):
    return to_text(a[0]).title()


@func("TRIM")
def f_trim(ctx, a):
    return re.sub(r" +", " ", to_text(a[0]).strip())


@func("SUBSTITUTE")
def f_substitute(ctx, a):
    text, old, new = to_text(a[0]), to_text(a[1]), to_text(a[2])
    if not old:
        return text
    if len(a) > 3:
        k, pos = int(to_num(a[3])), -1
        for _ in range(k):
            pos = text.find(old, pos + 1)
            if pos < 0:
                return text
        return text[:pos] + new + text[pos + len(old):]
    return text.replace(old, new)


@func("FIND")
def f_find(ctx, a):
    start = int(to_num(a[2])) if len(a) > 2 else 1
    pos = to_text(a[1]).find(to_text(a[0]), start - 1)
    if pos < 0:
        raise ExcelError(E_VALUE)
    return float(pos + 1)


@func("SEARCH")
def f_search(ctx, a):
    start = int(to_num(a[2])) if len(a) > 2 else 1
    pos = to_text(a[1]).lower().find(to_text(a[0]).lower(), start - 1)
    if pos < 0:
        raise ExcelError(E_VALUE)
    return float(pos + 1)


@func("REPT")
def f_rept(ctx, a):
    n = int(to_num(a[1]))
    if n < 0 or n > 10000:
        raise ExcelError(E_VALUE)
    return to_text(a[0]) * n


@func("EXACT")
def f_exact(ctx, a):
    return to_text(a[0]) == to_text(a[1])


@func("VALUE")
def f_value(ctx, a):
    v = scalar(a[0])
    if is_number(v):
        return float(v)
    lit = parse_literal(to_text(v))
    if is_number(lit):
        return float(lit)
    raise ExcelError(E_VALUE)


@func("TEXT")
def f_text(ctx, a):
    v, pat = scalar(a[0]), to_text(a[1])
    if isinstance(v, str):
        return v
    x = to_num(v)
    low = pat.lower()
    if re.search(r"[dmyh]", low) and "0" not in low:
        d = serial_to_date(x)
        out = low.replace("yyyy", "%04d" % d.year).replace("yy", "%02d" % (d.year % 100))
        out = out.replace("dd", "%02d" % d.day).replace("mm", "%02d" % d.month)
        out = re.sub(r"\bd\b", str(d.day), out)
        out = re.sub(r"\bm\b", str(d.month), out)
        return out
    percent = "%" in pat
    dec = len(pat.split(".")[1].rstrip("%").replace("#", "0")) if "." in pat else 0
    if percent:
        x *= 100
    s = fmt_number(x, dec, "," in pat or " " in pat)
    return s + ("\u00a0%" if percent else "")


@func("TODAY")
def f_today(ctx, a):
    return date_to_serial(dt.date.today())


@func("NOW")
def f_now(ctx, a):
    n = dt.datetime.now()
    return date_to_serial(n.date()) + (n.hour * 3600 + n.minute * 60 + n.second) / 86400


@func("DATE")
def f_date(ctx, a):
    y, m, d = int(to_num(a[0])), int(to_num(a[1])), int(to_num(a[2]))
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    try:
        return date_to_serial(dt.date(y, m, 1)) + (d - 1)
    except ValueError:
        raise ExcelError(E_NUM)


@func("YEAR")
def f_year(ctx, a):
    return float(serial_to_date(to_num(a[0])).year)


@func("MONTH")
def f_month(ctx, a):
    return float(serial_to_date(to_num(a[0])).month)


@func("DAY")
def f_day(ctx, a):
    return float(serial_to_date(to_num(a[0])).day)


@func("WEEKDAY")
def f_weekday(ctx, a):
    wd = serial_to_date(to_num(a[0])).weekday()
    kind = int(to_num(a[1])) if len(a) > 1 else 1
    return float(wd + 1 if kind == 2 else (wd + 1) % 7 + 1)


@func("DAYS")
def f_days(ctx, a):
    return float(math.floor(to_num(a[0])) - math.floor(to_num(a[1])))


def _lookup_pos(key, values, approx):
    """Index prvku nalezeného exaktně / přibližně (seřazeno vzestupně), jinak None."""
    if not approx:
        for k, v in enumerate(values):
            if v is not None and not _is_err(v) and compare_values(v, key) == 0:
                return k
        return None
    best = None
    for k, v in enumerate(values):
        if v is None or _is_err(v):
            continue
        if _cmp_key(v)[0] != _cmp_key(key)[0]:
            continue
        if compare_values(v, key) <= 0:
            best = k
        else:
            break
    return best


@func("VLOOKUP")
def f_vlookup(ctx, a):
    key, tbl, col = scalar(a[0]), a[1], int(to_num(a[2]))
    approx = to_bool(a[3]) if len(a) > 3 else True
    if not isinstance(tbl, RangeValue):
        raise ExcelError(E_VALUE)
    if not 1 <= col <= tbl.cols:
        raise ExcelError(E_REF)
    pos = _lookup_pos(key, [tbl.get(i, 0) for i in range(tbl.rows)], approx)
    if pos is None:
        raise ExcelError(E_NA)
    return tbl.get(pos, col - 1)


@func("HLOOKUP")
def f_hlookup(ctx, a):
    key, tbl, row = scalar(a[0]), a[1], int(to_num(a[2]))
    approx = to_bool(a[3]) if len(a) > 3 else True
    if not isinstance(tbl, RangeValue):
        raise ExcelError(E_VALUE)
    if not 1 <= row <= tbl.rows:
        raise ExcelError(E_REF)
    pos = _lookup_pos(key, [tbl.get(0, j) for j in range(tbl.cols)], approx)
    if pos is None:
        raise ExcelError(E_NA)
    return tbl.get(row - 1, pos)


@func("MATCH")
def f_match(ctx, a):
    key, rng = scalar(a[0]), a[1]
    kind = int(to_num(a[2])) if len(a) > 2 else 1
    if not isinstance(rng, RangeValue):
        raise ExcelError(E_VALUE)
    vals = [rng.get(i, j) for i in range(rng.rows) for j in range(rng.cols)]
    pos = _lookup_pos(key, vals, kind != 0)
    if pos is None:
        raise ExcelError(E_NA)
    return float(pos + 1)


@func("INDEX")
def f_index(ctx, a):
    rng = a[0]
    if not isinstance(rng, RangeValue):
        return rng
    r = int(to_num(a[1])) if len(a) > 1 and a[1] is not None else 0
    c = int(to_num(a[2])) if len(a) > 2 and a[2] is not None else 0
    if len(a) == 2 and rng.rows == 1:
        r, c = 1, r
    elif len(a) == 2 and rng.cols == 1:
        c = 1
    r, c = max(r, 1), max(c, 1)
    return rng.get(r - 1, c - 1)


@func("CHOOSE")
def f_choose(ctx, a):
    k = int(to_num(a[0]))
    if not 1 <= k < len(a):
        raise ExcelError(E_VALUE)
    return scalar(a[k])


@func("ROWS")
def f_rows(ctx, a):
    return float(a[0].rows if isinstance(a[0], RangeValue) else 1)


@func("COLUMNS")
def f_columns(ctx, a):
    return float(a[0].cols if isinstance(a[0], RangeValue) else 1)


def _pmt_args(a, i, default=0.0):
    return to_num(a[i]) if len(a) > i and a[i] is not None else default


@func("PMT")
def f_pmt(ctx, a):
    rate, n, pv, fv, typ = to_num(a[0]), to_num(a[1]), to_num(a[2]), _pmt_args(a, 3), _pmt_args(a, 4)
    if n == 0:
        raise ExcelError(E_NUM)
    if rate == 0:
        return -(pv + fv) / n
    g = (1 + rate) ** n
    return -(rate * (fv + pv * g)) / ((1 + rate * typ) * (g - 1))


@func("FV")
def f_fv(ctx, a):
    rate, n, pmt, pv, typ = to_num(a[0]), to_num(a[1]), to_num(a[2]), _pmt_args(a, 3), _pmt_args(a, 4)
    if rate == 0:
        return -(pv + pmt * n)
    g = (1 + rate) ** n
    return -(pv * g + pmt * (1 + rate * typ) * (g - 1) / rate)


@func("PV")
def f_pv(ctx, a):
    rate, n, pmt, fv, typ = to_num(a[0]), to_num(a[1]), to_num(a[2]), _pmt_args(a, 3), _pmt_args(a, 4)
    if rate == 0:
        return -(fv + pmt * n)
    g = (1 + rate) ** n
    return -(fv + pmt * (1 + rate * typ) * (g - 1) / rate) / g


# --- sešit ---------------------------------------------------------------------
class Sheet:
    def __init__(self, name: str):
        self.name = name
        self.cells: Dict[Tuple[int, int], dict] = {}   # klíč (řádek, sloupec) -> {"v": text, "f": formát}
        self.col_widths: Dict[int, int] = {}
        self.row_heights: Dict[int, int] = {}
        self.merges: List[Tuple[int, int, int, int]] = []
        self.n_rows = DEFAULT_ROWS
        self.n_cols = DEFAULT_COLS

    # Slovníky buněk se nikdy neupravují "na místě" - vždy se vytvoří nový, proto stačí mělká kopie.
    def snapshot(self):
        return (dict(self.cells), dict(self.col_widths), dict(self.row_heights),
                list(self.merges), self.n_rows, self.n_cols)

    def restore(self, snap):
        cells, cw, rh, mg, nr, nc = snap
        self.cells, self.col_widths, self.row_heights = dict(cells), dict(cw), dict(rh)
        self.merges, self.n_rows, self.n_cols = list(mg), nr, nc

    def used_bounds(self):
        if not self.cells:
            return 0, 0
        return max(k[0] for k in self.cells) + 1, max(k[1] for k in self.cells) + 1


def literal_value(raw: str, f):
    if raw.startswith("'"):
        return raw[1:]
    if (f or {}).get("nf") == "text":
        return raw if raw != "" else None
    return parse_literal(raw)


class Workbook:
    def __init__(self):
        self.sheets: List[Sheet] = [Sheet("List1")]
        self._cache: Dict[Any, Any] = {}
        self._busy = set()
        self._ast: Dict[str, Any] = {}

    def invalidate(self):
        self._cache.clear()

    def sheet_by_name(self, name):
        for s in self.sheets:
            if s.name.lower() == name.lower():
                return s
        return None

    def unique_name(self, base="List"):
        names = {s.name.lower() for s in self.sheets}
        if base.lower() not in names:
            return base
        k = 2
        while f"{base}{k}".lower() in names:
            k += 1
        return f"{base}{k}"

    def parse(self, body):
        ast = self._ast.get(body)
        if ast is None:
            ast = parse_formula(body)
            if len(self._ast) > 20000:
                self._ast.clear()
            self._ast[body] = ast
        return ast

    def value(self, sheet, r, c):
        key = (id(sheet), r, c)
        cache = self._cache
        if key in cache:
            return cache[key]
        cell = sheet.cells.get((r, c))
        if not cell:
            return None
        raw = cell.get("v", "")
        f = cell.get("f")
        if raw.startswith("=") and len(raw) > 1 and (f or {}).get("nf") != "text":
            if key in self._busy:
                return ExcelError(E_CYCLE)
            self._busy.add(key)
            try:
                val = ev(self.parse(raw[1:]), Ctx(self, sheet, r, c))
                if isinstance(val, RangeValue):
                    val = val.get(0, 0)
            except ExcelError as e:
                val = e
            except ZeroDivisionError:
                val = ExcelError(E_DIV0)
            except RecursionError:
                val = ExcelError(E_CYCLE)
            except (OverflowError, ValueError):
                val = ExcelError(E_NUM)
            except Exception:
                val = ExcelError(E_VALUE)
            finally:
                self._busy.discard(key)
        else:
            val = literal_value(raw, f)
        cache[key] = val
        return val

    # --- změny struktury ----------------------------------------------------
    def structural(self, sheet, axis, at, count):
        """Vložení (count>0) / smazání (count<0) řádků (axis 0) nebo sloupců (axis 1)."""
        n = abs(count)
        for sh in self.sheets:
            for key, cell in list(sh.cells.items()):
                v = cell.get("v", "")
                if v.startswith("="):
                    nv = adjust_formula(v, sh.name, sheet.name, axis, at, count)
                    if nv != v:
                        sh.cells[key] = dict(cell, v=nv)

        def mapped(idx):
            if count > 0:
                return idx + n if idx >= at else idx
            if at <= idx < at + n:
                return None
            return idx - n if idx >= at + n else idx

        new_cells = {}
        for (r, c), cell in sheet.cells.items():
            idx = mapped((r, c)[axis])
            if idx is not None:
                new_cells[(idx, c) if axis == 0 else (r, idx)] = cell
        sheet.cells = new_cells
        sizes = sheet.row_heights if axis == 0 else sheet.col_widths
        moved = {}
        for idx, size in sizes.items():
            m = mapped(idx)
            if m is not None:
                moved[m] = size
        sizes.clear()
        sizes.update(moved)
        merges = []
        for r1, c1, r2, c2 in sheet.merges:
            a, b = ((r1, r2) if axis == 0 else (c1, c2))
            na, nb = mapped(a), mapped(b)
            if na is None or nb is None:
                continue
            merges.append((na, c1, nb, c2) if axis == 0 else (r1, na, r2, nb))
        sheet.merges = merges
        if axis == 0:
            sheet.n_rows = min(MAX_ROWS, max(1, sheet.n_rows + count))
        else:
            sheet.n_cols = min(MAX_COLS, max(1, sheet.n_cols + count))

    def rename_sheet(self, sheet, new):
        old = sheet.name
        for sh in self.sheets:
            for key, cell in list(sh.cells.items()):
                v = cell.get("v", "")
                if v.startswith("="):
                    nv = rename_refs(v, old, new)
                    if nv != v:
                        sh.cells[key] = dict(cell, v=nv)
        sheet.name = new

    def sort_range(self, sheet, r1, c1, r2, c2, key_col, ascending=True):
        rows = list(range(r1, r2 + 1))

        def sort_key(r):
            v = self.value(sheet, r, key_col)
            if v is None or _is_err(v):
                return (1, (0, 0))
            return (0, _cmp_key(v))
        present = [r for r in rows if sort_key(r)[0] == 0]
        blanks = [r for r in rows if sort_key(r)[0] == 1]
        present.sort(key=sort_key, reverse=not ascending)
        order = present + blanks
        snapshot = {r: {c: sheet.cells.get((r, c)) for c in range(c1, c2 + 1)} for r in rows}
        for new_r, old_r in zip(rows, order):
            for c in range(c1, c2 + 1):
                cell = snapshot[old_r][c]
                if cell is None:
                    sheet.cells.pop((new_r, c), None)
                    continue
                v = cell.get("v", "")
                if v.startswith("=") and new_r != old_r:
                    cell = dict(cell, v=shift_formula(v, new_r - old_r, 0))
                sheet.cells[(new_r, c)] = cell
        self.invalidate()


# --- soubor .vlcr (VLCRTABL) --------------------------------------------------------
MAGIC = b"VLCRTABL"                 # 8 bajtů - identifikátor vlastního kontejneru
FORMAT_VERSION = 1
HEADER_SIZE = 14                    # magic (8) + verze (2) + délka metadat (4)
MAX_FILE_BYTES = 250 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
APP_KEYS = ("app", "application", "aplikace", "editor", "typ", "type", "format", "kind")
APP_ALIASES = {"pocitar", "spreadsheet", "tabulka", "sheet", "xlsx"}


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _norm(value) -> str:
    value = unicodedata.normalize("NFKD", str(value).strip().lower())
    return "".join(c for c in value if not unicodedata.combining(c))


def _metadata_values() -> dict:
    return {
        "app": "pocitar",
        "application": "spreadsheet",
        "aplikace": "tabulka",
        "type": "sheet",
        "format": "xlsx",
        "container_format": "VLCRTABL",
        "container_version": FORMAT_VERSION,
        "generator": f"{APP_NAME} {APP_VERSION}",
    }


def _is_our_metadata(meta) -> bool:
    if not isinstance(meta, dict):
        return False
    inner = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else meta
    return any(_norm(inner.get(k, "")) in APP_ALIASES for k in APP_KEYS)


def workbook_to_payload(wb: Workbook, active: int = 0) -> dict:
    sheets = []
    for sh in wb.sheets:
        cells = []
        for (r, c), cell in sorted(sh.cells.items()):
            item = [r, c, cell.get("v", "")]
            if cell.get("f"):
                item.append(cell["f"])
            cells.append(item)
        sheets.append({
            "name": sh.name, "cells": cells,
            "col_widths": [[k, v] for k, v in sh.col_widths.items()],
            "row_heights": [[k, v] for k, v in sh.row_heights.items()],
            "merges": [list(m) for m in sh.merges],
            "n_rows": sh.n_rows, "n_cols": sh.n_cols,
        })
    return {"sheets": sheets, "active": active}


def payload_to_workbook(payload: dict):
    if not isinstance(payload, dict) or not isinstance(payload.get("sheets"), list) or not payload["sheets"]:
        raise ValueError("Sešit nemá platnou strukturu listů.")
    wb = Workbook()
    wb.sheets = []
    for sd in payload["sheets"]:
        sh = Sheet(str(sd.get("name") or wb.unique_name()))
        sh.n_rows = max(1, min(MAX_ROWS, int(sd.get("n_rows", DEFAULT_ROWS))))
        sh.n_cols = max(1, min(MAX_COLS, int(sd.get("n_cols", DEFAULT_COLS))))
        for item in sd.get("cells", []):
            r, c, v = int(item[0]), int(item[1]), str(item[2])
            if not (0 <= r < MAX_ROWS and 0 <= c < MAX_COLS):
                continue
            cell = {"v": v}
            if len(item) > 3 and isinstance(item[3], dict) and item[3]:
                cell["f"] = item[3]
            sh.cells[(r, c)] = cell
            sh.n_rows, sh.n_cols = max(sh.n_rows, r + 1), max(sh.n_cols, c + 1)
        sh.col_widths = {int(k): int(v) for k, v in sd.get("col_widths", [])}
        sh.row_heights = {int(k): int(v) for k, v in sd.get("row_heights", [])}
        sh.merges = [tuple(int(x) for x in m) for m in sd.get("merges", []) if len(m) == 4]
        wb.sheets.append(sh)
    return wb, int(payload.get("active", 0))


def read_workbook(path: str):
    """Načte .vlcr kontejner -> (metadata, Workbook, aktivní list)."""
    path = os.path.abspath(path)
    if not path.lower().endswith(".vlcr"):
        raise ValueError("Počítář otevírá pouze soubory s příponou .vlcr.")
    size = os.path.getsize(path)
    if size < HEADER_SIZE or size > MAX_FILE_BYTES:
        raise ValueError("Soubor je prázdný nebo překračuje povolenou velikost.")
    with open(path, "rb") as handle:
        header = handle.read(HEADER_SIZE)
        if header[:8] != MAGIC:
            raise ValueError("Toto není sešit Počítáře (.vlcr).")
        version, meta_len = struct.unpack(">HI", header[8:14])
        if version != FORMAT_VERSION:
            raise ValueError(f"Tato verze sešitu ({version}) není podporována.")
        if meta_len <= 0 or meta_len > MAX_METADATA_BYTES:
            raise ValueError("Hlavička sešitu obsahuje neplatnou délku metadat.")
        meta_raw = handle.read(meta_len)
        if len(meta_raw) != meta_len:
            raise ValueError("Soubor je poškozený — neúplná metadata.")
        meta = json.loads(meta_raw.decode("utf-8"))
        if not _is_our_metadata(meta):
            raise ValueError("Soubor nepatří Počítáři.")
        compressed = handle.read(MAX_FILE_BYTES + 1)
    decoder = zlib.decompressobj()
    raw = decoder.decompress(compressed, MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES or decoder.unconsumed_tail:
        raise ValueError("Rozbalený obsah sešitu je příliš velký.")
    raw += decoder.flush()
    if len(raw) > MAX_FILE_BYTES or not decoder.eof:
        raise ValueError("Obsah sešitu je poškozený nebo neúplný.")
    wb, active = payload_to_workbook(json.loads(raw.decode("utf-8")))
    return meta, wb, active


def write_workbook(path: str, wb: Workbook, title: str, created: Optional[str] = None, active: int = 0) -> dict:
    path = os.path.abspath(path)
    if not path.lower().endswith(".vlcr"):
        path += ".vlcr"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    meta = _metadata_values()
    meta.update({
        "title": (title or "Nepojmenovaný sešit").strip(),
        "created": created or _now(), "modified": _now(),
        "sheet_count": len(wb.sheets), "content_encoding": "zlib+json",
    })
    meta_raw = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload = json.dumps(workbook_to_payload(wb, active), ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_FILE_BYTES:
        raise ValueError("Sešit je příliš velký.")
    data = MAGIC + struct.pack(">HI", FORMAT_VERSION, len(meta_raw)) + meta_raw + zlib.compress(payload, 8)
    fd, tmp = tempfile.mkstemp(prefix=".pocitar-", suffix=".tmp", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return meta
# ==== END ENGINE ===========================================================


# ===========================================================================
# UŽIVATELSKÉ ROZHRANÍ (PyQt5)
# ===========================================================================
from PyQt5.QtCore import (
    Qt, QAbstractTableModel, QModelIndex, QSize, QMimeData, QByteArray, QItemSelection,
    QItemSelectionModel, QRectF, QPointF, QUrl
)
from PyQt5.QtGui import (
    QColor, QFont, QFontMetrics, QIcon, QKeySequence, QPainter, QPen, QBrush, QPixmap,
    QTextDocument, QDesktopServices
)
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QDialog, QAction, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QToolBar, QTableView, QTabWidget, QLineEdit, QFontComboBox, QComboBox,
    QCheckBox, QColorDialog, QFileDialog, QMessageBox, QInputDialog, QStatusBar, QFrame,
    QStyledItemDelegate, QAbstractItemDelegate, QAbstractItemView, QStyle, QUndoStack,
    QUndoCommand, QMenu, QToolButton
)
from PyQt5.QtPrintSupport import QPrinter, QPrintDialog

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOGO_PATH = os.path.join(APP_DIR, "loga", "pocitar.png")     # ikona aplikace
MIME_CELLS = "application/x-vlcr-cells"
DEFAULT_FONT = "Segoe UI"
DEFAULT_SIZE = 10
DEFAULT_COL_W = 96
DEFAULT_ROW_H = 24
CHART_COLORS = ["#DA532C", "#2D89EF", "#00A300", "#9F00A7", "#603CBA", "#FFB900", "#00ABA9", "#E51400"]
NUMBER_FORMATS = [("Obecný", "general"), ("Číslo", "number"), ("Měna", "currency"),
                  ("Procenta", "percent"), ("Datum", "date"), ("Text", "text")]


class Theme:
    ACCENT = "#DA532C"          # barva dlaždice Počítáře v dashboardu
    ACCENT_DARK = "#B5421F"
    WINDOW = "#F5F5F5"
    SURFACE = "#FFFFFF"
    TEXT = "#202020"
    MUTED = "#666666"
    BORDER = "#D0D0D0"
    GRID = "#DADADA"
    HEADER = "#F0F0F0"
    SELECTION = "#FBE0D6"
    FONT = "Segoe UI"


def app_icon() -> QIcon:
    """Ikona z loga/pocitar.png; pokud soubor chybí, nakreslí se jednoduchá náhradní."""
    if os.path.isfile(LOGO_PATH):
        return QIcon(LOGO_PATH)
    pm = QPixmap(64, 64)
    pm.fill(QColor(Theme.ACCENT))
    p = QPainter(pm)
    p.setPen(QPen(QColor("white"), 3))
    p.drawRect(10, 10, 44, 44)
    for k in (25, 40):
        p.drawLine(k, 10, k, 54)
        p.drawLine(10, k, 54, k)
    p.end()
    return QIcon(pm)


def make_font(f) -> QFont:
    f = f or {}
    font = QFont(f.get("font") or DEFAULT_FONT, int(f.get("size") or DEFAULT_SIZE))
    font.setBold(bool(f.get("b")))
    font.setItalic(bool(f.get("i")))
    font.setUnderline(bool(f.get("u")))
    return font


# --- model, delegát, pohled -----------------------------------------------------
class SheetModel(QAbstractTableModel):
    def __init__(self, win, sheet):
        super().__init__()
        self.win, self.sheet = win, sheet

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else self.sheet.n_rows

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else self.sheet.n_cols

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole:
            return col_name(section) if orientation == Qt.Horizontal else str(section + 1)
        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignCenter)
        return None

    def data(self, index, role=Qt.DisplayRole):
        r, c = index.row(), index.column()
        cell = self.sheet.cells.get((r, c))
        if role == Qt.DisplayRole:
            if not cell:
                return ""
            return format_value(self.win.wb.value(self.sheet, r, c), cell.get("f"))
        if role == Qt.EditRole:
            return cell.get("v", "") if cell else ""
        return None

    def flags(self, index):
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.EditRole:
            return False
        self.win.set_cell_text(self.sheet, index.row(), index.column(), str(value))
        return True

    def refresh(self, structural):
        if structural:
            self.beginResetModel()
            self.endResetModel()
        elif self.sheet.n_rows and self.sheet.n_cols:
            self.dataChanged.emit(self.index(0, 0), self.index(self.sheet.n_rows - 1, self.sheet.n_cols - 1))


class CellDelegate(QStyledItemDelegate):
    def paint(self, p, option, index):
        model = index.model()
        r, c = index.row(), index.column()
        cell = model.sheet.cells.get((r, c))
        f = (cell.get("f") if cell else None) or {}
        rect = option.rect
        p.save()
        p.setClipRect(rect)
        p.fillRect(rect, QColor(f["bg"]) if f.get("bg") else QColor("#FFFFFF"))
        if cell:
            val = model.win.wb.value(model.sheet, r, c)
            text = format_value(val, f)
            if text:
                p.setFont(make_font(f))
                p.setPen(QColor(f.get("fg") or Theme.TEXT))
                default = "r" if is_number(val) else ("c" if isinstance(val, (bool, ExcelError)) else "l")
                align = {"l": Qt.AlignLeft, "c": Qt.AlignHCenter, "r": Qt.AlignRight}[f.get("align") or default]
                flags = int(align) | int(Qt.AlignVCenter)
                if f.get("wrap"):
                    flags |= int(Qt.TextWordWrap)
                p.drawText(rect.adjusted(5, 1, -5, -1), flags, text)
        bd = f.get("bd")
        if bd:
            p.setPen(QPen(QColor("#303030"), 1))
            if "l" in bd:
                p.drawLine(rect.topLeft(), rect.bottomLeft())
            if "r" in bd:
                p.drawLine(rect.topRight(), rect.bottomRight())
            if "t" in bd:
                p.drawLine(rect.topLeft(), rect.topRight())
            if "b" in bd:
                p.drawLine(rect.bottomLeft(), rect.bottomRight())
        if option.state & QStyle.State_Selected:
            p.fillRect(rect, QColor(218, 83, 44, 38))
        if option.state & QStyle.State_HasFocus:
            p.setPen(QPen(QColor(Theme.ACCENT), 2))
            p.drawRect(rect.adjusted(1, 1, -2, -2))
        p.restore()


class GridView(QTableView):
    def __init__(self, win, sheet):
        super().__init__()
        self.win, self.sheet = win, sheet
        self._applying = False
        self.setModel(SheetModel(win, sheet))
        self.setItemDelegate(CellDelegate(self))
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
                             | QAbstractItemView.AnyKeyPressed)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setFrameShape(QFrame.NoFrame)
        self.horizontalHeader().sectionResized.connect(self._col_resized)
        self.verticalHeader().sectionResized.connect(self._row_resized)
        self.apply_sheet_layout()
        self.setCurrentIndex(self.model().index(0, 0))
        sm = self.selectionModel()
        sm.currentChanged.connect(win.on_current_changed)
        sm.selectionChanged.connect(win.on_selection_changed)

    def apply_sheet_layout(self):
        self._applying = True
        self.horizontalHeader().setDefaultSectionSize(DEFAULT_COL_W)
        self.verticalHeader().setDefaultSectionSize(DEFAULT_ROW_H)
        for c, w in self.sheet.col_widths.items():
            if c < self.sheet.n_cols:
                self.setColumnWidth(c, w)
        for r, h in self.sheet.row_heights.items():
            if r < self.sheet.n_rows:
                self.setRowHeight(r, h)
        self.clearSpans()
        for r1, c1, r2, c2 in self.sheet.merges:
            self.setSpan(r1, c1, r2 - r1 + 1, c2 - c1 + 1)
        self._applying = False

    def _col_resized(self, c, old, new):
        if not self._applying:
            self.sheet.col_widths[c] = new
            self.win.mark_dirty()

    def _row_resized(self, r, old, new):
        if not self._applying:
            self.sheet.row_heights[r] = new
            self.win.mark_dirty()

    def refresh(self, structural):
        cur = self.currentIndex()
        row, col = (cur.row(), cur.column()) if cur.isValid() else (0, 0)
        self.model().refresh(structural)
        if structural:
            self.apply_sheet_layout()
            self.setCurrentIndex(self.model().index(min(row, self.sheet.n_rows - 1),
                                                    min(col, self.sheet.n_cols - 1)))
        self.viewport().update()

    def closeEditor(self, editor, hint):
        idx = self.currentIndex()
        super().closeEditor(editor, hint)
        if hint == QAbstractItemDelegate.SubmitModelCache and idx.isValid():
            nxt = self.model().index(idx.row() + 1, idx.column())
            if nxt.isValid():
                self.setCurrentIndex(nxt)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete and self.state() != QAbstractItemView.EditingState:
            self.win.clear_contents()
            return
        super().keyPressEvent(event)


class SnapshotCommand(QUndoCommand):
    """Krok zpět/znovu: stav listů před a po změně (stačí mělké kopie slovníků)."""

    def __init__(self, win, states, text):
        super().__init__(text)
        self.win, self.states, self._first = win, states, True

    def redo(self):
        if self._first:
            self._first = False
            return
        for sheet, before, after in self.states:
            sheet.restore(after)
        self.win.after_restore(self.states, use_after=True)

    def undo(self):
        for sheet, before, after in self.states:
            sheet.restore(before)
        self.win.after_restore(self.states, use_after=False)


# --- průzkumník souborů (otevření) --------------------------------------------------
class ExplorerOpenDialog(QDialog):
    """Otevření sešitu přes Průzkumník VLČR (pruzkumnik.py ve stejné složce)."""

    def __init__(self, start_path: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Otevřít sešit — Průzkumník VLČR")
        self.setMinimumSize(900, 580)
        self.resize(1100, 700)
        self.setModal(True)
        self.selected_path: Optional[str] = None
        self._module = None
        self._original_launcher = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Vyberte sešit Počítáře")
        title.setStyleSheet(f"padding:16px 22px; color:{Theme.ACCENT}; font-size:20px; background:white;")
        root.addWidget(title)
        explorer_path = os.path.join(APP_DIR, "pruzkumnik.py")
        if not os.path.isfile(explorer_path):
            missing = QLabel("Chybí pruzkumnik.py. Umístěte jej do stejné složky jako pocitar.py.")
            missing.setWordWrap(True)
            missing.setStyleSheet("padding:24px;")
            root.addWidget(missing, 1)
        else:
            try:
                spec = importlib.util.spec_from_file_location("vlcr_pocitar_explorer_runtime", explorer_path)
                if spec is None or spec.loader is None:
                    raise RuntimeError("Průzkumník se nepodařilo načíst.")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self._module = module
                self._original_launcher = module.launch_vlcr
                module.launch_vlcr = self._capture_launch
                root.addWidget(module.FileExplorerWidget(
                    base_dir=APP_DIR, title="Průzkumník souborů",
                    start_path=start_path or os.path.expanduser("~"), parent=self), 1)
            except Exception as exc:
                self._module = None
                error = QLabel(f"Průzkumník se nepodařilo spustit:\n{exc}")
                error.setWordWrap(True)
                error.setStyleSheet("padding:24px;")
                root.addWidget(error, 1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        close = QPushButton("Zrušit")
        close.clicked.connect(self.reject)
        footer.addWidget(close)
        root.addLayout(footer)
        self.setStyleSheet("QDialog{background:#F3F3F3;} QPushButton{padding:6px 18px;}")

    def _capture_launch(self, path, parent=None, base_dir=None):
        try:
            read_workbook(path)
            self.selected_path = os.path.abspath(path)
            self.accept()
            return True
        except Exception as exc:
            QMessageBox.warning(self, "Nelze otevřít tento soubor", str(exc))
            return False

    def _restore(self):
        if self._module is not None and self._original_launcher is not None:
            self._module.launch_vlcr = self._original_launcher
            self._original_launcher = None

    def done(self, result):
        self._restore()
        super().done(result)

    def closeEvent(self, event):
        self._restore()
        super().closeEvent(event)


# --- najít a nahradit --------------------------------------------------------------
class FindDialog(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Najít a nahradit")
        grid = QGridLayout(self)
        self.find_edit, self.repl_edit = QLineEdit(), QLineEdit()
        grid.addWidget(QLabel("Najít:"), 0, 0)
        grid.addWidget(self.find_edit, 0, 1, 1, 3)
        grid.addWidget(QLabel("Nahradit:"), 1, 0)
        grid.addWidget(self.repl_edit, 1, 1, 1, 3)
        self.chk_case = QCheckBox("Rozlišovat velikost písmen")
        self.chk_formula = QCheckBox("Hledat ve vzorcích")
        grid.addWidget(self.chk_case, 2, 0, 1, 2)
        grid.addWidget(self.chk_formula, 2, 2, 1, 2)
        for col, (text, slot) in enumerate((("Najít další", self.find_next), ("Nahradit", self.replace_one),
                                            ("Nahradit vše", self.replace_all), ("Zavřít", self.close))):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            grid.addWidget(btn, 3, col)
        self.info = QLabel("")
        grid.addWidget(self.info, 4, 0, 1, 4)
        self.find_edit.returnPressed.connect(self.find_next)

    def _regex(self):
        text = self.find_edit.text()
        return re.compile(re.escape(text), 0 if self.chk_case.isChecked() else re.IGNORECASE) if text else None

    def _haystack(self, sheet, r, c):
        cell = sheet.cells.get((r, c))
        if not cell:
            return ""
        raw = cell.get("v", "")
        if raw.startswith("=") and not self.chk_formula.isChecked():
            return format_value(self.win.wb.value(sheet, r, c), cell.get("f"))
        return raw

    def find_next(self, select=True):
        rx = self._regex()
        view = self.win.view()
        if rx is None or view is None:
            return None
        sheet = view.sheet
        cur = view.currentIndex()
        pos = (cur.row(), cur.column())
        keys = sorted(sheet.cells)
        ordered = [k for k in keys if k > pos] + [k for k in keys if k <= pos]
        for r, c in ordered:
            if rx.search(self._haystack(sheet, r, c)):
                view.setCurrentIndex(view.model().index(r, c))
                self.info.setText("")
                return r, c
        self.info.setText("Nic nenalezeno.")
        return None

    def _replace_cell(self, sheet, r, c, rx, repl):
        raw = sheet.cells[(r, c)].get("v", "")
        if raw.startswith("=") and not self.chk_formula.isChecked():
            return False
        new = rx.sub(lambda m: repl, raw)
        if new == raw:
            return False
        self.win._put_text(sheet, r, c, new)
        return True

    def replace_one(self):
        rx, view = self._regex(), self.win.view()
        if rx is None or view is None:
            return
        cur = view.currentIndex()
        key = (cur.row(), cur.column())
        if key in view.sheet.cells and rx.search(self._haystack(view.sheet, *key)):
            with self.win.change("Nahradit"):
                self._replace_cell(view.sheet, key[0], key[1], rx, self.repl_edit.text())
        self.find_next()

    def replace_all(self):
        rx, view = self._regex(), self.win.view()
        if rx is None or view is None:
            return
        count = 0
        with self.win.change("Nahradit vše"):
            for r, c in sorted(view.sheet.cells):
                if rx.search(self._haystack(view.sheet, r, c)):
                    count += self._replace_cell(view.sheet, r, c, rx, self.repl_edit.text())
        self.info.setText(f"Nahrazeno: {count}")


# --- grafy ------------------------------------------------------------------------
class ChartWidget(QWidget):
    def __init__(self, labels, series, kind="bar"):
        super().__init__()
        self.labels, self.series, self.kind = labels, series, kind
        self.setMinimumSize(640, 400)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("white"))
        w, h = self.width(), self.height()
        p.setFont(QFont(Theme.FONT, 9))
        # legenda
        x = 20
        for k, (name, _) in enumerate(self.series):
            p.fillRect(x, 14, 12, 12, QColor(CHART_COLORS[k % len(CHART_COLORS)]))
            p.setPen(QColor(Theme.TEXT))
            p.drawText(x + 18, 25, name)
            x += 40 + p.fontMetrics().horizontalAdvance(name)
        if not self.series or not self.labels:
            return
        if self.kind == "pie":
            vals = [max(0.0, v) for v in self.series[0][1]]
            total = sum(vals) or 1.0
            size = min(w - 260, h - 80)
            rect = QRectF(40, 50, size, size)
            angle = 90 * 16
            for k, v in enumerate(vals):
                span = int(-v / total * 360 * 16)
                p.setBrush(QBrush(QColor(CHART_COLORS[k % len(CHART_COLORS)])))
                p.setPen(QPen(QColor("white"), 2))
                p.drawPie(rect, angle, span)
                angle += span
                p.setPen(QColor(Theme.TEXT))
                p.drawText(int(size + 70), 70 + k * 20, f"{self.labels[k]}: {fmt_general(self.series[0][1][k])}")
            return
        left, right, top, bottom = 60, 20, 44, 50
        pw, ph = w - left - right, h - top - bottom
        allv = [v for _, vs in self.series for v in vs]
        vmax, vmin = max(allv + [0.0]), min(allv + [0.0])
        if vmax == vmin:
            vmax = vmin + 1
        span = vmax - vmin

        def ypos(v):
            return top + ph - (v - vmin) / span * ph
        p.setPen(QPen(QColor("#E0E0E0"), 1))
        for k in range(6):
            v = vmin + span * k / 5
            y = ypos(v)
            p.drawLine(left, int(y), left + pw, int(y))
            p.setPen(QColor(Theme.MUTED))
            p.drawText(2, int(y) + 4, left - 8, 14, int(Qt.AlignRight), fmt_general(round(v, 2)))
            p.setPen(QPen(QColor("#E0E0E0"), 1))
        n = len(self.labels)
        slot = pw / n
        p.setPen(QColor(Theme.TEXT))
        for i, lab in enumerate(self.labels):
            text = p.fontMetrics().elidedText(str(lab), Qt.ElideRight, int(slot) - 4)
            p.drawText(int(left + i * slot), h - bottom + 8, int(slot), 16, int(Qt.AlignCenter), text)
        zero = ypos(0)
        if self.kind == "bar":
            bw = slot * 0.8 / len(self.series)
            for k, (_, vs) in enumerate(self.series):
                p.setBrush(QBrush(QColor(CHART_COLORS[k % len(CHART_COLORS)])))
                p.setPen(Qt.NoPen)
                for i, v in enumerate(vs):
                    x0 = left + i * slot + slot * 0.1 + k * bw
                    y = ypos(v)
                    p.drawRect(QRectF(x0, min(y, zero), bw, abs(zero - y)))
        else:
            for k, (_, vs) in enumerate(self.series):
                color = QColor(CHART_COLORS[k % len(CHART_COLORS)])
                p.setPen(QPen(color, 2.5))
                pts = [QPointF(left + (i + 0.5) * slot, ypos(v)) for i, v in enumerate(vs)]
                for a, b in zip(pts, pts[1:]):
                    p.drawLine(a, b)
                p.setBrush(QBrush(color))
                for pt in pts:
                    p.drawEllipse(pt, 4, 4)


class ChartDialog(QDialog):
    def __init__(self, labels, series, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Graf")
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.kind = QComboBox()
        for text, key in (("Sloupcový", "bar"), ("Čárový", "line"), ("Výsečový", "pie")):
            self.kind.addItem(text, key)
        self.kind.currentIndexChanged.connect(self._change)
        bar.addWidget(QLabel("Typ grafu:"))
        bar.addWidget(self.kind)
        bar.addStretch(1)
        save = QPushButton("Uložit jako obrázek…")
        save.clicked.connect(self._save)
        bar.addWidget(save)
        lay.addLayout(bar)
        self.chart = ChartWidget(labels, series, "bar")
        lay.addWidget(self.chart, 1)

    def _change(self):
        self.chart.kind = self.kind.currentData()
        self.chart.update()

    def _save(self):
        path, _ = QFileDialog.getSaveFileName(self, "Uložit graf", "graf.png", "Obrázek PNG (*.png)")
        if path:
            self.chart.grab().save(path if path.lower().endswith(".png") else path + ".png")


# ===========================================================================
# HLAVNÍ OKNO
# ===========================================================================
class PocitarWindow(QMainWindow):
    def __init__(self, initial_path: Optional[str] = None):
        super().__init__()
        self.setMinimumSize(1000, 640)
        self.resize(1360, 860)
        self.setWindowIcon(app_icon())
        self.wb = Workbook()
        self.path: Optional[str] = None
        self.created: Optional[str] = None
        self.title = "Nový sešit VLČR"
        self._dirty = False
        self._loading = False
        self._find_dialog = None
        self.views: Dict[int, GridView] = {}
        self.undo_stack = QUndoStack(self)
        self.undo_stack.setUndoLimit(100)
        self.undo_stack.indexChanged.connect(lambda *_: self.mark_dirty())
        self._build_actions()
        self._build_menus()
        self._build_toolbars()
        self._build_ui()
        self._build_status()
        self._apply_theme()
        if initial_path:
            self.open_workbook(initial_path)
        else:
            self._rebuild_tabs(0)
            self._dirty = False
            self._refresh_title()

    # --- akce, menu, lišty --------------------------------------------------------
    def _act(self, text, shortcut=None, slot=None, tip=None, checkable=False):
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if slot:
            a.triggered.connect(lambda _=False, s=slot: s())
        if tip:
            a.setToolTip(tip)
        a.setCheckable(checkable)
        return a

    def _build_actions(self):
        A = self._act
        self.act_new = A("Nový", "Ctrl+N", self.new_workbook)
        self.act_open = A("Otevřít…", "Ctrl+O", self.open_dialog)
        self.act_save = A("Uložit", "Ctrl+S", self.save)
        self.act_save_as = A("Uložit jako…", "Ctrl+Shift+S", self.save_as)
        self.act_import = A("Importovat CSV…", None, self.import_csv)
        self.act_export = A("Exportovat CSV…", None, self.export_csv)
        self.act_pdf = A("Exportovat do PDF…", None, self.export_pdf)
        self.act_print = A("Tisk…", "Ctrl+P", self.print_sheet)
        self.act_undo = self.undo_stack.createUndoAction(self, "Zpět")
        self.act_undo.setShortcut(QKeySequence("Ctrl+Z"))
        self.act_redo = self.undo_stack.createRedoAction(self, "Znovu")
        self.act_redo.setShortcut(QKeySequence("Ctrl+Y"))
        self.act_cut = A("Vyjmout", "Ctrl+X", lambda: self.copy_selection(cut=True))
        self.act_copy = A("Kopírovat", "Ctrl+C", self.copy_selection)
        self.act_paste = A("Vložit", "Ctrl+V", self.paste)
        self.act_clear = A("Vymazat obsah (Del)", None, self.clear_contents)
        self.act_fill_down = A("Vyplnit dolů", "Ctrl+D", lambda: self.fill(True))
        self.act_fill_right = A("Vyplnit doprava", "Ctrl+R", lambda: self.fill(False))
        self.act_find = A("Najít a nahradit…", "Ctrl+F", self.show_find)
        self.act_ins_row = A("Vložit řádky", None, lambda: self.insert_delete(0, True))
        self.act_del_row = A("Odstranit řádky", None, lambda: self.insert_delete(0, False))
        self.act_ins_col = A("Vložit sloupce", None, lambda: self.insert_delete(1, True))
        self.act_del_col = A("Odstranit sloupce", None, lambda: self.insert_delete(1, False))
        self.act_add_sheet = A("Nový list", "Shift+F11", self.add_sheet)
        self.act_chart = A("Graf…", None, self.show_chart)
        self.act_sum = A("Σ Součet", "Alt+=", self.autosum, tip="Automatický součet")
        self.act_bold = A("B", "Ctrl+B", lambda: self.toggle_format("b"), "Tučně", True)
        self.act_italic = A("I", "Ctrl+I", lambda: self.toggle_format("i"), "Kurzíva", True)
        self.act_underline = A("U", "Ctrl+U", lambda: self.toggle_format("u"), "Podtržení", True)
        self.act_wrap = A("Zalomit", None, lambda: self.toggle_format("wrap"), "Zalamovat text", True)
        self.act_fg = A("A Barva textu", None, lambda: self.pick_color("fg"))
        self.act_bg = A("▇ Výplň", None, lambda: self.pick_color("bg"))
        self.act_no_fill = A("Bez výplně", None, lambda: self.apply_format(bg=None))
        self.act_left = A("⇤", None, lambda: self.apply_format(align="l"), "Zarovnat vlevo")
        self.act_center = A("↔", None, lambda: self.apply_format(align="c"), "Na střed")
        self.act_right = A("⇥", None, lambda: self.apply_format(align="r"), "Zarovnat vpravo")
        self.act_merge = A("Sloučit", None, self.merge_cells, "Sloučit / zrušit sloučení buněk")
        self.act_bd_all = A("Všechna ohraničení", None, lambda: self.set_borders("all"))
        self.act_bd_outer = A("Vnější ohraničení", None, lambda: self.set_borders("outer"))
        self.act_bd_bottom = A("Dolní ohraničení", None, lambda: self.set_borders("bottom"))
        self.act_bd_none = A("Bez ohraničení", None, lambda: self.set_borders("none"))
        self.act_dec_up = A(",0+", None, lambda: self.change_decimals(1), "Přidat desetinné místo")
        self.act_dec_down = A(",0−", None, lambda: self.change_decimals(-1), "Ubrat desetinné místo")
        self.act_sort_asc = A("Seřadit A→Z", None, lambda: self.sort(True))
        self.act_sort_desc = A("Seřadit Z→A", None, lambda: self.sort(False))
        self.act_col_width = A("Šířka sloupce…", None, lambda: self.set_size(True))
        self.act_row_height = A("Výška řádku…", None, lambda: self.set_size(False))
        self.act_autofit = A("Přizpůsobit šířku sloupce", None, self.autofit_columns)
        self.act_updates = A("Zkontrolovat aktualizace…", None, self.check_updates)
        self.act_about = A("O aplikaci Počítář", None, self.about)

    def _build_menus(self):
        bar = self.menuBar()
        m = bar.addMenu("Soubor")
        for a in (self.act_new, self.act_open, self.act_save, self.act_save_as):
            m.addAction(a)
        m.addSeparator()
        for a in (self.act_import, self.act_export, self.act_pdf, self.act_print):
            m.addAction(a)
        m.addSeparator()
        m.addAction("Konec", self.close)
        m = bar.addMenu("Úpravy")
        for a in (self.act_undo, self.act_redo, None, self.act_cut, self.act_copy, self.act_paste,
                  self.act_clear, None, self.act_fill_down, self.act_fill_right, None, self.act_find):
            m.addSeparator() if a is None else m.addAction(a)
        m = bar.addMenu("Vložení")
        for a in (self.act_ins_row, self.act_ins_col, self.act_add_sheet, None, self.act_sum, self.act_chart):
            m.addSeparator() if a is None else m.addAction(a)
        m = bar.addMenu("Formát")
        for a in (self.act_bold, self.act_italic, self.act_underline, self.act_wrap, None, self.act_fg,
                  self.act_bg, self.act_no_fill, None, self.act_merge, None, self.act_bd_all,
                  self.act_bd_outer, self.act_bd_bottom, self.act_bd_none, None, self.act_col_width,
                  self.act_row_height, self.act_autofit, None, self.act_del_row, self.act_del_col):
            m.addSeparator() if a is None else m.addAction(a)
        m = bar.addMenu("Data")
        m.addAction(self.act_sort_asc)
        m.addAction(self.act_sort_desc)
        m = bar.addMenu("Nápověda")
        m.addAction(self.act_updates)
        m.addAction(self.act_about)

    def _build_toolbars(self):
        tb = QToolBar("Hlavní")
        tb.setMovable(False)
        self.addToolBar(Qt.TopToolBarArea, tb)
        for a in (self.act_new, self.act_open, self.act_save, self.act_undo, self.act_redo,
                  self.act_cut, self.act_copy, self.act_paste, self.act_find, self.act_sum,
                  self.act_sort_asc, self.act_sort_desc, self.act_chart):
            tb.addAction(a)
        self.addToolBarBreak(Qt.TopToolBarArea)
        fb = QToolBar("Formát")
        fb.setMovable(False)
        self.addToolBar(Qt.TopToolBarArea, fb)
        self.font_combo = QFontComboBox()
        self.font_combo.setMaximumWidth(170)
        self.font_combo.activated[int].connect(lambda _: self.apply_format(font=self.font_combo.currentFont().family()))
        fb.addWidget(self.font_combo)
        self.size_combo = QComboBox()
        self.size_combo.setEditable(True)
        self.size_combo.setMaximumWidth(64)
        self.size_combo.addItems([str(s) for s in (8, 9, 10, 11, 12, 14, 16, 18, 20, 24, 28, 36, 48, 72)])
        self.size_combo.activated[int].connect(lambda _: self._size_chosen())
        self.size_combo.lineEdit().returnPressed.connect(self._size_chosen)
        fb.addWidget(self.size_combo)
        fb.addSeparator()
        for a in (self.act_bold, self.act_italic, self.act_underline, self.act_fg, self.act_bg):
            fb.addAction(a)
        fb.addSeparator()
        for a in (self.act_left, self.act_center, self.act_right, self.act_wrap, self.act_merge):
            fb.addAction(a)
        borders = QToolButton()
        borders.setText("Ohraničení ▾")
        borders.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(borders)
        for a in (self.act_bd_all, self.act_bd_outer, self.act_bd_bottom, self.act_bd_none):
            menu.addAction(a)
        borders.setMenu(menu)
        fb.addWidget(borders)
        fb.addSeparator()
        self.nf_combo = QComboBox()
        for text, key in NUMBER_FORMATS:
            self.nf_combo.addItem(text, key)
        self.nf_combo.activated[int].connect(lambda _: self.apply_format(nf=self.nf_combo.currentData(), dec=None))
        fb.addWidget(self.nf_combo)
        fb.addAction(self.act_dec_up)
        fb.addAction(self.act_dec_down)

    def _build_ui(self):
        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        strip = QFrame()
        strip.setFixedHeight(6)
        strip.setStyleSheet(f"background:{Theme.ACCENT};")
        lay.addWidget(strip)
        bar = QHBoxLayout()
        bar.setContentsMargins(8, 6, 8, 6)
        self.name_box = QLineEdit()
        self.name_box.setFixedWidth(100)
        self.name_box.returnPressed.connect(lambda: self.goto(self.name_box.text()))
        fx = QLabel("fx")
        fx.setStyleSheet(f"color:{Theme.ACCENT}; font-style:italic; font-weight:bold; padding:0 6px;")
        self.formula_edit = QLineEdit()
        self.formula_edit.returnPressed.connect(self._formula_entered)
        bar.addWidget(self.name_box)
        bar.addWidget(fx)
        bar.addWidget(self.formula_edit, 1)
        lay.addLayout(bar)
        self.tabs = QTabWidget()
        self.tabs.setTabPosition(QTabWidget.South)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        add = QToolButton()
        add.setText("＋")
        add.setToolTip("Nový list")
        add.clicked.connect(self.add_sheet)
        self.tabs.setCornerWidget(add, Qt.BottomLeftCorner)
        self.tabs.currentChanged.connect(self.on_tab_changed)
        self.tabs.tabBar().tabMoved.connect(self._tab_moved)
        self.tabs.tabBar().tabBarDoubleClicked.connect(self.rename_sheet)
        self.tabs.tabBar().setContextMenuPolicy(Qt.CustomContextMenu)
        self.tabs.tabBar().customContextMenuRequested.connect(self._tab_menu)
        lay.addWidget(self.tabs, 1)
        self.setCentralWidget(root)

    def _build_status(self):
        sb = QStatusBar()
        self.setStatusBar(sb)
        self.status_text = QLabel("Připraveno")
        self.stats_label = QLabel("")
        sb.addWidget(self.status_text, 1)
        sb.addPermanentWidget(self.stats_label)
        sb.addPermanentWidget(QLabel(f"{APP_NAME} {APP_VERSION}"))

    def _apply_theme(self):
        a, b = Theme.ACCENT, Theme.BORDER
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background:{Theme.WINDOW}; color:{Theme.TEXT}; font-family:'{Theme.FONT}'; }}
            QMenuBar {{ background:white; border-bottom:1px solid {b}; padding:3px; }}
            QMenuBar::item {{ padding:6px 10px; }} QMenuBar::item:selected {{ background:{Theme.SELECTION}; }}
            QMenu {{ background:white; border:1px solid {b}; }} QMenu::item {{ padding:6px 22px; }}
            QMenu::item:selected {{ background:{Theme.SELECTION}; }}
            QToolBar {{ background:#FAFAFA; border:0; border-bottom:1px solid {b}; spacing:4px; padding:4px; }}
            QToolButton {{ padding:5px 9px; border:1px solid transparent; }}
            QToolButton:hover {{ background:{Theme.SELECTION}; border:1px solid {a}; }}
            QToolButton:checked {{ background:{a}; color:white; border:1px solid {a}; }}
            QToolButton::menu-indicator {{ image:none; }}
            QLineEdit, QComboBox, QFontComboBox {{ background:white; border:1px solid {b}; padding:4px; min-height:22px; }}
            QLineEdit:focus {{ border:1px solid {a}; }}
            QPushButton {{ background:white; border:1px solid {b}; padding:6px 16px; }}
            QPushButton:hover {{ border-color:{a}; }}
            QTableView {{ gridline-color:{Theme.GRID}; background:white; border:none; }}
            QHeaderView::section {{ background:{Theme.HEADER}; color:#404040; border:0;
                border-right:1px solid {Theme.GRID}; border-bottom:1px solid {Theme.GRID}; padding:3px; }}
            QHeaderView::section:checked {{ background:#F6C9B8; color:black; }}
            QTableCornerButton::section {{ background:{Theme.HEADER}; border:0;
                border-right:1px solid {Theme.GRID}; border-bottom:1px solid {Theme.GRID}; }}
            QTabWidget::pane {{ border:0; border-top:1px solid {b}; }}
            QTabBar::tab {{ background:#EAEAEA; border:1px solid {b}; border-top:0; padding:6px 18px; }}
            QTabBar::tab:selected {{ background:white; border-bottom:3px solid {a}; font-weight:bold; }}
            QStatusBar {{ background:{a}; color:white; }} QStatusBar QLabel {{ color:white; padding:0 8px; background:transparent; }}
        """)

    # --- pomocné přístupy ----------------------------------------------------------
    def view(self) -> Optional[GridView]:
        return self.tabs.currentWidget()

    def sheet(self) -> Sheet:
        return self.view().sheet

    def mark_dirty(self):
        if self._loading:
            return
        self._dirty = True
        self._refresh_title()

    def _refresh_title(self):
        name = os.path.basename(self.path) if self.path else self.title
        self.setWindowTitle(f"{'● ' if self._dirty else ''}{name} — {APP_NAME} — VLČR Kancelář")

    def sel_bounds(self):
        v = self.view()
        sel = v.selectionModel().selection()
        if sel.isEmpty():
            i = v.currentIndex()
            return i.row(), i.column(), i.row(), i.column()
        return (min(r.top() for r in sel), min(r.left() for r in sel),
                max(r.bottom() for r in sel), max(r.right() for r in sel))

    def sel_cells(self, limit=300000):
        v = self.view()
        sel = v.selectionModel().selection()
        seen = 0
        if sel.isEmpty():
            i = v.currentIndex()
            yield i.row(), i.column()
            return
        for rng in sel:
            for r in range(rng.top(), rng.bottom() + 1):
                for c in range(rng.left(), rng.right() + 1):
                    seen += 1
                    if seen > limit:
                        return
                    yield r, c

    @contextmanager
    def change(self, text, sheet=None, all_sheets=False):
        """Změna s podporou Zpět/Znovu: uloží stav před a po a vloží krok do zásobníku."""
        sheets = list(self.wb.sheets) if all_sheets else [sheet or self.sheet()]
        before = [s.snapshot() for s in sheets]
        try:
            yield
        except Exception:
            for s, b in zip(sheets, before):
                s.restore(b)
            raise
        states = [(s, b, s.snapshot()) for s, b in zip(sheets, before)]
        self.after_restore(states, use_after=True)
        self.undo_stack.push(SnapshotCommand(self, states, text))

    def after_restore(self, states, use_after):
        self.wb.invalidate()
        for sheet, before, after in states:
            structural = before[1:] != after[1:]
            view = self.views.get(id(sheet))
            if view is not None:
                view.refresh(structural)
        for view in self.views.values():
            if not any(view.sheet is s for s, _, _ in states):
                view.refresh(False)
        self.on_current_changed()

    def refresh_all(self):
        self.wb.invalidate()
        for v in self.views.values():
            v.refresh(False)
        self.on_current_changed()

    # --- stav výběru, vzorcový řádek ----------------------------------------------
    def on_current_changed(self, *_):
        v = self.view()
        if v is None or self._loading:
            return
        cur = v.currentIndex()
        if not cur.isValid():
            return
        r, c = cur.row(), cur.column()
        self.name_box.setText(cell_name(r, c))
        cell = v.sheet.cells.get((r, c))
        self.formula_edit.setText(cell.get("v", "") if cell else "")
        self._sync_format_controls(cell.get("f") if cell else None)

    def on_selection_changed(self, *_):
        v = self.view()
        if v is None or self._loading:
            return
        nums_ = []
        count = 0
        for r, c in self.sel_cells(50000):
            count += 1
            val = self.wb.value(v.sheet, r, c)
            if is_number(val):
                nums_.append(float(val))
        if count > 1 and nums_:
            self.stats_label.setText(
                f"Součet: {fmt_general(round(math.fsum(nums_), 10))}   Průměr: "
                f"{fmt_general(round(math.fsum(nums_) / len(nums_), 10))}   Počet: {len(nums_)}")
        else:
            self.stats_label.setText("")

    def on_tab_changed(self, *_):
        if not self._loading:
            self.on_current_changed()
            self.on_selection_changed()

    def _sync_format_controls(self, f):
        f = f or {}
        for w in (self.font_combo, self.size_combo, self.nf_combo):
            w.blockSignals(True)
        self.font_combo.setCurrentFont(QFont(f.get("font") or DEFAULT_FONT))
        self.size_combo.setEditText(str(f.get("size") or DEFAULT_SIZE))
        self.nf_combo.setCurrentIndex(max(0, self.nf_combo.findData(f.get("nf", "general"))))
        for w in (self.font_combo, self.size_combo, self.nf_combo):
            w.blockSignals(False)
        for act, key in ((self.act_bold, "b"), (self.act_italic, "i"), (self.act_underline, "u"),
                         (self.act_wrap, "wrap")):
            act.blockSignals(True)
            act.setChecked(bool(f.get(key)))
            act.blockSignals(False)

    def _formula_entered(self):
        v = self.view()
        cur = v.currentIndex()
        self.set_cell_text(v.sheet, cur.row(), cur.column(), self.formula_edit.text())
        v.setFocus()
        nxt = v.model().index(cur.row() + 1, cur.column())
        if nxt.isValid():
            v.setCurrentIndex(nxt)

    def goto(self, text):
        v = self.view()
        parts = text.strip().split(":")
        a = parse_address(parts[0])
        b = parse_address(parts[1]) if len(parts) > 1 else a
        if a is None or b is None:
            QMessageBox.information(self, "Přejít na", "Zadejte adresu buňky (např. B5) nebo oblast (A1:C10).")
            return
        r1, r2, c1, c2 = min(a[0], b[0]), max(a[0], b[0]), min(a[1], b[1]), max(a[1], b[1])
        if r2 >= v.sheet.n_rows or c2 >= v.sheet.n_cols:
            QMessageBox.information(self, "Přejít na", "Adresa leží mimo tabulku.")
            return
        v.setCurrentIndex(v.model().index(r1, c1))
        v.selectionModel().select(QItemSelection(v.model().index(r1, c1), v.model().index(r2, c2)),
                                  QItemSelectionModel.ClearAndSelect)
        v.setFocus()

    # --- úpravy buněk ---------------------------------------------------------------
    @staticmethod
    def _in_hidden_merge(sheet, r, c):
        return any(m[0] <= r <= m[2] and m[1] <= c <= m[3] and (r, c) != (m[0], m[1]) for m in sheet.merges)

    def _put_text(self, sheet, r, c, text):
        old = sheet.cells.get((r, c))
        f = dict(old.get("f") or {}) if old else {}
        if text == "" and not f:
            sheet.cells.pop((r, c), None)
            return
        if text and not text.startswith(("=", "'")) and f.get("nf", "general") == "general":
            lit = parse_literal(text)
            if parse_date(text) is not None:
                f["nf"] = "date"
            elif text.strip().endswith("%") and is_number(lit):
                f["nf"] = "percent"
        cell = {"v": text}
        if f:
            cell["f"] = f
        sheet.cells[(r, c)] = cell

    def set_cell_text(self, sheet, r, c, text):
        if self._in_hidden_merge(sheet, r, c):
            return
        if (sheet.cells.get((r, c)) or {}).get("v", "") == text:
            return
        with self.change("Úprava buňky", sheet):
            self._put_text(sheet, r, c, text)

    def clear_contents(self):
        sheet = self.sheet()
        with self.change("Vymazat obsah"):
            for r, c in self.sel_cells():
                if (r, c) in sheet.cells:
                    self._put_text(sheet, r, c, "")

    def apply_format(self, **updates):
        sheet = self.sheet()
        with self.change("Formátování"):
            for r, c in self.sel_cells():
                cell = sheet.cells.get((r, c))
                f = dict(cell.get("f") or {}) if cell else {}
                for k, val in updates.items():
                    if val is None or val is False:
                        f.pop(k, None)
                    else:
                        f[k] = val
                raw = cell.get("v", "") if cell else ""
                if not raw and not f:
                    sheet.cells.pop((r, c), None)
                else:
                    sheet.cells[(r, c)] = {"v": raw, **({"f": f} if f else {})}

    def toggle_format(self, key):
        sheet = self.sheet()
        cur = self.view().currentIndex()
        cell = sheet.cells.get((cur.row(), cur.column()))
        on = bool(((cell or {}).get("f") or {}).get(key))
        self.apply_format(**{key: None if on else True})
        self.on_current_changed()

    def _size_chosen(self):
        try:
            size = int(float(self.size_combo.currentText().replace(",", ".")))
        except ValueError:
            return
        if 4 <= size <= 200:
            self.apply_format(size=size)

    def pick_color(self, key):
        cur = self.view().currentIndex()
        cell = self.sheet().cells.get((cur.row(), cur.column()))
        start = QColor(((cell or {}).get("f") or {}).get(key) or ("#000000" if key == "fg" else "#FFFFFF"))
        color = QColorDialog.getColor(start, self, "Barva textu" if key == "fg" else "Barva výplně")
        if color.isValid():
            self.apply_format(**{key: color.name().upper()})

    def change_decimals(self, delta):
        cur = self.view().currentIndex()
        cell = self.sheet().cells.get((cur.row(), cur.column()))
        f = (cell or {}).get("f") or {}
        dec = f.get("dec")
        if dec is None:
            dec = 2 if f.get("nf") in ("number", "currency") else 0
        self.apply_format(dec=max(0, min(10, dec + delta)))

    def set_borders(self, kind):
        sheet = self.sheet()
        r1, c1, r2, c2 = self.sel_bounds()
        with self.change("Ohraničení"):
            for r, c in self.sel_cells():
                cell = sheet.cells.get((r, c))
                f = dict(cell.get("f") or {}) if cell else {}
                if kind == "all":
                    sides = "lrtb"
                elif kind == "outer":
                    sides = ("l" if c == c1 else "") + ("r" if c == c2 else "") + \
                            ("t" if r == r1 else "") + ("b" if r == r2 else "")
                elif kind == "bottom":
                    sides = "b"
                else:
                    sides = ""
                if sides:
                    f["bd"] = sides
                else:
                    f.pop("bd", None)
                raw = cell.get("v", "") if cell else ""
                if not raw and not f:
                    sheet.cells.pop((r, c), None)
                else:
                    sheet.cells[(r, c)] = {"v": raw, **({"f": f} if f else {})}

    def merge_cells(self):
        sheet = self.sheet()
        r1, c1, r2, c2 = self.sel_bounds()
        exact = (r1, c1, r2, c2) in sheet.merges
        if (r1, c1) == (r2, c2) and not exact:
            return
        with self.change("Sloučit buňky"):
            sheet.merges = [m for m in sheet.merges
                            if m[2] < r1 or m[0] > r2 or m[3] < c1 or m[1] > c2]
            if not exact:
                sheet.merges.append((r1, c1, r2, c2))
                for r in range(r1, r2 + 1):
                    for c in range(c1, c2 + 1):
                        if (r, c) != (r1, c1) and (r, c) in sheet.cells:
                            sheet.cells.pop((r, c))

    def set_size(self, width):
        sheet = self.sheet()
        r1, c1, r2, c2 = self.sel_bounds()
        cur = self.view().columnWidth(c1) if width else self.view().rowHeight(r1)
        value, ok = QInputDialog.getInt(self, "Šířka sloupce" if width else "Výška řádku",
                                        "Velikost v pixelech:", cur, 10, 1000)
        if not ok:
            return
        with self.change("Změna velikosti"):
            if width:
                for c in range(c1, c2 + 1):
                    sheet.col_widths[c] = value
            else:
                for r in range(r1, r2 + 1):
                    sheet.row_heights[r] = value

    def autofit_columns(self):
        v, sheet = self.view(), self.sheet()
        r1, c1, r2, c2 = self.sel_bounds()
        with self.change("Přizpůsobit šířku"):
            for c in range(c1, c2 + 1):
                best = 40
                for (r, cc), cell in sheet.cells.items():
                    if cc != c:
                        continue
                    text = format_value(self.wb.value(sheet, r, c), cell.get("f"))
                    fm = make_font(cell.get("f"))
                    best = max(best, QFontMetrics(fm).horizontalAdvance(text) + 18)
                sheet.col_widths[c] = min(best, 600)

    # --- schránka --------------------------------------------------------------------
    def copy_selection(self, cut=False):
        sheet = self.sheet()
        r1, c1, r2, c2 = self.sel_bounds()
        cells = [[r - r1, c - c1, cell] for (r, c), cell in sheet.cells.items()
                 if r1 <= r <= r2 and c1 <= c <= c2 for cell in [cell]]
        lines = []
        for r in range(r1, r2 + 1):
            lines.append("\t".join(format_value(self.wb.value(sheet, r, c), (sheet.cells.get((r, c)) or {}).get("f"))
                                   for c in range(c1, c2 + 1)))
        mime = QMimeData()
        mime.setText("\n".join(lines))
        mime.setData(MIME_CELLS, QByteArray(json.dumps(
            {"r": r1, "c": c1, "rows": r2 - r1 + 1, "cols": c2 - c1 + 1, "cells": cells},
            ensure_ascii=False).encode("utf-8")))
        QApplication.clipboard().setMimeData(mime)
        if cut:
            with self.change("Vyjmout"):
                for r in range(r1, r2 + 1):
                    for c in range(c1, c2 + 1):
                        sheet.cells.pop((r, c), None)
        self.status_text.setText("Zkopírováno" if not cut else "Vyjmuto")

    def paste(self):
        md = QApplication.clipboard().mimeData()
        sheet = self.sheet()
        r1, c1, r2, c2 = self.sel_bounds()
        if md.hasFormat(MIME_CELLS):
            data = json.loads(bytes(md.data(MIME_CELLS)).decode("utf-8"))
            h, w = data["rows"], data["cols"]
            if (h, w) == (1, 1):
                origins = [(r, c) for r in range(r1, r2 + 1) for c in range(c1, c2 + 1)]
            else:
                origins = [(r1, c1)]
            with self.change("Vložit"):
                for orow, ocol in origins:
                    for r in range(orow, min(orow + h, sheet.n_rows)):
                        for c in range(ocol, min(ocol + w, sheet.n_cols)):
                            sheet.cells.pop((r, c), None)
                    for dr, dc, cell in data["cells"]:
                        r, c = orow + dr, ocol + dc
                        if r >= sheet.n_rows or c >= sheet.n_cols:
                            continue
                        new = dict(cell)
                        v = new.get("v", "")
                        if v.startswith("="):
                            new["v"] = shift_formula(v, orow - data["r"], ocol - data["c"])
                        sheet.cells[(r, c)] = new
            return
        text = md.text()
        if not text:
            return
        rows = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n").split("\n")
        with self.change("Vložit text"):
            for i, line in enumerate(rows):
                for j, val in enumerate(line.split("\t")):
                    r, c = r1 + i, c1 + j
                    if r < sheet.n_rows and c < sheet.n_cols:
                        self._put_text(sheet, r, c, val)

    def fill(self, down):
        sheet = self.sheet()
        r1, c1, r2, c2 = self.sel_bounds()
        if (down and r1 == r2) or (not down and c1 == c2):
            return
        with self.change("Vyplnit"):
            if down:
                for c in range(c1, c2 + 1):
                    src = sheet.cells.get((r1, c))
                    for r in range(r1 + 1, r2 + 1):
                        self._copy_cell(sheet, src, (r, c), r - r1, 0)
            else:
                for r in range(r1, r2 + 1):
                    src = sheet.cells.get((r, c1))
                    for c in range(c1 + 1, c2 + 1):
                        self._copy_cell(sheet, src, (r, c), 0, c - c1)

    @staticmethod
    def _copy_cell(sheet, src, dest, dr, dc):
        if src is None:
            sheet.cells.pop(dest, None)
            return
        new = dict(src)
        if new.get("v", "").startswith("="):
            new["v"] = shift_formula(new["v"], dr, dc)
        sheet.cells[dest] = new

    # --- struktura, řazení, autosumace ------------------------------------------------
    def insert_delete(self, axis, insert):
        sheet = self.sheet()
        r1, c1, r2, c2 = self.sel_bounds()
        at, n = (r1, r2 - r1 + 1) if axis == 0 else (c1, c2 - c1 + 1)
        if insert and ((axis == 0 and sheet.n_rows + n > MAX_ROWS) or (axis == 1 and sheet.n_cols + n > MAX_COLS)):
            return
        with self.change("Vložit" if insert else "Odstranit", all_sheets=True):
            self.wb.structural(sheet, axis, at, n if insert else -n)

    def _current_region(self, r, c):
        sheet = self.sheet()
        r1 = r2 = r
        c1 = c2 = c

        def filled(rr, cc):
            return (rr, cc) in sheet.cells and sheet.cells[(rr, cc)].get("v", "") != ""
        changed = True
        while changed:
            changed = False
            if r1 > 0 and any(filled(r1 - 1, cc) for cc in range(max(0, c1 - 1), c2 + 2)):
                r1 -= 1
                changed = True
            if any(filled(r2 + 1, cc) for cc in range(max(0, c1 - 1), c2 + 2)):
                r2 += 1
                changed = True
            if c1 > 0 and any(filled(rr, c1 - 1) for rr in range(max(0, r1 - 1), r2 + 2)):
                c1 -= 1
                changed = True
            if any(filled(rr, c2 + 1) for rr in range(max(0, r1 - 1), r2 + 2)):
                c2 += 1
                changed = True
        return r1, c1, r2, c2

    def sort(self, ascending):
        sheet = self.sheet()
        r1, c1, r2, c2 = self.sel_bounds()
        key_col = c1
        if (r1, c1) == (r2, c2):
            cur = self.view().currentIndex()
            key_col = cur.column()
            r1, c1, r2, c2 = self._current_region(cur.row(), cur.column())
        first = [self.wb.value(sheet, r1, c) for c in range(c1, c2 + 1)]
        second = [self.wb.value(sheet, r1 + 1, c) for c in range(c1, c2 + 1)] if r2 > r1 else []
        if r2 > r1 and all(isinstance(v, str) for v in first if v is not None) and any(is_number(v) for v in second):
            r1 += 1                                   # první řádek je záhlaví
        if r2 <= r1:
            return
        with self.change("Seřadit"):
            self.wb.sort_range(sheet, r1, c1, r2, c2, key_col, ascending)

    def autosum(self):
        sheet = self.sheet()
        r1, c1, r2, c2 = self.sel_bounds()
        with self.change("Automatický součet"):
            if (r1, c1) != (r2, c2):
                if r2 + 1 < sheet.n_rows:
                    for c in range(c1, c2 + 1):
                        n = col_name(c)
                        self._put_text(sheet, r2 + 1, c, f"=SUM({n}{r1 + 1}:{n}{r2 + 1})")
                return
            r = r1 - 1
            while r >= 0 and is_number(self.wb.value(sheet, r, c1)):
                r -= 1
            if r < r1 - 1:
                n = col_name(c1)
                self._put_text(sheet, r1, c1, f"=SUM({n}{r + 2}:{n}{r1})")
                return
            c = c1 - 1
            while c >= 0 and is_number(self.wb.value(sheet, r1, c)):
                c -= 1
            if c < c1 - 1:
                self._put_text(sheet, r1, c1, f"=SUM({col_name(c + 1)}{r1 + 1}:{col_name(c1 - 1)}{r1 + 1})")
            else:
                self._put_text(sheet, r1, c1, "=SUM()")

    def show_chart(self):
        sheet = self.sheet()
        r1, c1, r2, c2 = self.sel_bounds()
        vals = [[self.wb.value(sheet, r, c) for c in range(c1, c2 + 1)] for r in range(r1, r2 + 1)]
        if len(vals) * len(vals[0]) < 2:
            QMessageBox.information(self, "Graf", "Označte oblast s daty (včetně popisků) a zkuste to znovu.")
            return
        label_col = len(vals[0]) > 1 and any(isinstance(row[0], str) for row in vals[1:] or vals)
        start = 1 if label_col else 0
        header = any(isinstance(x, str) for x in vals[0][start:])
        body = vals[1:] if header else vals
        labels = [to_text(row[0]) if label_col else str(i + 1) for i, row in enumerate(body)]
        series = []
        for j in range(start, len(vals[0])):
            name = to_text(vals[0][j]) if header else f"Řada {j - start + 1}"
            series.append((name, [float(row[j]) if is_number(row[j]) else 0.0 for row in body]))
        ChartDialog(labels, series, self).exec_()

    # --- listy -----------------------------------------------------------------------
    def _add_view(self, sheet):
        view = GridView(self, sheet)
        self.views[id(sheet)] = view
        self.tabs.addTab(view, sheet.name)
        return view

    def _rebuild_tabs(self, active=0):
        self._loading = True
        while self.tabs.count():
            w = self.tabs.widget(0)
            self.tabs.removeTab(0)
            w.deleteLater()
        self.views = {}
        for sh in self.wb.sheets:
            self._add_view(sh)
        self.tabs.setCurrentIndex(max(0, min(active, self.tabs.count() - 1)))
        self._loading = False
        self.on_current_changed()
        self.on_selection_changed()

    def add_sheet(self):
        sheet = Sheet(self.wb.unique_name("List"))
        self.wb.sheets.append(sheet)
        self.undo_stack.clear()
        self._add_view(sheet)
        self.tabs.setCurrentIndex(self.tabs.count() - 1)
        self.mark_dirty()

    def _tab_moved(self, frm, to):
        self.wb.sheets.insert(to, self.wb.sheets.pop(frm))
        self.mark_dirty()

    def _tab_menu(self, pos):
        idx = self.tabs.tabBar().tabAt(pos)
        if idx < 0:
            return
        menu = QMenu(self)
        menu.addAction("Přejmenovat…", lambda: self.rename_sheet(idx))
        menu.addAction("Duplikovat", lambda: self.duplicate_sheet(idx))
        menu.addAction("Odstranit", lambda: self.delete_sheet(idx))
        menu.exec_(self.tabs.tabBar().mapToGlobal(pos))

    def rename_sheet(self, idx):
        sheet = self.tabs.widget(idx).sheet
        name, ok = QInputDialog.getText(self, "Přejmenovat list", "Název listu:", text=sheet.name)
        name = name.strip()
        if not ok or not name or name == sheet.name:
            return
        if re.search(r"[\[\]:*?/\\]", name) or len(name) > 31:
            QMessageBox.warning(self, "Přejmenovat list", "Název nesmí obsahovat znaky []:*?/\\ ani být delší než 31 znaků.")
            return
        other = self.wb.sheet_by_name(name)
        if other is not None and other is not sheet:
            QMessageBox.warning(self, "Přejmenovat list", "List s tímto názvem už existuje.")
            return
        self.wb.rename_sheet(sheet, name)
        self.tabs.setTabText(idx, name)
        self.undo_stack.clear()
        self.refresh_all()
        self.mark_dirty()

    def duplicate_sheet(self, idx):
        src = self.tabs.widget(idx).sheet
        new = Sheet(self.wb.unique_name(src.name + " (kopie)"))
        new.restore(src.snapshot())
        self.wb.sheets.insert(idx + 1, new)
        self.undo_stack.clear()
        self._rebuild_tabs(idx + 1)
        self.mark_dirty()

    def delete_sheet(self, idx):
        if len(self.wb.sheets) <= 1:
            QMessageBox.information(self, "Odstranit list", "Sešit musí obsahovat alespoň jeden list.")
            return
        sheet = self.tabs.widget(idx).sheet
        if QMessageBox.question(self, "Odstranit list", f"Opravdu odstranit list „{sheet.name}“?",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self.wb.sheets.remove(sheet)
        self.undo_stack.clear()
        self._rebuild_tabs(max(0, idx - 1))
        self.wb.invalidate()
        self.refresh_all()
        self.mark_dirty()

    def show_find(self):
        if self._find_dialog is None:
            self._find_dialog = FindDialog(self)
        self._find_dialog.show()
        self._find_dialog.raise_()
        self._find_dialog.find_edit.setFocus()

    # --- soubor ----------------------------------------------------------------------
    def _confirm_discard(self) -> bool:
        if not self._dirty:
            return True
        answer = QMessageBox.question(self, "Neuložené změny", "Sešit obsahuje neuložené změny. Chcete je uložit?",
                                      QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel, QMessageBox.Yes)
        if answer == QMessageBox.Cancel:
            return False
        return self.save() if answer == QMessageBox.Yes else True

    def new_workbook(self):
        if not self._confirm_discard():
            return
        self.wb = Workbook()
        self.path, self.created, self.title = None, _now(), "Nový sešit VLČR"
        self.undo_stack.clear()
        self._rebuild_tabs(0)
        self._dirty = False
        self._refresh_title()

    def open_dialog(self):
        dialog = ExplorerOpenDialog(parent=self)
        if dialog.exec_() == QDialog.Accepted and dialog.selected_path:
            self.open_workbook(dialog.selected_path)

    def open_workbook(self, path: str):
        if not self._confirm_discard():
            return
        try:
            meta, wb, active = read_workbook(path)
        except Exception as exc:
            QMessageBox.critical(self, "Otevření sešitu", str(exc))
            return
        self.wb = wb
        self.path = os.path.abspath(path)
        self.created = meta.get("created")
        self.title = str(meta.get("title") or Path(path).stem)
        self.undo_stack.clear()
        self._rebuild_tabs(active)
        self._dirty = False
        self._refresh_title()
        self.status_text.setText(f"Otevřeno: {self.path}")

    def save(self) -> bool:
        return self._save_to(self.path) if self.path else self.save_as()

    def save_as(self) -> bool:
        path, _ = QFileDialog.getSaveFileName(self, "Uložit sešit", os.path.expanduser("~/Nový sešit VLČR.vlcr"),
                                              "Sešit VLČR (*.vlcr)")
        if not path:
            return False
        return self._save_to(path if path.lower().endswith(".vlcr") else path + ".vlcr")

    def _save_to(self, path: str) -> bool:
        try:
            meta = write_workbook(path, self.wb, self.title, self.created, max(0, self.tabs.currentIndex()))
        except Exception as exc:
            QMessageBox.critical(self, "Uložení sešitu", str(exc))
            return False
        self.path = os.path.abspath(path)
        self.created = meta.get("created")
        self._dirty = False
        self._refresh_title()
        self.status_text.setText(f"Uloženo: {self.path}")
        return True

    def import_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Importovat CSV", os.path.expanduser("~"),
                                              "CSV (*.csv *.txt);;Všechny soubory (*)")
        if not path:
            return
        text = None
        for enc in ("utf-8-sig", "cp1250", "latin-1"):
            try:
                with open(path, encoding=enc, newline="") as fh:
                    text = fh.read()
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            QMessageBox.warning(self, "Import CSV", "Soubor se nepodařilo přečíst.")
            return
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=";,\t")
        except csv.Error:
            dialect = csv.excel
            dialect.delimiter = ";"
        rows = list(csv.reader(text.splitlines(), dialect))
        sheet = Sheet(self.wb.unique_name(Path(path).stem[:31] or "Import"))
        sheet.n_rows = max(DEFAULT_ROWS, min(MAX_ROWS, len(rows) + 50))
        sheet.n_cols = max(DEFAULT_COLS, min(MAX_COLS, max((len(r) for r in rows), default=0) + 5))
        for r, row in enumerate(rows[:MAX_ROWS]):
            for c, val in enumerate(row[:MAX_COLS]):
                if val != "":
                    self._put_text(sheet, r, c, val)
        self.wb.sheets.append(sheet)
        self.undo_stack.clear()
        self._add_view(sheet)
        self.tabs.setCurrentIndex(self.tabs.count() - 1)
        self.mark_dirty()

    def export_csv(self):
        sheet = self.sheet()
        path, _ = QFileDialog.getSaveFileName(self, "Exportovat CSV", f"{sheet.name}.csv", "CSV (*.csv)")
        if not path:
            return
        rows, cols = sheet.used_bounds()
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.writer(fh, delimiter=";")
                for r in range(rows):
                    writer.writerow([format_value(self.wb.value(sheet, r, c), (sheet.cells.get((r, c)) or {}).get("f"))
                                     for c in range(cols)])
            self.status_text.setText(f"CSV exportováno: {path}")
        except OSError as exc:
            QMessageBox.critical(self, "Export CSV", str(exc))

    def _sheet_html(self, sheet) -> str:
        rows, cols = sheet.used_bounds()
        out = ['<table border="0" cellspacing="0" cellpadding="3" style="border-collapse:collapse;font-family:Segoe UI;font-size:10pt">']
        for r in range(rows):
            out.append("<tr>")
            for c in range(cols):
                cell = sheet.cells.get((r, c))
                f = (cell.get("f") if cell else None) or {}
                val = self.wb.value(sheet, r, c) if cell else None
                text = escape(format_value(val, f)).replace("\n", "<br>")
                align = {"l": "left", "c": "center", "r": "right"}[f.get("align") or ("right" if is_number(val) else "left")[0]]
                style = f"text-align:{align};border:1px solid #BBBBBB;"
                if f.get("b"):
                    style += "font-weight:bold;"
                if f.get("i"):
                    style += "font-style:italic;"
                if f.get("u"):
                    style += "text-decoration:underline;"
                if f.get("fg"):
                    style += f"color:{f['fg']};"
                if f.get("bg"):
                    style += f"background-color:{f['bg']};"
                out.append(f'<td style="{style}">{text or "&nbsp;"}</td>')
            out.append("</tr>")
        out.append("</table>")
        return "".join(out)

    def _printer_doc(self, printer):
        doc = QTextDocument()
        doc.setHtml(self._sheet_html(self.sheet()))
        return doc

    def export_pdf(self):
        path, _ = QFileDialog.getSaveFileName(self, "Exportovat do PDF", f"{self.sheet().name}.pdf", "PDF (*.pdf)")
        if not path:
            return
        printer = QPrinter(QPrinter.HighResolution)
        printer.setOutputFormat(QPrinter.PdfFormat)
        printer.setOutputFileName(path if path.lower().endswith(".pdf") else path + ".pdf")
        printer.setPageSize(QPrinter.A4)
        printer.setOrientation(QPrinter.Landscape)
        self._printer_doc(printer).print_(printer)
        self.status_text.setText(f"PDF exportováno: {path}")

    def print_sheet(self):
        printer = QPrinter(QPrinter.HighResolution)
        printer.setOrientation(QPrinter.Landscape)
        if QPrintDialog(printer, self).exec_() == QDialog.Accepted:
            self._printer_doc(printer).print_(printer)

    # --- nápověda, aktualizace ------------------------------------------------------
    def check_updates(self):
        if not UPDATES_ENABLED:
            QMessageBox.information(self, "Aktualizace",
                                    f"Kontrola aktualizací zatím není zapnutá.\nAktuální verze: {APP_VERSION}")
            return
        info = check_for_updates()
        if info is None:
            QMessageBox.information(self, "Aktualizace", f"Používáte nejnovější verzi ({APP_VERSION}).")
        elif QMessageBox.question(self, "Aktualizace", f"Je dostupná verze {info['version']}. Otevřít stránku ke stažení?",
                                  QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes and info["url"]:
            QDesktopServices.openUrl(QUrl(info["url"]))

    def about(self):
        QMessageBox.about(self, "O aplikaci Počítář",
                          f"<b>Počítář {APP_VERSION}</b><br>Tabulkový procesor ze sady VLČR Kancelář.<br><br>"
                          "Vzorce, více listů, formátování, grafy, import a export CSV a vlastní formát .vlcr.")

    def closeEvent(self, event):
        if self._confirm_discard():
            event.accept()
        else:
            event.ignore()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv:
        print(APP_VERSION)
        return 0
    sys.setrecursionlimit(6000)         # hluboké řetězce závislých buněk
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("VLCR")
    app.setApplicationVersion(APP_VERSION)
    app.setFont(QFont(Theme.FONT, 9))
    app.setWindowIcon(app_icon())
    window = PocitarWindow(argv[0] if argv else None)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())