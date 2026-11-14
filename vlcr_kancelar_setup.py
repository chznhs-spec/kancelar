#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Instalátor sady "VLČR Kancelář".

Je to jediný soubor, který si uživatel stáhne. V sobě nese (payload.zip) celou sadu:
rozcestník, Písař, Počítář, Prezentář a Průzkumník. Instalátor je nakopíruje do počítače,
vytvoří zástupce a (ve Windows) záznam pro Odinstalaci programů.

payload.zip vytváří GitHub Actions (viz .github/workflows/release.yml).
Pro vývoj bez payload.zip stačí mít vedle skriptu složku dist/vlcr-kancelar.
"""
import base64
import math
import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import zipfile
from tkinter import filedialog, messagebox, ttk


def resource_path(relative_path):
    """Cesta k přibalenému souboru - funguje ve skriptu i v PyInstaller exe."""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


APP_NAME = "VLČR Kancelář"
APP_VERSION = "1.0.0"
PUBLISHER = "Vzdělaný lid ČR"
APP_KEY = "VLCRKancelar"            # název klíče v registru (Odinstalace programů)
MAIN_EXE = "vlcr-kancelar.exe" if sys.platform == "win32" else "vlcr-kancelar"

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

LOGO_PATH = resource_path(os.path.join("images", "VLCR_logo.png"))
BG_PATH = resource_path(os.path.join("images", "win72.png"))
PAYLOAD_ZIP = resource_path("payload.zip")

# Programy sady: (název zástupce, spustitelný soubor)
PROGRAMS = [
    (APP_NAME, "vlcr-kancelar.exe"),
    ("Písař", "pisar.exe"),
    ("Počítář", "pocitar.exe"),
    ("Prezentář", "prezentar.exe"),
    ("Průzkumník souborů", "pruzkumnik.exe"),
]

WIDTH, HEIGHT = 503, 392
BODY_BG = "#f0f0f0"
WHITE = "#ffffff"
LINE = "#a0a0a0"
ACCENT = "#1e3a8a"
FONT = ("Segoe UI", 9)
FONT_B = ("Segoe UI", 9, "bold")
FONT_TITLE = ("Segoe UI", 12, "bold")

LICENSE_TEXT = (
    "LICENČNÍ SMLOUVA K PROGRAMU %s\n\n"
    "1. Úvodní ustanovení\n"
    "Tato licenční smlouva upravuje podmínky používání kancelářského balíku %s "
    "(zahrnujícího moduly Hlavní rozcestník, Průzkumník, Písař, Počítář a Prezentář) "
    "poskytovaného společností %s.\n\n"
    "2. Rozsah licence\n"
    "Uživateli se uděluje nevýhradní, nepřenositelná licence k používání Programu na jednom "
    "počítači pro osobní i pracovní účely.\n\n"
    "3. Omezení\n"
    "Uživatel nesmí Program kopírovat, rozšiřovat ani upravovat jeho zdrojový kód, "
    "ledaže to výslovně dovoluje právní předpis.\n\n"
    "4. Vyloučení záruky\n"
    "Program je poskytován „tak, jak je“, bez jakékoli záruky.\n\n"
    "5. Závěrečná ustanovení\n"
    "Instalací Programu uživatel vyjadřuje souhlas s touto smlouvou.\n"
) % (APP_NAME, APP_NAME, PUBLISHER)


def default_dir():
    base = os.environ.get("ProgramFiles") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME)


# ---------------------------------------------------------------------------
# Instalační data (payload)
# ---------------------------------------------------------------------------
class Payload:
    """Obsah, který se instaluje: payload.zip (hotový instalátor) nebo složka dist/vlcr-kancelar (vývoj)."""

    def __init__(self, zip_path=PAYLOAD_ZIP, dir_candidates=None):
        self.zip_path = zip_path if zip_path and os.path.isfile(zip_path) else None
        self.dir_path = None
        if not self.zip_path:
            for cand in dir_candidates or (os.path.join(BASE_DIR, "dist", "vlcr-kancelar"),
                                           os.path.join(BASE_DIR, "vlcr-kancelar")):
                if os.path.isdir(cand):
                    self.dir_path = cand
                    break

    @property
    def available(self):
        return bool(self.zip_path or self.dir_path)

    def items(self):
        """Seznam (relativní cesta, velikost) všech souborů."""
        if self.zip_path:
            with zipfile.ZipFile(self.zip_path) as z:
                return [(i.filename, i.file_size) for i in z.infolist() if not i.is_dir()]
        out = []
        for root, _, files in os.walk(self.dir_path):
            for f in files:
                p = os.path.join(root, f)
                out.append((os.path.relpath(p, self.dir_path).replace(os.sep, "/"), os.path.getsize(p)))
        return out

    def total_size(self):
        return sum(size for _, size in self.items())

    @staticmethod
    def _safe_target(dest, rel):
        target = os.path.normpath(os.path.join(dest, *rel.split("/")))
        root = os.path.normpath(dest)
        if target != root and not target.startswith(root + os.sep):
            raise ValueError("Neplatná cesta v instalačních datech: %s" % rel)
        return target

    def extract(self, dest, progress):
        """Rozbalí všechny soubory do dest. progress(hotovo_bajtů, celkem_bajtů, název)."""
        if not self.available:
            raise RuntimeError("Instalační data (payload.zip) nebyla nalezena.")
        items = self.items()
        total = max(1, sum(size for _, size in items))
        done = 0
        zf = zipfile.ZipFile(self.zip_path) if self.zip_path else None
        try:
            for rel, size in items:
                target = self._safe_target(dest, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                if zf:
                    with zf.open(rel) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out, 1024 * 1024)
                    mode = (zf.getinfo(rel).external_attr >> 16) & 0o777
                    if mode and os.name != "nt":
                        os.chmod(target, mode)
                else:
                    shutil.copy2(os.path.join(self.dir_path, *rel.split("/")), target)
                done += size
                progress(done, total, rel)
        finally:
            if zf:
                zf.close()


# ---------------------------------------------------------------------------
# Windows: zástupci, odinstalace, registr
# ---------------------------------------------------------------------------
CREATE_NO_WINDOW = 0x08000000


def _q(text):
    """Text do jednoduchých uvozovek PowerShellu."""
    return text.replace("'", "''")


def run_powershell(script):
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        check=True, creationflags=CREATE_NO_WINDOW, timeout=120)


def build_shortcut_script(dest, desktop):
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "$ws = New-Object -ComObject WScript.Shell",
        "$menu = Join-Path ([Environment]::GetFolderPath('CommonPrograms')) '%s'" % _q(APP_NAME),
        "New-Item -ItemType Directory -Force -Path $menu | Out-Null",
    ]
    for label, exe in PROGRAMS:
        target = os.path.join(dest, exe)
        lines.append(
            "$s = $ws.CreateShortcut((Join-Path $menu '%s.lnk')); $s.TargetPath = '%s'; "
            "$s.WorkingDirectory = '%s'; $s.IconLocation = '%s'; $s.Save()"
            % (_q(label), _q(target), _q(dest), _q(target)))
    if desktop:
        target = os.path.join(dest, MAIN_EXE)
        lines.append(
            "$s = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath('CommonDesktopDirectory')) '%s.lnk')); "
            "$s.TargetPath = '%s'; $s.WorkingDirectory = '%s'; $s.IconLocation = '%s'; $s.Save()"
            % (_q(APP_NAME), _q(target), _q(dest), _q(target)))
    return "\n".join(lines)


UNINSTALL_PS1 = r"""$ErrorActionPreference = 'SilentlyContinue'
$dir = Split-Path -Parent $PSCommandPath
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $PSCommandPath + '"'))
    exit
}
Add-Type -AssemblyName System.Windows.Forms
$answer = [System.Windows.Forms.MessageBox]::Show('Opravdu chcete odinstalovat @@NAME@@?', 'Odinstalace', [System.Windows.Forms.MessageBoxButtons]::YesNo, [System.Windows.Forms.MessageBoxIcon]::Question)
if ($answer -ne [System.Windows.Forms.DialogResult]::Yes) { exit }
Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith($dir, [System.StringComparison]::OrdinalIgnoreCase) } | Stop-Process -Force
Start-Sleep -Milliseconds 700
Remove-Item -Recurse -Force (Join-Path ([Environment]::GetFolderPath('CommonPrograms')) '@@NAME@@')
Remove-Item -Force (Join-Path ([Environment]::GetFolderPath('CommonDesktopDirectory')) '@@NAME@@.lnk')
Remove-Item -Recurse -Force 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\@@KEY@@'
[System.Windows.Forms.MessageBox]::Show('@@NAME@@ byl odinstalován.', 'Odinstalace') | Out-Null
Start-Process -FilePath 'cmd.exe' -WindowStyle Hidden -ArgumentList '/c', ('timeout /t 2 /nobreak >nul & rmdir /s /q "' + $dir + '"')
"""


def uninstall_script_text():
    return UNINSTALL_PS1.replace("@@NAME@@", _q(APP_NAME)).replace("@@KEY@@", APP_KEY)


def register_uninstall(dest, size_bytes):
    import winreg
    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\%s" % APP_KEY
    uninstall_cmd = ('powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%s"'
                     % os.path.join(dest, "uninstall.ps1"))
    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, key_path, 0,
                            winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY) as key:
        for name, value in (("DisplayName", APP_NAME), ("DisplayVersion", APP_VERSION),
                            ("Publisher", PUBLISHER), ("InstallLocation", dest),
                            ("DisplayIcon", os.path.join(dest, MAIN_EXE)),
                            ("UninstallString", uninstall_cmd)):
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, int(size_bytes // 1024))
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)


# ---------------------------------------------------------------------------
# Okno instalátoru
# ---------------------------------------------------------------------------
class Setup(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Instalace – %s" % APP_NAME)
        self.resizable(False, False)
        x = (self.winfo_screenwidth() - WIDTH) // 2
        y = (self.winfo_screenheight() - HEIGHT) // 3
        self.geometry("%dx%d+%d+%d" % (WIDTH, HEIGHT, x, y))
        self.configure(bg=BODY_BG)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.option_add("*Font", FONT)

        st = ttk.Style(self)
        for theme in ("vista", "clam"):
            if theme in st.theme_names():
                st.theme_use(theme)
                break
        st.configure(".", font=FONT)

        self.page = None
        self.after_id = None
        self.payload = Payload()
        self.dest = tk.StringVar(value=default_dir())
        self.accept = tk.IntVar(value=0)
        self.launch = tk.IntVar(value=1)
        self.desktop_icon = tk.IntVar(value=1)
        self.state = {"percent": 0, "file": "", "done": False, "error": None, "warnings": []}

        self._refs = []
        self._load_local_images()

        self.header = tk.Frame(self, bg=WHITE, height=58)
        self.h_title = tk.Label(self.header, bg=WHITE, font=FONT_B, anchor="w")
        self.h_title.place(x=14, y=10)
        self.h_sub = tk.Label(self.header, bg=WHITE, font=FONT, anchor="w")
        self.h_sub.place(x=28, y=29)
        self.h_logo = tk.Label(self.header, bg=WHITE)
        self.h_logo.place(relx=1.0, x=-12, y=6, anchor="ne")
        self.h_line = tk.Frame(self, bg=LINE, height=1)
        self.content = tk.Frame(self, bg=BODY_BG)
        self.sep = tk.Frame(self, bg=LINE, height=1)
        self.bar = tk.Frame(self, bg=BODY_BG, height=48)
        self.bar.pack_propagate(False)
        self.bar.grid_columnconfigure(0, weight=1)

        self.btn_back = ttk.Button(self.bar, text="< Zpět", width=11, command=self.back)
        self.btn_next = ttk.Button(self.bar, text="Další >", width=11, command=self.next)
        self.btn_cancel = ttk.Button(self.bar, text="Zrušit", width=11, command=self.cancel)
        self.btn_back.grid(row=0, column=1, pady=11)
        self.btn_next.grid(row=0, column=2, padx=(0, 10), pady=11)
        self.btn_cancel.grid(row=0, column=3, padx=(0, 12), pady=11)

        self.goto("welcome")

    # --- obrázky -------------------------------------------------------------
    def _load_local_images(self):
        self._logo_full = None
        if os.path.exists(LOGO_PATH):
            try:
                self._logo_full = tk.PhotoImage(file=LOGO_PATH)
            except Exception:
                pass

        self._bg_full = None
        if os.path.exists(BG_PATH):
            try:
                from PIL import Image, ImageTk
                self._bg_full = ImageTk.PhotoImage(Image.open(BG_PATH))
            except ImportError:
                try:
                    self._bg_full = tk.PhotoImage(file=BG_PATH)
                except Exception:
                    pass
            except Exception:
                pass

    def scaled_logo(self, mw, mh):
        if self._logo_full is None:
            return None
        img = self._logo_full
        f = max(math.ceil(img.width() / mw), math.ceil(img.height() / mh), 1)
        small = img.subsample(f, f) if f > 1 else img
        self._refs.append(small)
        return small

    def logo_into(self, label, mw, mh):
        small = self.scaled_logo(mw, mh)
        if small is None:
            label.configure(text="VLČR", fg=ACCENT, font=("Segoe UI", 11, "bold"), image="")
        else:
            label.configure(image=small, text="")

    # --- rozvržení a navigace ----------------------------------------------
    def layout(self, with_header, body_bg):
        for w in (self.header, self.h_line, self.content, self.sep, self.bar):
            w.pack_forget()
        if with_header:
            self.header.pack(fill="x")
            self.h_line.pack(fill="x")
        self.content.configure(bg=body_bg)
        for w in self.content.winfo_children():
            w.destroy()
        self.content.pack(fill="both", expand=True)
        self.sep.pack(fill="x")
        self.bar.pack(fill="x")
        if with_header:
            self.logo_into(self.h_logo, 150, 44)

    def set_header(self, title, sub):
        self.h_title.configure(text=title)
        self.h_sub.configure(text=sub)

    def buttons(self, back=True, nxt=True, cancel=True, next_text="Další >", next_on=True):
        for b, show in ((self.btn_back, back), (self.btn_next, nxt), (self.btn_cancel, cancel)):
            b.grid() if show else b.grid_remove()
        self.btn_next.configure(text=next_text, state="normal" if next_on else "disabled")

    def goto(self, name):
        if self.after_id is not None:
            self.after_cancel(self.after_id)
            self.after_id = None
        self.page = name
        getattr(self, "page_" + name)()

    ORDER = ["welcome", "license", "folder", "ready", "installing", "finish"]

    def next(self):
        if self.page == "finish":
            self.finish()
            return
        if self.page == "folder" and not self.validate_folder():
            return
        self.goto(self.ORDER[self.ORDER.index(self.page) + 1])

    def back(self):
        self.goto(self.ORDER[self.ORDER.index(self.page) - 1])

    def cancel(self):
        if self.page == "finish":
            self.finish()
            return
        if self.page == "installing":
            messagebox.showinfo("Instalace probíhá", "Počkejte prosím na dokončení instalace.")
            return
        if messagebox.askyesno("Ukončit instalaci", "Opravdu chcete ukončit instalaci sady %s?" % APP_NAME,
                               icon="question"):
            self.destroy()

    def finish(self):
        if self.page == "finish" and self.state["warnings"]:
            messagebox.showwarning("Upozornění", "Instalace proběhla, ale některé kroky se nepodařily:\n\n"
                                   + "\n".join(self.state["warnings"]))
        if self.launch.get() and self.page == "finish":
            main_path = os.path.join(self.dest.get(), MAIN_EXE)
            if os.path.exists(main_path):
                try:
                    if sys.platform == "win32":
                        # přes Průzkumníka, aby se program nespustil s právy správce
                        subprocess.Popen(["explorer.exe", main_path])
                    elif sys.platform == "darwin":
                        subprocess.Popen(["open", main_path])
                    else:
                        subprocess.Popen([main_path])
                except Exception as e:
                    print("Nelze spustit aplikaci: %s" % e)
        self.destroy()

    # --- stránky ---------------------------------------------------------------
    def side_page(self, title, text):
        self.layout(False, WHITE)
        c = self.content

        side = tk.Canvas(c, width=164, bg="#e8eef7", highlightthickness=0, bd=0)
        side.pack(side="left", fill="y")
        if self._bg_full:
            side.create_image(0, 0, image=self._bg_full, anchor="nw")
        logo = self.scaled_logo(70, 70)
        if logo:
            side.create_image(82, 140, image=logo, anchor="center")
        else:
            side.create_text(82, 140, text="VLČR", fill=ACCENT, font=("Segoe UI", 14, "bold"))
        tk.Frame(side, bg=ACCENT, height=6).place(x=0, rely=1.0, anchor="sw", relwidth=1)

        right = tk.Frame(c, bg=WHITE)
        right.pack(side="left", fill="both", expand=True, padx=(18, 14), pady=(18, 0))
        tk.Label(right, text=title, bg=WHITE, font=FONT_TITLE, justify="left", wraplength=290,
                 anchor="w").pack(anchor="w", pady=(0, 18))
        tk.Label(right, text=text, bg=WHITE, justify="left", wraplength=300, anchor="w").pack(anchor="w")
        return right

    def page_welcome(self):
        self.side_page(
            "Vítá vás Průvodce instalací sady %s" % APP_NAME,
            "Tento průvodce nainstaluje kancelářský balík %s verze %s do vašeho počítače.\n\n"
            "Před zahájením instalace doporučujeme zavřít všechny ostatní aplikace.\n\n"
            "Pro pokračování klepněte na tlačítko Další." % (APP_NAME, APP_VERSION))
        self.buttons(back=False)

    def page_license(self):
        self.layout(True, BODY_BG)
        self.set_header("Licenční smlouva", "Přečtěte si prosím následující důležité informace, než budete pokračovat.")
        c = self.content
        tk.Label(c, bg=BODY_BG, justify="left", wraplength=470, anchor="w",
                 text="Přečtěte si prosím následující licenční smlouvu. Musíte souhlasit s podmínkami "
                      "této smlouvy, než budete moci pokračovat v instalaci.").pack(anchor="w", padx=14, pady=(10, 6))
        box = tk.Frame(c, bg=LINE, bd=0)
        box.pack(fill="both", expand=True, padx=14)
        inner = tk.Frame(box, bg=WHITE)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        sb = ttk.Scrollbar(inner, orient="vertical")
        txt = tk.Text(inner, wrap="word", bg=WHITE, relief="flat", bd=0, padx=6, pady=4, height=8, font=FONT,
                      yscrollcommand=sb.set)
        sb.configure(command=txt.yview)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.insert("1.0", LICENSE_TEXT)
        txt.configure(state="disabled")
        self.accept.set(0)
        for val, label in ((1, "Souhlasím s podmínkami smlouvy"), (0, "Nesouhlasím s podmínkami smlouvy")):
            tk.Radiobutton(c, text=label, variable=self.accept, value=val, bg=BODY_BG,
                           activebackground=BODY_BG, command=self.license_changed,
                           anchor="w").pack(anchor="w", padx=14, pady=(4 if val else 0, 0))
        tk.Frame(c, bg=BODY_BG, height=6).pack()
        self.buttons(next_on=False)

    def license_changed(self):
        self.btn_next.configure(state="normal" if self.accept.get() else "disabled")

    def required_mb(self):
        try:
            return max(1, int(self.payload.total_size() * 1.1 / (1024 * 1024)))
        except Exception:
            return 0

    def page_folder(self):
        self.layout(True, BODY_BG)
        self.set_header("Vyberte cílové umístění", "Kam má být sada %s nainstalována?" % APP_NAME)
        c = self.content
        tk.Label(c, bg=BODY_BG, justify="left", wraplength=470, anchor="w",
                 text="Průvodce nainstaluje balík %s do následující složky." % APP_NAME).pack(anchor="w", padx=14,
                                                                                            pady=(14, 8))
        row = tk.Frame(c, bg=BODY_BG)
        row.pack(fill="x", padx=14, pady=14)
        ttk.Entry(row, textvariable=self.dest).pack(side="left", fill="x", expand=True, ipady=2)
        ttk.Button(row, text="Procházet…", command=self.browse).pack(side="left", padx=(8, 0))
        if sys.platform == "win32":
            tk.Checkbutton(c, text="Vytvořit zástupce na ploše", variable=self.desktop_icon, bg=BODY_BG,
                           activebackground=BODY_BG, anchor="w").pack(anchor="w", padx=14)
        need = self.required_mb()
        tk.Label(c, bg=BODY_BG, anchor="w",
                 text="Potřebujete alespoň %s MB volného místa na disku." % (need or "?")).pack(
            side="bottom", anchor="w", padx=14, pady=14)
        self.buttons()

    def browse(self):
        d = filedialog.askdirectory(title="Vyberte složku", initialdir=self.dest.get() or None)
        if d:
            self.dest.set(os.path.normpath(os.path.join(d, APP_NAME)) if os.path.basename(d) != APP_NAME
                          else os.path.normpath(d))

    def validate_folder(self):
        dest = self.dest.get().strip()
        if not dest or not os.path.isabs(dest):
            messagebox.showerror("Složka", "Zadejte úplnou cestu ke složce, např. C:\\Program Files\\%s." % APP_NAME)
            return False
        dest = os.path.normpath(dest)
        try:
            os.makedirs(dest, exist_ok=True)
            probe = os.path.join(dest, ".vlcr_write_test")
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
        except OSError as e:
            messagebox.showerror("Složku nelze použít",
                                 "Do vybrané složky nelze zapisovat. Vyberte jinou složku nebo spusťte "
                                 "instalátor jako správce.\n\n%s" % e)
            return False
        need = self.required_mb()
        try:
            free_mb = shutil.disk_usage(dest).free // (1024 * 1024)
        except OSError:
            free_mb = None
        if need and free_mb is not None and free_mb < need:
            messagebox.showerror("Nedostatek místa",
                                 "Na disku je volných %d MB, potřebujete alespoň %d MB." % (free_mb, need))
            return False
        self.dest.set(dest)
        return True

    def page_ready(self):
        self.layout(True, BODY_BG)
        self.set_header("Připraven k instalaci", "Průvodce je připraven zahájit instalaci sady %s." % APP_NAME)
        c = self.content
        tk.Label(c, bg=BODY_BG, justify="left", wraplength=470, anchor="w",
                 text="Klepnutím na tlačítko Instalovat pokračujte v kopírování souborů.").pack(
            anchor="w", padx=14, pady=(14, 8))
        box = tk.Frame(c, bg=LINE)
        box.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        inner = tk.Frame(box, bg=WHITE)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Label(inner, bg=WHITE, anchor="nw", justify="left", wraplength=440,
                 text="Cílové umístění:\n    %s\n\nSoučásti sady:\n    • Hlavní rozcestník (vlcr-kancelar)\n"
                      "    • Písař\n    • Počítář\n    • Prezentář\n    • Průzkumník souborů" % self.dest.get()
                 ).pack(anchor="nw", padx=10, pady=8)
        self.buttons(next_text="Instalovat")

    def page_installing(self):
        self.layout(True, BODY_BG)
        self.set_header("Probíhá instalace", "Počkejte prosím, než průvodce nainstaluje moduly sady %s." % APP_NAME)
        c = self.content
        self.status = tk.Label(c, bg=BODY_BG, anchor="w", text="Kopírování souborů…")
        self.status.pack(anchor="w", padx=14, pady=(30, 4))
        self.pbar = ttk.Progressbar(c, maximum=100, length=300, mode="determinate")
        self.pbar.pack(fill="x", padx=14, pady=(0, 6))
        self.fname = tk.Label(c, bg=BODY_BG, anchor="w", text="")
        self.fname.pack(anchor="w", padx=14)
        self.buttons(back=False, cancel=False, next_on=False)

        self.state = {"percent": 0, "file": "", "done": False, "error": None, "warnings": []}
        threading.Thread(target=self._install_worker, daemon=True).start()
        self.after_id = self.after(100, self._poll)

    def _poll(self):
        st = self.state
        if st["error"]:
            messagebox.showerror("Chyba instalace", "Při instalaci došlo k chybě:\n%s" % st["error"])
            self.destroy()
            return
        self.pbar["value"] = st["percent"]
        self.fname.configure(text=st["file"])
        if st["done"]:
            self.status.configure(text="Dokončování instalace…")
            self.after_id = self.after(300, lambda: self.goto("finish"))
            return
        self.after_id = self.after(100, self._poll)

    def _install_worker(self):
        """Běží ve vlákně; stav předává přes self.state (čte ho _poll v hlavním vlákně)."""
        try:
            dest = self.dest.get()
            os.makedirs(dest, exist_ok=True)
            self._clean_previous(dest)

            def progress(done, total, name):
                self.state["percent"] = done * 100.0 / total
                self.state["file"] = name

            self.payload.extract(dest, progress)
            if sys.platform == "win32":
                self._post_install_windows(dest)
            self.state["percent"] = 100
            self.state["done"] = True
        except Exception as e:
            self.state["error"] = str(e)

    @staticmethod
    def _clean_previous(dest):
        """Při aktualizaci odstraní jen vlastní součásti staré verze (ne celou složku)."""
        shutil.rmtree(os.path.join(dest, "_internal"), ignore_errors=True)
        for name in ["uninstall.ps1"] + [exe for _, exe in PROGRAMS]:
            try:
                os.remove(os.path.join(dest, name))
            except OSError:
                pass

    def _post_install_windows(self, dest):
        warnings = self.state["warnings"]
        try:
            with open(os.path.join(dest, "uninstall.ps1"), "w", encoding="utf-8-sig") as fh:
                fh.write(uninstall_script_text())
        except OSError as e:
            warnings.append("Odinstalační skript: %s" % e)
        try:
            run_powershell(build_shortcut_script(dest, bool(self.desktop_icon.get())))
        except Exception as e:
            warnings.append("Zástupci v nabídce Start: %s" % e)
        try:
            register_uninstall(dest, self.payload.total_size())
        except Exception as e:
            warnings.append("Záznam v Odinstalaci programů: %s" % e)

    def page_finish(self):
        right = self.side_page(
            "Dokončení Průvodce instalací sady %s" % APP_NAME,
            "Sada %s byla úspěšně nainstalována do vašeho počítače.\n\n"
            "Klepnutím na tlačítko Dokončit ukončíte instalaci." % APP_NAME)
        tk.Checkbutton(right, text="Spustit hlavní aplikaci (vlcr-kancelar)", variable=self.launch,
                       bg=WHITE, activebackground=WHITE, anchor="w").pack(anchor="w", pady=(24, 0))
        self.buttons(back=False, cancel=False, next_text="Dokončit")


if __name__ == "__main__":
    Setup().mainloop()
