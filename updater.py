#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Aktualizace celé sady VLČR Kancelář z GitHub Releases.

Postup: zjistí nejnovější vydání -> nabídne aktualizaci -> stáhne správný balíček pro
daný systém -> ověří SHA256 podle SHA256SUMS.txt z vydání -> nainstaluje:

  Windows  spustí vlcr_kancelar_setup.exe v režimu "--update" (bez průvodce, jen průběh)
  openSUSE / Fedora  pkexec zypper|dnf install <balíček.rpm>
  Debian / Ubuntu    pkexec apt-get install <balíček.deb>
  macOS    otevře .dmg (aplikaci je potřeba přetáhnout do Aplikací)

Celý balíček se vždy nahrazuje najednou, takže všechny aplikace mají stejnou verzi.
"""
import hashlib
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request

# ==== ČISTÁ LOGIKA (bez Qt, testovatelná) ===================================
USER_AGENT = "VLCR-Kancelar-Updater"
CHECK_INTERVAL_S = 12 * 3600          # automatická kontrola nejvýš dvakrát denně
CA_CANDIDATES = (
    "/etc/ssl/ca-bundle.pem",              # openSUSE
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",    # Fedora, RHEL
    "/etc/ssl/cert.pem",                   # macOS, Alpine
)


def parse_version(text):
    """'v1.2.3' / '1.2.3-beta' -> srovnatelná dvojice (čísla, 1 = plná verze, 0 = předběžná)."""
    t = str(text).strip().lstrip("vV")
    m = re.match(r"^(\d+(?:\.\d+)*)(.*)$", t)
    if not m:
        return ((0, 0, 0, 0), 0)
    nums = [int(x) for x in m.group(1).split(".")]
    nums += [0] * max(0, 4 - len(nums))
    prerelease = bool(m.group(2).strip(" .+-~"))
    return (tuple(nums), 0 if prerelease else 1)


def is_newer(latest, current):
    return parse_version(latest) > parse_version(current)


def read_os_release():
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            continue
    return ""


def package_kind(platform_name=None, os_release="", which=shutil.which):
    """Který balíček se hodí pro tento systém: exe / dmg / rpm / deb / None."""
    plat = platform_name or sys.platform
    if plat.startswith("win"):
        return "exe"
    if plat == "darwin":
        return "dmg"
    if plat.startswith("linux"):
        info = {}
        for line in os_release.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip().strip('"')
        ids = (info.get("ID", "") + " " + info.get("ID_LIKE", "")).lower()
        if re.search(r"suse|fedora|rhel|centos|rocky|alma", ids):
            return "rpm"
        if re.search(r"debian|ubuntu|mint|pop", ids):
            return "deb"
        if which("zypper") or which("dnf") or which("yum"):
            return "rpm"
        if which("apt-get") or which("dpkg"):
            return "deb"
    return None


def pick_asset(assets, kind, machine=None):
    machine = (machine or platform.machine()).lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x86_64"
    if kind in ("rpm", "deb") and arch != "x86_64":
        return None                      # balíčky pro Linux jsou zatím jen x86_64
    for a in assets or []:
        low = str(a.get("name", "")).lower()
        if kind == "exe" and low.endswith(".exe"):
            return a
        if kind == "rpm" and low.endswith(".rpm") and not low.endswith(".src.rpm"):
            return a
        if kind == "deb" and low.endswith(".deb"):
            return a
        if kind == "dmg" and low.endswith(".dmg") and arch in low:
            return a
    return None


def parse_checksums(text):
    """Výstup příkazu sha256sum -> {název souboru: hash}."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
        if m:
            out[os.path.basename(m.group(2))] = m.group(1).lower()
    return out


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def asset_url_ok(url, repo):
    """Stahuje se výhradně z vydání našeho repozitáře na github.com."""
    return isinstance(url, str) and url.startswith("https://github.com/%s/releases/download/" % repo)


def ssl_context():
    """Kontext s certifikáty; v zabalených verzích na Linuxu/macOS se použije systémový balíček CA."""
    if not sys.platform.startswith("win"):
        for cand in CA_CANDIDATES:
            if os.path.isfile(cand):
                return ssl.create_default_context(cafile=cand)
    return ssl.create_default_context()


def http_get(url, timeout=20, accept=None):
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
        return resp.read()


def fetch_latest_release(repo):
    raw = http_get("https://api.github.com/repos/%s/releases/latest" % repo,
                   accept="application/vnd.github+json")
    return json.loads(raw.decode("utf-8"))


def evaluate_release(release, current, kind, machine, repo):
    """Vrátí popis aktualizace, pokud je vydání novější než current, jinak None."""
    tag = str(release.get("tag_name", ""))
    if not tag or not is_newer(tag, current):
        return None
    assets = release.get("assets") or []
    asset = pick_asset(assets, kind, machine) if kind else None
    if asset and not asset_url_ok(asset.get("browser_download_url"), repo):
        asset = None
    sums = next((a for a in assets
                 if str(a.get("name", "")).lower() == "sha256sums.txt"
                 and asset_url_ok(a.get("browser_download_url"), repo)), None)
    return {"version": tag.lstrip("vV"), "tag": tag, "notes": release.get("body") or "",
            "page": release.get("html_url", ""), "asset": asset, "sums": sums}


