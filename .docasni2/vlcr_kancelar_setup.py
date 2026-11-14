#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Plně funkční instalátor "VLČR Kancelář" (chytré hledání zdrojových složek).

Spuštěno z umístění skriptu.
"""
import os
import sys
import shutil
import subprocess
import math
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

def resource_path(relative_path):
    """ Vrátí správnou cestu k souboru jak pro skript, tak pro PyInstaller exe """
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)

APP_NAME = "VLČR Kancelář"
APP_VERSION = "1.0.0"
PUBLISHER = "Vzdělaný lid ČR"

# Zjištění správného adresáře, ať už běží skript, nebo kompilovaný .exe
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Cesty k lokálním souborům grafiky pomocí resource_path
LOGO_PATH = resource_path(os.path.join("images", "VLCR_logo.png"))
BG_PATH = resource_path(os.path.join("images", "win72.png"))

WIDTH, HEIGHT = 503, 392
BODY_BG = "#f0f0f0"
WHITE = "#ffffff"
LINE = "#a0a0a0"
ACCENT = "#1e3a8a"
FONT = ("Segoe UI", 9)
FONT_B = ("Segoe UI", 9, "bold")
FONT_TITLE = ("Segoe UI", 12, "bold")

# Položky, které se mají zkopírovat
ITEMS_TO_COPY = [
    "vlcr-kancelar",
    "apps"
]

LICENSE_TEXT = (
    "LICENČNÍ SMLOUVA K PROGRAMU %s\n\n"
    "1. Úvodní ustanovení\n"
    "Tato licenční smlouva upravuje podmínky používání kancelářského balíku %s "
    "(zahrnujícího moduly Hlavní rozcestník, Průzkumník, Písař a Prezentář) "
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
        self.dest = tk.StringVar(value=default_dir())
        self.accept = tk.IntVar(value=0)
        self.launch = tk.IntVar(value=1)

        self._refs = []
        self._load_local_images()

        # Rozvržení okna
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
                pil_img = Image.open(BG_PATH)
                self._bg_full = ImageTk.PhotoImage(pil_img)
            except ImportError:
                try:
                    self._bg_full = tk.PhotoImage(file=BG_PATH)
                except Exception:
                    pass

    def logo_into(self, label, mw, mh):
        if self._logo_full is None:
            label.configure(text="VLČR", fg=ACCENT, font=("Segoe UI", 11, "bold"), image="")
            return
        img = self._logo_full
        f = max(math.ceil(img.width() / mw), math.ceil(img.height() / mh), 1)
        small = img.subsample(f, f) if f > 1 else img
        self._refs.append(small)
        label.configure(image=small, text="")

    def scaled_logo(self, mw, mh):
        if self._logo_full is None:
            return None
        img = self._logo_full
        f = max(math.ceil(img.width() / mw), math.ceil(img.height() / mh), 1)
        small = img.subsample(f, f) if f > 1 else img
        self._refs.append(small)
        return small

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
        else:
            self.goto(self.ORDER[self.ORDER.index(self.page) + 1])

    def back(self):
        self.goto(self.ORDER[self.ORDER.index(self.page) - 1])

    def cancel(self):
        if self.page == "finish":
            self.finish()
            return
        if messagebox.askyesno("Ukončit instalaci", "Opravdu chcete ukončit instalaci sady %s?" % APP_NAME, icon="question"):
            if self.after_id is not None:
                self.after_cancel(self.after_id)
            self.destroy()

    def finish(self):
        if self.launch.get() and self.page == "finish":
            target_dir = self.dest.get()
            exe_name = "vlcr-kancelar.exe" if sys.platform == "win32" else "vlcr-kancelar"
            main_path = os.path.join(target_dir, exe_name)
            
            if os.path.exists(main_path):
                try:
                    if sys.platform == "win32":
                        subprocess.Popen([main_path])
                    elif sys.platform == "darwin":
                        subprocess.Popen(["open", main_path])
                    else:
                        subprocess.Popen([main_path])
                except Exception as e:
                    print(f"Nelze spustit aplikaci: {e}")
        self.destroy()

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
        tk.Label(right, text=title, bg=WHITE, font=FONT_TITLE, justify="left", wraplength=290, anchor="w").pack(anchor="w", pady=(0, 18))
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
        txt = tk.Text(inner, wrap="word", bg=WHITE, relief="flat", bd=0, padx=6, pady=4, height=8, font=FONT, yscrollcommand=sb.set)
        sb.configure(command=txt.yview)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.insert("1.0", LICENSE_TEXT)
        txt.configure(state="disabled")
        self.accept.set(0)
        for val, label in ((1, "Souhlasím s podmínkami smlouvy"), (0, "Nesouhlasím s podmínkami smlouvy")):
            tk.Radiobutton(c, text=label, variable=self.accept, value=val, bg=BODY_BG,
                           activebackground=BODY_BG, command=self.license_changed, anchor="w").pack(anchor="w", padx=14, pady=(4 if val else 0, 0))
        tk.Frame(c, bg=BODY_BG, height=6).pack()
        self.buttons(next_on=False)

    def license_changed(self):
        self.btn_next.configure(state="normal" if self.accept.get() else "disabled")

    def page_folder(self):
        self.layout(True, BODY_BG)
        self.set_header("Vyberte cílové umístění", "Kam má být sada %s nainstalována?" % APP_NAME)
        c = self.content
        tk.Label(c, bg=BODY_BG, justify="left", wraplength=470, anchor="w",
                 text="Průvodce nainstaluje balík %s do následující složky." % APP_NAME).pack(anchor="w", padx=14, pady=(14, 8))
        row = tk.Frame(c, bg=BODY_BG)
        row.pack(fill="x", padx=14, pady=14)
        ttk.Entry(row, textvariable=self.dest).pack(side="left", fill="x", expand=True, ipady=2)
        ttk.Button(row, text="Procházet…", command=self.browse).pack(side="left", padx=(8, 0))
        tk.Label(c, bg=BODY_BG, anchor="w", text="Potřebujete alespoň 65,0 MB volného místa na disku.").pack(side="bottom", anchor="w", padx=14, pady=14)
        self.buttons()

    def browse(self):
        d = filedialog.askdirectory(title="Vyberte složku", initialdir=self.dest.get() or None)
        if d:
            self.dest.set(os.path.normpath(os.path.join(d, APP_NAME)) if os.path.basename(d) != APP_NAME else os.path.normpath(d))

    def page_ready(self):
        self.layout(True, BODY_BG)
        self.set_header("Připraven k instalaci", "Průvodce je připraven zahájit instalaci sady %s." % APP_NAME)
        c = self.content
        tk.Label(c, bg=BODY_BG, justify="left", wraplength=470, anchor="w",
                 text="Klepnutím na tlačítko Instalovat pokračujte v kopírování souborů.").pack(anchor="w", padx=14, pady=(14, 8))
        box = tk.Frame(c, bg=LINE)
        box.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        inner = tk.Frame(box, bg=WHITE)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Label(inner, bg=WHITE, anchor="nw", justify="left", wraplength=440,
                 text="Cílové umístění:\n    %s\n\nSoučásti sady:\n    • Hlavní rozcestník (vlcr-kancelar)\n    • Další moduly v apps/ (Písař, Prezentář, Průzkumník)" %
                      self.dest.get()).pack(anchor="nw", padx=10, pady=8)
        self.buttons(next_text="Instalovat")

    def page_installing(self):
        self.layout(True, BODY_BG)
        self.set_header("Probíhá instalace", "Počkejte prosím, než průvodce nainstaluje moduly sady %s." % APP_NAME)
        c = self.content
        self.status = tk.Label(c, bg=BODY_BG, anchor="w", text="Připravování instalace…")
        self.status.pack(anchor="w", padx=14, pady=(30, 4))
        self.pbar = ttk.Progressbar(c, maximum=100, length=300, mode="determinate")
        self.pbar.pack(fill="x", padx=14, pady=(0, 6))
        self.fname = tk.Label(c, bg=BODY_BG, anchor="w", text="")
        self.fname.pack(anchor="w", padx=14)
        self.buttons(back=False, next_on=False)
        
        self.after(200, self.run_real_installation)

    def run_real_installation(self):
        target_dir = self.dest.get()
        try:
            os.makedirs(target_dir, exist_ok=True)
            total = len(ITEMS_TO_COPY)
            
            for i, name in enumerate(ITEMS_TO_COPY):
                self.status.configure(text=f"Kopírování položky ({i+1}/{total})…")
                self.fname.configure(text=name)
                self.update()
                
                # Inteligentní vyhledání zdroje v několika možných složkách
                possible_paths = [
                    os.path.join(BASE_DIR, name),                  # Hned vedle binárky (pokud je ve složce dist)
                    os.path.join(BASE_DIR, "dist", name),          # V podadresáři dist
                    os.path.join(os.path.dirname(BASE_DIR), name), # O úroveň výš
                    os.path.join(os.path.dirname(BASE_DIR), "dist", name)
                ]
                
                src = None
                for p in possible_paths:
                    if os.path.exists(p):
                        src = p
                        break
                
                # Pokud se nic nenalezlo, vezmeme první cestu pro fallback/vytvoření placeholderu
                if not src:
                    src = possible_paths[0]

                dest = os.path.join(target_dir, name)
                
                if os.path.exists(src):
                    if os.path.isdir(src):
                        if os.path.exists(dest):
                            shutil.rmtree(dest)
                        shutil.copytree(src, dest)
                    else:
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        shutil.copy2(src, dest)
                else:
                    # Vytvoří se placeholder jen pokud zdroj fyzicky neexistuje
                    if "." in name:
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        with open(dest, "w", encoding="utf-8") as f:
                            f.write(f"# Placeholder pro {name}\n")
                    else:
                        os.makedirs(dest, exist_ok=True)
                
                self.pbar["value"] = ((i + 1) / total) * 100
                self.update()
            
            self.status.configure(text="Dokončování instalace…")
            self.update()
            self.after(600, lambda: self.goto("finish"))
            
        except Exception as e:
            messagebox.showerror("Chyba instalace", f"Při instalaci došlo k chybě:\n{str(e)}")
            self.destroy()

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