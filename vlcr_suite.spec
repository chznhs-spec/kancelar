# -*- mode: python ; coding: utf-8 -*-
"""Společné sestavení celé sady VLČR Kancelář (PyInstaller >= 6).

Výsledkem je JEDEN adresář dist/vlcr-kancelar/, ve kterém leží všechny programy
(vlcr-kancelar, pisar, pocitar, prezentar, pruzkumnik) a sdílejí jednu složku
_internal s Pythonem a Qt. Díky tomu je sada několikrát menší, než kdyby měl
každý program své vlastní kopie knihoven.

Spuštění:  pyinstaller --noconfirm --clean vlcr_suite.spec
"""
import os
import sys

# (skript, název programu)
PROGRAMS = [
    ("main.py", "vlcr-kancelar"),
    ("pisar.py", "pisar"),
    ("pocitar.py", "pocitar"),
    ("prezentar.py", "prezentar"),
    ("pruzkumnik.py", "pruzkumnik"),
]

# Data dostupná všem programům (v instalaci ve složce _internal).
# pruzkumnik.py se přibaluje i jako soubor, protože ho Písař/Prezentář/Počítář
# načítají za běhu (importlib) pro dialog "Otevřít".
datas = []
for folder in ("images", "loga"):
    if os.path.isdir(os.path.join(SPECPATH, folder)):
        datas.append((os.path.join(SPECPATH, folder), folder))
datas.append((os.path.join(SPECPATH, "pruzkumnik.py"), "."))

# Moduly, které se načítají dynamicky a statická analýza je nevidí.
hiddenimports = [
    "zipfile", "subprocess", "struct", "unicodedata", "zlib", "json", "csv",
    "importlib.util", "statistics", "decimal", "html", "tempfile", "base64",
    "PyQt5.QtPrintSupport", "PyQt5.sip",
]


def icon_for(name):
    path = os.path.join(SPECPATH, "build_icons", name + ".ico")
    return path if sys.platform == "win32" and os.path.isfile(path) else None


analyses, exes = [], []
for script, name in PROGRAMS:
    if not os.path.isfile(os.path.join(SPECPATH, script)):
        print("PŘESKOČENO (soubor neexistuje):", script)
        continue
    a = Analysis(
        [script],
        pathex=[SPECPATH],
        binaries=[],
        datas=datas,
        hiddenimports=hiddenimports,
        hookspath=[],
        runtime_hooks=[],
        excludes=["tkinter", "matplotlib", "numpy", "scipy", "pandas"],
        noarchive=False,
    )
    pyz = PYZ(a.pure)
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=name,
        console=False,
        icon=icon_for(name),
        upx=False,
    )
    analyses.append(a)
    exes.append(exe)

collect_args = []
for a, exe in zip(analyses, exes):
    collect_args += [exe, a.binaries, a.datas]

coll = COLLECT(
    *collect_args,
    strip=False,
    upx=False,
    name="vlcr-kancelar",
)


# macOS: zabalení do aplikace "VLCR Kancelar.app".
# Všechny programy sady leží uvnitř Contents/MacOS vedle sebe; hlavní je vlcr-kancelar.
if sys.platform == "darwin":
    icns = os.path.join(SPECPATH, "build_icons", "vlcr-kancelar.icns")
    app = BUNDLE(
        coll,
        name="VLCR Kancelar.app",
        icon=icns if os.path.isfile(icns) else None,
        bundle_identifier="cz.vlcr.kancelar",
        info_plist={
            "CFBundleName": "VLČR Kancelář",
            "CFBundleDisplayName": "VLČR Kancelář",
            "CFBundleShortVersionString": os.environ.get("VLCR_VERSION", "0.0.0"),
            "CFBundleVersion": os.environ.get("VLCR_VERSION", "0.0.0"),
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