def download_asset(url, dest, progress=None, cancelled=None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30, context=ssl_context()) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        while True:
            if cancelled and cancelled():
                raise RuntimeError("Stahování bylo zrušeno.")
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            if progress and total:
                progress(int(got * 100 / total))


def linux_install_command(kind, path, which=shutil.which):
    """Příkaz pro instalaci balíčku s vyžádáním hesla (polkit), nebo None."""
    if not which("pkexec"):
        return None
    if kind == "rpm":
        if which("zypper"):
            return ["pkexec", "zypper", "--non-interactive", "--no-gpg-checks",
                    "install", "--allow-unsigned-rpm", path]
        if which("dnf"):
            return ["pkexec", "dnf", "install", "-y", "--nogpgcheck", path]
        return None
    if kind == "deb":
        return ["pkexec", "apt-get", "install", "-y", path]
    return None
# ==== KONEC ČISTÉ LOGIKY =====================================================

from PyQt5.QtCore import QObject, QProcess, QSettings, Qt, QThread, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QApplication, QMessageBox, QProgressDialog


class _CheckWorker(QThread):
    result = pyqtSignal(object)           # dict nebo None
    failed = pyqtSignal(str)

    def __init__(self, repo, version, parent=None):
        super().__init__(parent)
        self.repo, self.version = repo, version

    def run(self):
        try:
            release = fetch_latest_release(self.repo)
            kind = package_kind(os_release=read_os_release())
            self.result.emit(evaluate_release(release, self.version, kind, platform.machine(), self.repo))
        except Exception as e:
            self.failed.emit(str(e))


class _DownloadWorker(QThread):
    progress = pyqtSignal(int)
    done = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, asset, sums, parent=None):
        super().__init__(parent)
        self.asset, self.sums, self._cancel = asset, sums, False

    def cancel(self):
        self._cancel = True

    def run(self):
        dest = None
        try:
            if not self.sums:
                raise RuntimeError("Vydání neobsahuje SHA256SUMS.txt, aktualizaci nelze ověřit.")
            sums = parse_checksums(http_get(self.sums["browser_download_url"]).decode("utf-8", "replace"))
            name = os.path.basename(self.asset["name"])
            expected = sums.get(name)
            if not expected:
                raise RuntimeError("Pro soubor %s chybí kontrolní součet." % name)
            dest = os.path.join(tempfile.mkdtemp(prefix="vlcr-update-"), name)
            download_asset(self.asset["browser_download_url"], dest, self.progress.emit, lambda: self._cancel)
            if sha256_file(dest) != expected:
                raise RuntimeError("Kontrolní součet stažného souboru se neshoduje - soubor nebude použit.")
            self.done.emit(dest)
        except Exception as e:
            if dest and os.path.exists(dest):
                try:
                    os.remove(dest)
                except OSError:
                    pass
            self.failed.emit(str(e))


