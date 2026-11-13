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
    num_re = _NUM_COMMA if ";" in src else _NUM_DOT
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
    return fn(ctx, [ev(a, ctx) for a in args])


FUNCS: Dict[str, Any] = {}
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
        elif a is not None or True:
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


@func("AND")
def f_and(ctx, a):
    vals = [to_bool(v) for v in flat_values(a) if not isinstance(v, str) or not isinstance(v, RangeValue)]
    return all(vals) if vals else True


@func("OR")
def f_or(ctx, a):
    return any(to_bool(v) for v in flat_values(a))


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