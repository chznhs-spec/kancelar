
# -*- mode: python ; coding: utf-8 -*-
"""Společné sestavení celé sady VLČR Kancelář (PyInstaller >= 6).

Výsledkem je jeden adresář dist/vlcr-kancelar/ se všemi programy,
které sdílejí společnou složku _internal s Pythonem a knihovnami.

Spuštění: pyinstaller --noconfirm --clean vlcr_suite.spec
"""

import os
import sys


# (vstupní skript, název výsledného programu)
PROGRAMS = [
    ("main.py", "vlcr-kancelar"),
    ("pisar.py", "pisar"),
    ("pocitar.py", "pocitar"),
    ("prezentar.py", "prezentar"),
    ("pruzkumnik.py", "pruzkumnik"),
    ("poscilogramar.py", "poscilogramar"),
]


# ===========================================================================
# DATA DOSTUPNÁ VŠEM PROGRAMŮM V ADRESÁŘI _internal
# ===========================================================================

datas = []

# Společné datové složky.
for folder in ("images", "loga"):
    path = os.path.join(SPECPATH, folder)
    if os.path.isdir(path):
        datas.append((path, folder))


# Průzkumník je za běhu načítán také dynamicky z ostatních aplikací.
# Proto musí být jeho zdrojový soubor přibalený mezi datovými soubory.
pruzkumnik_path = os.path.join(SPECPATH, "pruzkumnik.py")

if not os.path.isfile(pruzkumnik_path):
    raise FileNotFoundError(
        f"Chybí zdrojový soubor Průzkumníka: {pruzkumnik_path}"
    )

datas.append((pruzkumnik_path, "."))


# ===========================================================================
# DYNAMICKÉ IMPORTY A QT MODULY
# ===========================================================================

# PyInstaller obvykle lokální import vlcr_format zachytí automaticky.
# Následující importy jsou používány dynamicky nebo Qt pluginy.
hiddenimports = [
    "zipfile",
    "subprocess",
    "struct",
    "unicodedata",
    "zlib",
    "json",
    "csv",
    "importlib.util",
    "statistics",
    "decimal",
    "html",
    "tempfile",
    "base64",
    "PyQt5.QtPrintSupport",
    "PyQt5.sip",
]


# ===========================================================================
# IKONY
# ===========================================================================

def icon_for(name):
    path = os.path.join(SPECPATH, "build_icons", name + ".ico")
    return path if sys.platform == "win32" and os.path.isfile(path) else None


# ===========================================================================
# SESTAVENÍ JEDNOTLIVÝCH PROGRAMŮ
# ===========================================================================

analyses = []
exes = []

for script, name in PROGRAMS:
    script_path = os.path.join(SPECPATH, script)

    if not os.path.isfile(script_path):
        raise SystemExit(
            "CHYBÍ POVINNÝ SOUBOR APLIKACE: " + script
        )

    a = Analysis(
        [script],
        pathex=[SPECPATH],
        binaries=[],
        datas=datas,
        hiddenimports=hiddenimports,
        hookspath=[],
        runtime_hooks=[],
        excludes=[
            "tkinter",
            "matplotlib",
            "numpy",
            "scipy",
            "pandas",
        ],
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


# ===========================================================================
# SPOLEČNÝ VÝSLEDNÝ ADRESÁŘ
# ===========================================================================

collect_args = []

for analysis, exe in zip(analyses, exes):
    collect_args += [
        exe,
        analysis.binaries,
        analysis.datas,
    ]

coll = COLLECT(
    *collect_args,
    strip=False,
    upx=False,
    name="vlcr-kancelar",
)


# ===========================================================================
# macOS: APLIKACE VLČR KANCELÁŘ
# ===========================================================================

# Všechny spustitelné programy jsou v Contents/MacOS vedle sebe.
if sys.platform == "darwin":

    icns = os.path.join(
        SPECPATH,
        "build_icons",
        "vlcr-kancelar.icns",
    )

    app = BUNDLE(
        coll,
        name="VLCR Kancelar.app",
        icon=icns if os.path.isfile(icns) else None,
        bundle_identifier="cz.vlcr.kancelar",
        info_plist={
            "CFBundleName": "VLČR Kancelář",
            "CFBundleDisplayName": "VLČR Kancelář",
            "CFBundleShortVersionString": os.environ.get(
                "VLCR_VERSION",
                "0.0.0",
            ),
            "CFBundleVersion": os.environ.get(
                "VLCR_VERSION",
                "0.0.0",
            ),
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