class UpdateManager(QObject):
    """Hlídá aktualizace a provádí je. Vytvoří se jednou v hlavním okně."""

    def __init__(self, window, version, repo, install_dir):
        super().__init__(window)
        self.window, self.version, self.repo, self.install_dir = window, version, repo, install_dir
        self.settings = QSettings("VLCR", "Kancelar")
        self._busy = False
        self._keep = []

    # --- kontrola --------------------------------------------------------------
    def check_silently(self):
        """Automatická kontrola při spuštění (nejvýš jednou za CHECK_INTERVAL_S)."""
        try:
            last = float(self.settings.value("updates/last_check", 0) or 0)
        except (TypeError, ValueError):
            last = 0
        if time.time() - last >= CHECK_INTERVAL_S:
            self._start_check(False)

    def check_manually(self):
        self._start_check(True)

    def _start_check(self, manual):
        if self._busy:
            if manual:
                QMessageBox.information(self.window, "Aktualizace", "Kontrola už probíhá.")
            return
        self._busy = True
        worker = _CheckWorker(self.repo, self.version, self)
        worker.result.connect(lambda info, m=manual: self._on_checked(info, m))
        worker.failed.connect(lambda msg, m=manual: self._on_check_failed(msg, m))
        self._keep.append(worker)
        worker.start()

    def _on_check_failed(self, message, manual):
        self._busy = False
        if manual:
            QMessageBox.warning(self.window, "Aktualizace",
                                "Kontrola aktualizací se nepodařila (zkontrolujte připojení k internetu).\n\n%s" % message)

    def _on_checked(self, info, manual):
        self._busy = False
        self.settings.setValue("updates/last_check", time.time())
        if info is None:
            if manual:
                QMessageBox.information(self.window, "Aktualizace",
                                        "Používáte nejnovější verzi (%s)." % self.version)
            return
        if info["asset"] is None:
            if manual:
                QMessageBox.information(
                    self.window, "Aktualizace",
                    "Je dostupná verze %s, ale pro tento systém zatím není připraven balíček.\n\n%s"
                    % (info["version"], info["page"]))
            return
        if not manual and self.settings.value("updates/skip_version", "") == info["version"]:
            return
        self._offer(info)

    # --- nabídka a stažení ---------------------------------------------------
    def _offer(self, info):
        box = QMessageBox(self.window)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Dostupná aktualizace")
        box.setText("Je dostupná nová verze %s (nyní máte %s)." % (info["version"], self.version))
        box.setInformativeText(
            "Aktualizuje se celá sada (rozcestník i všechny aplikace). "
            "Před pokračováním uložte práci a zavřete ostatní aplikace Kanceláře.")
        notes = info["notes"].strip()
        if notes:
            box.setDetailedText(notes[:1500])
        b_update = box.addButton("Aktualizovat", QMessageBox.AcceptRole)
        box.addButton("Později", QMessageBox.RejectRole)
        b_skip = box.addButton("Přeskočit tuto verzi", QMessageBox.ActionRole)
        box.setDefaultButton(b_update)
        box.exec_()
        clicked = box.clickedButton()
        if clicked is b_update:
            self._download_and_apply(info)
        elif clicked is b_skip:
            self.settings.setValue("updates/skip_version", info["version"])

    def _download_and_apply(self, info):
        if not getattr(sys, "frozen", False):
            QMessageBox.information(self.window, "Aktualizace",
                                    "Automatická aktualizace funguje jen v nainstalované verzi.\n\n%s" % info["page"])
            return
        dlg = QProgressDialog("Stahování aktualizace…", "Zrušit", 0, 100, self.window)
        dlg.setWindowTitle("Aktualizace")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setValue(0)
        worker = _DownloadWorker(info["asset"], info["sums"], self)
        worker.progress.connect(dlg.setValue)
        dlg.canceled.connect(worker.cancel)
        worker.done.connect(lambda path: (dlg.close(), self._apply(path)))
        worker.failed.connect(lambda msg: (dlg.close(), self._download_failed(msg)))
        self._keep.append(worker)
        worker.start()
        dlg.show()

    def _download_failed(self, message):
        if not message.startswith("Stahování bylo zrušeno"):
            QMessageBox.warning(self.window, "Aktualizaci se nepodařilo stáhnout", message)

    # --- instalace ---------------------------------------------------------------
    def _apply(self, path):
        kind = package_kind(os_release=read_os_release())
        if kind == "exe":
            self._apply_windows(path)
        elif kind == "dmg":
            subprocess.Popen(["open", path])
            QMessageBox.information(
                self.window, "Aktualizace stažena",
                "Otevřel se obraz disku s novou verzí. Přetáhněte aplikaci do složky Aplikace "
                "(nahraďte starou verzi) a spusťte ji znovu.")
        elif kind in ("rpm", "deb"):
            self._apply_linux(kind, path)

    def _apply_windows(self, path):
        import ctypes
        params = '--update --dest "%s"' % self.install_dir
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", path, params, None, 1)
        if rc <= 32:
            QMessageBox.warning(self.window, "Aktualizace",
                                "Instalátor se nepodařilo spustit. Potvrďte prosím okno Řízení uživatelských účtů.")
            return
        QApplication.quit()          # instalátor přepíše soubory, rozcestník se tedy zavře

    def _apply_linux(self, kind, path):
        cmd = linux_install_command(kind, path)
        if cmd is None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            QMessageBox.information(self.window, "Aktualizace stažena",
                                    "Balíček se otevřel ve správci softwaru. Dokončete instalaci a spusťte aplikaci znovu.")
            return
        dlg = QProgressDialog("Instalace aktualizace… (budete požádáni o heslo)", None, 0, 0, self.window)
        dlg.setWindowTitle("Aktualizace")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setCancelButton(None)
        dlg.show()
        proc = QProcess(self)
        proc.setProgram(cmd[0])
        proc.setArguments(cmd[1:])
        proc.finished.connect(lambda code, status: self._linux_done(code, proc, dlg))
        proc.errorOccurred.connect(lambda err: self._linux_failed(proc, dlg))
        self._keep.append(proc)
        proc.start()

    def _linux_failed(self, proc, dlg):
        dlg.close()
        QMessageBox.warning(self.window, "Aktualizace", "Instalaci se nepodařilo spustit (chybí pkexec?).")

    def _linux_done(self, code, proc, dlg):
        dlg.close()
        if code == 0:
            QMessageBox.information(self.window, "Aktualizace dokončena", "Nová verze je nainstalována. Aplikace se restartuje.")
            env = os.environ.copy()
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
            subprocess.Popen([sys.executable], env=env, start_new_session=True)
            QApplication.quit()
            return
        err = bytes(proc.readAllStandardError()).decode("utf-8", "replace").strip()[-600:]
        text = ("Instalace byla zrušena nebo selhala (kód %d)." % code) + ("\n\n" + err if err else "")
        QMessageBox.warning(self.window, "Aktualizace", text)