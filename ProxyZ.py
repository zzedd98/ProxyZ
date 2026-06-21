import sys
import json
import math
import logging
import importlib
import importlib.util
import contextlib
import os
from PySide6.QtWidgets import *
from PySide6.QtCore import *
from PySide6.QtGui import *
import psutil
import select
import socket
from dataclasses import dataclass
import threading
from concurrent.futures import ThreadPoolExecutor
import re
import subprocess
import shutil
import time
import traceback
import signal
import asyncio
import httpx
from enum import Enum
from typing import Optional, Dict, Callable, Awaitable, Tuple
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
import urllib.error
import urllib.request

# Sous Windows, empêche l'ouverture de consoles éphémères pour netsh / control.exe
if sys.platform.startswith("win"):
    CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
else:
    CREATE_NO_WINDOW = 0

# Script de reset par défaut (nom ou chemin relatif au dossier de l'app / exe)
DEFAULT_RESET_SCRIPT = "reset_modem.py"

# Ne jamais réécrire proxy_configs.json depuis l'app (édition manuelle du JSON).
PERSIST_CONFIG_TO_DISK = False

# Warmup Playwright au démarrage : délai entre chaque port (éviter burst si activé).
WARMUP_STAGGER_ENABLED = False
WARMUP_STAGGER_SECONDS = 2.0
# Valeur par défaut si absent de proxy_configs.json → "playwright_warmup_enabled"
DEFAULT_PLAYWRIGHT_WARMUP_ENABLED = True

# Polling interfaces / IP publique (perf CPU-GPU, réactivité préservée sur changement réel)
INTERFACE_REFRESH_BASE_MS = 5000
INTERFACE_NETSH_INTERVAL_S = 8.0
INTERFACE_PUBLIC_IP_BASE_MS = 12000
INTERFACE_PUBLIC_IP_WORKERS = 4
INTERFACE_PUBLIC_IP_STABLE_SKIP_S = 45.0
INTERFACE_STABLE_CYCLES_BEFORE_SLOW = 5
INTERFACE_SLOW_MULTIPLIER = 2

# Scripts reset Playwright avec navigateur persistant (warmup page + reset rapide)
PLAYWRIGHT_RESET_SCRIPT_NAMES = frozenset(
    {
        "reset_modem.py",
        "reset_xpro.py",
        "reset_huawei.py",
    }
)


def is_playwright_reset_script(script_path: Path) -> bool:
    return script_path.name.lower() in PLAYWRIGHT_RESET_SCRIPT_NAMES


INTERFACE_CARD_QSS = """
QFrame#interfaceCard {
    background-color: #2c3e50;
    border-radius: 8px;
    border: 1px solid rgba(255, 255, 255, 0.05);
}
QFrame#interfaceCard[connected="true"] {
    background-color: #244938;
    border: 1px solid rgba(46, 204, 113, 0.6);
}
QFrame#interfaceCard[disconnected="true"] {
    background-color: #2b2b2b;
    border: 1px dashed rgba(255, 255, 255, 0.15);
}
QLabel#ifaceName {
    color: #ecf0f1;
    font-size: 14px;
    font-weight: 700;
}
QLabel#metricBadge {
    background-color: rgba(149, 165, 166, 0.18);
    color: #bdc3c7;
    border-radius: 9px;
    padding: 1px 6px;
    font-size: 11px;
}
QLabel#resetAvgBadge {
    background-color: rgba(52, 152, 219, 0.14);
    color: #85c1e9;
    border-radius: 9px;
    padding: 1px 6px;
    font-size: 11px;
    font-weight: 600;
}
QLabel#autoBadge {
    background-color: rgba(52, 152, 219, 0.18);
    color: #3498db;
    border-radius: 9px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
}
QLabel#distBadge {
    background-color: rgba(155, 89, 182, 0.22);
    color: #d2b4de;
    border-radius: 9px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
}
QPushButton#remoteDeleteButton {
    background-color: rgba(231, 76, 60, 0.2);
    color: #e74c3c;
    border-radius: 9px;
    border: 1px solid rgba(231, 76, 60, 0.45);
    font-size: 13px;
    font-weight: 700;
    min-width: 22px;
    max-width: 22px;
    min-height: 22px;
    max-height: 22px;
    padding: 0px;
}
QPushButton#remoteDeleteButton:hover {
    background-color: rgba(231, 76, 60, 0.45);
    color: #ffffff;
}
QPushButton#addRemoteIfaceButton {
    background-color: rgba(46, 204, 113, 0.2);
    color: #2ecc71;
    border-radius: 14px;
    border: 1px solid rgba(46, 204, 113, 0.55);
    font-size: 16px;
    font-weight: 700;
    min-width: 28px;
    max-width: 28px;
    min-height: 28px;
    max-height: 28px;
    padding: 0px;
}
QPushButton#addRemoteIfaceButton:hover {
    background-color: rgba(46, 204, 113, 0.45);
    color: #ffffff;
}
QLabel#ipLabel {
    color: #bdc3c7;
    font-size: 11px;
}
QLabel#publicIpHeaderLabel {
    color: #ecf0f1;
    font-size: 13px;
    font-weight: 700;
}
QLabel#proxyOnChip {
    background-color: rgba(52, 152, 219, 0.3);
    color: #ecf0f1;
    border-radius: 10px;
    padding: 3px 10px;
    font-size: 12px;
    font-weight: 700;
    border: 1px solid rgba(52, 152, 219, 0.8);
}
QLabel#proxyOffChip {
    background-color: rgba(127, 140, 141, 0.25);
    color: #bdc3c7;
    border-radius: 8px;
    padding: 2px 8px;
    font-size: 11px;
    border: 1px solid transparent;
}
QLabel#proxyOnChip:hover,
QLabel#proxyOffChip:hover {
    background-color: rgba(59, 130, 246, 0.35);
    color: #ffffff;
    border-color: rgba(59, 130, 246, 0.9);
}
QLabel#resetBadge {
    background-color: rgba(255, 255, 255, 0.15);
    color: #ffffff;
    border-radius: 8px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
    border: 1px solid rgba(255, 255, 255, 0.3);
}
QLabel#resetBadge:hover {
    background-color: rgba(255, 255, 255, 0.25);
    color: #ffffff;
    border-color: rgba(255, 255, 255, 0.5);
}
QLabel#resetBadge[loading="true"] {
    background-color: rgba(59, 130, 246, 0.3);
    color: #ffffff;
    border-color: rgba(59, 130, 246, 0.6);
}
QLineEdit#portEdit {
    background-color: #22313f;
    border-radius: 6px;
    border: 1px solid rgba(255, 255, 255, 0.05);
    padding: 3px 6px;
    color: #ecf0f1;
    font-size: 12px;
}
QLineEdit#portEdit:focus {
    border: 1px solid #3498db;
}
"""

ZROTATE_INTERFACE_ROW_QSS = """
QFrame#zrotateInterfaceRow {
    background-color: rgba(15, 23, 42, 0.78);
    border: 1px solid rgba(59, 130, 246, 0.22);
    border-radius: 10px;
}
QFrame#zrotateInterfaceRow:hover {
    border: 1px solid rgba(59, 130, 246, 0.45);
    background-color: rgba(30, 41, 59, 0.9);
}
QFrame#zrotateInterfaceRow[poolEnabled="false"] {
    background-color: rgba(51, 65, 85, 0.42);
    border: 1px solid rgba(148, 163, 184, 0.22);
}
QFrame#zrotateInterfaceRow[poolEnabled="false"] QCheckBox#zrotateInterfaceCheckbox {
    color: #94a3b8;
}
QFrame#zrotateInterfaceRow[poolEnabled="false"] QLabel#zrotateIpChip {
    color: #94a3b8;
    background-color: rgba(71, 85, 105, 0.28);
    border-color: rgba(148, 163, 184, 0.35);
}
QFrame#zrotateInterfaceRow[zrotateLive="active"] {
    background-color: rgba(36, 73, 56, 0.88);
    border: 1px solid rgba(46, 204, 113, 0.55);
}
QFrame#zrotateInterfaceRow[zrotateLive="standby"] {
    background-color: rgba(90, 70, 30, 0.55);
    border: 1px solid rgba(241, 196, 15, 0.5);
}
QFrame#zrotateInterfaceRow[zrotateLive="resetting"] {
    background-color: rgba(70, 55, 30, 0.65);
    border: 1px solid rgba(243, 156, 18, 0.65);
}
QFrame#zrotateInterfaceRow[zrotateLive="quarantine"] {
    background-color: rgba(90, 35, 35, 0.55);
    border: 1px solid rgba(231, 76, 60, 0.6);
}
QFrame#zrotateInterfaceRow[zrotateLive="session"] {
    background-color: rgba(30, 41, 59, 0.55);
    border: 1px solid rgba(100, 116, 139, 0.45);
}
QCheckBox#zrotateInterfaceCheckbox {
    color: #e2e8f0;
    font-size: 14px;
    font-weight: 600;
}
QLabel#zrotateIpChip, QLabel#zrotateGetChip, QLabel#zrotateConnectChip {
    color: #dbeafe;
    background-color: rgba(30, 64, 175, 0.24);
    border: 1px solid rgba(96, 165, 250, 0.45);
    border-radius: 8px;
    padding: 2px 8px;
    font-size: 12px;
    font-weight: 600;
}
QLabel#zrotateConnectChip {
    background-color: rgba(6, 95, 70, 0.28);
    border-color: rgba(16, 185, 129, 0.5);
}
"""


def _qt_apply_properties(widget, properties: dict) -> bool:
    """Met à jour les propriétés dynamiques Qt ; polish uniquement si changement."""
    changed = False
    for key, value in properties.items():
        if widget.property(key) != value:
            widget.setProperty(key, value)
            changed = True
    if changed:
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
    return changed


def interface_is_usable(info: "InterfaceInfo") -> bool:
    """Interface locale (IPv4) ou distante (amont configuré)."""
    if info.is_remote:
        return bool(info.is_up and info.upstream_host and info.upstream_port)
    return bool(info.is_up and info.local_ip)


def interfaces_ui_snapshot(interfaces: dict[str, "InterfaceInfo"]) -> tuple:
    """Snapshot hashable pour interfaces_updated (sans IP publique / online)."""
    return tuple(
        sorted(
            (
                name,
                info.metric,
                info.is_up,
                info.local_ip,
                info.automatic,
                info.is_remote,
                info.upstream_host,
                info.upstream_port,
            )
            for name, info in interfaces.items()
        )
    )


def zrotate_visible_structure_signature(interfaces: dict[str, "InterfaceInfo"]) -> tuple:
    """Signature structurelle de la liste ZRotate (interfaces visibles, ordre trié)."""
    return tuple(
        sorted(name for name, info in interfaces.items() if interface_is_usable(info))
    )


def parse_host_port_field(text: str) -> tuple[str, int] | None:
    """Parse 'host:port' ou 'ip:port' ; retourne (host, port) ou None."""
    raw = (text or "").strip()
    if not raw or ":" not in raw:
        return None
    host, port_str = raw.rsplit(":", 1)
    host = host.strip()
    port_str = port_str.strip()
    if not host or not port_str.isdigit():
        return None
    port = int(port_str)
    if port <= 0 or port > 65535:
        return None
    return host, port


def get_app_dir() -> Path:
    """
    Retourne le répertoire de l'application, compatible script Python et .exe PyInstaller.
    - En .exe (frozen) : dossier contenant l'exécutable (où l'utilisateur place reset_modem.py).
    - En script : dossier contenant ProxyZ.py.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def _read_embedded_build_id() -> str:
    """
    Lit l'identifiant de build embarque (version.txt).
    - En onefile PyInstaller, version.txt est extrait dans sys._MEIPASS.
    - En mode script, on lit version.txt a cote de ProxyZ.py si present.
    """
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "version.txt")
    candidates.append(get_app_dir() / "version.txt")
    for p in candidates:
        try:
            if p.is_file():
                value = p.read_text(encoding="utf-8", errors="replace").strip()
                if value:
                    return value
        except Exception:
            continue
    return ""


def ensure_local_build_id_file() -> None:
    """
    Ecrit/rafraichit version.txt a cote de ProxyZ.exe pour que ProxyZUpdater
    puisse detecter la version locale de maniere fiable.
    """
    try:
        build_id = _read_embedded_build_id()
        if not build_id:
            return
        local_path = get_app_dir() / "version.txt"
        current = ""
        if local_path.is_file():
            try:
                current = local_path.read_text(
                    encoding="utf-8", errors="replace"
                ).strip()
            except Exception:
                current = ""
        if current != build_id:
            local_path.write_text(build_id + "\n", encoding="utf-8")
    except Exception:
        # Ne jamais bloquer le demarrage de l'app pour ce mecanisme.
        pass


UPDATE_GITHUB_REPO = "zzedd98/ProxyZ"
_UPDATE_UA = "ProxyZ-UpdateCheck/1.0"
_RE_GH_LATEST = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/releases/latest/download/([^/?#]+)$",
    re.I,
)


def _read_local_build_id() -> str:
    app_dir = get_app_dir()
    for fname in ("version.txt", "build_id.txt"):
        path = app_dir / fname
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            if lines and lines[0].strip():
                return lines[0].strip()
        except Exception:
            continue
    return _read_embedded_build_id()


def _repo_from_gh_latest_url(url: str) -> str:
    match = _RE_GH_LATEST.match((url or "").strip())
    return f"{match.group(1)}/{match.group(2)}" if match else ""


def _manifest_url_from_repo(repo: str) -> str:
    cleaned = (repo or "").strip().strip("/")
    if not cleaned:
        return ""
    return f"https://github.com/{cleaned}/releases/latest/download/update-manifest.json"


def _resolve_update_manifest_url() -> tuple[str, str]:
    env_manifest = (os.environ.get("PROXYZ_UPDATE_MANIFEST_URL") or "").strip()
    if env_manifest:
        repo = (
            (os.environ.get("PROXYZ_GITHUB_REPO") or "").strip().strip("/")
            or _repo_from_gh_latest_url(env_manifest)
            or UPDATE_GITHUB_REPO
        )
        return env_manifest, repo

    repo = (
        (os.environ.get("PROXYZ_GITHUB_REPO") or "").strip().strip("/")
        or UPDATE_GITHUB_REPO
    )
    return _manifest_url_from_repo(repo), repo


def _gh_headers_api() -> dict:
    return {
        "User-Agent": _UPDATE_UA,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _gh_api_latest_release(owner: str, repo: str) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/releases/latest",
        headers=_gh_headers_api(),
    )
    with urllib.request.urlopen(req, timeout=22.0) as resp:
        return json.loads(resp.read().decode("utf-8-sig", errors="replace"))


def _gh_find_asset_url(release: dict, filename: str) -> str:
    for asset in release.get("assets") or []:
        if (asset.get("name") or "") == filename:
            url = (asset.get("browser_download_url") or "").strip()
            if url:
                return url
    raise FileNotFoundError(f"Asset {filename!r} introuvable dans la release.")


def _gh_http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _UPDATE_UA})
    with urllib.request.urlopen(req, timeout=22.0) as resp:
        return json.loads(resp.read().decode("utf-8-sig", errors="replace"))


def fetch_update_manifest_dict(
    manifest_url: str, github_repo_fallback: str = ""
) -> dict:
    repo = (github_repo_fallback or "").strip().strip("/") or _repo_from_gh_latest_url(
        manifest_url or ""
    )
    req = urllib.request.Request(
        (manifest_url or "").strip(), headers={"User-Agent": _UPDATE_UA}
    )
    try:
        with urllib.request.urlopen(req, timeout=18.0) as resp:
            return json.loads(resp.read().decode("utf-8-sig", errors="replace"))
    except urllib.error.HTTPError as exc:
        if exc.code not in (404, 403):
            raise
    if "/" not in repo:
        raise RuntimeError("Manifest introuvable : verifie UPDATE_GITHUB_REPO.")
    owner, _, name = repo.partition("/")
    rel = _gh_api_latest_release(owner, name)
    return _gh_http_json(_gh_find_asset_url(rel, "update-manifest.json"))


def resolve_latest_release_asset_url(
    asset_url: str, github_repo_fallback: str, asset_filename: str
) -> str:
    repo = (github_repo_fallback or "").strip().strip("/") or _repo_from_gh_latest_url(
        asset_url or ""
    )
    url = (asset_url or "").strip()
    if not url or "github.com" not in url.lower() or "/releases/" not in url.lower():
        return url
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": _UPDATE_UA})
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            if resp.status < 400:
                return url
    except Exception:
        pass
    if "/" not in repo:
        return url
    owner, _, name = repo.partition("/")
    rel = _gh_api_latest_release(owner, name)
    return _gh_find_asset_url(rel, asset_filename)


@dataclass(frozen=True)
class ProxyZUpdateInfo:
    local_build_id: str
    remote_build_id: str
    download_url: str
    manifest_url: str
    github_repo: str


def is_update_check_enabled() -> bool:
    if (os.environ.get("PROXYZ_SKIP_UPDATE_CHECK") or "").strip() in (
        "1",
        "true",
        "yes",
    ):
        return False
    if (os.environ.get("PROXYZ_UPDATER_CHILD") or "").strip() in ("1", "true", "yes"):
        return False
    return True


def _updater_paths() -> tuple[Optional[Path], Optional[Path]]:
    app_dir = get_app_dir()
    exe = app_dir / "ProxyZUpdater.exe"
    script = app_dir / "ProxyZUpdater.py"
    return (exe if exe.is_file() else None, script if script.is_file() else None)


def check_proxyz_update() -> Optional[ProxyZUpdateInfo]:
    """Compare la version locale au manifest distant."""
    manifest_url, github_repo = _resolve_update_manifest_url()
    if not manifest_url:
        return None

    try:
        manifest = fetch_update_manifest_dict(manifest_url, github_repo)
        remote_build_id = str(
            manifest.get("build_id") or manifest.get("version") or ""
        ).strip()
        download_url = resolve_latest_release_asset_url(
            str(manifest.get("download_url") or "").strip(),
            github_repo,
            "ProxyZ.exe",
        )
    except Exception as exc:
        print(f"[UPDATE] Verification impossible : {exc}")
        return None

    if not remote_build_id or not download_url:
        print("[UPDATE] Manifest distant invalide.")
        return None

    local_build_id = _read_local_build_id()
    if local_build_id and local_build_id == remote_build_id:
        print(f"[UPDATE] {local_build_id} — logiciel a jour.")
        return None

    print(
        f"[UPDATE] Mise a jour disponible "
        f"(local={local_build_id or '?'} -> distant={remote_build_id})."
    )
    return ProxyZUpdateInfo(
        local_build_id=local_build_id,
        remote_build_id=remote_build_id,
        download_url=download_url,
        manifest_url=manifest_url,
        github_repo=github_repo,
    )


def launch_proxyz_updater(info: ProxyZUpdateInfo) -> bool:
    """Lance ProxyZUpdater.exe (ou .py) avec les URLs deja resolues."""
    updater_exe, updater_py = _updater_paths()
    if updater_exe is None and updater_py is None:
        print(
            "[UPDATE] ProxyZUpdater introuvable a cote de ProxyZ "
            "(ProxyZUpdater.exe ou ProxyZUpdater.py)."
        )
        return False

    app_dir = get_app_dir()
    target_exe = app_dir / "ProxyZ.exe"
    args = [
        "--target-exe",
        str(target_exe),
        "--manifest-url",
        info.manifest_url,
        "--download-url",
        info.download_url,
        "--github-repo",
        info.github_repo,
        "--local-build-id",
        info.local_build_id,
        "--remote-build-id",
        info.remote_build_id,
        "--app-display-name",
        "ProxyZ",
    ]

    if updater_exe is not None:
        cmd = [str(updater_exe), *args]
    else:
        cmd = [sys.executable, str(updater_py), *args]

    try:
        subprocess.Popen(
            cmd,
            cwd=str(app_dir),
            close_fds=not sys.platform.startswith("win"),
        )
        print("[UPDATE] ProxyZUpdater lance.")
        return True
    except Exception as exc:
        print(f"[UPDATE] Impossible de lancer ProxyZUpdater : {exc}")
        return False


def handle_startup_update_check() -> bool:
    """
    Verifie la version au demarrage.
    Retourne True si ProxyZUpdater a ete lance (ProxyZ ne doit pas demarrer).
    """
    if not is_update_check_enabled():
        return False
    info = check_proxyz_update()
    if info is None:
        return False
    if launch_proxyz_updater(info):
        print(
            "[UPDATE] ProxyZ ne demarre pas — "
            "utilisez le bouton « Lancer ProxyZ » dans ProxyZUpdater une fois la mise a jour terminee."
        )
        return True
    print(
        "[UPDATE] Mise a jour disponible mais ProxyZUpdater introuvable — "
        "demarrage normal."
    )
    return False


def resolve_reset_script_path(script_key: str, app_dir: Path) -> Path:
    """
    Résout le chemin d'un script de reset.
    - Chemin absolu ou avec lecteur (C:\\...) : utilisé tel quel.
    - Sinon : relatif à app_dir (ex. "reset_modem.py" -> app_dir / "reset_modem.py").
    """
    if not script_key or not script_key.strip():
        return app_dir / DEFAULT_RESET_SCRIPT
    p = Path(script_key)
    if p.is_absolute() or (
        sys.platform == "win32" and len(script_key) >= 2 and script_key[1] == ":"
    ):
        return p
    return (app_dir / script_key).resolve()


def get_python_executable() -> str:
    """
    Retourne le chemin de l'exécutable Python à utiliser.
    - En .exe (frozen) : préfère 'pythonw' (sans console) puis 'python' dans le PATH.
    - En script : utilise sys.executable (Python actuel).
    """
    if getattr(sys, "frozen", False):
        # On est dans un .exe, préférer pythonw (sans console) puis python
        python_exe = shutil.which("pythonw") or shutil.which("python")
        if python_exe:
            return python_exe
        # Fallback : essayer python3w puis python3
        python_exe = shutil.which("python3w") or shutil.which("python3")
        if python_exe:
            return python_exe
        # IMPORTANT: en mode .exe, sys.executable == ProxyZ.exe (pas un interpréteur Python).
        # On retourne vide pour éviter de lancer un faux "python" qui ferait des succès instantanés.
        return ""
    # En script Python, utiliser l'interpréteur actuel
    return sys.executable


def build_reset_command(script_path: Path, proxy_port: int) -> list[str]:
    """
    Construit la commande de reset Python.
    """
    if script_path.suffix.lower() != ".py":
        raise RuntimeError("Seuls les scripts reset Python (.py) sont supportés.")
    python_exe = get_python_executable()
    if not python_exe:
        raise RuntimeError(
            "Interpréteur Python introuvable pour lancer un script .py en mode exécutable."
        )
    return [python_exe, str(script_path), str(proxy_port)]


_RESET_MODULE_CACHE: dict[str, tuple] = {}


class _LineForwarder:
    """Redirige stdout/stderr vers un callback ligne par ligne."""

    def __init__(self, callback):
        self._callback = callback
        self._buf = ""

    def write(self, data):
        if not data:
            return 0
        self._buf += str(data)
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")
            if line and self._callback:
                self._callback(line)
        return len(data)

    def flush(self):
        if self._buf:
            line = self._buf.rstrip("\r")
            self._buf = ""
            if line and self._callback:
                self._callback(line)

    def isatty(self):
        return False


@contextlib.contextmanager
def _quiet_third_party_loggers():
    """Évite les 'Logging error' httpx/httpcore quand stderr est None (exe windowed)."""
    targets = (
        logging.root,
        logging.getLogger("httpx"),
        logging.getLogger("httpcore"),
    )
    prev_levels = {lg: lg.level for lg in targets}
    removed_handlers: list[tuple[logging.Logger, logging.Handler]] = []
    try:
        for lg in targets:
            lg.setLevel(logging.WARNING)
            for handler in list(lg.handlers):
                stream = getattr(handler, "stream", None)
                if isinstance(handler, logging.StreamHandler) and (
                    stream is None or stream is sys.stderr
                ):
                    removed_handlers.append((lg, handler))
                    lg.removeHandler(handler)
        yield
    finally:
        for lg, level in prev_levels.items():
            lg.setLevel(level)
        for lg, handler in removed_handlers:
            lg.addHandler(handler)


def _load_reset_modem_functions(script_path: Path):
    """
    Charge un script reset Playwright depuis son chemin absolu.
    Fiable en mode script ET en mode .exe (indépendant du working directory).
    Retourne (reset_modem_by_port, initialize_browser_service, resolved_path).
    """
    resolved = str(script_path.resolve())
    cached = _RESET_MODULE_CACHE.get(resolved.lower())
    if cached is not None:
        return cached[0], cached[1]

    module = None
    load_resolved = resolved
    if script_path.exists():
        module_name = f"_proxyz_reset_{abs(hash(resolved))}"
        spec = importlib.util.spec_from_file_location(module_name, resolved)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Impossible de charger le module reset: {resolved}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        stem = script_path.stem
        try:
            module = importlib.import_module(stem)
            load_resolved = f"<embedded {stem}>"
        except Exception as e:
            raise FileNotFoundError(f"Script reset introuvable: {script_path}") from e

    reset_func = getattr(module, "reset_modem_by_port", None)
    if not callable(reset_func):
        raise RuntimeError(
            f"Fonction reset_modem_by_port introuvable dans {load_resolved}"
        )
    init_func = getattr(module, "initialize_browser_service", None)
    if not callable(init_func):
        init_func = None

    _RESET_MODULE_CACHE[resolved.lower()] = (
        reset_func,
        init_func,
        load_resolved,
        module,
    )
    return reset_func, init_func


def _reset_module_source(script_path: Path) -> str:
    resolved = str(script_path.resolve()).lower()
    cached = _RESET_MODULE_CACHE.get(resolved)
    if cached:
        return cached[2]
    return resolved


def extract_interface_reset_options(iface_cfg: dict | None) -> dict:
    """
    Options reset par interface (proxy_configs.json).
    modem_gateway : IP ou URL de l'interface web du modem (ex. 192.168.100.1).
    """
    if not iface_cfg:
        return {}
    for key in (
        "modem_gateway",
        "reset_modem_gateway",
        "modem_url",
        "passerelle_modem",
    ):
        val = iface_cfg.get(key)
        if val is not None and str(val).strip():
            return {"modem_gateway": str(val).strip()}
    return {}


def resolve_interface_reset_script(
    iface_cfg: dict | None, default_reset: str
) -> tuple[str, bool]:
    """
    Résout le script reset d'une interface.
    Retourne (script, is_explicit_mapping).
    - explicite si `reset_script` est défini
    - explicite aussi si `modem_gateway` est défini (mapping XPro implicite)
    """
    cfg = iface_cfg or {}
    script = str(cfg.get("reset_script", "") or "").strip()
    if script:
        return script, True

    options = extract_interface_reset_options(cfg)
    if options.get("modem_gateway"):
        return "reset_XPro.py", True

    return default_reset, False


def _configure_reset_port(
    script_path: Path, proxy_port: int, reset_options: dict | None
) -> None:
    if not reset_options:
        return
    _load_reset_modem_functions(script_path)
    resolved = str(script_path.resolve()).lower()
    cached = _RESET_MODULE_CACHE.get(resolved)
    if not cached or len(cached) < 4:
        return
    module = cached[3]
    port = int(proxy_port)
    configure = getattr(module, "configure_port_options", None)
    if callable(configure):
        configure({port: dict(reset_options)})
        return
    gateway = reset_options.get("modem_gateway")
    set_gw = getattr(module, "set_port_modem_gateway", None)
    if callable(set_gw) and gateway:
        set_gw(port, str(gateway))


def collect_playwright_warmup_by_script(
    config: dict, app_dir: Path
) -> dict[str, tuple[list[int], dict[int, dict]]]:
    """
    Regroupe ports + options par script Playwright.
    Retourne {chemin_script: (ports, port_options)}.
    """
    default_reset = config.get("reset_script_default", DEFAULT_RESET_SCRIPT)
    by_script: dict[str, tuple[list[int], dict[int, dict]]] = {}

    for iface_name, cfg in (config.get("interface_proxies", {}) or {}).items():
        if not cfg or cfg.get("enabled", True) is False:
            continue
        if cfg.get("remote"):
            continue
        try:
            port = int(cfg.get("port", 0) or 0)
        except (TypeError, ValueError):
            continue
        if port <= 0:
            continue
        reset_script = cfg.get("reset_script", default_reset)
        script_path = resolve_reset_script_path(reset_script, app_dir)
        if not is_playwright_reset_script(script_path) or not script_path.exists():
            continue

        key = str(script_path.resolve()).lower()
        ports_list, port_options = by_script.get(key, ([], {}))
        opts = extract_interface_reset_options(cfg)
        if port in port_options and opts.get("modem_gateway") != port_options[port].get(
            "modem_gateway"
        ):
            print(
                f"[RESET] ⚠️ Port {port}: conflit modem_gateway entre interfaces "
                f"('{iface_name}' écrase la config précédente)"
            )
        port_options[port] = {**port_options.get(port, {}), **opts}
        if port not in ports_list:
            ports_list.append(port)
        by_script[key] = (ports_list, port_options)

    return by_script


def _pick_system_python_for_reset() -> str:
    """Trouve un interpréteur Python système utilisable pour reset_modem.py."""
    candidates = [
        shutil.which("python"),
        shutil.which("python3"),
        shutil.which("py"),
    ]
    for exe in candidates:
        if exe:
            return exe
    return ""


def _ensure_playwright_runtime(python_exe: str, log_fn=None) -> bool:
    """Installe playwright + chromium si nécessaire pour un Python système."""
    if not python_exe:
        return False

    def _log(msg: str):
        if log_fn:
            log_fn(msg)

    def _run(cmd: list[str], timeout_s: int) -> bool:
        try:
            p = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                creationflags=CREATE_NO_WINDOW,
            )
            if p.returncode != 0:
                _log(f"[RESET] ⚠️ Commande échouée: {' '.join(cmd)}")
                if p.stderr.strip():
                    _log(f"[RESET] stderr: {p.stderr.strip()[:220]}")
                return False
            return True
        except Exception as e:
            _log(f"[RESET] ⚠️ Erreur commande {' '.join(cmd)}: {e}")
            return False

    # Vérifie si playwright est déjà dispo
    if _run([python_exe, "-c", "import playwright"], 25):
        return True

    _log("[RESET] Playwright manquant, installation automatique en cours...")
    if not _run([python_exe, "-m", "pip", "install", "-U", "playwright"], 600):
        return False
    if not _run([python_exe, "-m", "playwright", "install", "chromium"], 900):
        return False
    return _run([python_exe, "-c", "import playwright"], 25)


def _run_playwright_reset_subprocess(
    script_path: Path,
    proxy_port: int,
    timeout_seconds: int,
    log_fn=None,
    reset_options: dict | None = None,
) -> int | None:
    """Lance le reset en subprocess Python système. Retourne None si indisponible."""
    if not script_path.exists():
        return None
    py = _pick_system_python_for_reset()
    if not py or not _ensure_playwright_runtime(py, log_fn=log_fn):
        if log_fn:
            log_fn(
                "[RESET] ⚠️ Python système introuvable ou installation Playwright impossible."
            )
        return None
    cmd = [py, str(script_path), str(proxy_port)]
    gateway = (reset_options or {}).get("modem_gateway")
    if gateway:
        cmd.append(str(gateway))
    if log_fn:
        log_fn(
            f"[RESET] fallback subprocess: {' '.join(cmd)} | cwd={str(get_app_dir())}"
        )
    result = subprocess.run(
        cmd,
        timeout=timeout_seconds,
        cwd=str(get_app_dir()),
        creationflags=CREATE_NO_WINDOW,
    )
    return result.returncode


def run_reset_script(
    script_path: Path,
    proxy_port: int,
    timeout_seconds: int = 120,
    log_fn=None,
    reset_options: dict | None = None,
) -> int:
    """
    Exécute un reset:
    - scripts Playwright (reset_modem, reset_XPro, reset_huawei) en-process.
    - autres scripts Python en subprocess.
    Retourne un code process-like (0 succès, 1 échec).
    """
    in_process = is_playwright_reset_script(script_path)

    if in_process:
        try:
            reset_func, _ = _load_reset_modem_functions(script_path)
        except ModuleNotFoundError as e:
            if "playwright" in str(e).lower():
                code = _run_playwright_reset_subprocess(
                    script_path,
                    proxy_port,
                    timeout_seconds,
                    log_fn=log_fn,
                    reset_options=reset_options,
                )
                if code is not None:
                    return code
            raise
        _configure_reset_port(script_path, proxy_port, reset_options)
        kwargs = {}
        if reset_options and reset_options.get("modem_gateway"):
            kwargs["modem_gateway"] = reset_options["modem_gateway"]
        out = _LineForwarder(log_fn) if log_fn else None
        with _quiet_third_party_loggers():
            if out:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                    ok = bool(reset_func(proxy_port, **kwargs))
            else:
                ok = bool(reset_func(proxy_port, **kwargs))
        return 0 if ok else 1

    cmd = build_reset_command(script_path, proxy_port)
    if log_fn:
        log_fn(f"[RESET] subprocess: {' '.join(cmd)} | cwd={str(get_app_dir())}")
    result = subprocess.run(
        cmd,
        timeout=timeout_seconds,
        cwd=str(get_app_dir()),
    )
    return result.returncode


@dataclass
class ProxyConfig:
    name: str
    bind_ip: str
    port: int
    interface_name: str
    is_remote: bool = False
    upstream_host: str | None = None
    upstream_port: int | None = None


class ProxyThread(QThread):
    status_changed = Signal(bool)

    def __init__(self, config: ProxyConfig):
        super().__init__()
        self.config = config
        self.running = False
        self.server_socket = None

    def run(self):
        self.running = True
        self.status_changed.emit(True)
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.bind(("127.0.0.1", self.config.port))
            self.server_socket.listen(5)
            if self.config.is_remote:
                print(
                    f"[OK] Relais proxy sur 127.0.0.1:{self.config.port} "
                    f"→ amont {self.config.upstream_host}:{self.config.upstream_port}"
                )
            else:
                print(
                    f"[OK] Proxy écoute sur 127.0.0.1:{self.config.port}, "
                    f"envoi via l'IP source {self.config.bind_ip}"
                )

            while self.running:
                try:
                    client_socket, client_address = self.server_socket.accept()
                    client_thread = threading.Thread(
                        target=self.handle_client,
                        args=(client_socket, self.config.bind_ip),
                    )
                    client_thread.daemon = True
                    client_thread.start()
                except Exception:
                    if self.running:
                        print("Erreur lors de l'acceptation de la connexion")
                    break
        except Exception as e:
            print(f"Erreur du serveur proxy: {str(e)}")
        finally:
            self.running = False
            self.status_changed.emit(False)

    def stop(self):
        self.running = False
        if self.server_socket:
            try:
                self.server_socket.close()
            except Exception:
                pass

    def handle_client(self, client_socket, bind_ip):
        try:
            request = client_socket.recv(4096)
            if not request:
                return

            request_line = request.split(b"\r\n")[0].decode(errors="ignore")

            if self.config.is_remote:
                if request_line.startswith("CONNECT"):
                    self.handle_https_tunnel_upstream(client_socket, request_line)
                else:
                    self.handle_http_request_upstream(client_socket, request)
                return

            if request_line.startswith("CONNECT"):
                self.handle_https_tunnel(client_socket, request_line, bind_ip)
            else:
                self.handle_http_request(client_socket, request, bind_ip)
        except Exception as e:
            print(f"Erreur handle_client: {e}")
        finally:
            try:
                client_socket.close()
            except Exception:
                pass

    def _connect_upstream_proxy_socket(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((self.config.upstream_host, int(self.config.upstream_port)))
        return sock

    def handle_https_tunnel_upstream(self, client_socket, request_line):
        try:
            match = re.match(r"CONNECT ([^:]+):(\d+)", request_line)
            if not match:
                print("Requête CONNECT mal formée (relais amont)")
                return

            host, port = match.groups()
            port = int(port)

            upstream_sock = self._connect_upstream_proxy_socket()
            try:
                connect_req = (
                    f"CONNECT {host}:{port} HTTP/1.1\r\n"
                    f"Host: {host}:{port}\r\n\r\n"
                )
                upstream_sock.sendall(connect_req.encode())
                response = upstream_sock.recv(4096)
                if not response or b"200" not in response.split(b"\r\n", 1)[0]:
                    print("Échec CONNECT via proxy amont")
                    return

                client_socket.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self.relay_data(client_socket, upstream_sock)
            finally:
                try:
                    upstream_sock.close()
                except Exception:
                    pass
        except Exception as e:
            print(f"Erreur HTTPS relais amont: {e}")

    def handle_http_request_upstream(self, client_socket, request):
        try:
            upstream_sock = self._connect_upstream_proxy_socket()
            try:
                upstream_sock.sendall(request)
                self.relay_data(client_socket, upstream_sock)
            finally:
                try:
                    upstream_sock.close()
                except Exception:
                    pass
        except Exception as e:
            print(f"Erreur HTTP relais amont: {e}")

    def handle_http_request(self, client_socket, request, bind_ip):
        try:
            headers = request.split(b"\r\n")
            host = None
            port = 80

            for header in headers:
                if header.lower().startswith(b"host:"):
                    host_line = header.decode(errors="ignore")
                    host = host_line.split(":", 1)[1].strip()
                    if ":" in host:
                        host, port = host.split(":")
                        port = int(port)
                    break

            if not host:
                print("Impossible de trouver l'hôte dans la requête")
                return

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
                server_socket.bind((bind_ip, 0))
                server_socket.connect((host, port))
                server_socket.sendall(request)

                self.relay_data(client_socket, server_socket)
        except Exception as e:
            print(f"Erreur HTTP: {e}")

    def handle_https_tunnel(self, client_socket, request_line, bind_ip):
        try:
            match = re.match(r"CONNECT ([^:]+):(\d+)", request_line)
            if not match:
                print("Requête CONNECT mal formée")
                return

            host, port = match.groups()
            port = int(port)

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
                server_socket.bind((bind_ip, 0))
                server_socket.connect((host, port))

                client_socket.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

                self.relay_data(client_socket, server_socket)
        except Exception as e:
            print(f"Erreur HTTPS: {e}")

    def relay_data(self, client_socket, server_socket):
        sockets = [client_socket, server_socket]
        while self.running:
            try:
                readable, _, _ = select.select(sockets, [], [], 1)
                for sock in readable:
                    data = sock.recv(4096)
                    if not data:
                        return
                    if sock is client_socket:
                        server_socket.sendall(data)
                    else:
                        client_socket.sendall(data)
            except Exception:
                break


@dataclass
class InterfaceInfo:
    idx: int
    name: str
    metric: int
    automatic: bool
    state: str
    is_up: bool
    local_ip: str | None
    public_ip: str | None = None
    online: bool = False
    is_remote: bool = False
    upstream_host: str | None = None
    upstream_port: int | None = None


class InterfaceManager(QObject):
    interfaces_updated = Signal(list)  # list[InterfaceInfo]
    public_ip_updated = Signal(str, str, bool)  # name, public_ip, online
    metrics_update_failed = Signal(str)  # message

    def __init__(self, parent=None):
        super().__init__(parent)
        self.interfaces: dict[str, InterfaceInfo] = {}
        self._last_ui_snapshot: tuple | None = None
        self._stable_refresh_cycles = 0
        self._ui_minimized = False
        self._netsh_cache: dict[str, dict] = {}
        self._netsh_cache_time = 0.0
        self._force_netsh_refresh = True
        self._last_psutil_iface_keys: frozenset[str] = frozenset()
        self._public_ip_executor = ThreadPoolExecutor(
            max_workers=INTERFACE_PUBLIC_IP_WORKERS,
            thread_name_prefix="pubip",
        )
        self._public_ip_inflight: set[str] = set()
        self._public_ip_inflight_lock = threading.Lock()
        self._public_ip_last_ok: dict[str, tuple[str, float]] = {}

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(
            lambda: self.refresh_interfaces(force_netsh=False)
        )

        self.public_ip_timer = QTimer(self)
        self.public_ip_timer.timeout.connect(
            lambda: self.refresh_public_ips(force=False)
        )

        self._recompute_timer_intervals()
        self.refresh_timer.start()
        self.public_ip_timer.start()

        # Première charge
        try:
            self.refresh_interfaces(force_netsh=True)
            self.refresh_public_ips(force=True)
        except Exception:
            traceback.print_exc()

    def notify_window_minimized(self, minimized: bool) -> None:
        if self._ui_minimized == minimized:
            return
        self._ui_minimized = minimized
        if not minimized:
            self._stable_refresh_cycles = 0
        self._recompute_timer_intervals()

    def request_immediate_refresh(self) -> None:
        """Refresh complet (netsh + psutil + IP) après reset, rename, métriques, etc."""
        self._force_netsh_refresh = True
        self._stable_refresh_cycles = 0
        self._recompute_timer_intervals()
        self.refresh_interfaces(force_netsh=True)
        self.refresh_public_ips(force=True)

    def _recompute_timer_intervals(self) -> None:
        mult = 1
        if self._ui_minimized:
            mult *= INTERFACE_SLOW_MULTIPLIER
        if self._stable_refresh_cycles >= INTERFACE_STABLE_CYCLES_BEFORE_SLOW:
            mult *= INTERFACE_SLOW_MULTIPLIER
        refresh_ms = INTERFACE_REFRESH_BASE_MS * mult
        public_ip_ms = INTERFACE_PUBLIC_IP_BASE_MS * mult
        if getattr(self, "refresh_timer", None) is not None:
            self.refresh_timer.setInterval(refresh_ms)
        if getattr(self, "public_ip_timer", None) is not None:
            self.public_ip_timer.setInterval(public_ip_ms)

    def shutdown(self):
        """Arrête proprement les timers et le pool IP publique."""
        try:
            self.refresh_timer.stop()
            self.public_ip_timer.stop()
        except Exception:
            traceback.print_exc()

        try:
            self._public_ip_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            traceback.print_exc()
        with self._public_ip_inflight_lock:
            self._public_ip_inflight.clear()

    # --- Récupération des interfaces & métriques ---
    def _parse_netsh_interfaces(self) -> dict[str, dict]:
        result = {}
        try:
            completed = subprocess.run(
                ["netsh", "interface", "ipv4", "show", "interfaces"],
                capture_output=True,
                shell=False,
                creationflags=CREATE_NO_WINDOW,
            )
            if completed.returncode != 0:
                return result
            # Décodage manuel pour éviter les problèmes d'accents (netsh renvoie souvent de l'UTF-8)
            if isinstance(completed.stdout, (bytes, bytearray)):
                try:
                    stdout_txt = completed.stdout.decode("utf-8", errors="ignore")
                except Exception:
                    stdout_txt = completed.stdout.decode(errors="ignore")
            else:
                stdout_txt = str(completed.stdout)

            lines = stdout_txt.splitlines()
            for line in lines:
                raw_line = line
                line = line.strip()
                if not line:
                    continue
                if line.lower().startswith("idx") or line.startswith("---"):
                    continue

                # Exemple de ligne:
                # 13        25        1500      connected     Wi-Fi
                m = re.match(r"^(\d+)\s+(\S+)\s+\S+\s+(\S+)\s+(.+)$", line)
                if not m:
                    continue
                idx_str, metric_str, state, name = m.groups()
                idx = int(idx_str)
                automatic = not metric_str.isdigit()
                metric = int(metric_str) if metric_str.isdigit() else 9999
                result[name] = {
                    "idx": idx,
                    "metric": metric,
                    "automatic": automatic,
                    "state": state.lower(),
                }
        except Exception as e:
            traceback.print_exc()

        return result

    def _should_refresh_netsh(self, force: bool, addrs) -> bool:
        if force or self._force_netsh_refresh:
            return True
        if not self._netsh_cache:
            return True
        if frozenset(addrs.keys()) != self._last_psutil_iface_keys:
            return True
        return (time.monotonic() - self._netsh_cache_time) >= INTERFACE_NETSH_INTERVAL_S

    def refresh_interfaces(self, force_netsh: bool = False):
        try:
            addrs = psutil.net_if_addrs()
            stats = psutil.net_if_stats()
        except Exception:
            traceback.print_exc()
            return

        iface_keys = frozenset(addrs.keys())
        if iface_keys != self._last_psutil_iface_keys:
            self._last_psutil_iface_keys = iface_keys
            force_netsh = True

        if self._should_refresh_netsh(force_netsh, addrs):
            netsh_data = self._parse_netsh_interfaces()
            if netsh_data:
                self._netsh_cache = netsh_data
                self._netsh_cache_time = time.monotonic()
                self._force_netsh_refresh = False
        else:
            netsh_data = self._netsh_cache

        if not netsh_data:
            return

        new_interfaces: dict[str, InterfaceInfo] = {}

        for name, info in netsh_data.items():
            local_ip = None
            if name in addrs:
                for addr in addrs[name]:
                    if addr.family == socket.AF_INET and addr.address != "127.0.0.1":
                        local_ip = addr.address
                        break
            # Ne garder que les interfaces "internet" :
            # - IPv4 locale présente
            # - pas d'adresse APIPA 169.254.x.x
            # - exclure explicitement les interfaces Bluetooth
            if not local_ip:
                continue
            if local_ip.startswith("169.254."):
                continue
            if "bluetooth" in name.lower():
                continue
            is_up = False
            if name in stats:
                is_up = stats[name].isup

            prev = self.interfaces.get(name)
            public_ip = prev.public_ip if prev else None
            # Si l'interface vient de passer hors ligne, on considère qu'elle n'est plus "online"
            if not is_up:
                online = False
                public_ip = None
            else:
                online = prev.online if prev else False

            new_interfaces[name] = InterfaceInfo(
                idx=info["idx"],
                name=name,
                metric=info["metric"],
                automatic=info["automatic"],
                state=info["state"],
                is_up=is_up,
                local_ip=local_ip,
                public_ip=public_ip,
                online=online,
            )

        snapshot = interfaces_ui_snapshot(new_interfaces)
        if snapshot == self._last_ui_snapshot:
            self._stable_refresh_cycles += 1
        else:
            self._stable_refresh_cycles = 0
            self._last_ui_snapshot = snapshot
            self.interfaces = new_interfaces
            self.interfaces_updated.emit(list(self.interfaces.values()))
            self._recompute_timer_intervals()
            return

        self.interfaces = new_interfaces
        self._recompute_timer_intervals()

    # --- Public IP / connectivité ---
    def refresh_public_ips(self, force: bool = False):
        now = time.monotonic()
        for name, info in list(self.interfaces.items()):
            if not info.is_up or not info.local_ip:
                continue
            with self._public_ip_inflight_lock:
                if name in self._public_ip_inflight:
                    continue
            if not force:
                last_ok = self._public_ip_last_ok.get(name)
                if (
                    last_ok
                    and info.online
                    and info.public_ip == last_ok[0]
                    and (now - last_ok[1]) < INTERFACE_PUBLIC_IP_STABLE_SKIP_S
                ):
                    continue
            with self._public_ip_inflight_lock:
                self._public_ip_inflight.add(name)
            self._public_ip_executor.submit(
                self._public_ip_worker_thread, name, info.local_ip
            )

    def _public_ip_worker_thread(self, name: str, local_ip: str, timeout: float = 4.0):
        public_ip = None
        online = False
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.bind((local_ip, 0))
            s.connect(("api.ipify.org", 80))
            req = (
                "GET /?format=text HTTP/1.1\r\n"
                "Host: api.ipify.org\r\n"
                "Connection: close\r\n\r\n"
            )
            s.sendall(req.encode("ascii"))
            chunks = []
            while True:
                data = s.recv(4096)
                if not data:
                    break
                chunks.append(data)
            raw = b"".join(chunks).decode(errors="ignore")
            parts = raw.split("\r\n\r\n", 1)
            if len(parts) == 2:
                body = parts[1].strip()
                if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", body):
                    public_ip = body
                    online = True
        except Exception:
            online = False
        finally:
            try:
                s.close()
            except Exception:
                pass
        try:
            prev_ip = ""
            prev_online = False
            if name in self.interfaces:
                info = self.interfaces[name]
                prev_ip = info.public_ip or ""
                prev_online = info.online
                new_ip = public_ip or info.public_ip
                info.public_ip = new_ip
                info.online = online
                self.interfaces[name] = info
                if online and new_ip:
                    self._public_ip_last_ok[name] = (new_ip, time.monotonic())
            new_ip_str = public_ip or prev_ip or ""
            if new_ip_str != prev_ip or online != prev_online:
                self.public_ip_updated.emit(name, new_ip_str, online)
        except Exception:
            traceback.print_exc()
        finally:
            with self._public_ip_inflight_lock:
                self._public_ip_inflight.discard(name)

    @Slot(str, str, bool)
    def _on_public_ip_result(self, name: str, public_ip: str, online: bool):
        if name in self.interfaces:
            info = self.interfaces[name]
            info.public_ip = public_ip or info.public_ip
            info.online = online
            self.interfaces[name] = info
        self.public_ip_updated.emit(name, public_ip, online)

    # --- Mise à jour des métriques après drag & drop ---
    def apply_manual_order(self, manual_names: list[str]):
        """
        Applique les métriques 1/11/21/... aux interfaces manuelles
        dans l'ordre donné. Les interfaces en auto ne sont pas touchées.
        """
        print(f"[METRIC] apply_manual_order(manual_names={manual_names})")
        errors: list[str] = []
        for index, name in enumerate(manual_names):
            info = self.interfaces.get(name)
            if not info or info.automatic:
                continue
            metric_value = 1 + index * 10
            try:
                print(
                    f"[METRIC] netsh set metric pour '{name}' (idx={info.idx}) -> {metric_value}"
                )
                completed = subprocess.run(
                    [
                        "netsh",
                        "interface",
                        "ipv4",
                        "set",
                        "interface",
                        # Utilise l'index comme argument positionnel (name|index)
                        str(info.idx),
                        f"metric={metric_value}",
                    ],
                    capture_output=True,
                    text=True,
                    shell=False,
                    creationflags=CREATE_NO_WINDOW,
                )
                if completed.returncode != 0:
                    err = completed.stderr.strip() or completed.stdout.strip()
                    msg = f"Échec netsh pour l'interface '{name}' (metric={metric_value}): {err}"
                    errors.append(msg)
                    print(msg)
                else:
                    # Netsh a accepté : mettre aussi à jour la valeur en mémoire
                    info.metric = metric_value
                    self.interfaces[name] = info
                    print(f"[METRIC] Metric appliquée pour '{name}' -> {metric_value}")
            except Exception as e:
                msg = f"Exception netsh set metric ({name}, metric={metric_value}): {e}"
                errors.append(msg)
                print(msg)

        if errors:
            self.metrics_update_failed.emit(
                "Impossible de modifier certaines métriques IPv4.\n\n"
                + "\n".join(errors[:5])
            )
        # Dans tous les cas on resynchronise avec l'état réel (netsh + psutil)
        self.request_immediate_refresh()


# ============================================================
# CLASSES ZROTATE
# ============================================================


"""
ZRotate Single Proxy Server
Proxy HTTP/HTTPS avec rotation round-robin des clés Huawei via bind source.
Écoute sur 127.0.0.1:9999 et force chaque connexion à sortir via une des deux clés.
"""

# Configuration des clés Huawei
# IMPORTANT: Ajustez ces IPs selon vos interfaces réseau réelles
EGRESS_IPS = [
    {"name": "KEY101", "ip": "192.168.8.101"},
    {"name": "KEY102", "ip": "192.168.8.102"},
]

# Configuration du serveur
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 9999

# Taille du buffer pour le relay
BUFFER_SIZE = 65536

# Configuration du logging
# Ne pas créer de handler si un handler existe déjà (évite le double logging)
if not logging.root.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
# Utiliser un nom de logger spécifique pour que ProxyZ.py puisse le capturer
logger = logging.getLogger("zrotate_single_proxy")
# Empêcher la propagation vers le logger root si un handler est déjà configuré ailleurs
logger.propagate = True  # Laisser propager si aucun handler n'est configuré dans ProxyZ


class QuotaInfo:
    """Informations sur les quotas d'une interface pour un type de requête et un domaine"""

    def __init__(self, max_requests: int = 2):
        self.max_requests = max_requests
        self.temporary_requests = 0  # Requêtes en cours (pas encore terminées)
        self.completed_requests = 0  # Requêtes terminées avec succès
        self.last_activity = datetime.now()

    def can_start_request(self) -> bool:
        """Vérifie si on peut démarrer une nouvelle requête (vérifie temporaires + complétées)"""
        total = self.temporary_requests + self.completed_requests
        return total < self.max_requests

    def start_request(self):
        """Démarre une requête (incrémente le quota temporaire)"""
        self.temporary_requests += 1
        self.last_activity = datetime.now()

    def complete_request(self):
        """Termine une requête avec succès (décrémente temporaire, incrémente complétée)"""
        if self.temporary_requests > 0:
            self.temporary_requests -= 1
        self.completed_requests += 1
        self.last_activity = datetime.now()

    def cancel_request(self):
        """Annule une requête (décrémente seulement le temporaire)"""
        if self.temporary_requests > 0:
            self.temporary_requests -= 1
        self.last_activity = datetime.now()

    def reset(self):
        """Réinitialise le quota à 0 (temporaires et complétées)"""
        self.temporary_requests = 0
        self.completed_requests = 0
        self.last_activity = datetime.now()

    def is_partial(self) -> bool:
        """Vérifie si le quota est partiel (complétées entre 0 et max)"""
        return 0 < self.completed_requests < self.max_requests

    def is_full(self) -> bool:
        """Vérifie si le quota est plein (complétées >= max)"""
        return self.completed_requests >= self.max_requests

    def get_total(self) -> int:
        """Retourne le total (temporaires + complétées)"""
        return self.temporary_requests + self.completed_requests


# Clés de quota
GAME_SERVER_QUOTA_KEY = "game_server"  # CONNECT vers IP (x.x.x.x)
GET_QUOTA_KEY = "get"  # GET (ex. ipinfo.io/ip) : 2 max par interface

# Après N échecs consécutifs du script de reset : quarantaine (plus de reset auto, hors pool).
MAX_CONSECUTIVE_RESET_FAILURES = 5


def _host_is_ip_only(host: str) -> bool:
    """True si host est une adresse IP (chiffres et points uniquement, pas de lettres). Exclut les noms (waf, awswaf, etc.)."""
    if not host or not host.strip():
        return False
    return host.strip().replace(".", "").isdigit()


class InterfaceQuotaManager:
    """Gestionnaire de quotas par interface.

    - GET : 2 requêtes max par interface (ex. ipinfo.io/ip).
    - CONNECT vers une IP (x.x.x.x, sans lettres) : 2 requêtes max par interface.
    Les CONNECT vers des noms (haapi.ankama.com, waf, etc.) ne sont pas comptées.
    Le reset est déclenché dès que le quota CONNECT game server atteint 2/2.
    """

    def __init__(
        self,
        egress_configs: list,
        quota_timeout_seconds: float = 60.0,
        max_requests_per_quota: int = 2,
    ):
        """
        Args:
            egress_configs: Liste de dicts avec 'name' et 'ip' pour les proxies disponibles
            quota_timeout_seconds: Timeout pour réinitialiser les quotas partiels (défaut: 60s)
            max_requests_per_quota: Nombre maximum de requêtes par quota (défaut: 2)
        """
        self.egress_configs = egress_configs.copy()
        # Structure: {interface_name: {request_type: {domain: QuotaInfo}}}
        # Exemple: {"Clé 101": {"GET": {"ipinfo.io:80": QuotaInfo(2)}}}
        self.quotas: Dict[str, Dict[str, Dict[str, QuotaInfo]]] = {}
        self.available_interfaces: list = (
            egress_configs.copy()
        )  # Interfaces disponibles
        self.resetting_interfaces: set = set()  # Interfaces en cours de reset
        self._lock = asyncio.Lock()
        self.quota_timeout_seconds = quota_timeout_seconds
        self.max_requests_per_quota = max_requests_per_quota
        self._cleanup_task: Optional[asyncio.Task] = None
        self._retry_reset_task: Optional[asyncio.Task] = None
        self._reset_callback: Optional[Callable] = (
            None  # Callback pour déclencher le reset dans ProxyZ
        )
        self._usage_callback: Optional[Callable[[str, bool], None]] = (
            None  # (interface_name, in_use) pour le badge RESET / In use
        )
        # Mapping pour suivre les connexions actives et leurs quotas
        self._active_connections: Dict[int, Dict] = (
            {}
        )  # {connection_id: {interface_name, request_type, domain_key}}
        # Nombre de GET "en attente" d'un CONNECT pour chaque interface.
        # Idée : un GET réussi ne compte comme "complété" qu'une fois apparié
        # avec un CONNECT (game server) réussi sur la même clé.
        self._pending_gets: Dict[str, int] = {}
        # Compteur d'échecs consécutifs par interface : après 3 échecs → retrait du pool + reset
        self._interface_failure_count: Dict[str, int] = {}
        # Clés retirées du pool (3 échecs) : retry reset toutes les 30s jusqu'à remise en pool
        self._keys_removed_from_pool: set = set()
        self._consecutive_reset_failures: Dict[str, int] = {}
        self._quarantine_interfaces: set = set()
        self._pool_health_task: Optional[asyncio.Task] = None
        # Event pour réveiller les requêtes en attente quand une interface redevient disponible
        self._interface_available_event = asyncio.Event()
        if self.available_interfaces:
            self._interface_available_event.set()

    async def wait_for_interface_available(self, timeout: float = 120.0) -> None:
        """Attend qu'au moins une interface soit disponible (ex. après un reset). Timeout en secondes."""
        if self.available_interfaces:
            return
        try:
            await asyncio.wait_for(
                self._interface_available_event.wait(), timeout=timeout
            )
        except asyncio.TimeoutError:
            pass

    def _interface_available_event_set(self) -> None:
        """À appeler quand une interface est ajoutée à available_interfaces."""
        self._interface_available_event.set()

    def _interface_available_event_clear_if_empty(self) -> None:
        """À appeler quand on retire une interface ; clear l'event si plus aucune dispo."""
        if not self.available_interfaces:
            self._interface_available_event.clear()

    def _is_important_request(self, request_type: str, host: str, port: int) -> bool:
        """Compte : GET (tous), et CONNECT vers une IP (chiffres et points uniquement)."""
        if request_type == "GET":
            return True
        if request_type == "CONNECT":
            return _host_is_ip_only(host)
        return False

    def _get_quota_key_for_important(
        self, request_type: str, host: str, port: int
    ) -> str:
        """Clé de quota : GET → 'get', CONNECT IP → 'game_server'."""
        if request_type == "GET":
            return GET_QUOTA_KEY
        return GAME_SERVER_QUOTA_KEY

    def set_reset_callback(self, callback: Callable):
        """Définit le callback pour déclencher le reset dans ProxyZ avec animation"""
        self._reset_callback = callback

    def set_usage_callback(self, callback: Callable[[str, bool], None]):
        """Définit le callback (interface_name, in_use) pour le badge RESET / In use"""
        self._usage_callback = callback

    async def start_cleanup_task(self):
        """Démarre la tâche de nettoyage des quotas partiels"""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_partial_quotas())

    async def start_retry_reset_task(self):
        """Démarre la tâche qui retente le reset toutes les 30s pour les clés retirées du pool."""
        if self._retry_reset_task is None or self._retry_reset_task.done():
            self._retry_reset_task = asyncio.create_task(self._retry_reset_loop())

    async def start_pool_health_task(self):
        """Surveillance du pool ZRotate : toutes les 30s, clés sans accès Internet (IP publique) retirées."""
        if self._pool_health_task is None or self._pool_health_task.done():
            self._pool_health_task = asyncio.create_task(self._pool_health_loop())

    async def _retry_reset_loop(self):
        """Toutes les 30s, relance un reset pour les clés hors pool tant qu'elles n'ont pas été remises."""
        while True:
            try:
                await asyncio.sleep(30)
                async with self._lock:
                    for key_name in list(self._keys_removed_from_pool):
                        if key_name in self._quarantine_interfaces:
                            continue
                        if key_name in (a["name"] for a in self.available_interfaces):
                            continue  # déjà remise
                        if not self._reset_callback:
                            continue
                        self.resetting_interfaces.add(key_name)
                        try:
                            self._reset_callback.reset_interface(key_name)
                            logger.info(
                                f"[QUOTA] 🔄 Nouvelle tentative de reset pour {key_name} (toutes les 30s)"
                            )
                        except Exception as e:
                            logger.error(f"[QUOTA] Erreur retry reset {key_name}: {e}")
                            self.resetting_interfaces.discard(key_name)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[QUOTA] Erreur dans _retry_reset_loop: {e}")

    async def _pool_health_loop(self):
        """Toutes les 30s : retire du pool les clés qui n'obtiennent pas d'IP publique via leur egress."""
        while True:
            try:
                await asyncio.sleep(30)
                async with self._lock:
                    snapshot = [
                        dict(x)
                        for x in self.available_interfaces
                        if x.get("name") not in self.resetting_interfaces
                        and x.get("name") not in self._quarantine_interfaces
                    ]
                n_snap = len(snapshot)
                if n_snap == 0:
                    logger.info(
                        "[QUOTA] Health pool: contrôle connectivité (ipify) — "
                        "aucune clé à vérifier (pool vide ou clés en reset/quarantaine)"
                    )
                else:
                    names_preview = ", ".join(
                        (x.get("name") or "?") for x in snapshot[:8]
                    )
                    if n_snap > 8:
                        names_preview += f", … (+{n_snap - 8})"
                    logger.info(
                        f"[QUOTA] Health pool: contrôle (IP locale système puis ipify) pour "
                        f"{n_snap} clé(s): {names_preview}"
                    )
                for info in snapshot:
                    name = info.get("name") or ""
                    ip = info.get("ip") or ""
                    is_remote = bool(info.get("remote"))
                    proxy_port = info.get("proxy_port")
                    if not name:
                        continue
                    if is_remote:
                        if not proxy_port:
                            continue
                        ok = await async_check_egress_public_internet(
                            "", timeout=10.0, proxy_port=int(proxy_port)
                        )
                        if ok:
                            logger.info(
                                f"[QUOTA] Health pool: ✅ {name} (relais 127.0.0.1:{proxy_port}) — Internet OK"
                            )
                            continue
                        logger.warning(
                            f"[QUOTA] Health pool: ❌ {name} (relais 127.0.0.1:{proxy_port}) — test ipify échoué"
                        )
                        async with self._lock:
                            before = [a["name"] for a in self.available_interfaces]
                            if name not in before or name in self.resetting_interfaces:
                                continue
                            self.available_interfaces = [
                                i for i in self.available_interfaces if i["name"] != name
                            ]
                            self._interface_available_event_clear_if_empty()
                            self._keys_removed_from_pool.add(name)
                            logger.warning(
                                f"[QUOTA] ⚠️ {name} retirée du pool ZRotate (relais distant inaccessible)"
                            )
                        continue

                    if not ip:
                        continue
                    local_ok = await asyncio.to_thread(local_ipv4_assigned_on_host, ip)
                    if not local_ok:
                        logger.warning(
                            f"[QUOTA] Health pool: ⏸️ {name} ({ip}) — IP locale absente "
                            f"(clé débranchée ou interface inactive), retrait du pool sans test Internet ni reset"
                        )
                        async with self._lock:
                            before = [a["name"] for a in self.available_interfaces]
                            if name not in before or name in self.resetting_interfaces:
                                continue
                            self.available_interfaces = [
                                i
                                for i in self.available_interfaces
                                if i["name"] != name
                            ]
                            self._interface_available_event_clear_if_empty()
                        continue

                    ok = await async_check_egress_public_internet(ip, timeout=10.0)
                    if ok:
                        logger.info(
                            f"[QUOTA] Health pool: ✅ {name} ({ip}) — accès Internet / IP publique OK"
                        )
                        continue
                    logger.warning(
                        f"[QUOTA] Health pool: ❌ {name} ({ip}) — test ipify échoué, "
                        f"tentative de retrait du pool / reset"
                    )
                    async with self._lock:
                        before = [a["name"] for a in self.available_interfaces]
                        if name not in before or name in self.resetting_interfaces:
                            continue
                        self.available_interfaces = [
                            i for i in self.available_interfaces if i["name"] != name
                        ]
                        self._interface_available_event_clear_if_empty()
                        self._keys_removed_from_pool.add(name)
                        logger.warning(
                            f"[QUOTA] ⚠️ {name} retirée du pool ZRotate (pas d'IP publique / pas d'accès Internet)"
                        )
                        if self._reset_callback:
                            try:
                                self.resetting_interfaces.add(name)
                                self._reset_callback.reset_interface(name)
                                logger.info(
                                    f"[QUOTA] 🔄 Reset déclenché pour {name} (contrôle connectivité)"
                                )
                            except Exception as e:
                                logger.error(
                                    f"[QUOTA] Erreur callback reset (health): {e}"
                                )
                                self.resetting_interfaces.discard(name)
                    await self.start_retry_reset_task()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[QUOTA] Erreur dans _pool_health_loop: {e}")

    async def _cleanup_partial_quotas(self):
        """
        Après 60s sans activité sur un quota partiel, on réinitialise TOUS les quotas
        de cette interface à 0 (y compris GET si présent, pour repartir à zéro).
        """
        while True:
            try:
                await asyncio.sleep(10)  # Vérifier toutes les 10 secondes
                async with self._lock:
                    now = datetime.now()

                    for interface_name, request_types in list(self.quotas.items()):
                        if interface_name in self.resetting_interfaces:
                            continue  # Ne pas nettoyer les interfaces en reset

                        # Chercher si un quota partiel est inactif depuis trop longtemps
                        should_reset_interface = False
                        for request_type, domains in list(request_types.items()):
                            for domain, quota_info in list(domains.items()):
                                # Quota partiel classique (complétées entre 0 et max)
                                if quota_info.is_partial():
                                    inactivity_delta = (
                                        now - quota_info.last_activity
                                    ).total_seconds()
                                    if inactivity_delta >= self.quota_timeout_seconds:
                                        should_reset_interface = True
                                        break
                            if should_reset_interface:
                                break

                        # Nouveau cas : uniquement des GET complétés en attente de CONNECT
                        # (pending_gets > 0) sans activité depuis longtemps → on remet la clé à zéro.
                        if (
                            not should_reset_interface
                            and self._pending_gets.get(interface_name, 0) > 0
                        ):
                            # On utilise l'activité du quota CONNECT game_server si présent,
                            # sinon celle du quota GET, à défaut on applique le timeout directement.
                            last_activity_dt = None
                            connect_quota = (
                                self.quotas.get(interface_name, {})
                                .get("CONNECT", {})
                                .get(GAME_SERVER_QUOTA_KEY)
                            )
                            get_quota = (
                                self.quotas.get(interface_name, {})
                                .get("GET", {})
                                .get(GET_QUOTA_KEY)
                            )
                            if connect_quota:
                                last_activity_dt = connect_quota.last_activity
                            elif get_quota:
                                last_activity_dt = get_quota.last_activity

                            if last_activity_dt is None:
                                inactivity_delta = self.quota_timeout_seconds
                            else:
                                inactivity_delta = (
                                    now - last_activity_dt
                                ).total_seconds()

                            if inactivity_delta >= self.quota_timeout_seconds:
                                should_reset_interface = True

                        if should_reset_interface:
                            self._request_interface_reset(
                                interface_name, "timeout quota partiel"
                            )
            except Exception as e:
                logger.error(f"[QUOTA] Erreur dans cleanup: {e}")

    def _request_interface_reset(self, interface_name: str, reason: str):
        """Retire l'interface du pool et déclenche un reset réel avant retour à 0/0."""
        if interface_name in self.resetting_interfaces:
            return
        if interface_name in self._quarantine_interfaces:
            logger.warning(
                f"[QUOTA] Reset ignoré pour {interface_name} ({reason}) — interface en quarantaine"
            )
            return
        self.resetting_interfaces.add(interface_name)
        self.available_interfaces = [
            i for i in self.available_interfaces if i["name"] != interface_name
        ]
        self._interface_available_event_clear_if_empty()
        logger.info(
            f"[QUOTA] 🔄 Reset demandé pour {interface_name} ({reason})"
        )
        if self._reset_callback:
            try:
                self._reset_callback.reset_interface(interface_name)
                logger.info(
                    f"[QUOTA] Reset déclenché via callback pour {interface_name}"
                )
            except Exception as e:
                logger.error(f"[QUOTA] Erreur callback reset: {e}")
                logger.error(traceback.format_exc())
                asyncio.create_task(self._reset_interface_direct(interface_name))
        else:
            logger.warning(
                f"[QUOTA] Aucun callback défini, reset direct pour {interface_name}"
            )
            asyncio.create_task(self._reset_interface_direct(interface_name))

    def _get_quota_key(self, request_type: str, host: str, port: int) -> str:
        """Génère une clé de domaine pour le quota"""
        return f"{host}:{port}"

    async def get_interface_for_request(
        self, request_type: str, host: str, port: int, connection_id: int
    ) -> Optional[Dict[str, str]]:
        """
        Récupère une interface disponible pour une requête donnée.
        Incrémente le quota temporaire si c'est une requête importante.

        Args:
            request_type: Type de requête ("GET", "CONNECT", etc.)
            host: Host de destination
            port: Port de destination
            connection_id: ID de la connexion pour suivre les quotas temporaires

        Returns:
            Dict avec 'name' et 'ip' de l'interface, ou None si aucune disponible
        """
        async with self._lock:
            # Vérifier si c'est une requête importante (seulement celles-ci sont comptées)
            is_important = self._is_important_request(request_type, host, port)

            if not is_important:
                # Requête non importante : prioriser les interfaces avec le moins de connexions actives
                eligible = [
                    (
                        sum(
                            1
                            for c in self._active_connections.values()
                            if c["interface_name"] == info["name"]
                        ),
                        info,
                    )
                    for info in self.available_interfaces
                    if info["name"] not in self.resetting_interfaces
                    and info["name"] not in self._quarantine_interfaces
                ]
                if not eligible:
                    return None
                eligible.sort(key=lambda x: x[0])
                interface_info = eligible[0][1]
                interface_name = interface_info["name"]
                # Tracker la connexion pour répartir la charge (éviter qu'une seule clé prenne tout)
                # Ne pas mettre le badge "In use" pour les tunnels CONNECT hostname : ils restent ouverts longtemps
                self._active_connections[connection_id] = {
                    "interface_name": interface_name,
                    "is_important": False,
                }
                logger.debug(
                    f"[QUOTA] Requête non importante {request_type} {host}:{port} → {interface_name} (priorité charge)"
                )
                return interface_info

            # Requête importante (CONNECT game server) : une seule clé "game_server"
            domain_key = self._get_quota_key_for_important(request_type, host, port)

            # Construire la liste des interfaces éligibles avec leur quota pour ce domaine
            candidates = []
            for interface_info in self.available_interfaces:
                interface_name = interface_info["name"]

                if interface_name in self.resetting_interfaces:
                    continue
                if interface_name in self._quarantine_interfaces:
                    continue

                if interface_name not in self.quotas:
                    self.quotas[interface_name] = {}
                if request_type not in self.quotas[interface_name]:
                    self.quotas[interface_name][request_type] = {}
                if domain_key not in self.quotas[interface_name][request_type]:
                    self.quotas[interface_name][request_type][domain_key] = QuotaInfo(
                        self.max_requests_per_quota
                    )

                quota_info = self.quotas[interface_name][request_type][domain_key]
                # Gestion spécifique des quotas importants :
                # - GET : limité par (temporaires + complétées GET + GET en attente d'un CONNECT)
                # - CONNECT (game server) : limité uniquement par le quota CONNECT (ignorer les GET en attente)
                pending_gets = self._pending_gets.get(interface_name, 0)
                if request_type == "GET":
                    used = (
                        quota_info.temporary_requests
                        + quota_info.completed_requests
                        + pending_gets
                    )
                else:
                    # CONNECT vers IP (game server) ou autre requête importante
                    used = quota_info.temporary_requests + quota_info.completed_requests

                if used < quota_info.max_requests:
                    # Prioriser celles qui ont le moins de charge "importante" pour ce type
                    candidates.append(
                        (used, interface_info, quota_info, interface_name)
                    )

            # Stratégie de sélection :
            # - CONNECT game server : prioriser les quotas déjà avancés (1/2 avant 0/2)
            #   pour atteindre 2/2 plus vite et déclencher le reset plus tôt.
            # - Autres requêtes importantes : conserver la distribution équilibrée (charge croissante).
            if request_type == "CONNECT" and domain_key == GAME_SERVER_QUOTA_KEY:
                candidates.sort(key=lambda x: x[0], reverse=True)
                selection_reason = "priorité quota CONNECT avancé"
                candidates_debug = ", ".join(
                    f"{name}:{used}/{quota.max_requests}"
                    for used, _info, quota, name in candidates
                )
                logger.info(
                    f"[QUOTA] 📊 Ordre candidates CONNECT {domain_key} (priorité 1/2 puis 0/2): {candidates_debug}"
                )
            else:
                candidates.sort(key=lambda x: x[0])
                selection_reason = "priorité charge"

            if candidates:
                _total_used, interface_info, quota_info, interface_name = candidates[0]
                quota_info.start_request()
                logger.info(
                    f"[QUOTA] 🚀 {interface_name}: Démarrage {request_type} {domain_key} → Temporaire: {quota_info.temporary_requests}, Complétée: {quota_info.completed_requests}/{quota_info.max_requests} ({selection_reason})"
                )

                self._active_connections[connection_id] = {
                    "interface_name": interface_name,
                    "request_type": request_type,
                    "domain_key": domain_key,
                    "is_important": True,
                }

                if self._usage_callback:
                    try:
                        self._usage_callback(interface_name, True)
                    except Exception:
                        pass

                await self.start_cleanup_task()
                return interface_info

            # Aucune interface disponible avec quota temporaire disponible
            logger.warning(
                f"[QUOTA] Aucune interface disponible avec quota temporaire pour {request_type} {domain_key}"
            )
            return None

    async def complete_request(self, connection_id: int, success: bool = True):
        """
        Marque une requête comme terminée.
        Si succès : décrémente le quota temporaire et incrémente le quota complété.
        Si échec : décrémente seulement le quota temporaire.

        Args:
            connection_id: ID de la connexion
            success: True si la requête s'est terminée avec succès
        """
        async with self._lock:
            if connection_id not in self._active_connections:
                # Connexion déjà traitée ou non importante - ignorer silencieusement
                return

            conn_info = self._active_connections.pop(connection_id)
            interface_name = conn_info["interface_name"]
            # Connexion non importante (CONNECT hostname, etc.) : pas de quota, juste libérer le slot
            if not conn_info.get("is_important", True):
                # Ne compter que les connexions importantes pour le badge "In use" (pas les tunnels haapi)
                still_in_use = any(
                    c["interface_name"] == interface_name
                    and c.get("is_important", True)
                    for c in self._active_connections.values()
                )
                if not still_in_use and self._usage_callback:
                    try:
                        self._usage_callback(interface_name, False)
                    except Exception:
                        pass
                return

            request_type = conn_info["request_type"]
            domain_key = conn_info["domain_key"]

            if interface_name not in self.quotas:
                logger.warning(
                    f"[QUOTA] ⚠️ Interface {interface_name} n'existe pas dans quotas"
                )
                return
            if request_type not in self.quotas[interface_name]:
                logger.warning(
                    f"[QUOTA] ⚠️ Type {request_type} n'existe pas pour {interface_name}"
                )
                return
            if domain_key not in self.quotas[interface_name][request_type]:
                logger.warning(
                    f"[QUOTA] ⚠️ Domaine {domain_key} n'existe pas pour {interface_name}/{request_type}"
                )
                return

            quota_info = self.quotas[interface_name][request_type][domain_key]

            if success:
                # Requête réussie : logique différente pour GET et CONNECT importants
                self._interface_failure_count[interface_name] = 0

                # Gestion spéciale des quotas importants :
                # - GET (GET_QUOTA_KEY) : reste "en attente" tant qu'il n'y a pas un CONNECT game server correspondant.
                # - CONNECT game server (GAME_SERVER_QUOTA_KEY) : consomme d'abord un GET en attente s'il existe.
                if request_type == "GET" and domain_key == GET_QUOTA_KEY:
                    # GET réussi : libère le temporaire, mais n'incrémente pas directement "complétée".
                    # On le marque comme GET en attente d'un CONNECT.
                    if quota_info.temporary_requests > 0:
                        quota_info.temporary_requests -= 1
                    self._pending_gets[interface_name] = (
                        self._pending_gets.get(interface_name, 0) + 1
                    )
                    quota_info.last_activity = datetime.now()
                    logger.info(
                        f"[QUOTA] ✅ {interface_name}: GET {domain_key} terminé → GET en attente: {self._pending_gets[interface_name]}, Temporaire: {quota_info.temporary_requests}"
                    )
                elif request_type == "CONNECT" and domain_key == GAME_SERVER_QUOTA_KEY:
                    # CONNECT game server réussi : libère le temporaire et consomme d'abord un GET en attente
                    if quota_info.temporary_requests > 0:
                        quota_info.temporary_requests -= 1

                    pending = self._pending_gets.get(interface_name, 0)
                    if pending > 0:
                        self._pending_gets[interface_name] = pending - 1
                        quota_info.completed_requests += 1
                        logger.info(
                            f"[QUOTA] ✅ {interface_name}: CONNECT {domain_key} apparié avec un GET → Complétées: {quota_info.completed_requests}/{quota_info.max_requests}, GET en attente restant: {self._pending_gets[interface_name]}"
                        )
                    else:
                        # Aucun GET en attente : compter le CONNECT directement
                        quota_info.completed_requests += 1
                        logger.info(
                            f"[QUOTA] ✅ {interface_name}: CONNECT {domain_key} terminé (sans GET en attente) → Complétées: {quota_info.completed_requests}/{quota_info.max_requests}"
                        )

                    quota_info.last_activity = datetime.now()
                    # Le reset n'est déclenché que par le quota CONNECT game server
                    await self._check_and_reset_if_needed(interface_name)
                else:
                    # Cas générique (autres quotas éventuels) : comportement standard
                    quota_info.complete_request()
                    logger.info(
                        f"[QUOTA] ✅ {interface_name}: {request_type} {domain_key} terminée avec succès → Temporaire: {quota_info.temporary_requests}, Complétée: {quota_info.completed_requests}/{quota_info.max_requests}"
                    )
            else:
                # Requête échouée : décrémenter seulement le temporaire
                quota_info.cancel_request()

                # Ne pas compter comme échec si l'interface est déjà en reset : les déconnexions
                # (ex. ConnectionResetError WinError 64 "nom réseau plus disponible") sont normales
                # quand le modem est réinitialisé et ne doivent pas déclencher retrait du pool.
                if interface_name in self.resetting_interfaces:
                    logger.debug(
                        f"[QUOTA] {interface_name}: {request_type} {domain_key} fermée pendant reset (non comptée comme échec)"
                    )
                else:
                    fail_count = (
                        self._interface_failure_count.get(interface_name, 0) + 1
                    )
                    self._interface_failure_count[interface_name] = fail_count
                    logger.info(
                        f"[QUOTA] ❌ {interface_name}: {request_type} {domain_key} annulée/échouée → Temporaire: {quota_info.temporary_requests}, Complétée: {quota_info.completed_requests}/{quota_info.max_requests} (échec {fail_count}/3)"
                    )

                    # Après 3 échecs : retirer la clé du pool et lancer un reset (retry toutes les 30s si échec)
                    if fail_count >= 3:
                        self.available_interfaces = [
                            i
                            for i in self.available_interfaces
                            if i["name"] != interface_name
                        ]
                        self._interface_available_event_clear_if_empty()
                        self.resetting_interfaces.add(interface_name)
                        self._keys_removed_from_pool.add(interface_name)
                        logger.warning(
                            f"[QUOTA] ⚠️ {interface_name} retirée du pool après 3 échecs → reset automatique"
                        )
                        if self._reset_callback:
                            try:
                                self._reset_callback.reset_interface(interface_name)
                            except Exception as e:
                                logger.error(f"[QUOTA] Erreur callback reset: {e}")
                                self.resetting_interfaces.discard(interface_name)
                        await self.start_retry_reset_task()

            # Si plus aucune connexion importante pour cette interface, notifier "disponible" (ignorer tunnels haapi)
            still_in_use = any(
                c["interface_name"] == interface_name and c.get("is_important", True)
                for c in self._active_connections.values()
            )
            if not still_in_use and self._usage_callback:
                try:
                    self._usage_callback(interface_name, False)
                except Exception:
                    pass

    async def _check_and_reset_if_needed(self, interface_name: str):
        """Déclenche le reset uniquement quand le quota CONNECT game server est plein (2/2)."""
        if interface_name in self.resetting_interfaces:
            return

        if interface_name not in self.quotas:
            self.quotas[interface_name] = {}
        if "CONNECT" not in self.quotas[interface_name]:
            self.quotas[interface_name]["CONNECT"] = {}
        if GAME_SERVER_QUOTA_KEY not in self.quotas[interface_name]["CONNECT"]:
            self.quotas[interface_name]["CONNECT"][GAME_SERVER_QUOTA_KEY] = QuotaInfo(
                self.max_requests_per_quota
            )

        quota_info = self.quotas[interface_name]["CONNECT"][GAME_SERVER_QUOTA_KEY]
        if not quota_info.is_full():
            logger.debug(
                f"[QUOTA] CONNECT game_server {quota_info.completed_requests}/{quota_info.max_requests} pour {interface_name}, pas de reset"
            )
            return

        logger.info(
            f"[QUOTA] ✅ Quota CONNECT game server plein pour {interface_name}, reset requis"
        )
        self._request_interface_reset(interface_name, "quota CONNECT plein")

    async def _reset_interface_direct(self, interface_name: str):
        """Reset direct d'une interface via script (fallback si pas de callback)"""
        try:
            # Port et script : depuis egress_configs si fournis (depuis ZRotate GUI), sinon fallback
            entry = next(
                (c for c in self.egress_configs if c.get("name") == interface_name),
                None,
            )
            proxy_port = entry.get("proxy_port") if entry else None
            if proxy_port is None:
                match = re.search(r"(\d+)", interface_name)
                if not match:
                    logger.error(
                        f"[QUOTA] Impossible d'extraire le port depuis '{interface_name}'"
                    )
                    return
                proxy_port = int(match.group(1))

            script_path = None
            if entry and entry.get("reset_script_path"):
                script_path = Path(entry["reset_script_path"])
            else:
                app_dir = get_app_dir()
                script_path = resolve_reset_script_path(DEFAULT_RESET_SCRIPT, app_dir)

            logger.info(
                f"[QUOTA] 🔄 Reset direct de {interface_name} (port {proxy_port})..."
            )

            reset_ok = False
            reset_options = (entry or {}).get("reset_options") or {}
            if script_path.exists() or is_playwright_reset_script(script_path):
                result = await asyncio.to_thread(
                    run_reset_script,
                    script_path,
                    proxy_port,
                    120,
                    None,
                    reset_options or None,
                )

                if result == 0:
                    reset_ok = True
                    logger.info(f"[QUOTA] ✅ Reset réussi pour {interface_name}")
                else:
                    logger.warning(
                        f"[QUOTA] ⚠️ Reset échoué pour {interface_name} (code: {result})"
                    )
            else:
                logger.error(f"[QUOTA] ❌ Script reset introuvable: {script_path}")

            # Réinitialiser tous les quotas et remettre l'interface disponible
            await self._release_interface_after_reset(interface_name, reset_ok)

        except Exception as e:
            logger.error(f"[QUOTA] ❌ Erreur lors du reset de {interface_name}: {e}")
            import traceback

            logger.error(traceback.format_exc())
            await self._release_interface_after_reset(interface_name, False)

    async def get_quota_stats(self) -> dict:
        """
        Retourne un snapshot des quotas par interface pour l'UI.
        Dict[interface_name, {"get": (used, max), "connect": (used, max)}].
        used = completed_requests + temporary_requests.
        """
        async with self._lock:
            result = {}
            for info in self.egress_configs:
                name = info["name"]
                get_used, get_max = 0, self.max_requests_per_quota
                connect_used, connect_max = 0, self.max_requests_per_quota
                if name in self.quotas:
                    # GET : on affiche les GET en attente + les temporaires (les GET déjà "appariés"
                    # avec un CONNECT sont comptés côté CONNECT).
                    if (
                        "GET" in self.quotas[name]
                        and GET_QUOTA_KEY in self.quotas[name]["GET"]
                    ):
                        q = self.quotas[name]["GET"][GET_QUOTA_KEY]
                        pending_gets = self._pending_gets.get(name, 0)
                        get_used = q.temporary_requests + pending_gets
                        get_max = q.max_requests
                    if (
                        "CONNECT" in self.quotas[name]
                        and GAME_SERVER_QUOTA_KEY in self.quotas[name]["CONNECT"]
                    ):
                        q = self.quotas[name]["CONNECT"][GAME_SERVER_QUOTA_KEY]
                        connect_used = q.completed_requests + q.temporary_requests
                        connect_max = q.max_requests
                result[name] = {
                    "get": (get_used, get_max),
                    "connect": (connect_used, connect_max),
                }
            return result

    async def get_quarantine_snapshot(self) -> list[str]:
        """Noms d'interfaces en quarantaine (ZRotate), triés pour l'UI."""
        async with self._lock:
            return sorted(self._quarantine_interfaces)

    async def get_pool_ui_snapshot(self) -> Dict[str, Dict[str, bool]]:
        """
        État runtime du pool ZRotate pour colorer la liste (clés du dernier démarrage).
        Clés = egress_configs du serveur actif.
        """
        async with self._lock:
            in_pool = {x["name"] for x in self.available_interfaces}
            out: Dict[str, Dict[str, bool]] = {}
            for cfg in self.egress_configs:
                n = cfg["name"]
                out[n] = {
                    "in_pool": n in in_pool,
                    "resetting": n in self.resetting_interfaces,
                    "quarantine": n in self._quarantine_interfaces,
                }
            return out

    async def release_interface_after_reset(
        self, interface_name: str, reset_succeeded: bool = True
    ):
        """Remet une interface en disponibilité après reset (appelé depuis ProxyZ)"""
        await self._release_interface_after_reset(interface_name, reset_succeeded)

    async def _release_interface_after_reset(
        self, interface_name: str, reset_succeeded: bool = True
    ):
        """Remet une interface en disponibilité après reset (manuel ou ZRotate) et réinitialise tous ses quotas."""
        async with self._lock:
            # Réinitialiser tous les quotas de l'interface (CONNECT game_server, GET, etc.)
            new_quotas_str = []
            if interface_name in self.quotas:
                for request_type in list(self.quotas[interface_name].keys()):
                    for domain in list(
                        self.quotas[interface_name][request_type].keys()
                    ):
                        q = self.quotas[interface_name][request_type][domain]
                        q.reset()
                        new_quotas_str.append(
                            f"{request_type} {domain}: {q.completed_requests}/{q.max_requests}"
                        )

            # Réinitialiser aussi les GET en attente d'un CONNECT pour cette interface
            if interface_name in self._pending_gets:
                self._pending_gets[interface_name] = 0

            # Log cohérent avec l'issue réelle du reset.
            if reset_succeeded:
                if new_quotas_str:
                    logger.info(
                        f"[QUOTA] ✅ Reset réussi pour {interface_name} - "
                        f"Nouveaux quotas: {', '.join(new_quotas_str)}"
                    )
                else:
                    logger.info(
                        f"[QUOTA] ✅ Reset réussi pour {interface_name} - Aucun quota à réinitialiser (déjà à zéro)"
                    )
            else:
                logger.warning(
                    f"[QUOTA] ⚠️ Reset échoué pour {interface_name} - quotas remis à zéro pour éviter le blocage du pool"
                )

            # Retirer de la liste des interfaces en reset
            self.resetting_interfaces.discard(interface_name)

            interface_info = next(
                (i for i in self.egress_configs if i["name"] == interface_name), None
            )

            if reset_succeeded:
                self._consecutive_reset_failures.pop(interface_name, None)
                self._quarantine_interfaces.discard(interface_name)
                if interface_info:
                    names_in_available = [a["name"] for a in self.available_interfaces]
                    if interface_name not in names_in_available:
                        self.available_interfaces.append(interface_info)
                        self._interface_available_event_set()
                        self._interface_failure_count[interface_name] = 0
                        self._keys_removed_from_pool.discard(interface_name)
                        logger.info(
                            f"[QUOTA] ✅ Interface {interface_name} remise en disponibilité (reset OK)"
                        )
            elif not reset_succeeded:
                logger.warning(
                    f"[QUOTA] ⚠️ {interface_name} non remise dans le pool ZRotate (reset échoué ou incomplet)"
                )
                self.available_interfaces = [
                    i for i in self.available_interfaces if i["name"] != interface_name
                ]
                self._interface_available_event_clear_if_empty()
                n = self._consecutive_reset_failures.get(interface_name, 0) + 1
                self._consecutive_reset_failures[interface_name] = n
                if n >= MAX_CONSECUTIVE_RESET_FAILURES:
                    already_q = interface_name in self._quarantine_interfaces
                    self._quarantine_interfaces.add(interface_name)
                    self._keys_removed_from_pool.discard(interface_name)
                    if not already_q:
                        logger.error(
                            f"[QUOTA] 🛑 {interface_name} en quarantaine après {n} échec(s) de reset consécutifs "
                            f"(seuil {MAX_CONSECUTIVE_RESET_FAILURES}) — plus de reset automatique, clé hors pool"
                        )
                else:
                    self._keys_removed_from_pool.add(interface_name)
                    await self.start_retry_reset_task()
                    if self._reset_callback and interface_info:
                        try:
                            self.resetting_interfaces.add(interface_name)
                            self._reset_callback.reset_interface(interface_name)
                            logger.info(
                                f"[QUOTA] 🔄 Nouvelle tentative de reset pour {interface_name} (suite à échec, {n}/{MAX_CONSECUTIVE_RESET_FAILURES})"
                            )
                        except Exception as e:
                            logger.error(
                                f"[QUOTA] Erreur callback reset après échec: {e}"
                            )
                            self.resetting_interfaces.discard(interface_name)

            # Notifier l'UI : interface à nouveau disponible (badge RESET)
            if self._usage_callback:
                try:
                    self._usage_callback(interface_name, False)
                except Exception:
                    pass


class RoundRobinEgressSelector:
    """Sélecteur d'egress en round-robin pour les clés Huawei (ancien système, conservé pour compatibilité)"""

    def __init__(self, egress_configs: list):
        """
        Args:
            egress_configs: Liste de dicts avec 'name' et 'ip'
        """
        self.egress_configs = egress_configs
        self._index = 0
        self._lock = asyncio.Lock()

    async def get_egress(self) -> Dict[str, str]:
        """
        Retourne la prochaine clé en round-robin.

        Returns:
            Dict avec 'name' et 'ip'
        """
        async with self._lock:
            egress = self.egress_configs[self._index]
            self._index = (self._index + 1) % len(self.egress_configs)
            return egress


def list_network_interfaces():
    """
    Liste les interfaces réseau IPv4 disponibles.
    Utile pour identifier les IPs locales des clés Huawei.
    """
    try:
        import psutil

        interfaces = []
        for interface_name, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    interfaces.append(
                        {
                            "name": interface_name,
                            "ip": addr.address,
                            "netmask": addr.netmask,
                        }
                    )
        return interfaces
    except ImportError:
        logger.warning("psutil non disponible, utilisation de socket.getaddrinfo")
        # Fallback basique
        hostname = socket.gethostname()
        try:
            local_ip = socket.gethostbyname(hostname)
            return [{"name": "default", "ip": local_ip, "netmask": None}]
        except Exception:
            return []


def validate_egress_ip(ip: str) -> bool:
    """
    Valide qu'une IP egress peut être bindée.

    Args:
        ip: IP à valider

    Returns:
        True si l'IP est bindable, False sinon
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind((ip, 0))
        s.close()
        return True
    except OSError:
        return False


def local_ipv4_assigned_on_host(ip: str) -> bool:
    """
    True si cette IPv4 est encore présente sur une interface système.
    Permet de détecter une clé débranchée sans tenter de bind (évite WinError 10049)
    et sans lancer de reset inutile.
    """
    if not ip or not str(ip).strip():
        return False
    target = str(ip).strip()
    try:
        for _iface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET and addr.address == target:
                    return True
    except Exception:
        pass
    return False


async def open_connection_with_bind(
    host: str,
    port: int,
    source_ip: str,
    timeout: float = 10.0,
) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """
    Ouvre une connexion TCP avec bind source sur l'IP spécifiée.

    IMPORTANT: Résout le hostname en IPv4 AVANT sock_connect pour éviter WinError 10022.

    Args:
        host: Host de destination (hostname ou IP)
        port: Port de destination
        source_ip: IP locale à utiliser comme source (egress)
        timeout: Timeout de connexion en secondes

    Returns:
        Tuple (reader, writer)

    Raises:
        OSError, ConnectionRefusedError, asyncio.TimeoutError
    """
    loop = asyncio.get_running_loop()

    # Résoudre le hostname en IPv4 AVANT sock_connect
    # C'est critique sur Windows pour éviter WinError 10022
    try:
        infos = socket.getaddrinfo(
            host,
            port,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
        if not infos:
            raise OSError(f"No IPv4 address found for {host}:{port}")

        # Prendre la première adresse IPv4 résolue
        addr = infos[0][4]  # (ip, port)
        logger.debug(f"Hostname {host} résolu en {addr[0]}")
    except socket.gaierror as e:
        raise OSError(f"Failed to resolve {host}:{port}: {e}")

    # Créer un socket TCP
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)

    try:
        # Vérifier que l'IP source est valide
        try:
            socket.inet_aton(source_ip)
        except socket.error:
            raise OSError(f"IP source invalide: {source_ip}")

        # Bind sur l'IP source (port 0 = système choisit)
        # Sur Windows, bind() doit être appelé en mode bloquant
        sock.setblocking(True)
        try:
            sock.bind((source_ip, 0))
        except OSError as bind_err:
            raise OSError(
                f"Impossible de bind sur {source_ip}: {bind_err}. Vérifiez que l'IP est valide et que l'interface est active."
            )

        # Passer en mode non-bloquant APRÈS le bind
        sock.setblocking(False)

        # Connexion asynchrone avec l'adresse IPv4 résolue
        await asyncio.wait_for(
            loop.sock_connect(sock, addr),
            timeout=timeout,
        )

        # Utiliser UNIQUEMENT l'API publique d'asyncio
        reader, writer = await asyncio.open_connection(sock=sock)

        return reader, writer

    except Exception as e:
        try:
            sock.close()
        except Exception:
            pass
        logger.error(f"Erreur open_connection_with_bind: {type(e).__name__}: {e}")
        raise


async def open_connection_via_proxy(
    host: str,
    port: int,
    proxy_host: str,
    proxy_port: int,
    timeout: float = 10.0,
) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Ouvre une connexion TCP vers host:port via un proxy HTTP (CONNECT)."""
    loop = asyncio.get_running_loop()
    try:
        infos = socket.getaddrinfo(
            host,
            port,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
        if not infos:
            raise OSError(f"No IPv4 address found for {host}:{port}")
        dest_addr = infos[0][4]
    except socket.gaierror as e:
        raise OSError(f"Failed to resolve {host}:{port}: {e}")

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(proxy_host, proxy_port),
        timeout=timeout,
    )
    connect_req = (
        f"CONNECT {dest_addr[0]}:{dest_addr[1]} HTTP/1.1\r\n"
        f"Host: {dest_addr[0]}:{dest_addr[1]}\r\n\r\n"
    )
    writer.write(connect_req.encode("ascii"))
    await writer.drain()
    response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout)
    status_line = response.split(b"\r\n", 1)[0]
    if b"200" not in status_line:
        writer.close()
        await writer.wait_closed()
        raise OSError(f"Proxy CONNECT failed: {status_line.decode(errors='ignore')}")
    return reader, writer


async def async_check_egress_public_internet(
    source_ip: str, timeout: float = 10.0, proxy_port: int | None = None
) -> bool:
    """
    Vérifie qu'on peut atteindre Internet et obtenir une IP publique
    (api.ipify.org). Via bind source ou via proxy local (interfaces distantes).
    """
    reader: Optional[asyncio.StreamReader] = None
    writer: Optional[asyncio.StreamWriter] = None
    try:
        if proxy_port:
            reader, writer = await open_connection_via_proxy(
                "api.ipify.org", 80, "127.0.0.1", int(proxy_port), timeout=timeout
            )
        else:
            reader, writer = await open_connection_with_bind(
                "api.ipify.org", 80, source_ip, timeout=timeout
            )
        req = (
            "GET /?format=text HTTP/1.1\r\n"
            "Host: api.ipify.org\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(req.encode("ascii"))
        await writer.drain()
        chunks: list[bytes] = []
        while True:
            data = await asyncio.wait_for(reader.read(4096), timeout=timeout)
            if not data:
                break
            chunks.append(data)
        raw = b"".join(chunks).decode(errors="ignore")
        parts = raw.split("\r\n\r\n", 1)
        if len(parts) != 2:
            return False
        body = parts[1].strip()
        return bool(re.match(r"^\d{1,3}(\.\d{1,3}){3}$", body))
    except Exception:
        return False
    finally:
        if writer:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


def parse_connect_request(request_line: str) -> Optional[Tuple[str, int]]:
    """
    Parse une requête CONNECT pour extraire host:port.

    Args:
        request_line: Première ligne de la requête (ex: "CONNECT example.com:443 HTTP/1.1")

    Returns:
        Tuple (host, port) ou None si invalide
    """
    match = re.match(
        r"CONNECT\s+([^:\s]+):(\d+)\s+HTTP/1\.\d", request_line, re.IGNORECASE
    )
    if match:
        host = match.group(1)
        port = int(match.group(2))
        return host, port
    return None


def parse_http_proxy_request(request_lines: list) -> Optional[Dict]:
    """
    Parse une requête HTTP proxy-form (ex: "GET http://example.com/path HTTP/1.1").

    Args:
        request_lines: Liste des lignes de la requête (première ligne + headers)

    Returns:
        Dict avec 'method', 'url', 'host', 'port', 'path', 'headers', 'body_start' ou None
    """
    if not request_lines:
        return None

    # Parse première ligne
    first_line = request_lines[0]
    parts = first_line.split(None, 2)
    if len(parts) < 3:
        return None

    method = parts[0]
    url_str = parts[1]
    version = parts[2]

    # Parse URL
    try:
        parsed = urlparse(url_str)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
    except Exception:
        return None

    if not host:
        return None

    # Parse headers
    headers = {}
    body_start_idx = None

    for i, line in enumerate(request_lines[1:], start=1):
        if not line.strip():
            body_start_idx = i + 1
            break
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()

    return {
        "method": method,
        "url": url_str,
        "host": host,
        "port": port,
        "path": path,
        "headers": headers,
        "version": version,
        "body_start": body_start_idx,
    }


def rebuild_http_request(parsed: Dict, original_request: bytes) -> bytes:
    """
    Reconstruit une requête HTTP en origin-form à partir de la requête proxy-form.

    Args:
        parsed: Dict retourné par parse_http_proxy_request
        original_request: Requête originale complète (bytes)

    Returns:
        Requête reconstruite en bytes
    """
    # Construire la première ligne (origin-form)
    first_line = f"{parsed['method']} {parsed['path']} {parsed['version']}\r\n"

    # Construire le Host header correct (depuis l'URL, pas depuis les headers du client)
    host_header = parsed["host"]
    if parsed["port"] not in (80, 443):
        host_header += f":{parsed['port']}"

    # Reconstruire les headers
    headers_lines = []
    host_added = False
    for key, value in parsed["headers"].items():
        # Ignorer certains headers proxy
        if key in ("proxy-connection", "proxy-authorization"):
            continue
        # Remplacer le Host header par celui de l'URL
        if key == "host":
            headers_lines.append(f"Host: {host_header}\r\n")
            host_added = True
        # Forcer Connection: close pour simplifier (MVP)
        elif key == "connection":
            headers_lines.append("Connection: close\r\n")
        else:
            headers_lines.append(f"{key}: {value}\r\n")

    # Ajouter Host si pas déjà présent
    if not host_added:
        headers_lines.insert(0, f"Host: {host_header}\r\n")

    # Ajouter Connection: close si pas déjà présent
    if "connection" not in parsed["headers"]:
        headers_lines.append("Connection: close\r\n")

    # Reconstruire la requête
    request = (
        first_line.encode() + b"".join(h.encode() for h in headers_lines) + b"\r\n"
    )

    # Ajouter le body s'il existe (tout ce qui suit \r\n\r\n dans original_request)
    crlf_crlf_pos = original_request.find(b"\r\n\r\n")
    if crlf_crlf_pos >= 0:
        body = original_request[crlf_crlf_pos + 4 :]
        if body:
            request += body
            # Note: Si le body est partiel, le reste sera lu par pipe_data

    return request


async def read_until_double_crlf(
    reader: asyncio.StreamReader, max_bytes: int = 8192
) -> bytes:
    """
    Lit jusqu'à trouver \r\n\r\n (fin des headers HTTP).

    Returns:
        Bytes jusqu'à et incluant \r\n\r\n
    """
    buffer = b""
    while len(buffer) < max_bytes:
        chunk = await reader.read(1024)
        if not chunk:
            break
        buffer += chunk
        if b"\r\n\r\n" in buffer:
            # Trouvé, retourner jusqu'à \r\n\r\n inclus
            idx = buffer.find(b"\r\n\r\n") + 4
            return buffer[:idx]
    return buffer


async def pipe(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """
    Relay TCP robuste : pipe les données d'un reader vers un writer.
    Ferme le writer en finally pour débloquer l'autre sens du tunnel.
    """
    try:
        while True:
            data = await reader.read(BUFFER_SIZE)
            if not data:
                break
            writer.write(data)
            await writer.drain()

    except (ConnectionResetError, BrokenPipeError, OSError, asyncio.CancelledError):
        # Connexion fermée / task annulée
        pass
    finally:
        # IMPORTANT: fermer le writer pour débloquer l'autre sens du tunnel
        try:
            writer.close()
        except Exception:
            pass


async def relay_tunnel(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    """
    Tunnel bidirectionnel CONNECT:
    - lance 2 pipes
    - dès qu'un côté se termine, annule l'autre
    - ferme tout pour garantir la sortie
    """
    t1 = asyncio.create_task(pipe(client_reader, upstream_writer))  # client -> upstream
    t2 = asyncio.create_task(pipe(upstream_reader, client_writer))  # upstream -> client

    done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)

    # Annuler ce qui reste (sinon ça peut bloquer indéfiniment)
    for task in pending:
        task.cancel()

    # Attendre proprement la fin des tasks
    await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.gather(*done, return_exceptions=True)

    # Fermer explicitement (idempotent)
    try:
        upstream_writer.close()
    except Exception:
        pass
    try:
        client_writer.close()
    except Exception:
        pass


class ZRotateSingleProxyServer:
    """Serveur proxy HTTP/HTTPS avec rotation round-robin des clés Huawei"""

    def __init__(
        self,
        host: str = SERVER_HOST,
        port: int = SERVER_PORT,
        egress_configs: Optional[list] = None,
        max_requests_per_quota: int = 2,
        quota_timeout_seconds: float = 60.0,
        close_haapi_tunnel_after_seconds: float = 0.0,
    ):
        """
        Args:
            host: Adresse d'écoute
            port: Port d'écoute
            egress_configs: Liste de configs egress (défaut: EGRESS_IPS)
            max_requests_per_quota: Nombre max de requêtes GET/CONNECT par IP (défaut: 2)
            quota_timeout_seconds: Timeout pour réinitialiser les quotas partiels (défaut: 60s)
            close_haapi_tunnel_after_seconds: Si > 0, ferme les tunnels CONNECT vers haapi après ce délai (secondes). 0 = désactivé.
        """
        self.host = host
        self.port = port
        self._close_haapi_tunnel_after_seconds = max(
            0.0, float(close_haapi_tunnel_after_seconds)
        )
        egress_configs = egress_configs or EGRESS_IPS

        # Validation des egress IPs au démarrage
        valid_configs = []
        for cfg in egress_configs:
            if cfg.get("remote"):
                if cfg.get("proxy_port"):
                    valid_configs.append(cfg)
                else:
                    logger.error(
                        f"❌ Interface distante invalide (port relais manquant): {cfg['name']}"
                    )
                continue
            if validate_egress_ip(cfg["ip"]):
                valid_configs.append(cfg)
            else:
                logger.error(
                    f"❌ IP egress invalide ou non bindable: {cfg['name']} ({cfg['ip']})"
                )

        if not valid_configs:
            raise ValueError("Aucune IP egress valide. Vérifiez la configuration.")

        # Utiliser le système de quotas au lieu du round-robin simple
        self.quota_manager = InterfaceQuotaManager(
            valid_configs,
            max_requests_per_quota=max_requests_per_quota,
            quota_timeout_seconds=quota_timeout_seconds,
        )
        # Garder l'ancien sélecteur pour compatibilité (non utilisé si quota_manager est actif)
        self.egress_selector = RoundRobinEgressSelector(valid_configs)
        self.server: Optional[asyncio.Server] = None
        self.running = False
        self._connection_counter = 0
        self._use_quotas = True  # Activer le système de quotas
        # Statistiques simples
        self.total_requests: int = 0
        self.successful_requests: int = 0
        self.rejected_requests: int = 0

    async def start(self):
        """Démarre le serveur"""
        if self.running:
            return

        self.running = True
        try:
            self.server = await asyncio.start_server(
                self._handle_client, self.host, self.port
            )
        except OSError as e:
            # Erreur 10048 sur Windows = port déjà utilisé
            if e.errno == 10048 or (hasattr(e, "winerror") and e.winerror == 10048):
                logger.error(
                    f"❌ Le port {self.port} est déjà utilisé. "
                    f"Une autre instance de ZRotate est peut-être déjà en cours d'exécution."
                )
                self.running = False
                raise
            # Autre erreur OSError, la propager
            self.running = False
            raise

        addr = self.server.sockets[0].getsockname()
        logger.info(f"✅ ZRotate démarré sur {addr[0]}:{addr[1]}")
        if self._use_quotas:
            await self.quota_manager.start_pool_health_task()
            logger.info(
                f"Système de quotas activé - {len(self.quota_manager.egress_configs)} interface(s) disponible(s)"
            )
            logger.info(
                f"Interfaces: {[e['name'] for e in self.quota_manager.egress_configs]}"
            )
            logger.info(
                f"Quota par requête: {self.quota_manager.max_requests_per_quota} requêtes max"
            )
            logger.info(
                f"Timeout quotas partiels: {self.quota_manager.quota_timeout_seconds}s"
            )
        else:
            logger.info(
                f"Egress IPs configurées: {[e['name'] for e in (self.egress_selector.egress_configs)]}"
            )

    async def stop(self):
        """Arrête le serveur"""
        self.running = False

        if self.server:
            self.server.close()
            await self.server.wait_closed()
            logger.info("✅ Serveur ZRotate arrêté")

    async def serve_forever(self):
        """Lance le serveur et attend indéfiniment"""
        if not self.server:
            await self.start()

        async with self.server:
            await self.server.serve_forever()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        """Gère une connexion client"""
        client_addr = writer.get_extra_info("peername")
        self._connection_counter += 1
        connection_id = self._connection_counter

        upstream_reader = None
        upstream_writer = None
        egress_info = None
        request_type = None
        dest_host = None
        dest_port = None
        is_get_request = False

        # Extraire IP et port du client
        client_ip = client_addr[0] if client_addr else "unknown"
        client_port = client_addr[1] if client_addr else 0

        try:
            # Lire la requête initiale (jusqu'à \r\n\r\n) pour déterminer le type et la destination
            request_data = await read_until_double_crlf(reader)
            if not request_data:
                logger.warning(f"[{connection_id}] Connexion fermée avant requête")
                return

            # Parser la première ligne
            request_lines = request_data.decode("latin-1", errors="ignore").split(
                "\r\n"
            )
            first_line = request_lines[0] if request_lines else ""

            # Déterminer le type de requête et la destination AVANT de sélectionner l'interface
            if first_line.upper().startswith("CONNECT"):
                # Requête CONNECT (HTTPS)
                dest = parse_connect_request(first_line)
                if not dest:
                    logger.warning(
                        f"[{connection_id}] Requête CONNECT invalide: {first_line}"
                    )
                    writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                    await writer.drain()
                    return

                dest_host, dest_port = dest
                request_type = "CONNECT"
                is_get_request = False
            else:
                # Requête HTTP proxy-form (GET http://...)
                parsed = parse_http_proxy_request(request_lines)
                if not parsed:
                    logger.warning(
                        f"[{connection_id}] Requête HTTP invalide: {first_line}"
                    )
                    writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                    await writer.drain()
                    # Si la première ligne ressemble à un GET, compter comme GET rejetée
                    if first_line.upper().startswith("GET "):
                        self.total_requests += 1
                        self.rejected_requests += 1
                    return

                dest_host = parsed["host"]
                dest_port = parsed["port"]
                request_type = parsed["method"]  # "GET", "POST", etc.
                is_get_request = request_type.upper() == "GET"

            # On a une requête valide à ce stade : ne compter en stats que les GET
            if is_get_request:
                self.total_requests += 1

            # Sélectionner l'interface selon le système utilisé
            if self._use_quotas:
                egress_info = await self.quota_manager.get_interface_for_request(
                    request_type, dest_host, dest_port, connection_id
                )

                # Si aucune interface n'est disponible au moment de la requête,
                # on renvoie immédiatement une erreur au client au lieu d'attendre.
                if not egress_info:
                    logger.warning(
                        f"[{connection_id}] Aucune interface disponible pour {request_type} {dest_host}:{dest_port} (quotas pleins ou clés en reset)"
                    )
                    writer.write(b"HTTP/1.1 503 Service Unavailable\r\n\r\n")
                    writer.write(
                        b"No interface available at the moment. Please try again later.\r\n"
                    )
                    await writer.drain()
                    if is_get_request:
                        self.rejected_requests += 1
                    return
            else:
                # Ancien système round-robin
                egress_info = await self.egress_selector.get_egress()

            if request_type == "CONNECT":
                logger.info(
                    f"[{connection_id}] Requête CONNECT reçue → {egress_info['name']} → {dest_host}:{dest_port}"
                )
            else:
                logger.info(
                    f"[{connection_id}] Nouvelle connexion depuis {client_ip}:{client_port} "
                    f"→ Egress: {egress_info['name']} ({egress_info['ip']})"
                )

            # Traiter la requête selon son type
            if request_type == "CONNECT":
                # Ouvrir connexion upstream (bind local ou relais amont)
                try:
                    if egress_info.get("remote"):
                        upstream_reader, upstream_writer = await open_connection_via_proxy(
                            dest_host,
                            dest_port,
                            "127.0.0.1",
                            int(egress_info["proxy_port"]),
                        )
                    else:
                        upstream_reader, upstream_writer = await open_connection_with_bind(
                            dest_host, dest_port, egress_info["ip"]
                        )
                except (
                    OSError,
                    ConnectionRefusedError,
                    asyncio.TimeoutError,
                ) as e:
                    logger.error(
                        f"[{connection_id}] ❌ Erreur connexion {dest_host}:{dest_port}: {e}"
                    )
                    writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    await writer.drain()
                    # Libérer le quota temporaire si pris
                    # complete_request vérifie elle-même si la connexion est dans _active_connections avec le lock
                    if self._use_quotas:
                        await self.quota_manager.complete_request(
                            connection_id, success=False
                        )
                    return

                # Répondre au client
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()

                # Fermer les tunnels CONNECT vers un hostname (haapi, waf, etc.) après N secondes
                # pour ne pas garder les interfaces bloquées. CONNECT vers IP (game server) restent ouvertes.
                is_non_important = request_type == "CONNECT" and not _host_is_ip_only(
                    dest_host or ""
                )
                if self._close_haapi_tunnel_after_seconds > 0 and is_non_important:
                    delay = self._close_haapi_tunnel_after_seconds
                    _uw = upstream_writer
                    _w = writer

                    async def _close_tunnel_after():
                        await asyncio.sleep(delay)
                        try:
                            _uw.close()
                            await _uw.wait_closed()
                        except Exception:
                            pass
                        try:
                            _w.close()
                            await _w.wait_closed()
                        except Exception:
                            pass

                    asyncio.create_task(_close_tunnel_after())

                # Tunnel CONNECT : relay_tunnel termine dès qu'un côté se ferme,
                # annule l'autre et ferme tout → le finally est toujours atteint
                try:
                    await relay_tunnel(reader, writer, upstream_reader, upstream_writer)
                finally:
                    # SIMPLE : Marquer comme complétée dès que le tunnel se termine
                    if self._use_quotas:
                        await self.quota_manager.complete_request(
                            connection_id, success=True
                        )

            else:
                # Requête HTTP proxy-form (GET http://...)
                # Re-parser la requête pour obtenir les détails complets
                parsed = parse_http_proxy_request(request_lines)
                if not parsed:
                    logger.warning(
                        f"[{connection_id}] Requête HTTP invalide après parsing: {first_line}"
                    )
                    writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                    await writer.drain()
                    # Libérer le quota temporaire si pris
                    # complete_request vérifie elle-même si la connexion est dans _active_connections avec le lock
                    if self._use_quotas:
                        await self.quota_manager.complete_request(
                            connection_id, success=False
                        )
                    if is_get_request:
                        self.rejected_requests += 1
                    return

                logger.info(
                    f"[{connection_id}] HTTP {request_type} {dest_host}:{dest_port}{parsed.get('path', '')}"
                )

                # Reconstruire la requête en origin-form
                rebuilt_request = rebuild_http_request(parsed, request_data)

                # Lire le body complet si Content-Length est présent
                body_remaining = 0
                if "content-length" in parsed["headers"]:
                    try:
                        content_length = int(parsed["headers"]["content-length"])
                        body_in_request = (
                            len(request_data) - request_data.find(b"\r\n\r\n") - 4
                        )
                        if body_in_request < 0:
                            body_in_request = 0
                        body_remaining = content_length - body_in_request
                        if body_remaining < 0:
                            body_remaining = 0
                    except (ValueError, KeyError):
                        body_remaining = 0

                # Ouvrir connexion upstream (bind local ou relais amont)
                try:
                    if egress_info.get("remote"):
                        upstream_reader, upstream_writer = await open_connection_via_proxy(
                            dest_host,
                            dest_port,
                            "127.0.0.1",
                            int(egress_info["proxy_port"]),
                        )
                    else:
                        upstream_reader, upstream_writer = await open_connection_with_bind(
                            dest_host, dest_port, egress_info["ip"]
                        )
                except (
                    OSError,
                    ConnectionRefusedError,
                    asyncio.TimeoutError,
                ) as e:
                    logger.error(
                        f"[{connection_id}] ❌ Erreur connexion {dest_host}:{dest_port}: {e}"
                    )
                    writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    await writer.drain()
                    # Libérer le quota temporaire si pris
                    # complete_request vérifie elle-même si la connexion est dans _active_connections avec le lock
                    if self._use_quotas:
                        await self.quota_manager.complete_request(
                            connection_id, success=False
                        )
                    if is_get_request:
                        self.rejected_requests += 1
                    return

                # Envoyer la requête reconstruite
                upstream_writer.write(rebuilt_request)
                await upstream_writer.drain()

                # Lire et forwarder le body restant si nécessaire
                if body_remaining > 0:
                    try:
                        body_data = await reader.read(body_remaining)
                        if body_data:
                            upstream_writer.write(body_data)
                            await upstream_writer.drain()
                    except Exception as e:
                        logger.warning(
                            f"[{connection_id}] Erreur lors de la lecture du body: {e}"
                        )

                # Relay bidirectionnel (avec Connection: close, on ferme après réponse)
                try:
                    if is_get_request:
                        self.successful_requests += 1
                    await asyncio.gather(
                        pipe(reader, upstream_writer),
                        pipe(upstream_reader, writer),
                        return_exceptions=True,
                    )
                except Exception as e:
                    logger.error(f"[{connection_id}] Erreur lors du relay HTTP: {e}")
                finally:
                    # Marquer la requête comme terminée à la fermeture (approche simple et fiable)
                    if self._use_quotas:
                        await self.quota_manager.complete_request(
                            connection_id, success=True
                        )

        except asyncio.CancelledError:
            logger.debug(f"[{connection_id}] ⏹️ Connexion annulée")
            # Marquer la requête comme annulée (si quota temporaire pris)
            # complete_request vérifie elle-même si la connexion est dans _active_connections avec le lock
            if self._use_quotas:
                await self.quota_manager.complete_request(connection_id, success=False)
            raise
        except Exception as e:
            # WinError 64 (nom réseau plus disponible) : normal pendant un reset modem, ne pas logger en erreur
            if (
                isinstance(e, ConnectionResetError)
                and getattr(e, "winerror", None) == 64
            ):
                logger.debug(
                    f"[{connection_id}] Connexion fermée (interface en reset, WinError 64)"
                )
            else:
                logger.error(f"[{connection_id}] ❌ Erreur: {type(e).__name__}: {e}")
            # Marquer la requête comme terminée (si quota temporaire pris)
            # complete_request ne comptera pas d'échec si l'interface est déjà en reset
            if self._use_quotas:
                await self.quota_manager.complete_request(connection_id, success=False)
        finally:
            # Fermeture propre
            if upstream_writer:
                try:
                    upstream_writer.close()
                    await upstream_writer.wait_closed()
                except Exception:
                    pass

            if writer:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

            # Note: Les quotas sont gérés lors de l'attribution de l'interface, pas lors de la fermeture
            # Le système de quotas vérifie automatiquement si tous les quotas sont pleins et déclenche le reset


@dataclass
class Modem4G:
    """Représente une interface réseau utilisée pour la rotation d'IP"""

    interface_name: str  # Nom de l'interface réseau
    proxy_port: int  # Port du proxy local pour accéder à cette interface
    ip_address: Optional[str] = None  # IP actuelle (détectée automatiquement)

    def __hash__(self):
        return hash(self.interface_name)


class ModemState(Enum):
    """États possibles d'un modem"""

    AVAILABLE = "available"  # Prêt à être utilisé
    IN_USE = "in_use"  # IP distribuée, en attente de reset
    RESETTING = "resetting"  # En cours de reset
    ERROR = "error"  # Erreur, nécessite intervention manuelle


@dataclass
class ModemStatus:
    """Statut complet d'un modem"""

    modem: Modem4G
    state: ModemState = ModemState.AVAILABLE
    current_ip: Optional[str] = None
    last_used: Optional[datetime] = None
    last_reset: Optional[datetime] = None
    error_message: Optional[str] = None
    use_count: int = 0


class IPPoolManager:
    """
    Gestionnaire principal du pool d'IPs
    Gère la rotation et le reset des interfaces réseau
    """

    def __init__(
        self,
        modems: list[Modem4G],
        log_callback: Optional[Callable[[str], None]] = None,
        interface_manager: Optional[object] = None,
    ):
        self.modems: Dict[str, ModemStatus] = {
            modem.interface_name: ModemStatus(modem=modem) for modem in modems
        }
        self._lock = asyncio.Lock()
        self._reset_callback: Optional[Callable[[Modem4G], Awaitable[bool]]] = None
        self.log_callback = log_callback
        self.interface_manager = (
            interface_manager  # Référence à InterfaceManager de ProxyZ
        )

    def set_reset_callback(self, callback: Callable[[Modem4G], Awaitable[bool]]):
        """Définit la fonction de callback pour reset un modem"""
        self._reset_callback = callback

    def _log(self, message: str):
        """Log un message via le callback"""
        if self.log_callback:
            self.log_callback(message)

    async def initialize(self):
        """Initialise le pool en détectant les IPs actuelles de chaque modem"""
        for modem_name, status in self.modems.items():
            # Essayer d'abord d'utiliser l'IP publique déjà détectée par ProxyZ
            ip = None
            if self.interface_manager:
                interface_info = self.interface_manager.interfaces.get(modem_name)
                if interface_info and interface_info.public_ip:
                    ip = interface_info.public_ip
                    self._log(
                        f"✅ {status.modem.interface_name}: IP récupérée depuis ProxyZ ({ip})"
                    )

            # Si pas d'IP disponible depuis ProxyZ, essayer de la détecter via le proxy
            if not ip:
                ip = await self._get_modem_ip(status.modem)
                if ip:
                    self._log(
                        f"✅ {status.modem.interface_name}: IP détectée via proxy ({ip})"
                    )

            if ip:
                status.current_ip = ip
                status.state = ModemState.AVAILABLE
                self._log(
                    f"✅ {status.modem.interface_name}: IP détectée ({ip}) - Prêt à l'emploi"
                )
            else:
                status.state = ModemState.ERROR
                status.error_message = "Impossible de détecter l'IP"
                self._log(
                    f"⚠️ {status.modem.interface_name}: Impossible de détecter l'IP"
                )

    async def get_available_ip(self, reset_after_use: bool = False) -> Optional[dict]:
        """
        Récupère une IP disponible du pool.
        Ne bloque plus indéfiniment : si aucune IP n'est disponible au moment
        de l'appel, retourne None au lieu d'attendre.

        Args:
            reset_after_use: Si True, déclenche le reset après utilisation (pour CONNECT uniquement)

        Returns:
            dict avec les infos d'IP disponible, ou None si aucune IP n'est disponible.
        """
        async with self._lock:
            # Chercher d'abord les IPs disponibles
            for modem_name, status in self.modems.items():
                if status.state == ModemState.AVAILABLE and status.current_ip:
                    status.state = ModemState.IN_USE
                    status.last_used = datetime.now()
                    status.use_count += 1

                    ip_info = {
                        "ip": status.current_ip,
                        "modem_name": modem_name,
                        "proxy_port": status.modem.proxy_port,
                        "use_count": status.use_count,
                    }

                    # Ne lance le reset que si demandé explicitement (pour CONNECT)
                    if reset_after_use:
                        # Lance le reset en arrière-plan
                        asyncio.create_task(self._reset_modem(modem_name))
                        self._log(
                            f"🔄 IP distribuée: {status.modem.interface_name} ({status.current_ip})"
                        )
                    else:
                        # Pour les requêtes GET (vérifications d'IP), on remet l'IP en disponible après utilisation
                        # On marque juste qu'elle a été utilisée
                        self._log(
                            f"📋 IP utilisée (sans reset): {status.modem.interface_name} ({status.current_ip})"
                        )

                    return ip_info

            # Si aucune IP disponible, vérifier s'il y a des modems en erreur qui peuvent être récupérés
            recovered = False
            for modem_name, status in self.modems.items():
                if status.state == ModemState.ERROR and self.interface_manager:
                    interface_info = self.interface_manager.interfaces.get(modem_name)
                    if interface_info and interface_info.public_ip:
                        status.current_ip = interface_info.public_ip
                        status.state = ModemState.AVAILABLE
                        status.error_message = None
                        recovered = True
                        self._log(
                            f"✅ {modem_name}: Récupération depuis ProxyZ ({interface_info.public_ip})"
                        )

            # Si on a pu récupérer au moins une IP, retourner immédiatement la première disponible
            if recovered:
                for modem_name, status in self.modems.items():
                    if status.state == ModemState.AVAILABLE and status.current_ip:
                        status.state = ModemState.IN_USE
                        status.last_used = datetime.now()
                        status.use_count += 1

                        ip_info = {
                            "ip": status.current_ip,
                            "modem_name": modem_name,
                            "proxy_port": status.modem.proxy_port,
                            "use_count": status.use_count,
                        }

                        if reset_after_use:
                            asyncio.create_task(self._reset_modem(modem_name))
                            self._log(
                                f"🔄 IP distribuée: {status.modem.interface_name} ({status.current_ip})"
                            )
                        else:
                            self._log(
                                f"📋 IP utilisée (sans reset): {status.modem.interface_name} ({status.current_ip})"
                            )

                        return ip_info

            # Aucune IP disponible immédiatement
            return None

    async def release_ip(self, modem_name: str):
        """Remet une IP en disponible après utilisation (pour les requêtes GET)"""
        async with self._lock:
            if modem_name in self.modems:
                status = self.modems[modem_name]
                if status.state == ModemState.IN_USE:
                    status.state = ModemState.AVAILABLE

    async def trigger_reset(self, modem_name: str):
        """Déclenche le reset d'un modem après une requête CONNECT réussie"""
        if modem_name in self.modems:
            asyncio.create_task(self._reset_modem(modem_name))

    async def _reset_modem(self, modem_name: str):
        """Reset un modem et attend que son IP soit de nouveau disponible"""
        status = self.modems[modem_name]
        status.state = ModemState.RESETTING
        status.last_reset = datetime.now()

        try:
            self._log(f"🔄 Reset du modem {status.modem.interface_name} en cours...")

            if self._reset_callback:
                success = await self._reset_callback(status.modem)
                if not success:
                    raise Exception("Le callback de reset a échoué")
            else:
                # Pas de callback de reset défini, on attend juste un peu
                await asyncio.sleep(20)

            # Attendre un peu que le proxy soit prêt après le reset
            await asyncio.sleep(3)

            # Récupère la nouvelle IP avec plusieurs tentatives
            new_ip = None
            max_ip_retries = 5
            for ip_attempt in range(max_ip_retries):
                new_ip = await self._get_modem_ip(status.modem)
                if new_ip:
                    break
                if ip_attempt < max_ip_retries - 1:
                    # Attendre un peu plus longtemps entre chaque tentative
                    await asyncio.sleep(5)

            if new_ip:
                async with self._lock:
                    old_ip = status.current_ip
                    status.current_ip = new_ip
                    status.state = ModemState.AVAILABLE
                    status.error_message = None

                    if old_ip != new_ip:
                        self._log(
                            f"✅ Nouvelle IP pour {status.modem.interface_name}: {old_ip} → {new_ip}"
                        )
                    else:
                        self._log(
                            f"✅ Reset terminé pour {status.modem.interface_name}, IP inchangée: {new_ip}"
                        )
            else:
                # Au lieu de lever une exception, marquer en erreur mais permettre la récupération
                async with self._lock:
                    status.state = ModemState.ERROR
                    status.error_message = "IP non récupérée après reset"
                raise Exception("IP non récupérée après reset")

        except Exception as e:
            async with self._lock:
                status.state = ModemState.ERROR
                status.error_message = str(e)
            self._log(f"❌ Erreur lors du reset de {status.modem.interface_name}: {e}")

    async def _get_modem_ip(self, modem: Modem4G) -> Optional[str]:
        """Récupère l'IP publique d'un modem via son proxy local"""
        proxy_url = f"http://127.0.0.1:{modem.proxy_port}"

        services = [
            "https://api.ipify.org",
            "https://ifconfig.me",
            "https://icanhazip.com",
            "http://ipinfo.io/ip",
        ]

        last_error = None
        max_retries = 3

        for service in services:
            for attempt in range(max_retries):
                try:
                    # Attendre un peu avant chaque tentative pour laisser le proxy se stabiliser
                    if attempt > 0:
                        await asyncio.sleep(2)

                    async with httpx.AsyncClient(
                        proxy=proxy_url, timeout=20.0, follow_redirects=True
                    ) as client:
                        response = await client.get(service)

                        if response.status_code == 200:
                            ip = response.text.strip()
                            # Nettoyer l'IP (enlever les espaces, retours à la ligne, etc.)
                            ip = ip.replace("\n", "").replace("\r", "").strip()
                            if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
                                return ip
                            else:
                                last_error = f"IP invalide reçue: {ip[:50]}"
                                continue
                        else:
                            last_error = (
                                f"Code HTTP {response.status_code} sur {service}"
                            )
                            continue

                except httpx.ProxyError as e:
                    error_msg = str(e)
                    if (
                        "RemoteProtocolError" in error_msg
                        or "Server disconnected" in error_msg
                    ):
                        # Le proxy peut être en train de se reconnecter, réessayer
                        if attempt < max_retries - 1:
                            continue
                    last_error = f"Erreur proxy: {error_msg[:100]}"
                    continue
                except httpx.TimeoutException:
                    if attempt < max_retries - 1:
                        continue
                    last_error = f"Timeout sur {service}"
                    continue
                except httpx.ConnectError as e:
                    # Le proxy n'est peut-être pas encore prêt, réessayer
                    if attempt < max_retries - 1:
                        continue
                    last_error = f"Connexion impossible: {str(e)[:100]}"
                    continue
                except Exception as e:
                    error_type = type(e).__name__
                    error_msg = str(e)
                    if attempt < max_retries - 1 and (
                        "disconnected" in error_msg.lower()
                        or "connection" in error_msg.lower()
                    ):
                        # Erreur de connexion temporaire, réessayer
                        continue
                    last_error = f"Erreur: {error_type}: {error_msg[:100]}"
                    continue

        # Si toutes les tentatives ont échoué, logger l'erreur
        if last_error:
            self._log(f"⚠️ {modem.interface_name}: Échec détection IP - {last_error}")
        return None

    def get_available_count(self) -> int:
        """Retourne le nombre d'IPs disponibles"""
        return sum(
            1 for status in self.modems.values() if status.state == ModemState.AVAILABLE
        )

    async def add_modem(self, modem: Modem4G) -> bool:
        """Ajoute un modem au pool dynamiquement"""
        if modem.interface_name in self.modems:
            return False  # Déjà présent

        # Détecter l'IP
        ip = None
        if self.interface_manager:
            interface_info = self.interface_manager.interfaces.get(modem.interface_name)
            if interface_info and interface_info.public_ip:
                ip = interface_info.public_ip

        if not ip:
            ip = await self._get_modem_ip(modem)

        status = ModemStatus(modem=modem)
        if ip:
            status.current_ip = ip
            status.state = ModemState.AVAILABLE
            self._log(f"✅ {modem.interface_name}: Ajouté au pool avec IP {ip}")
        else:
            status.state = ModemState.ERROR
            status.error_message = "Impossible de détecter l'IP"
            self._log(f"⚠️ {modem.interface_name}: Ajouté mais IP non détectée")

        self.modems[modem.interface_name] = status
        return True

    async def remove_modem(self, interface_name: str) -> bool:
        """Retire un modem du pool dynamiquement"""
        if interface_name not in self.modems:
            return False

        status = self.modems[interface_name]
        # Si le modem est en cours d'utilisation, on le marque pour suppression après utilisation
        if status.state == ModemState.IN_USE:
            self._log(
                f"⚠️ {interface_name}: En cours d'utilisation, sera retiré après utilisation"
            )
            # On pourrait implémenter une logique plus sophistiquée ici
            # Pour l'instant, on le retire directement
        else:
            self._log(f"✅ {interface_name}: Retiré du pool")

        del self.modems[interface_name]
        return True


class ZRotateProxyServer(QThread):
    """
    Wrapper QThread pour ZRotateSingleProxyServer.
    Permet d'intégrer le serveur asyncio dans l'application Qt.
    """

    log_message = Signal(str)
    reset_interface_requested = Signal(
        str
    )  # Signal émis quand ZRotate veut reset une interface
    interface_usage_changed = Signal(
        str, bool
    )  # (interface_name, in_use) pour badge RESET / In use
    stats_updated = Signal(int, int, int)  # total, successful, rejected
    quota_stats_updated = Signal(
        object
    )  # dict[interface_name, {"get": (used, max), "connect": (used, max)}]
    quarantine_updated = Signal(object)  # list[str] noms en quarantaine
    pool_state_updated = Signal(object)  # dict[name, {in_pool, resetting, quarantine}]

    def __init__(
        self,
        egress_configs: list,
        host: str = "127.0.0.1",
        port: int = 9999,
        max_requests_per_quota: int = 2,
        quota_timeout_seconds: float = 60.0,
        close_haapi_tunnel_after_seconds: float = 0.0,
    ):
        """
        Args:
            egress_configs: Liste de dicts avec 'name' et 'ip' pour les clés Huawei
            host: Adresse d'écoute
            port: Port d'écoute
            max_requests_per_quota: Nombre max de requêtes GET/CONNECT par IP (proxy_configs.json)
            quota_timeout_seconds: Timeout pour réinitialiser les quotas partiels
            close_haapi_tunnel_after_seconds: Si > 0, ferme les tunnels CONNECT haapi après ce délai (0 = désactivé)
        """
        super().__init__()
        self.egress_configs = egress_configs
        self.host = host
        self.port = port
        self.max_requests_per_quota = max_requests_per_quota
        self.quota_timeout_seconds = quota_timeout_seconds
        self.close_haapi_tunnel_after_seconds = close_haapi_tunnel_after_seconds
        self.running = False
        self.loop = None
        self.proxy_server: Optional[ZRotateSingleProxyServer] = None

    async def _publish_stats_loop(self):
        """Publie périodiquement les statistiques de ZRotate vers l'UI."""
        # Boucle tant que le thread est en cours et que le serveur existe
        while self.running:
            try:
                if self.proxy_server is not None:
                    total = getattr(self.proxy_server, "total_requests", 0)
                    successful = getattr(self.proxy_server, "successful_requests", 0)
                    rejected = getattr(self.proxy_server, "rejected_requests", 0)
                    self.stats_updated.emit(int(total), int(successful), int(rejected))
                    qm = getattr(self.proxy_server, "quota_manager", None)
                    if qm is not None:
                        stats = await qm.get_quota_stats()
                        self.quota_stats_updated.emit(stats)
                        qlist = await qm.get_quarantine_snapshot()
                        self.quarantine_updated.emit(qlist)
                        pool_snap = await qm.get_pool_ui_snapshot()
                        self.pool_state_updated.emit(pool_snap)
            except Exception:
                pass
            # Intervalle raisonnable pour l'UI sans charger la boucle
            await asyncio.sleep(1.0)

    def run(self):
        """Démarre le serveur proxy dans un thread séparé"""
        self.running = True
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        try:
            # Créer le serveur ZRotate avec les egress IPs
            self.proxy_server = ZRotateSingleProxyServer(
                host=self.host,
                port=self.port,
                egress_configs=self.egress_configs,
                max_requests_per_quota=self.max_requests_per_quota,
                quota_timeout_seconds=self.quota_timeout_seconds,
                close_haapi_tunnel_after_seconds=self.close_haapi_tunnel_after_seconds,
            )

            # Rediriger les logs vers le signal Qt
            import logging

            logger = logging.getLogger("zrotate_single_proxy")
            # Retirer les handlers existants pour éviter le double logging
            logger.handlers.clear()
            handler = LogHandler(self.log_message)
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            # Empêcher la propagation vers le logger root pour éviter le double affichage
            logger.propagate = False

            # Configurer le callback pour le reset avec animation
            reset_callback = ResetCallbackWrapper(self.reset_interface_requested)
            if hasattr(self.proxy_server, "quota_manager"):
                qm = self.proxy_server.quota_manager
                qm.set_reset_callback(reset_callback)
                qm.set_usage_callback(
                    lambda name, in_use: self.interface_usage_changed.emit(name, in_use)
                )

            # Lancer une tâche asynchrone pour publier périodiquement les stats vers l'UI
            self.loop.create_task(self._publish_stats_loop())

            # Démarrer et faire tourner le serveur
            self.loop.run_until_complete(self.proxy_server.serve_forever())

        except asyncio.CancelledError:
            pass
        except Exception as e:
            if self.running:  # Ne logger que si on n'a pas arrêté volontairement
                self.log_message.emit(f"❌ Erreur serveur: {e}")
                import traceback

                self.log_message.emit(f"Traceback: {traceback.format_exc()}")
        finally:
            # Fermer proprement le serveur si nécessaire
            if self.proxy_server:
                try:
                    self.loop.run_until_complete(self.proxy_server.stop())
                except Exception:
                    pass

            # Fermer toutes les tâches en cours
            try:
                pending = asyncio.all_tasks(self.loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self.loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                pass

            self.loop.close()
            self.running = False

    def stop(self):
        """Arrête le serveur proprement"""
        self.running = False
        if self.loop and not self.loop.is_closed():
            # Fermer le serveur asyncio proprement
            if self.proxy_server:
                try:
                    # Créer une tâche pour fermer le serveur
                    future = asyncio.run_coroutine_threadsafe(
                        self.proxy_server.stop(), self.loop
                    )
                    # Attendre que le stop soit terminé (timeout 2s)
                    future.result(timeout=2.0)
                except Exception:
                    pass
            # Arrêter la boucle
            try:
                self.loop.call_soon_threadsafe(self.loop.stop)
            except Exception:
                pass


class LogHandler(logging.Handler):
    """Handler de logging qui émet un signal Qt"""

    def __init__(self, signal_emitter):
        super().__init__()
        self.signal_emitter = signal_emitter

    def emit(self, record):
        try:
            msg = self.format(record)
            self.signal_emitter.emit(msg)
        except Exception:
            pass


class RemoteInterfaceDialog(QDialog):
    """Popup d'ajout ou modification d'une interface réseau distante."""

    def __init__(self, parent=None, edit_mode: bool = False):
        super().__init__(parent)
        self._edit_mode = edit_mode
        self.setWindowTitle(
            "Modifier une interface distante"
            if edit_mode
            else "Ajouter une interface distante"
        )
        self.setModal(True)
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        help_label = QLabel(
            "Relais vers un proxy déjà actif sur un autre serveur (même réseau local)."
        )
        help_label.setWordWrap(True)
        layout.addWidget(help_label)

        form = QFormLayout()
        form.setSpacing(8)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Ex: Serveur clés 1")
        form.addRow("Nom :", self.name_edit)

        self.upstream_edit = QLineEdit()
        self.upstream_edit.setPlaceholderText("Ex: 192.168.1.50:101")
        form.addRow("Proxy amont (IP:port) :", self.upstream_edit)

        self.port_edit = QLineEdit()
        self.port_edit.setPlaceholderText("Port local du relais (127.0.0.1)")
        form.addRow("Port relais local :", self.port_edit)

        reset_row = QHBoxLayout()
        reset_row.setSpacing(6)
        self.reset_script_edit = QLineEdit()
        self.reset_script_edit.setPlaceholderText("Optionnel — ex: reset_huawei.py")
        browse_btn = QPushButton("…")
        browse_btn.setObjectName("remoteResetBrowseButton")
        browse_btn.setFixedWidth(34)
        browse_btn.setToolTip("Parcourir pour choisir un script de reset")
        browse_btn.clicked.connect(self._browse_reset_script)
        reset_row.addWidget(self.reset_script_edit, 1)
        reset_row.addWidget(browse_btn)
        reset_widget = QWidget()
        reset_widget.setLayout(reset_row)
        form.addRow("Script reset :", reset_widget)

        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def set_values(
        self,
        *,
        name: str,
        upstream_host: str,
        upstream_port: int,
        local_port: int,
        reset_script: str = "",
    ) -> None:
        self.name_edit.setText(name)
        self.upstream_edit.setText(f"{upstream_host}:{upstream_port}")
        self.port_edit.setText(str(local_port))
        self.reset_script_edit.setText(reset_script or "")

    def _browse_reset_script(self) -> None:
        app_dir = get_app_dir()
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choisir un script de reset",
            str(app_dir),
            "Scripts Python (*.py);;Tous les fichiers (*.*)",
        )
        if not path:
            return
        self.reset_script_edit.setText(normalize_reset_script_storage_path(path))

    def values(self) -> tuple[str, str, int, int, str] | None:
        name = self.name_edit.text().strip()
        upstream = self.upstream_edit.text().strip()
        port_text = self.port_edit.text().strip()
        if not name or not upstream or not port_text.isdigit():
            return None
        parsed = parse_host_port_field(upstream)
        if not parsed:
            return None
        port = int(port_text)
        if port <= 0 or port > 65535:
            return None
        host, upstream_port = parsed
        reset_script = self.reset_script_edit.text().strip()
        return name, host, upstream_port, port, reset_script


def normalize_reset_script_storage_path(path: str) -> str:
    """Chemin relatif au dossier app si possible, sinon chemin absolu."""
    raw = (path or "").strip()
    if not raw:
        return ""
    p = Path(raw)
    if not p.is_absolute():
        return raw.replace("\\", "/")
    try:
        rel = p.resolve().relative_to(get_app_dir().resolve())
        return str(rel).replace("\\", "/")
    except ValueError:
        return str(p.resolve())


AddRemoteInterfaceDialog = RemoteInterfaceDialog


class InterfaceWidget(QFrame):
    proxy_toggled = Signal(str, bool, int)  # name, enabled, port
    rename_requested = Signal(str)  # name
    settings_requested = Signal(str)  # ouverture des propriétés de la carte
    reset_requested = Signal(str)  # name
    delete_requested = Signal(str)  # interface distante
    edit_requested = Signal(str)  # interface distante
    user_interaction = Signal()  # toute interaction utilisateur sur ce widget

    def __init__(self, interface: InterfaceInfo, parent=None):
        super().__init__(parent)
        self.interface_name = interface.name
        self.interface: InterfaceInfo = interface
        self.proxy_thread: ProxyThread | None = None
        self._disconnected = False
        self._reset_loading = False
        self._reset_loading_timer = QTimer()
        self._reset_loading_timer.timeout.connect(self._update_reset_loading_animation)
        self._reset_loading_dots = 0
        self._reset_in_use = (
            False  # True si la clé a une requête/connexion en cours (ZRotate)
        )

        self.setObjectName("interfaceCard")
        self.setMinimumHeight(84)
        self.setContextMenuPolicy(Qt.DefaultContextMenu)
        self._build_ui()
        self.setStyleSheet(INTERFACE_CARD_QSS)
        self.update_from_interface(interface)

    # --- UI ---
    def _build_ui(self):
        self.setFrameShape(QFrame.StyledPanel)
        self.setFrameShadow(QFrame.Raised)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 4, 8, 4)
        main_layout.setSpacing(3)

        # Ligne titre + badges (sans bouton paramètres local)
        header = QHBoxLayout()
        header.setSpacing(8)

        self.name_label = QLabel()
        self.name_label.setObjectName("ifaceName")
        header.addWidget(self.name_label, 1)

        self.dist_badge = QLabel("DIST")
        self.dist_badge.setObjectName("distBadge")
        self.dist_badge.setVisible(False)
        header.addWidget(self.dist_badge, 0, Qt.AlignLeft)

        # Badge AUTO (métrique automatique) seulement, la métrique numérique est déplacée en bas
        self.auto_badge = QLabel("AUTO")
        self.auto_badge.setObjectName("autoBadge")
        self.auto_badge.setVisible(False)
        header.addWidget(self.auto_badge, 0, Qt.AlignLeft)

        self.delete_button = QPushButton("×")
        self.delete_button.setObjectName("remoteDeleteButton")
        self.delete_button.setToolTip("Supprimer cette interface distante")
        self.delete_button.setVisible(False)
        self.delete_button.clicked.connect(self._on_delete_clicked)
        header.addWidget(self.delete_button, 0, Qt.AlignRight)

        # IP publique visible en haut à droite, sur la même ligne
        header.addStretch(1)

        # Bouton Reset (style badge cliquable)
        self.reset_badge = QLabel("RESET")
        self.reset_badge.setObjectName("resetBadge")
        self.reset_badge.installEventFilter(self)
        self.reset_badge.setCursor(Qt.PointingHandCursor)
        header.addWidget(self.reset_badge, 0, Qt.AlignRight)

        self.public_ip_header_label = QLabel("-")
        self.public_ip_header_label.setObjectName("publicIpHeaderLabel")
        header.addWidget(self.public_ip_header_label, 0, Qt.AlignRight)

        main_layout.addLayout(header)

        # Ligne IPs + statut (encore plus compacte)
        info_row = QHBoxLayout()
        info_row.setSpacing(5)

        ip_col = QVBoxLayout()
        ip_col.setSpacing(0)

        self.local_ip_label = QLabel("IPv4 locale: -")
        self.local_ip_label.setObjectName("ipLabel")
        ip_col.addWidget(self.local_ip_label)

        info_row.addLayout(ip_col, 2)

        status_col = QVBoxLayout()
        status_col.setSpacing(2)

        self.proxy_status_chip = QLabel("PROXY OFF")
        self.proxy_status_chip.setObjectName("proxyOffChip")
        self.proxy_status_chip.installEventFilter(self)
        self.proxy_status_chip.setCursor(Qt.PointingHandCursor)
        status_col.addWidget(self.proxy_status_chip, 0, Qt.AlignRight)

        info_row.addLayout(status_col, 1)
        main_layout.addLayout(info_row)

        # Ligne proxy controls + métrique (tout sur une seule ligne)
        proxy_row = QHBoxLayout()
        proxy_row.setSpacing(6)

        proxy_row.addWidget(QLabel("Proxy"), 0, Qt.AlignLeft)

        proxy_row.addSpacing(6)

        self.port_edit = QLineEdit()
        self.port_edit.setObjectName("portEdit")
        self.port_edit.setPlaceholderText("Port")
        self.port_edit.setFixedWidth(64)
        self.port_edit.textEdited.connect(lambda _text: self.user_interaction.emit())
        proxy_row.addWidget(QLabel("127.0.0.1:"), 0, Qt.AlignLeft)
        proxy_row.addWidget(self.port_edit, 0, Qt.AlignLeft)

        proxy_row.addStretch(1)

        self.reset_avg_badge = QLabel()
        self.reset_avg_badge.setObjectName("resetAvgBadge")
        self.reset_avg_badge.setVisible(False)
        proxy_row.addWidget(self.reset_avg_badge, 0, Qt.AlignRight)

        self.metric_badge = QLabel()
        self.metric_badge.setObjectName("metricBadge")
        proxy_row.addWidget(self.metric_badge, 0, Qt.AlignRight)

        main_layout.addLayout(proxy_row)

        # Autoriser le renommage via double-clic sur le nom
        self.name_label.installEventFilter(self)

    # --- Mise à jour depuis InterfaceInfo ---
    def update_from_interface(self, interface: InterfaceInfo):
        self.interface = interface
        self.dist_badge.setVisible(interface.is_remote)
        self.delete_button.setVisible(interface.is_remote)
        if not interface.is_remote:
            self.reset_badge.setVisible(True)
        self.metric_badge.setVisible(not interface.is_remote)
        if self.name_label.text() != interface.name:
            self.name_label.setText(interface.name)
        if interface.is_remote:
            metric_txt = "Relais"
        else:
            metric_txt = f"Metric {interface.metric}"
        if self.metric_badge.text() != metric_txt:
            self.metric_badge.setText(metric_txt)
        self.auto_badge.setVisible(interface.automatic and not interface.is_remote)

        if interface.is_remote and interface.upstream_host and interface.upstream_port:
            local_txt = f"Amont: {interface.upstream_host}:{interface.upstream_port}"
        else:
            local_txt = (
                f"IPv4 locale: {interface.local_ip}"
                if interface.local_ip
                else "IPv4 locale: -"
            )
        if self.local_ip_label.text() != local_txt:
            self.local_ip_label.setText(local_txt)

        public_txt = interface.public_ip if interface.public_ip else "-"
        if self.public_ip_header_label.text() != public_txt:
            self.public_ip_header_label.setText(public_txt)

        has_local = bool(interface.local_ip) or (
            interface.is_remote
            and interface.upstream_host
            and interface.upstream_port
        )
        if not has_local:
            self.set_proxy_running(False)

        if interface.is_remote:
            disconnected = False
            connected = bool(interface.online)
        else:
            disconnected = not interface.is_up
            connected = bool(interface.online and interface.is_up)

        _qt_apply_properties(
            self,
            {
                "disconnected": disconnected,
                "connected": connected,
            },
        )

    def set_remote_reset_visible(self, visible: bool) -> None:
        if self.interface.is_remote:
            self.reset_badge.setVisible(bool(visible))

    def contextMenuEvent(self, event):
        if not self.interface.is_remote:
            return super().contextMenuEvent(event)
        self.user_interaction.emit()
        menu = QMenu(self)
        edit_action = menu.addAction("Éditer…")
        delete_action = menu.addAction("Supprimer…")
        chosen = menu.exec(event.globalPos())
        if chosen == edit_action:
            self.edit_requested.emit(self.interface_name)
        elif chosen == delete_action:
            self.delete_requested.emit(self.interface_name)

    def _on_delete_clicked(self):
        self.user_interaction.emit()
        self.delete_requested.emit(self.interface_name)

    # --- Proxy ---
    def _on_proxy_button_clicked(self):
        port_text = self.port_edit.text().strip()
        # Activer / désactiver en fonction de l'état actuel du bouton
        want_enable = self.proxy_status_chip.objectName() != "proxyOnChip"

        if want_enable:
            if not (
                self.interface.local_ip
                or (
                    self.interface.is_remote
                    and self.interface.upstream_host
                    and self.interface.upstream_port
                )
            ):
                QMessageBox.warning(
                    self,
                    "Proxy impossible",
                    "Aucune interface valide pour cette entrée.",
                )
                return
            if not port_text.isdigit():
                QMessageBox.warning(
                    self, "Port invalide", "Veuillez saisir un port valide."
                )
                return
            port = int(port_text)
            if port <= 0 or port > 65535:
                QMessageBox.warning(
                    self, "Port invalide", "Le port doit être compris entre 1 et 65535."
                )
                return

            # Déléguer au parent (MainWindow) pour validation globale des ports
            self.proxy_toggled.emit(self.interface_name, True, port)
        else:
            self.proxy_toggled.emit(self.interface_name, False, 0)

    def set_proxy_running(self, running: bool, port: int | None = None):
        if running:
            chip_txt = f"PROXY ON · 127.0.0.1:{port}"
            chip_name = "proxyOnChip"
        else:
            chip_txt = "PROXY OFF"
            chip_name = "proxyOffChip"
        chip_changed = False
        if self.proxy_status_chip.text() != chip_txt:
            self.proxy_status_chip.setText(chip_txt)
            chip_changed = True
        if self.proxy_status_chip.objectName() != chip_name:
            self.proxy_status_chip.setObjectName(chip_name)
            chip_changed = True
        if chip_changed:
            st = self.proxy_status_chip.style()
            st.unpolish(self.proxy_status_chip)
            st.polish(self.proxy_status_chip)

    def mark_disconnected(self, disconnected: bool):
        self._disconnected = disconnected
        _qt_apply_properties(self, {"disconnected": disconnected})
        # Si l'interface est déconnectée, désactiver le bouton et afficher OFF
        if disconnected:
            self.set_proxy_running(False)
            self.proxy_button.setEnabled(False)
        else:
            self.proxy_button.setEnabled(bool(self.interface.local_ip))

    def set_port(self, port: int | None):
        if port:
            self.port_edit.setText(str(port))

    def set_display_name(self, display_name: str):
        self.name_label.setText(display_name)

    def set_reset_avg(self, avg_seconds: float | None):
        if avg_seconds is None:
            self.reset_avg_badge.setVisible(False)
            return
        txt = f"{avg_seconds:.1f}s"
        if self.reset_avg_badge.text() != txt:
            self.reset_avg_badge.setText(txt)
        self.reset_avg_badge.setVisible(True)

    def set_reset_loading(self, loading: bool):
        """Active ou désactive l'animation de loading sur le bouton reset"""
        self._reset_loading = loading
        if loading:
            _qt_apply_properties(self.reset_badge, {"loading": True})
            self._reset_loading_dots = 0
            self._reset_loading_timer.start(500)  # Mise à jour toutes les 500ms
        else:
            self._reset_loading_timer.stop()
            _qt_apply_properties(self.reset_badge, {"loading": False})
            txt = "In use" if self._reset_in_use else "RESET"
            if self.reset_badge.text() != txt:
                self.reset_badge.setText(txt)

    def set_reset_badge_in_use(self, in_use: bool):
        """Affiche 'In use' si la clé a une requête/connexion en cours, sinon 'RESET'."""
        self._reset_in_use = in_use
        if not self._reset_loading:
            txt = "In use" if in_use else "RESET"
            if self.reset_badge.text() != txt:
                self.reset_badge.setText(txt)

    def _update_reset_loading_animation(self):
        """Met à jour l'animation de loading du bouton reset"""
        if self._reset_loading:
            self._reset_loading_dots = (self._reset_loading_dots + 1) % 4
            dots = "." * self._reset_loading_dots
            self.reset_badge.setText(f"RESET{dots}")

    def eventFilter(self, obj, event):
        if (
            hasattr(self, "name_label")
            and obj is self.name_label
            and event.type() == QEvent.MouseButtonDblClick
        ):
            if self.interface.is_remote:
                return True
            self.user_interaction.emit()
            self.rename_requested.emit(self.interface_name)
            return True
        if (
            hasattr(self, "proxy_status_chip")
            and obj is self.proxy_status_chip
            and event.type() == QEvent.MouseButtonRelease
        ):
            self.user_interaction.emit()
            self._on_proxy_button_clicked()
            return True
        if (
            hasattr(self, "reset_badge")
            and obj is self.reset_badge
            and event.type() == QEvent.MouseButtonRelease
        ):
            self.user_interaction.emit()
            self.reset_requested.emit(self.interface_name)
            return True
        return super().eventFilter(obj, event)


class ManualInterfacesList(QListWidget):
    order_changed = Signal(list)  # list of interface names
    user_interaction = Signal()  # clic / drag dans la liste
    console_general_requested = Signal()  # clic dans le vide → console générale

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QAbstractItemView.NoSelection)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        # Cartes un peu plus rapprochées
        self.setSpacing(4)
        # Évite un fond de sélection qui dépasse du widget
        self.setStyleSheet(
            """
            QListWidget::item {
                padding: 0px;
                margin: 0px;
            }
            """
        )
        self.setFrameShape(QFrame.NoFrame)

    def mousePressEvent(self, event):
        self.user_interaction.emit()
        if self.itemAt(event.pos()) is None:
            self.console_general_requested.emit()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # Émettre une fois au début du drag est suffisant
        self.user_interaction.emit()
        super().mouseMoveEvent(event)

    def dropEvent(self, event):
        super().dropEvent(event)
        # Drag & drop terminé : interaction utilisateur + nouvel ordre
        self.user_interaction.emit()
        names = []
        for row in range(self.count()):
            item = self.item(row)
            w = self.itemWidget(item)
            if isinstance(w, InterfaceWidget):
                names.append(w.interface_name)
        self.order_changed.emit(names)


class ZRotateInterfaceRow(QFrame):
    toggled = Signal(str, int)  # interface_name, Qt.CheckState

    def __init__(
        self,
        interface_name: str,
        public_ip: str,
        display_name: str | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.interface_name = interface_name
        self._last_zrotate_live: str | None = None
        self._last_pool_enabled: bool | None = None

        self.setObjectName("zrotateInterfaceRow")
        self.setMinimumHeight(34)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(10)

        self.checkbox = QCheckBox(display_name or interface_name)
        self.checkbox.setObjectName("zrotateInterfaceCheckbox")
        self.checkbox.stateChanged.connect(
            lambda state: self.toggled.emit(self.interface_name, state)
        )
        layout.addWidget(self.checkbox, 1)

        self.stats_widget = QWidget(self)
        stats_layout = QHBoxLayout(self.stats_widget)
        stats_layout.setContentsMargins(0, 0, 0, 0)
        stats_layout.setSpacing(8)

        self.get_chip = QLabel("0/2")
        self.get_chip.setObjectName("zrotateGetChip")
        self.get_chip.setAlignment(Qt.AlignCenter)
        self.get_chip.setFixedWidth(74)
        self.get_chip.setFixedHeight(22)
        stats_layout.addWidget(self.get_chip)

        self.connect_chip = QLabel("0/2")
        self.connect_chip.setObjectName("zrotateConnectChip")
        self.connect_chip.setAlignment(Qt.AlignCenter)
        self.connect_chip.setFixedWidth(104)
        self.connect_chip.setFixedHeight(22)
        stats_layout.addWidget(self.connect_chip)

        self.ip_chip = QLabel(public_ip or "-")
        self.ip_chip.setObjectName("zrotateIpChip")
        self.ip_chip.setAlignment(Qt.AlignCenter)
        self.ip_chip.setFixedWidth(102)
        self.ip_chip.setFixedHeight(22)
        stats_layout.addWidget(self.ip_chip)

        layout.addWidget(self.stats_widget, 0, Qt.AlignCenter)

    def set_checked(self, checked: bool):
        blocked = self.checkbox.blockSignals(True)
        self.checkbox.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self.checkbox.blockSignals(blocked)
        self._apply_checked_visual_state()

    def is_checked(self) -> bool:
        try:
            return self.checkbox.checkState() == Qt.Checked
        except RuntimeError:
            # Widget Qt déjà détruit (liste ZRotate en cours de refresh)
            return False

    def set_public_ip(self, public_ip: str):
        value = public_ip or "-"
        try:
            if self.ip_chip.text() != value:
                self.ip_chip.setText(value)
        except RuntimeError:
            # Ligne en cours de destruction pendant un refresh UI.
            pass

    def set_quota_values(self, g_used: int, g_max: int, c_used: int, c_max: int):
        get_txt = f"<b>{g_used}/{g_max}</b>"
        con_txt = f"<b>{c_used}/{c_max}</b>"
        if self.get_chip.text() != get_txt:
            self.get_chip.setText(get_txt)
        if self.connect_chip.text() != con_txt:
            self.connect_chip.setText(con_txt)

    def _apply_checked_visual_state(self):
        enabled = self.is_checked()
        self.get_chip.setVisible(enabled)
        self.connect_chip.setVisible(enabled)
        self.get_chip.setEnabled(enabled)
        self.connect_chip.setEnabled(enabled)
        self.ip_chip.setEnabled(enabled)
        self.checkbox.setEnabled(True)
        if self._last_pool_enabled != enabled:
            self._last_pool_enabled = enabled
            _qt_apply_properties(self, {"poolEnabled": enabled})

    def set_live_pool_state(
        self, zrotate_running: bool, snap: Optional[Dict[str, bool]]
    ) -> None:
        """
        Couleurs d'état du pool runtime (ZRotate démarré). snap = None ou clsé hors session.
        Clés snap : in_pool, resetting, quarantine, not_in_session
        """
        if not zrotate_running or snap is None:
            live = "off"
        elif snap.get("not_in_session"):
            live = "session"
        elif snap.get("quarantine"):
            live = "quarantine"
        elif snap.get("resetting"):
            live = "resetting"
        elif snap.get("in_pool"):
            live = "active"
        else:
            live = "standby"
        if self._last_zrotate_live != live:
            self._last_zrotate_live = live
            _qt_apply_properties(self, {"zrotateLive": live})
        self._apply_checked_visual_state()


class ZRotateInterfacesHeaderRow(QFrame):
    """Ligne d'en-tête fixe pour la liste des interfaces ZRotate."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("zrotateInterfacesHeaderRow")
        self.setMinimumHeight(34)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(10)

        self.name_label = QLabel("Nom de l'interface")
        self.name_label.setObjectName("zrotateHeaderName")
        layout.addWidget(self.name_label, 1)

        self.stats_widget = QWidget(self)
        stats_layout = QHBoxLayout(self.stats_widget)
        stats_layout.setContentsMargins(0, 0, 0, 0)
        stats_layout.setSpacing(8)

        self.get_label = QLabel("GET")
        self.get_label.setObjectName("zrotateHeaderChip")
        self.get_label.setAlignment(Qt.AlignCenter)
        self.get_label.setFixedWidth(74)
        self.get_label.setFixedHeight(22)
        stats_layout.addWidget(self.get_label)

        self.connect_label = QLabel("CONNECT")
        self.connect_label.setObjectName("zrotateHeaderChip")
        self.connect_label.setAlignment(Qt.AlignCenter)
        self.connect_label.setFixedWidth(104)
        self.connect_label.setFixedHeight(22)
        stats_layout.addWidget(self.connect_label)

        self.ip_label = QLabel("IP")
        self.ip_label.setObjectName("zrotateHeaderChip")
        self.ip_label.setAlignment(Qt.AlignCenter)
        self.ip_label.setFixedWidth(102)
        self.ip_label.setFixedHeight(22)
        stats_layout.addWidget(self.ip_label)

        layout.addWidget(self.stats_widget, 0, Qt.AlignCenter)

        self.setStyleSheet(
            """
            QFrame#zrotateInterfacesHeaderRow {
                background-color: rgba(30, 58, 138, 0.46);
                border: 1px solid rgba(96, 165, 250, 0.55);
                border-radius: 10px;
            }
            QLabel#zrotateHeaderName {
                color: #bfdbfe;
                font-size: 13px;
                font-weight: 700;
            }
            QLabel#zrotateHeaderChip {
                color: #e0f2fe;
                background-color: rgba(37, 99, 235, 0.28);
                border: 1px solid rgba(147, 197, 253, 0.55);
                border-radius: 8px;
                padding: 2px 8px;
                font-size: 11px;
                font-weight: 700;
            }
            """
        )


class ResetCallbackWrapper:
    """Wrapper pour permettre au quota_manager d'émettre un signal Qt pour le reset"""

    def __init__(self, signal_emitter):
        """
        Args:
            signal_emitter: Signal Qt qui sera émis (reset_interface_requested)
        """
        self.signal_emitter = signal_emitter

    def reset_interface(self, interface_name: str):
        """Méthode appelée par le quota_manager pour déclencher le reset avec animation"""
        # Émettre le signal Qt (thread-safe)
        self.signal_emitter.emit(interface_name)


class MainWindow(QMainWindow):
    CONFIG_FILE = "proxy_configs.json"
    # Config JSON : optionnellement "reset_script_default" (ex: "reset_modem.py") et par interface
    # dans interface_proxies["NomInterface"] : "reset_script" (ex: "reset_modem.py" ou chemin absolu).
    # Si absent, défaut = "reset_modem.py". Les chemins relatifs sont résolus depuis le dossier de l'exe/script.
    # Signal émis par le thread de reset vers le thread Qt principal (name, returncode: 0=ok, -1=script absent, -2=timeout, autre=échec)
    reset_completed = Signal(str, int, float)
    reset_log = Signal(str)
    remote_public_ip_updated = Signal(str, str, bool)  # name, public_ip, online

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ProxyZ - 0 proxy actif")
        # Taille minimale = deux panneaux (400 + 540) + espacement + marges, hauteur agrandie de 50%
        self.setMinimumSize(
            1070, 944
        )  # Largeur: 400 + 540 + 20 espacement + marges ≈ 1070, Hauteur: 644 * 1.5 ≈ 966, arrondi à 944

        try:
            self.interface_manager = InterfaceManager(self)
        except Exception:
            print("[FATAL] Exception lors de la création de InterfaceManager :")
            traceback.print_exc()
            raise

        self.interface_widgets: dict[str, InterfaceWidget] = {}
        self._remote_interfaces: dict[str, InterfaceInfo] = {}
        self.proxy_threads: dict[str, ProxyThread] = {}
        self.active_proxies = 0
        # Ensemble des noms d'interfaces dont le proxy est réellement en cours d'exécution
        self._running_proxies: set[str] = set()
        self.config = {
            "interface_proxies": {},
            "ui": {},
            "interface_aliases": {},
            "zrotate": {},
        }
        self.last_user_interaction = 0.0
        self._initial_proxies_restored = False

        # ZRotate
        self.zrotate_proxy_server: Optional[ZRotateProxyServer] = None
        self.zrotate_selected_interfaces: set[str] = set()
        self.zrotate_running = False
        self._last_zrotate_pool_state: Dict[str, Dict[str, bool]] = {}
        self._last_zrotate_structure_sig: tuple | None = None
        self._last_quarantine_names: tuple[str, ...] | None = None
        self._last_pool_state_for_ui: Dict[str, Dict[str, bool]] | None = None
        self._was_minimized = False
        self.playwright_warmup_enabled = DEFAULT_PLAYWRIGHT_WARMUP_ENABLED

        # Resets en parallèle (un thread par interface) ; suivi pour éviter doublons et refresh en rafale
        self._reset_in_progress: set[str] = set()  # interfaces en cours de reset
        self._reset_duration_sums: dict[str, float] = {}
        self._reset_duration_counts: dict[str, int] = {}
        self._refresh_after_reset_timer = QTimer(self)
        self._refresh_after_reset_timer.setSingleShot(True)
        self._refresh_after_reset_timer.timeout.connect(
            self.interface_manager.request_immediate_refresh
        )

        self._console_view: str | None = None
        self._console_lines: dict[str | None, list[str]] = {None: []}
        self._iface_by_port: dict[int, str] = {}
        self._remote_public_ip_inflight: set[str] = set()
        self._remote_public_ip_inflight_lock = threading.Lock()
        self._remote_public_ip_last_ok: dict[str, tuple[str, float]] = {}

        self._build_ui()
        self._load_config()

        self.interface_manager.interfaces_updated.connect(self.on_interfaces_updated)
        self.interface_manager.public_ip_updated.connect(self.on_public_ip_updated)
        self.interface_manager.metrics_update_failed.connect(
            self.on_metrics_update_failed
        )
        # Connexion du signal reset_completed pour arrêter l'animation après un reset
        self.reset_completed.connect(self._on_reset_completed)
        self.reset_log.connect(self._on_reset_log)
        self.remote_public_ip_updated.connect(self._on_remote_public_ip_updated)
        self.interface_manager.public_ip_timer.timeout.connect(
            self.refresh_remote_public_ips
        )

        # Première sync
        self.on_interfaces_updated(list(self.interface_manager.interfaces.values()))
        self._restore_initial_proxies()
        # Attendre que les ProxyThread soient réellement en écoute avant warmup Playwright.
        QTimer.singleShot(1500, self._start_playwright_browser_warmup)
        QTimer.singleShot(2000, lambda: self.refresh_remote_public_ips(force=True))
        self._maybe_rebuild_zrotate_interfaces_list(force=True)
        self._set_quarantine_ui_stopped()

        # Démarrer ZRotate automatiquement si configuré
        zrotate_cfg = self.config.get("zrotate", {})
        if zrotate_cfg.get("auto_start", False):
            # Vérifier qu'il y a des interfaces sélectionnées
            if self.zrotate_selected_interfaces:
                # Attendre un peu que les proxies soient prêts
                QTimer.singleShot(2000, self._auto_start_zrotate)

    def _start_playwright_browser_warmup(self):
        """
        Prépare les navigateurs reset (auth + page de rotation) en arrière-plan,
        par script Playwright et par port proxy configuré.
        Délai optionnel entre ports : WARMUP_STAGGER_ENABLED / WARMUP_STAGGER_SECONDS.
        """
        if not self.playwright_warmup_enabled:
            print("[RESET] Warmup Playwright désactivé (playwright_warmup_enabled=false)")
            return

        def _warmup():
            try:
                app_dir = get_app_dir()
                default_reset = self.config.get(
                    "reset_script_default", DEFAULT_RESET_SCRIPT
                )
                # Ne warmup QUE les ports dont le ProxyThread correspondant est déjà running,
                # sinon Playwright passe par un proxy TCP non ouvert => ERR_EMPTY_RESPONSE.
                proxy_threads_snapshot = dict(self.proxy_threads)
                ports_by_script: dict[str, list[int]] = {}
                port_options_by_script: dict[str, dict[int, dict]] = {}

                for iface_name, thread in proxy_threads_snapshot.items():
                    if not getattr(thread, "running", False):
                        continue
                    cfg = getattr(thread, "config", None)
                    if cfg and getattr(cfg, "is_remote", False):
                        continue
                    if iface_name in self._remote_interfaces:
                        continue
                    proxy_port = int(getattr(cfg, "port", 0) or 0) if cfg else 0
                    if proxy_port <= 0:
                        continue

                    iface_cfg = self.config.get("interface_proxies", {}).get(
                        iface_name, {}
                    )
                    reset_script, is_explicit_mapping = resolve_interface_reset_script(
                        iface_cfg, default_reset
                    )
                    script_path = resolve_reset_script_path(reset_script, app_dir)
                    if not is_playwright_reset_script(script_path):
                        continue
                    # Évite les warmups "par défaut" Playwright (ex: reset_huawei.py en global)
                    # quand l'interface n'a pas de mapping explicite reset_script/modem_gateway.
                    if not is_explicit_mapping:
                        continue
                    if not script_path.exists():
                        continue

                    key = str(script_path.resolve()).lower()
                    ports_by_script.setdefault(key, [])
                    port_options_by_script.setdefault(key, {})
                    if proxy_port not in ports_by_script[key]:
                        ports_by_script[key].append(proxy_port)

                    opts = extract_interface_reset_options(iface_cfg)
                    if opts:
                        prev = port_options_by_script[key].get(proxy_port) or {}
                        port_options_by_script[key][proxy_port] = {
                            **prev,
                            **opts,
                        }

                for script_key, ports in ports_by_script.items():
                    script_path = Path(script_key)
                    _, init_fn = _load_reset_modem_functions(script_path)
                    if not callable(init_fn):
                        continue
                    unique_ports = sorted({int(p) for p in ports if int(p) > 0})
                    port_options = port_options_by_script.get(script_key) or {}
                    for index, port in enumerate(unique_ports):
                        single_opts = {port: dict(port_options.get(port) or {})}
                        try:
                            init_fn([port], port_options=single_opts)
                        except TypeError:
                            init_fn([port])

                        gw = (single_opts.get(port) or {}).get("modem_gateway")
                        print(
                            f"[RESET] Préparation navigateur en arrière-plan "
                            f"({script_path.name}, port {port}"
                            + (f", gateway {gw}" if gw else "")
                            + ")"
                        )
                        if (
                            WARMUP_STAGGER_ENABLED
                            and index < len(unique_ports) - 1
                            and WARMUP_STAGGER_SECONDS > 0
                        ):
                            time.sleep(WARMUP_STAGGER_SECONDS)
            except Exception as e:
                print(f"[RESET] Warmup Playwright ignoré: {e}")

        threading.Thread(target=_warmup, daemon=True).start()

    # --- UI ---
    def _build_ui(self):
        # Widget central classique (fond géré par la feuille de style)
        central = QWidget()
        central.setObjectName("mainWidget")
        self.setCentralWidget(central)

        # Marges fixes autour du panneau pour un rendu symétrique et esthétique
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(
            24, 24, 24, 24
        )  # Marges symétriques : gauche, haut, droite, bas
        main_layout.setSpacing(0)

        # Panneau interfaces (panneau unique 400x900 - agrandi de 50%)
        interfaces_panel = QWidget(central)
        interfaces_panel.setObjectName("interfacesPanel")
        interfaces_panel.setFixedSize(400, 900)

        left = QVBoxLayout(interfaces_panel)
        left.setContentsMargins(12, 12, 12, 12)
        left.setSpacing(10)

        # Ligne titre + bouton paramètres réseau global (sur la même hauteur)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)

        title = QLabel("ProxyZ")
        title.setObjectName("titleLabel")
        title_row.addWidget(title)

        # Bouton paramètres réseau global
        self.global_settings_button = QPushButton()
        self.global_settings_button.setObjectName("globalSettingsButton")
        self.global_settings_button.setText("⚙ Paramètres réseau")
        self.global_settings_button.setToolTip("Ouvrir les connexions réseau Windows")
        # Hauteur fixe pour garantir un vrai "pill button" arrondi
        self.global_settings_button.setFixedHeight(34)
        self.global_settings_button.clicked.connect(
            lambda: self.on_interface_settings_requested("")
        )
        # Laisse le titre à gauche et pousse le bouton vers le centre/droite
        title_row.addStretch(1)
        title_row.addWidget(self.global_settings_button)

        left.addLayout(title_row)

        # Statut global juste sous le titre
        self.global_status_label = QLabel("0 connexion / 0 proxy")
        self.global_status_label.setObjectName("globalStatus")
        left.addWidget(self.global_status_label)

        self.playwright_warmup_checkbox = QCheckBox(
            "Warmup Playwright au démarrage"
        )
        self.playwright_warmup_checkbox.setObjectName("playwrightWarmupCheckbox")
        self.playwright_warmup_checkbox.setToolTip(
            "Prépare les navigateurs reset (page modem) en arrière-plan au lancement. "
            "Interfaces distantes toujours exclues."
        )
        self.playwright_warmup_checkbox.stateChanged.connect(
            self._on_playwright_warmup_changed
        )
        left.addWidget(self.playwright_warmup_checkbox)

        self.auto_container = QWidget()
        self.auto_layout = QVBoxLayout(self.auto_container)
        self.auto_layout.setContentsMargins(0, 0, 0, 0)
        self.auto_layout.setSpacing(6)
        # Largeur raisonnable des cartes pour un rendu équilibré
        self.auto_container.setMaximumWidth(620)
        left.addWidget(self.auto_container)

        interfaces_header = QHBoxLayout()
        interfaces_header.setSpacing(8)
        interfaces_list_label = QLabel("Interfaces")
        interfaces_list_label.setObjectName("globalStatus")
        interfaces_header.addWidget(interfaces_list_label)
        interfaces_header.addStretch(1)
        self.add_remote_iface_button = QPushButton("+")
        self.add_remote_iface_button.setObjectName("addRemoteIfaceButton")
        self.add_remote_iface_button.setToolTip("Ajouter une interface réseau distante")
        self.add_remote_iface_button.clicked.connect(self._on_add_remote_interface)
        interfaces_header.addWidget(self.add_remote_iface_button)
        left.addLayout(interfaces_header)

        self.manual_list = ManualInterfacesList()
        self.manual_list.order_changed.connect(self.on_manual_order_changed)
        self.manual_list.user_interaction.connect(self._mark_user_interaction)
        self.manual_list.itemClicked.connect(self._on_manual_interface_clicked)
        self.manual_list.console_general_requested.connect(self._show_general_console)

        manual_scroll = QScrollArea()
        manual_scroll.setWidgetResizable(True)
        manual_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        manual_scroll.setFrameShape(QFrame.NoFrame)

        manual_container = QWidget()
        manual_container_layout = QVBoxLayout(manual_container)
        manual_container_layout.setContentsMargins(0, 0, 0, 0)
        manual_container_layout.addWidget(self.manual_list)
        manual_scroll.setWidget(manual_container)

        left.addWidget(manual_scroll, 1)

        # Utiliser un QSplitter pour rendre le panel de droite expandable
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        # Ajouter le panneau d'interfaces à gauche
        splitter.addWidget(interfaces_panel)

        # Panneau de droite pour ZRotate (agrandi de 50% en hauteur: 600 -> 900, et de 20% en largeur: 450 -> 540)
        zrotate_panel = QWidget(central)
        zrotate_panel.setObjectName("zrotatePanel")
        zrotate_panel.setMinimumWidth(400)  # Largeur minimale
        zrotate_panel.setMaximumWidth(
            1200
        )  # Largeur maximale pour permettre l'expansion
        zrotate_panel.setFixedHeight(900)

        zrotate_layout = QVBoxLayout(zrotate_panel)
        zrotate_layout.setContentsMargins(12, 12, 12, 12)
        zrotate_layout.setSpacing(10)

        # Onglets : configuration pool ZRotate / liste quarantaine
        self.zrotate_section_tabs = QTabWidget()
        self.zrotate_section_tabs.setObjectName("zrotateSectionTabs")

        tab_zrotate = QWidget()
        tab_zrotate.setObjectName("zrotateConfigPanel")
        tab_zrotate_layout = QVBoxLayout(tab_zrotate)
        tab_zrotate_layout.setContentsMargins(10, 10, 10, 10)
        tab_zrotate_layout.setSpacing(10)

        zrotate_title = QLabel("ZRotate - Rotation d'IP")
        zrotate_title.setObjectName("zrotateTitle")
        tab_zrotate_layout.addWidget(zrotate_title)

        interfaces_label = QLabel("Interfaces pour le pool d'IP:")
        interfaces_label.setObjectName("zrotateLabel")
        tab_zrotate_layout.addWidget(interfaces_label)

        self.zrotate_interfaces_list = QListWidget()
        self.zrotate_interfaces_list.setObjectName("zrotateInterfacesList")
        tab_zrotate_layout.addWidget(self.zrotate_interfaces_list, 2)

        self.zrotate_auto_start_checkbox = QCheckBox(
            "Démarrer automatiquement au lancement"
        )
        self.zrotate_auto_start_checkbox.setObjectName("zrotateAutoStartCheckbox")
        self.zrotate_auto_start_checkbox.stateChanged.connect(
            self._on_zrotate_auto_start_changed
        )
        tab_zrotate_layout.addWidget(self.zrotate_auto_start_checkbox)

        self.zrotate_start_button = QPushButton("Démarrer ZRotate")
        self.zrotate_start_button.setObjectName("zrotateStartButton")
        self.zrotate_start_button.setProperty("stopped", True)
        self.zrotate_start_button.clicked.connect(self.on_zrotate_toggle)
        self.zrotate_start_button.style().unpolish(self.zrotate_start_button)
        self.zrotate_start_button.style().polish(self.zrotate_start_button)
        tab_zrotate_layout.addWidget(self.zrotate_start_button)

        tab_quarantine = QWidget()
        tab_quarantine.setObjectName("zrotateQuarantinePanel")
        tab_quarantine_layout = QVBoxLayout(tab_quarantine)
        tab_quarantine_layout.setContentsMargins(10, 10, 10, 10)
        tab_quarantine_layout.setSpacing(8)

        quarantine_help = QLabel(
            "Clés exclues du pool après échecs répétés du script de reset. "
            "Un reset réussi les retire de cette liste."
        )
        quarantine_help.setObjectName("zrotateLabel")
        quarantine_help.setWordWrap(True)
        tab_quarantine_layout.addWidget(quarantine_help)

        self.zrotate_quarantine_status = QLabel("")
        self.zrotate_quarantine_status.setObjectName("zrotateQuarantineStatus")
        self.zrotate_quarantine_status.setWordWrap(True)
        tab_quarantine_layout.addWidget(self.zrotate_quarantine_status)

        self.zrotate_quarantine_list = QListWidget()
        self.zrotate_quarantine_list.setObjectName("zrotateQuarantineList")
        tab_quarantine_layout.addWidget(self.zrotate_quarantine_list, 1)

        self.zrotate_section_tabs.addTab(tab_zrotate, "ZRotate")
        self.zrotate_section_tabs.addTab(tab_quarantine, "Quarantaine")

        # Panel bas : Console de logs
        console_panel = QWidget()
        console_panel.setObjectName("zrotateConsolePanel")
        console_layout = QVBoxLayout(console_panel)
        console_layout.setContentsMargins(10, 10, 10, 10)
        console_layout.setSpacing(5)

        # Ligne titre + bouton clear
        console_title_row = QHBoxLayout()
        console_title_row.setSpacing(8)

        self.zrotate_general_console_button = QPushButton("Console générale")
        self.zrotate_general_console_button.setObjectName("zrotateGeneralConsoleButton")
        self.zrotate_general_console_button.setFixedHeight(28)
        self.zrotate_general_console_button.clicked.connect(self._show_general_console)
        self.zrotate_general_console_button.setVisible(False)
        console_title_row.addWidget(self.zrotate_general_console_button)

        self.zrotate_console_title = QLabel("Console ZRotate")
        self.zrotate_console_title.setObjectName("zrotateTitle")
        console_title_row.addWidget(self.zrotate_console_title)

        console_title_row.addStretch(1)

        # Bouton Clear
        self.zrotate_clear_button = QPushButton("Clear")
        self.zrotate_clear_button.setObjectName("zrotateClearButton")
        self.zrotate_clear_button.setFixedSize(60, 28)
        self.zrotate_clear_button.clicked.connect(self._clear_zrotate_console)
        console_title_row.addWidget(self.zrotate_clear_button, 0, Qt.AlignRight)

        console_layout.addLayout(console_title_row)

        # Statistiques ZRotate (total / succès / rejets)
        self.zrotate_stats_label = QLabel("0 requête · 0 OK · 0 rejetée")
        self.zrotate_stats_label.setObjectName("zrotateStatsLabel")
        console_layout.addWidget(self.zrotate_stats_label)

        self.zrotate_log_box = QTextEdit()
        self.zrotate_log_box.setReadOnly(True)
        self.zrotate_log_box.setObjectName("zrotateLogBox")
        console_layout.addWidget(self.zrotate_log_box, 1)

        self.zrotate_vertical_splitter = QSplitter(Qt.Vertical)
        self.zrotate_vertical_splitter.setObjectName("zrotateVerticalSplitter")
        self.zrotate_vertical_splitter.setChildrenCollapsible(False)
        self.zrotate_vertical_splitter.addWidget(self.zrotate_section_tabs)
        self.zrotate_vertical_splitter.addWidget(console_panel)
        self.zrotate_vertical_splitter.setStretchFactor(0, 3)
        self.zrotate_vertical_splitter.setStretchFactor(1, 2)
        self.zrotate_vertical_splitter.setSizes([540, 360])

        zrotate_layout.addWidget(self.zrotate_vertical_splitter, 1)

        # Ajouter le panneau ZRotate au splitter
        splitter.addWidget(zrotate_panel)

        # Définir les tailles initiales du splitter (400 pour interfaces, 540 pour ZRotate)
        splitter.setSizes([400, 540])

        # Ajouter le splitter au layout principal
        main_layout.addWidget(splitter)

        # Log box pour ProxyZ (gardé pour compatibilité mais caché)
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setObjectName("logBox")
        self.log_box.hide()

        # Style global moderne
        self.setStyleSheet(
            """
            QMainWindow {
                background-color: #000b1a;
            }
            QWidget#mainWidget {
                background: qradialgradient(
                    cx:0.5, cy:0.25, radius:1.1,
                    fx:0.5, fy:0.25,
                    stop:0   #0a7ce5,
                    stop:0.55 #0258b8,
                    stop:1   #02173a
                );
            }
            QWidget#interfacesPanel {
                background-color: #011324;
                border-radius: 18px;
                border: 1px solid rgba(15, 23, 42, 0.9);
                box-shadow: 0 12px 30px rgba(0, 0, 0, 0.65);
            }
            QLabel#titleLabel {
                color: #ecf0f1;
                font-size: 17px;
                font-weight: 600;
            }
            QLabel#sectionLabel {
                color: #95a5a6;
                font-size: 12px;
                text-transform: uppercase;
                letter-spacing: 0.12em;
            }
            QLabel#globalStatus {
                color: #ecf0f1;
                font-size: 13px;
                font-weight: 500;
            }
            QScrollArea {
                background-color: transparent;
                border: none;
            }
            QListWidget {
                background-color: #02172e;
                border-radius: 12px;
                border: 1px solid rgba(31, 41, 55, 0.9);
            }
            QPushButton#globalSettingsButton, QToolButton#globalSettingsButton {
                background-color: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #3b82f6,
                    stop:0.6 #2563eb,
                    stop:1 #1d4ed8
                );
                color: #f9fafb;
                border-radius: 17px;
                padding: 0 18px;
                font-size: 13px;
                font-weight: 600;
                letter-spacing: 0.02em;
                border: 1px solid rgba(15, 23, 42, 0.9);
                box-shadow: 0 4px 10px rgba(15, 23, 42, 0.65);
            }
            QPushButton#globalSettingsButton:hover {
                background-color: #2563eb;
            }
            QWidget#zrotatePanel {
                background-color: #011324;
                border-radius: 18px;
                border: 1px solid rgba(15, 23, 42, 0.9);
                box-shadow: 0 12px 30px rgba(0, 0, 0, 0.65);
            }
            QWidget#zrotateConfigPanel {
                background-color: #02172e;
                border-radius: 12px;
                border: 1px solid rgba(31, 41, 55, 0.9);
            }
            QWidget#zrotateQuarantinePanel {
                background-color: #02172e;
                border-radius: 12px;
                border: 1px solid rgba(31, 41, 55, 0.9);
            }
            QTabWidget#zrotateSectionTabs::pane {
                border: 1px solid rgba(31, 41, 55, 0.9);
                border-radius: 10px;
                top: -1px;
                background-color: #02172e;
            }
            QTabWidget#zrotateSectionTabs QTabBar::tab {
                background-color: #1a2f45;
                color: #bdc3c7;
                border: 1px solid rgba(31, 41, 55, 0.9);
                border-bottom: none;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                min-width: 6em;
                padding: 8px 14px;
                font-size: 12px;
            }
            QTabWidget#zrotateSectionTabs QTabBar::tab:selected {
                background-color: #02172e;
                color: #ecf0f1;
                font-weight: 600;
            }
            QTabWidget#zrotateSectionTabs QTabBar::tab:hover:!selected {
                background-color: #22313f;
            }
            QLabel#zrotateQuarantineStatus {
                color: #95a5a6;
                font-size: 11px;
            }
            QListWidget#zrotateQuarantineList {
                background-color: #22313f;
                border-radius: 6px;
                border: 1px solid rgba(255, 255, 255, 0.05);
                color: #ecf0f1;
                font-size: 12px;
            }
            QListWidget#zrotateQuarantineList::item {
                padding: 6px 8px;
            }
            QListWidget#zrotateQuarantineList::item:selected {
                background-color: rgba(52, 152, 219, 0.3);
            }
            QWidget#zrotateConsolePanel {
                background-color: #02172e;
                border-radius: 12px;
                border: 1px solid rgba(31, 41, 55, 0.9);
            }
            QSplitter#zrotateVerticalSplitter::handle {
                background-color: rgba(52, 152, 219, 0.28);
                height: 8px;
                margin: 4px 10px;
                border-radius: 4px;
            }
            QSplitter#zrotateVerticalSplitter::handle:hover {
                background-color: rgba(59, 130, 246, 0.65);
            }
            QLabel#zrotateTitle {
                color: #ecf0f1;
                font-size: 15px;
                font-weight: 600;
            }
            QLabel#zrotateLabel {
                color: #bdc3c7;
                font-size: 12px;
            }
            QLineEdit#zrotateUrlEdit {
                background-color: #22313f;
                border-radius: 6px;
                border: 1px solid rgba(255, 255, 255, 0.05);
                padding: 6px 10px;
                color: #ecf0f1;
                font-size: 12px;
            }
            QLineEdit#zrotateUrlEdit:focus {
                border: 1px solid #3498db;
            }
            QListWidget#zrotateInterfacesList {
                background-color: #22313f;
                border-radius: 6px;
                border: 1px solid rgba(255, 255, 255, 0.05);
                color: #ecf0f1;
                font-size: 12px;
            }
            QListWidget#zrotateInterfacesList::item {
                padding: 5px;
            }
            QListWidget#zrotateInterfacesList::item:selected {
                background-color: rgba(52, 152, 219, 0.3);
            }
            QLabel#zrotateStatsLabel {
                color: #bdc3c7;
                font-size: 11px;
            }
            QPushButton#zrotateStartButton {
                background-color: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #27ae60,
                    stop:0.6 #229954,
                    stop:1 #1e8449
                );
                color: #f9fafb;
                border-radius: 8px;
                padding: 8px 16px;
                font-size: 13px;
                font-weight: 600;
                border: 1px solid rgba(15, 23, 42, 0.9);
            }
            QPushButton#zrotateStartButton:hover {
                background-color: #229954;
            }
            QPushButton#zrotateStartButton[stopped="true"] {
                background-color: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #e74c3c,
                    stop:0.6 #c0392b,
                    stop:1 #a93226
                );
            }
            QPushButton#zrotateStartButton[stopped="true"]:hover {
                background-color: #c0392b;
            }
            QPushButton#zrotateClearButton {
                background-color: rgba(127, 140, 141, 0.25);
                color: #bdc3c7;
                border-radius: 6px;
                border: 1px solid rgba(127, 140, 141, 0.4);
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton#zrotateClearButton:hover {
                background-color: rgba(59, 130, 246, 0.35);
                color: #ffffff;
                border-color: rgba(59, 130, 246, 0.9);
            }
            QCheckBox#zrotateAutoStartCheckbox {
                color: #bdc3c7;
                font-size: 12px;
            }
            QCheckBox#zrotateAutoStartCheckbox::indicator {
                width: 16px;
                height: 16px;
            }
            QCheckBox#zrotateAutoStartCheckbox::indicator:unchecked {
                background-color: #22313f;
                border: 1px solid rgba(255, 255, 255, 0.2);
                border-radius: 3px;
            }
            QCheckBox#zrotateAutoStartCheckbox::indicator:checked {
                background-color: #27ae60;
                border: 1px solid #27ae60;
                border-radius: 3px;
            }
            QCheckBox#playwrightWarmupCheckbox {
                color: #bdc3c7;
                font-size: 12px;
            }
            QCheckBox#playwrightWarmupCheckbox::indicator {
                width: 16px;
                height: 16px;
            }
            QCheckBox#playwrightWarmupCheckbox::indicator:unchecked {
                background-color: #22313f;
                border: 1px solid rgba(255, 255, 255, 0.2);
                border-radius: 3px;
            }
            QCheckBox#playwrightWarmupCheckbox::indicator:checked {
                background-color: #27ae60;
                border: 1px solid #27ae60;
                border-radius: 3px;
            }
            QTextEdit#zrotateLogBox {
                background-color: #1a1a1a;
                border-radius: 6px;
                border: 1px solid rgba(255, 255, 255, 0.05);
                color: #ecf0f1;
                font-family: 'Consolas', 'Monaco', monospace;
                font-size: 11px;
            }
        """
            + INTERFACE_CARD_QSS
            + ZROTATE_INTERFACE_ROW_QSS
        )

    # --- Logging / titre ---
    def _rebuild_iface_port_map(self) -> None:
        mapping: dict[int, str] = {}
        for name, widget in self.interface_widgets.items():
            port = 0
            cfg = self.config.get("interface_proxies", {}).get(name) or {}
            try:
                port = int(cfg.get("port") or 0)
            except (TypeError, ValueError):
                port = 0
            if port <= 0 and widget is not None:
                try:
                    port = int((widget.port_edit.text() or "").strip())
                except ValueError:
                    port = 0
            if port > 0:
                mapping[port] = name
        self._iface_by_port = mapping

    def _interface_for_log_message(self, message: str) -> str | None:
        port_match = re.search(r"\[port (\d+)\]", message)
        if port_match:
            iface = self._iface_by_port.get(int(port_match.group(1)))
            if iface:
                return iface

        if message.startswith("Interfaces:"):
            return None

        names = sorted(self.interface_widgets.keys(), key=len, reverse=True)
        matched: list[str] = []
        for name in names:
            if (
                f"Egress: {name}" in message
                or f"egress: {name}" in message
                or f"→ {name} →" in message
                or f"→ {name} (" in message
                or f"'{name}'" in message
                or f'"{name}"' in message
                or f" {name}:" in message
                or f" pour {name}" in message
                or f" pour '{name}'" in message
                or f"Interface {name}" in message
                or f"Reset réussi pour {name}" in message
                or f"Reset réussi pour l'interface '{name}'" in message
                or f"Fin reset '{name}'" in message
            ):
                matched.append(name)

        if len(matched) == 1:
            return matched[0]
        return None

    def _ensure_console_buffer(self, key: str | None) -> None:
        if key not in self._console_lines:
            self._console_lines[key] = []

    def _scroll_console_to_bottom(self) -> None:
        cursor = self.zrotate_log_box.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.zrotate_log_box.setTextCursor(cursor)
        self.zrotate_log_box.ensureCursorVisible()
        scrollbar = self.zrotate_log_box.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _refresh_console_text(self) -> None:
        lines = self._console_lines.get(self._console_view, [])
        self.zrotate_log_box.setPlainText("\n".join(lines))
        self._scroll_console_to_bottom()

    def _show_general_console(self) -> None:
        self._console_view = None
        self.zrotate_console_title.setText("Console ZRotate")
        self.zrotate_general_console_button.setVisible(False)
        self._refresh_console_text()

    def _show_interface_console(self, interface_name: str) -> None:
        self._console_view = interface_name
        self._ensure_console_buffer(interface_name)
        self.zrotate_console_title.setText(f"Console — {interface_name}")
        self.zrotate_general_console_button.setVisible(True)
        self._refresh_console_text()

    def _on_manual_interface_clicked(self, item: QListWidgetItem) -> None:
        widget = self.manual_list.itemWidget(item)
        if isinstance(widget, InterfaceWidget):
            self._show_interface_console(widget.interface_name)

    def _append_log(self, text: str):
        timestamp = time.strftime("%H:%M:%S")
        self.log_box.append(f"[{timestamp}] {text}")

    def _update_window_title(self):
        # Connexions actives : IP publique résolue (locales + distantes)
        try:
            online_count = 0
            for info in self._get_all_interfaces().values():
                if not info.public_ip:
                    continue
                if info.is_remote:
                    if info.online:
                        online_count += 1
                elif info.is_up and info.local_ip:
                    online_count += 1
        except Exception:
            online_count = 0

        self.setWindowTitle(f"ProxyZ - {online_count} Co / {self.active_proxies} Prox")

        self.global_status_label.setText(
            f"{online_count} connexion{'s' if online_count != 1 else ''} / "
            f"{self.active_proxies} proxy actif{'s' if self.active_proxies != 1 else ''}"
        )

    # --- Config / persistance ---
    def _read_playwright_warmup_enabled_from_config(self) -> bool:
        val = self.config.get(
            "playwright_warmup_enabled", DEFAULT_PLAYWRIGHT_WARMUP_ENABLED
        )
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            return bool(val)
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return DEFAULT_PLAYWRIGHT_WARMUP_ENABLED

    def _config_path(self) -> Path:
        return get_app_dir() / self.CONFIG_FILE

    def _load_config_disk(self) -> dict:
        config_path = self._config_path()
        if not config_path.is_file():
            return {}
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            return dict(loaded) if isinstance(loaded, dict) else {}
        except Exception:
            return {}

    def _merge_write_config_disk(self, merge_fn: Callable[[dict], None]) -> None:
        """Fusion partielle dans proxy_configs.json (ne remplace pas tout le fichier)."""
        try:
            data = self._load_config_disk()
            merge_fn(data)
            with open(self._config_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"Erreur sauvegarde config (merge): {e}")

    def _save_interface_proxy_config(self, name: str) -> None:
        """Persiste port/enabled d'une interface (merge, sans toucher reset_script etc.)."""
        iface_cfg = (self.config.get("interface_proxies") or {}).get(name)
        if not iface_cfg:
            return
        port = iface_cfg.get("port")
        enabled = bool(iface_cfg.get("enabled", False))
        is_remote = bool(name in self._remote_interfaces or iface_cfg.get("remote"))

        def merge(data: dict) -> None:
            entry = dict((data.get("interface_proxies") or {}).get(name) or {})
            if port is not None:
                entry["port"] = port
            entry["enabled"] = enabled
            if is_remote:
                entry["remote"] = True
            data.setdefault("interface_proxies", {})[name] = entry

        self._merge_write_config_disk(merge)

    def _save_interface_proxy_rename(self, old_name: str, new_name: str) -> None:
        """Renomme les clés interface dans proxy_configs.json (merge)."""

        def merge(data: dict) -> None:
            proxies = data.setdefault("interface_proxies", {})
            if old_name in proxies and new_name not in proxies:
                proxies[new_name] = proxies.pop(old_name)
            remote = data.setdefault("remote_interfaces", {})
            if old_name in remote and new_name not in remote:
                remote[new_name] = remote.pop(old_name)
            zrotate = data.setdefault("zrotate", {})
            selected = zrotate.get("selected_interfaces")
            if isinstance(selected, list) and old_name in selected:
                zrotate["selected_interfaces"] = [
                    new_name if n == old_name else n for n in selected
                ]

        self._merge_write_config_disk(merge)

    def _save_zrotate_auto_start_config(self) -> None:
        if not hasattr(self, "zrotate_auto_start_checkbox"):
            return
        auto = self.zrotate_auto_start_checkbox.checkState() == Qt.Checked
        self.config.setdefault("zrotate", {})["auto_start"] = auto

        def merge(data: dict) -> None:
            data.setdefault("zrotate", {})["auto_start"] = auto

        self._merge_write_config_disk(merge)

    def _zrotate_selection_from_ui(self) -> set[str]:
        selected: set[str] = set()
        rows = getattr(self, "_zrotate_interface_rows", None) or {}
        for name, row_widget in rows.items():
            if not isinstance(row_widget, ZRotateInterfaceRow):
                continue
            try:
                if row_widget.is_checked():
                    selected.add(name)
            except RuntimeError:
                continue
        return selected

    def _sync_zrotate_selection_from_ui(self) -> None:
        """Source de vérité : cases cochées visibles dans la liste ZRotate."""
        rows = getattr(self, "_zrotate_interface_rows", None) or {}
        if not rows:
            return
        visible = set(rows.keys())
        ui_checked = self._zrotate_selection_from_ui()
        self.zrotate_selected_interfaces = (
            (self.zrotate_selected_interfaces - visible) | ui_checked
        )

    def _load_config(self):
        try:
            with open(self._config_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                # Ancien format -> migration simple vers mapping interfaces
                mapping = {}
                for entry in data:
                    iface_name = entry.get("interface_name") or entry.get("name")
                    port = entry.get("port")
                    if iface_name and port:
                        mapping[iface_name] = {"enabled": True, "port": port}
                self.config = {
                    "interface_proxies": mapping,
                    "ui": {},
                    "interface_aliases": {},
                    "zrotate": {},
                    "remote_interfaces": {},
                }
                self.config.setdefault("reset_script_default", DEFAULT_RESET_SCRIPT)

                def merge(data: dict) -> None:
                    data.setdefault("interface_proxies", {}).update(mapping)

                self._merge_write_config_disk(merge)
            else:
                # Garder tout le JSON (dont reset_script_default) et s'assurer que les clés attendues existent
                self.config = dict(data)
                self.config.setdefault("interface_proxies", {})
                self.config.setdefault("ui", {})
                self.config.setdefault("interface_aliases", {})
                self.config.setdefault("zrotate", {})
                self.config.setdefault("remote_interfaces", {})
        except FileNotFoundError:
            self.config = {
                "interface_proxies": {},
                "ui": {},
                "interface_aliases": {},
                "zrotate": {},
                "remote_interfaces": {},
            }
            self.config.setdefault("reset_script_default", DEFAULT_RESET_SCRIPT)
        except Exception as e:
            print(f"Erreur de chargement config: {e}")
            self.config = {
                "interface_proxies": {},
                "ui": {},
                "interface_aliases": {},
                "zrotate": {},
                "remote_interfaces": {},
            }
            self.config.setdefault("reset_script_default", DEFAULT_RESET_SCRIPT)

        # Charger la configuration ZRotate (jamais accepter 1 pour max_requests_per_quota)
        zrotate_cfg = self.config.setdefault("zrotate", {})
        _max_req = zrotate_cfg.get("max_requests_per_quota", 2)
        if not isinstance(_max_req, int) or _max_req < 2:
            _max_req = 2
        zrotate_cfg["max_requests_per_quota"] = _max_req
        zrotate_cfg.setdefault("quota_timeout_seconds", 60.0)
        zrotate_cfg.setdefault("close_haapi_tunnel_after_seconds", 0.0)
        self.zrotate_server_url = str(
            zrotate_cfg.get("server_url", "http://127.0.0.1:9999")
        ).strip() or "http://127.0.0.1:9999"
        zrotate_cfg["server_url"] = self.zrotate_server_url
        if "selected_interfaces" in zrotate_cfg:
            self.zrotate_selected_interfaces = set(zrotate_cfg["selected_interfaces"])
        if "auto_start" in zrotate_cfg and hasattr(self, "zrotate_auto_start_checkbox"):
            self.zrotate_auto_start_checkbox.setCheckState(
                Qt.Checked if zrotate_cfg["auto_start"] else Qt.Unchecked
            )

        self.playwright_warmup_enabled = self._read_playwright_warmup_enabled_from_config()
        self.config["playwright_warmup_enabled"] = self.playwright_warmup_enabled
        if hasattr(self, "playwright_warmup_checkbox"):
            blocked = self.playwright_warmup_checkbox.blockSignals(True)
            self.playwright_warmup_checkbox.setCheckState(
                Qt.Checked if self.playwright_warmup_enabled else Qt.Unchecked
            )
            self.playwright_warmup_checkbox.blockSignals(blocked)

        ui = self.config.get("ui", {})
        size = ui.get("last_window_size")
        if isinstance(size, list) and len(size) == 2:
            self.resize(size[0], size[1])
        else:
            # Taille de départ qui épouse le panneau central (agrandie de 50% en hauteur et 20% en largeur pour le panel droit)
            self.resize(1070, 920)  # Largeur: 400 + 540 + 20 espacement + marges ≈ 1070

        self._load_remote_interfaces_from_config()

    def _get_all_interfaces(self) -> dict[str, InterfaceInfo]:
        merged = dict(self.interface_manager.interfaces)
        merged.update(self._remote_interfaces)
        return merged

    def _build_remote_proxy_entries(self, name: str) -> tuple[dict, dict]:
        """Entrées synchronisées interface_proxies + remote_interfaces pour une distante."""
        info = self._remote_interfaces.get(name)
        if not info:
            return {}, {}
        iface_cfg = self.config.get("interface_proxies", {}).get(name) or {}
        remote_cfg = (self.config.get("remote_interfaces") or {}).get(name) or {}

        try:
            port = int(iface_cfg.get("port") or remote_cfg.get("port") or 0)
        except (TypeError, ValueError):
            port = 0

        enabled = bool(iface_cfg.get("enabled", remote_cfg.get("enabled", False)))
        reset_script = str(
            iface_cfg.get("reset_script") or remote_cfg.get("reset_script") or ""
        ).strip()

        remote_entry: dict = {
            "upstream_host": info.upstream_host,
            "upstream_port": info.upstream_port,
            "port": port or None,
            "enabled": enabled,
        }
        proxy_entry: dict = {
            "port": port or None,
            "enabled": enabled,
            "remote": True,
        }
        if reset_script:
            remote_entry["reset_script"] = reset_script
            proxy_entry["reset_script"] = reset_script
        return proxy_entry, remote_entry

    def _apply_remote_proxy_entries(self, name: str, proxy_entry: dict, remote_entry: dict) -> None:
        """Met à jour self.config (les deux sections) pour une interface distante."""
        if proxy_entry:
            self.config.setdefault("interface_proxies", {})[name] = dict(proxy_entry)
        if remote_entry:
            self.config.setdefault("remote_interfaces", {})[name] = dict(remote_entry)

    def _load_remote_interfaces_from_config(self) -> None:
        self._remote_interfaces.clear()
        remote_cfg = self.config.get("remote_interfaces") or {}
        if not isinstance(remote_cfg, dict):
            return
        for name, entry in remote_cfg.items():
            if not isinstance(entry, dict):
                continue
            host = str(entry.get("upstream_host") or "").strip()
            try:
                upstream_port = int(entry.get("upstream_port") or 0)
            except (TypeError, ValueError):
                upstream_port = 0
            if not host or upstream_port <= 0:
                continue
            self._remote_interfaces[name] = InterfaceInfo(
                idx=-1,
                name=name,
                metric=10000,
                automatic=False,
                state="connected",
                is_up=True,
                local_ip=None,
                public_ip=None,
                online=False,
                is_remote=True,
                upstream_host=host,
                upstream_port=upstream_port,
            )
            # Source de vérité au chargement : remote_interfaces → interface_proxies
            try:
                local_port = int(entry.get("port") or 0)
            except (TypeError, ValueError):
                local_port = 0
            enabled = bool(entry.get("enabled", False))
            reset_script = str(entry.get("reset_script") or "").strip()
            remote_entry: dict = {
                "upstream_host": host,
                "upstream_port": upstream_port,
                "port": local_port or None,
                "enabled": enabled,
            }
            proxy_entry: dict = {
                "port": local_port or None,
                "enabled": enabled,
                "remote": True,
            }
            if reset_script:
                remote_entry["reset_script"] = reset_script
                proxy_entry["reset_script"] = reset_script
            self._apply_remote_proxy_entries(name, proxy_entry, remote_entry)

    def _save_remote_interfaces_config(self) -> None:
        """Persiste remote_interfaces + interface_proxies (entrées distantes) dans proxy_configs.json."""
        try:
            config_path = self._config_path()
            data: dict = {}
            if config_path.is_file():
                with open(config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    data = loaded

            remote_payload: dict[str, dict] = {}
            iface_proxies_on_disk = data.setdefault("interface_proxies", {})
            for name in self._remote_interfaces:
                proxy_entry, remote_entry = self._build_remote_proxy_entries(name)
                remote_payload[name] = remote_entry
                iface_proxies_on_disk[name] = proxy_entry
                self._apply_remote_proxy_entries(name, proxy_entry, remote_entry)

            data["remote_interfaces"] = remote_payload
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

            self.config["remote_interfaces"] = remote_payload
            self.config["interface_proxies"] = {
                **self.config.get("interface_proxies", {}),
                **{name: iface_proxies_on_disk[name] for name in remote_payload},
            }
        except Exception as e:
            print(f"Erreur sauvegarde interfaces distantes: {e}")

    def _suggest_next_local_port(self) -> int:
        ports: set[int] = set()
        for cfg in self.config.get("interface_proxies", {}).values():
            if not isinstance(cfg, dict):
                continue
            try:
                port = int(cfg.get("port") or 0)
            except (TypeError, ValueError):
                port = 0
            if port > 0:
                ports.add(port)
        for entry in (self.config.get("remote_interfaces") or {}).values():
            if not isinstance(entry, dict):
                continue
            try:
                port = int(entry.get("port") or 0)
            except (TypeError, ValueError):
                port = 0
            if port > 0:
                ports.add(port)
        for widget in self.interface_widgets.values():
            try:
                port = int((widget.port_edit.text() or "").strip())
            except ValueError:
                port = 0
            if port > 0:
                ports.add(port)
        return max(max(ports, default=100) + 1, 101)

    def _is_local_port_in_use(self, port: int, exclude_name: str | None = None) -> bool:
        for name, thread in self.proxy_threads.items():
            if exclude_name and name == exclude_name:
                continue
            cfg = getattr(thread, "config", None)
            if cfg and getattr(thread, "running", False) and cfg.port == port:
                return True
        for name, widget in self.interface_widgets.items():
            if exclude_name and name == exclude_name:
                continue
            try:
                w_port = int((widget.port_edit.text() or "").strip())
            except ValueError:
                continue
            if w_port == port:
                return True
        return False

    def _create_interface_widget(self, info: InterfaceInfo) -> InterfaceWidget:
        widget = InterfaceWidget(info)
        widget.proxy_toggled.connect(self.on_proxy_toggled)
        widget.rename_requested.connect(self.on_interface_rename_requested)
        widget.settings_requested.connect(self.on_interface_settings_requested)
        widget.reset_requested.connect(self.on_interface_reset_requested)
        widget.delete_requested.connect(self.on_remote_interface_delete_requested)
        widget.edit_requested.connect(self._on_edit_remote_interface)
        widget.user_interaction.connect(self._mark_user_interaction)
        self.interface_widgets[info.name] = widget

        iface_cfg = self.config.get("interface_proxies", {}).get(info.name)
        if iface_cfg:
            port = iface_cfg.get("port")
            if port:
                widget.set_port(port)
        return widget

    def _remote_iface_reset_script(self, name: str) -> str:
        iface_cfg = self.config.get("interface_proxies", {}).get(name) or {}
        remote_cfg = (self.config.get("remote_interfaces") or {}).get(name) or {}
        return str(
            iface_cfg.get("reset_script") or remote_cfg.get("reset_script") or ""
        ).strip()

    def _sync_remote_reset_badge(self, name: str) -> None:
        widget = self.interface_widgets.get(name)
        if widget and name in self._remote_interfaces:
            widget.set_remote_reset_visible(bool(self._remote_iface_reset_script(name)))

    def _prompt_remote_interface_dialog(
        self, edit_name: str | None = None
    ) -> tuple[str, str, int, int, str] | None:
        dialog = RemoteInterfaceDialog(self, edit_mode=edit_name is not None)
        if edit_name:
            info = self._remote_interfaces.get(edit_name)
            if not info:
                return None
            iface_cfg = self.config.get("interface_proxies", {}).get(edit_name) or {}
            remote_cfg = (self.config.get("remote_interfaces") or {}).get(
                edit_name
            ) or {}
            try:
                local_port = int(
                    iface_cfg.get("port") or remote_cfg.get("port") or 0
                )
            except (TypeError, ValueError):
                local_port = 0
            dialog.set_values(
                name=edit_name,
                upstream_host=info.upstream_host or "",
                upstream_port=int(info.upstream_port or 0),
                local_port=local_port,
                reset_script=self._remote_iface_reset_script(edit_name),
            )
        else:
            dialog.port_edit.setText(str(self._suggest_next_local_port()))

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        values = dialog.values()
        if not values:
            QMessageBox.warning(
                self,
                "Champs invalides",
                "Vérifiez le nom, le proxy amont (IP:port) et le port local du relais.",
            )
            return None
        reset_script = values[4]
        if reset_script:
            script_path = resolve_reset_script_path(
                normalize_reset_script_storage_path(reset_script), get_app_dir()
            )
            if not script_path.exists():
                QMessageBox.warning(
                    self,
                    "Script introuvable",
                    f"Le script de reset est introuvable :\n{script_path}",
                )
                return None
        return values

    def _apply_reset_script_cfg(self, cfg: dict, reset_script: str) -> None:
        script = normalize_reset_script_storage_path(reset_script)
        if script:
            cfg["reset_script"] = script
        else:
            cfg.pop("reset_script", None)

    def _apply_remote_interface_config(
        self,
        *,
        old_name: str | None,
        new_name: str,
        upstream_host: str,
        upstream_port: int,
        local_port: int,
        reset_script: str,
        is_new: bool,
    ) -> bool:
        if is_new:
            if new_name in self._get_all_interfaces():
                QMessageBox.warning(
                    self,
                    "Nom déjà utilisé",
                    f"Une interface nommée « {new_name} » existe déjà.",
                )
                return False
        else:
            if not old_name or old_name not in self._remote_interfaces:
                return False
            if new_name != old_name and new_name in self._get_all_interfaces():
                QMessageBox.warning(
                    self,
                    "Nom déjà utilisé",
                    f"Une interface nommée « {new_name} » existe déjà.",
                )
                return False

        exclude = old_name if old_name and not is_new else None
        if self._is_local_port_in_use(local_port, exclude_name=exclude):
            QMessageBox.warning(
                self,
                "Port déjà utilisé",
                f"Le port local {local_port} est déjà utilisé par un autre proxy.",
            )
            return False

        was_running = bool(old_name and old_name in self._running_proxies)
        widget = self.interface_widgets.get(old_name) if old_name else None
        if widget and was_running:
            self._stop_proxy_for_widget(widget, silent=True)

        if is_new:
            info = InterfaceInfo(
                idx=-1,
                name=new_name,
                metric=10000,
                automatic=False,
                state="connected",
                is_up=True,
                local_ip=None,
                public_ip=None,
                online=False,
                is_remote=True,
                upstream_host=upstream_host,
                upstream_port=upstream_port,
            )
            self._remote_interfaces[new_name] = info
            iface_cfg: dict = {
                "port": local_port,
                "enabled": False,
                "remote": True,
            }
            self._apply_reset_script_cfg(iface_cfg, reset_script)
            self.config.setdefault("interface_proxies", {})[new_name] = iface_cfg
            remote_entry = {
                "upstream_host": upstream_host,
                "upstream_port": upstream_port,
                "port": local_port,
                "enabled": False,
            }
            self._apply_reset_script_cfg(remote_entry, reset_script)
            self.config.setdefault("remote_interfaces", {})[new_name] = remote_entry
            self._save_remote_interfaces_config()
            self.zrotate_selected_interfaces.add(new_name)
            self._save_zrotate_selection_config()

            widget = self._create_interface_widget(info)
            widget.update_from_interface(info)
            widget.set_display_name(self._get_interface_display_name(new_name))
            self._sync_remote_reset_badge(new_name)
            item = QListWidgetItem()
            item.setSizeHint(widget.sizeHint())
            self.manual_list.addItem(item)
            self.manual_list.setItemWidget(item, widget)

            self._update_window_title()
            self._maybe_rebuild_zrotate_interfaces_list(force=True)
            self._append_log(
                f"Interface distante ajoutée: {new_name} "
                f"(relais 127.0.0.1:{local_port} → {upstream_host}:{upstream_port})"
            )
            self._refresh_remote_public_ip(new_name, force=True)
            return True

        assert old_name is not None
        info = self._remote_interfaces[old_name]
        info.name = new_name
        info.upstream_host = upstream_host
        info.upstream_port = upstream_port

        iface_proxies = self.config.setdefault("interface_proxies", {})
        remote_interfaces = self.config.setdefault("remote_interfaces", {})
        old_iface_cfg = dict(iface_proxies.get(old_name) or {})
        old_remote_cfg = dict(remote_interfaces.get(old_name) or {})
        was_enabled = bool(
            old_iface_cfg.get("enabled", old_remote_cfg.get("enabled", False))
        )

        if new_name != old_name:
            self._remote_interfaces.pop(old_name, None)
            self._remote_interfaces[new_name] = info
            if old_name in self.interface_widgets:
                self.interface_widgets[new_name] = self.interface_widgets.pop(old_name)
            if old_name in self.proxy_threads:
                self.proxy_threads[new_name] = self.proxy_threads.pop(old_name)
            if old_name in self._running_proxies:
                self._running_proxies.discard(old_name)
                self._running_proxies.add(new_name)
            if old_name in self.zrotate_selected_interfaces:
                self.zrotate_selected_interfaces.discard(old_name)
                self.zrotate_selected_interfaces.add(new_name)
            iface_proxies.pop(old_name, None)
            remote_interfaces.pop(old_name, None)

        iface_cfg = {
            **old_iface_cfg,
            "port": local_port,
            "enabled": was_enabled,
            "remote": True,
        }
        self._apply_reset_script_cfg(iface_cfg, reset_script)
        iface_proxies[new_name] = iface_cfg

        remote_entry = {
            **old_remote_cfg,
            "upstream_host": upstream_host,
            "upstream_port": upstream_port,
            "port": local_port,
            "enabled": was_enabled,
        }
        self._apply_reset_script_cfg(remote_entry, reset_script)
        remote_interfaces[new_name] = remote_entry

        widget = self.interface_widgets.get(new_name)
        if widget:
            widget.interface_name = new_name
            widget.update_from_interface(info)
            widget.set_display_name(self._get_interface_display_name(new_name))
            widget.set_port(local_port)
            self._sync_remote_reset_badge(new_name)
            self._sync_manual_list_item_height(new_name)

        self._save_remote_interfaces_config()
        self._save_zrotate_selection_config()
        self._maybe_rebuild_zrotate_interfaces_list(force=True)
        self._update_window_title()
        self._append_log(
            f"Interface distante modifiée: {new_name} "
            f"(relais 127.0.0.1:{local_port} → {upstream_host}:{upstream_port})"
        )

        if was_running and widget:
            self._start_proxy_for_widget(widget, local_port, auto=True)
        else:
            self._refresh_remote_public_ip(new_name, force=True)
        return True

    def _on_add_remote_interface(self):
        self._mark_user_interaction()
        values = self._prompt_remote_interface_dialog()
        if not values:
            return
        name, upstream_host, upstream_port, local_port, reset_script = values
        self._apply_remote_interface_config(
            old_name=None,
            new_name=name,
            upstream_host=upstream_host,
            upstream_port=upstream_port,
            local_port=local_port,
            reset_script=reset_script,
            is_new=True,
        )

    def _on_edit_remote_interface(self, name: str):
        if name not in self._remote_interfaces:
            return
        self._mark_user_interaction()
        values = self._prompt_remote_interface_dialog(edit_name=name)
        if not values:
            return
        new_name, upstream_host, upstream_port, local_port, reset_script = values
        self._apply_remote_interface_config(
            old_name=name,
            new_name=new_name,
            upstream_host=upstream_host,
            upstream_port=upstream_port,
            local_port=local_port,
            reset_script=reset_script,
            is_new=False,
        )

    def on_remote_interface_delete_requested(self, name: str):
        if name not in self._remote_interfaces:
            return
        self._mark_user_interaction()
        answer = QMessageBox.question(
            self,
            "Supprimer l'interface distante",
            f"Supprimer « {name} » et arrêter son relais proxy ?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        widget = self.interface_widgets.get(name)
        if widget:
            self._stop_proxy_for_widget(widget, silent=True)
            for row in range(self.manual_list.count()):
                item = self.manual_list.item(row)
                if self.manual_list.itemWidget(item) is widget:
                    self.manual_list.removeItemWidget(item)
                    self.manual_list.takeItem(row)
                    break
            widget.deleteLater()
            self.interface_widgets.pop(name, None)

        self._remote_interfaces.pop(name, None)
        self.config.get("interface_proxies", {}).pop(name, None)
        self.config.get("remote_interfaces", {}).pop(name, None)
        if name in self.zrotate_selected_interfaces:
            self.zrotate_selected_interfaces.discard(name)
        self._save_remote_interfaces_config()
        self._save_zrotate_selection_config()
        self._maybe_rebuild_zrotate_interfaces_list(force=True)
        self._update_window_title()
        self._append_log(f"Interface distante supprimée: {name}")

    def _refresh_remote_public_ip(self, name: str, force: bool = False) -> None:
        info = self._remote_interfaces.get(name)
        if not info or not info.upstream_host or not info.upstream_port:
            return

        with self._remote_public_ip_inflight_lock:
            if name in self._remote_public_ip_inflight:
                return

        if not force:
            now = time.monotonic()
            last_ok = self._remote_public_ip_last_ok.get(name)
            if (
                last_ok
                and info.online
                and info.public_ip == last_ok[0]
                and (now - last_ok[1]) < INTERFACE_PUBLIC_IP_STABLE_SKIP_S
            ):
                return

        iface_cfg = self.config.get("interface_proxies", {}).get(name) or {}
        try:
            proxy_port = int(iface_cfg.get("port") or 0)
        except (TypeError, ValueError):
            proxy_port = 0

        use_local_relay = name in self._running_proxies and proxy_port > 0
        upstream_host = info.upstream_host
        upstream_port = int(info.upstream_port)

        with self._remote_public_ip_inflight_lock:
            self._remote_public_ip_inflight.add(name)

        def _worker():
            public_ip = None
            online = False
            proxy_urls: list[str] = []
            if use_local_relay:
                proxy_urls.append(f"http://127.0.0.1:{proxy_port}")
            proxy_urls.append(f"http://{upstream_host}:{upstream_port}")

            services = [
                "https://api.ipify.org",
                "https://ifconfig.me",
                "https://icanhazip.com",
            ]
            for proxy_url in proxy_urls:
                for service in services:
                    try:
                        with httpx.Client(proxy=proxy_url, timeout=8.0) as client:
                            response = client.get(service)
                            if response.status_code == 200:
                                candidate = response.text.strip()
                                if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", candidate):
                                    public_ip = candidate
                                    online = True
                                    break
                    except Exception:
                        continue
                if online:
                    break

            self.remote_public_ip_updated.emit(name, public_ip or "", online)
            with self._remote_public_ip_inflight_lock:
                self._remote_public_ip_inflight.discard(name)

        threading.Thread(target=_worker, daemon=True).start()

    def refresh_remote_public_ips(self, force: bool = False) -> None:
        for name in list(self._remote_interfaces.keys()):
            self._refresh_remote_public_ip(name, force=force)

    @Slot(str, str, bool)
    def _on_remote_public_ip_updated(self, name: str, public_ip: str, online: bool):
        info = self._remote_interfaces.get(name)
        widget = self.interface_widgets.get(name)
        if not info:
            return
        prev_ip = info.public_ip or ""
        prev_online = info.online
        info.public_ip = public_ip or None
        info.online = online
        if online and public_ip:
            self._remote_public_ip_last_ok[name] = (public_ip, time.monotonic())
        if widget:
            widget.update_from_interface(info)
        if (info.public_ip or "") != prev_ip or online != prev_online:
            self._update_window_title()
            self._refresh_zrotate_row_public_ip(name)

    def _save_zrotate_selection_config(self) -> None:
        """
        Persiste uniquement zrotate.selected_interfaces dans proxy_configs.json.
        Toujours actif (même si PERSIST_CONFIG_TO_DISK est False) pour ne pas
        écraser le reste du fichier édité à la main.
        """
        try:
            self._sync_zrotate_selection_from_ui()

            config_path = self._config_path()
            data: dict = {}
            if config_path.is_file():
                with open(config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    data = loaded

            zrotate_cfg = data.setdefault("zrotate", {})
            selected = sorted(self.zrotate_selected_interfaces)
            zrotate_cfg["selected_interfaces"] = selected

            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

            self.config.setdefault("zrotate", {})["selected_interfaces"] = list(selected)
        except Exception as e:
            print(f"Erreur sauvegarde sélection ZRotate: {e}")

    def _save_playwright_warmup_config(self) -> None:
        """Persiste playwright_warmup_enabled dans proxy_configs.json (merge)."""
        try:
            config_path = self._config_path()
            data: dict = {}
            if config_path.is_file():
                with open(config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    data = loaded

            data["playwright_warmup_enabled"] = bool(self.playwright_warmup_enabled)
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

            self.config["playwright_warmup_enabled"] = bool(
                self.playwright_warmup_enabled
            )
        except Exception as e:
            print(f"Erreur sauvegarde warmup Playwright: {e}")

    def _save_config(self):
        """Écriture complète désactivée — utiliser les _save_* merge par changement."""
        return

    def _prune_zrotate_selections(self) -> None:
        """Retire les sélections ZRotate pour interfaces hors ligne (sans rebuild liste)."""
        before = set(self.zrotate_selected_interfaces)
        available = {
            name
            for name, info in self._get_all_interfaces().items()
            if interface_is_usable(info)
        }
        self.zrotate_selected_interfaces &= available
        if self.zrotate_selected_interfaces != before:
            self._save_zrotate_selection_config()

    def _maybe_rebuild_zrotate_interfaces_list(self, force: bool = False) -> None:
        """Rebuild complet uniquement si l'ensemble d'interfaces visibles change."""
        sig = zrotate_visible_structure_signature(self._get_all_interfaces())
        if not force and sig == self._last_zrotate_structure_sig:
            self._prune_zrotate_selections()
            return
        self._last_zrotate_structure_sig = sig
        self._update_zrotate_interfaces_list()

    # --- Gestion des interfaces depuis InterfaceManager ---
    @Slot(list)
    def on_interfaces_updated(self, interfaces: list):
        self._prune_zrotate_selections()
        if time.time() - self.last_user_interaction < 2.5:
            try:
                self._maybe_rebuild_zrotate_interfaces_list(force=False)
            except Exception:
                traceback.print_exc()
            return
        try:
            # Mapping nom -> InterfaceInfo (locales + distantes)
            local_by_name = {i.name: i for i in interfaces}
            iface_by_name = {**local_by_name, **self._remote_interfaces}

            # Supprimer les widgets orphelins (interfaces disparues ou renommées)
            for old_name, w in list(self.interface_widgets.items()):
                if old_name not in iface_by_name:
                    # Retirer du layout AUTO
                    for i in range(self.auto_layout.count()):
                        item = self.auto_layout.itemAt(i)
                        if item.widget() is w:
                            self.auto_layout.removeWidget(w)
                            break
                    # Retirer de la liste MANUEL
                    for row in range(self.manual_list.count()):
                        item = self.manual_list.item(row)
                        if self.manual_list.itemWidget(item) is w:
                            self.manual_list.removeItemWidget(item)
                            self.manual_list.takeItem(row)
                            break
                    w.deleteLater()
                    del self.interface_widgets[old_name]

            auto_infos = [i for i in interfaces if i.automatic]
            manual_infos = [i for i in interfaces if not i.automatic]
            manual_infos.extend(self._remote_interfaces.values())

            auto_infos.sort(key=lambda x: (x.metric, x.name.lower()))
            manual_infos.sort(key=lambda x: (x.is_remote, x.metric, x.name.lower()))

            # Créer les widgets manquants pour AUTO
            for info in auto_infos:
                if info.name not in self.interface_widgets:
                    widget = self._create_interface_widget(info)
                    self.auto_layout.addWidget(widget)

            # Créer les widgets manquants pour MANUEL (+ distantes)
            for info in manual_infos:
                if info.name not in self.interface_widgets:
                    widget = self._create_interface_widget(info)

                    item = QListWidgetItem()
                    item.setSizeHint(widget.sizeHint())
                    self.manual_list.addItem(item)
                    self.manual_list.setItemWidget(item, widget)

            # Mettre à jour les infos et alias pour tous les widgets existants
            for name, info in iface_by_name.items():
                w = self.interface_widgets.get(name)
                if w:
                    w.update_from_interface(info)
                    w.set_display_name(self._get_interface_display_name(name))
                    if info.is_remote:
                        self._sync_remote_reset_badge(name)
                    self._update_reset_avg_ui(name)

            for row in range(self.manual_list.count()):
                item = self.manual_list.item(row)
                w = self.manual_list.itemWidget(item)
                if isinstance(w, InterfaceWidget):
                    item.setSizeHint(w.sizeHint())

            # Resynchroniser l'état visuel des widgets avec les ProxyThread existants
            for name, thread in self.proxy_threads.items():
                if getattr(thread, "config", None) and thread.running:
                    w = self.interface_widgets.get(name)
                    if w:
                        w.set_proxy_running(True, thread.config.port)

            # Mettre à jour le titre / compteur dès qu'on a une nouvelle photo des interfaces
            self._update_window_title()
            self._maybe_rebuild_zrotate_interfaces_list(force=False)
            self._rebuild_iface_port_map()
        except Exception:
            traceback.print_exc()

    @Slot(str, str, bool)
    def on_public_ip_updated(self, name: str, public_ip: str, online: bool):
        widget = self.interface_widgets.get(name)
        info = self.interface_manager.interfaces.get(name)
        if widget and info:
            widget.update_from_interface(info)
        # Chaque changement d'IP publique peut modifier le nombre de connexions actives
        self._update_window_title()
        self._refresh_zrotate_row_public_ip(name)

    def _get_interface_display_name(self, name: str) -> str:
        return name

    def _mark_user_interaction(self):
        self.last_user_interaction = time.time()

    @Slot(str, bool)
    def _on_interface_usage_changed(self, name: str, in_use: bool):
        """Met à jour le badge RESET → 'In use' ou 'RESET' selon l'état de la clé."""
        widget = self.interface_widgets.get(name)
        if widget:
            widget.set_reset_badge_in_use(in_use)

    def _release_interface_to_zrotate(self, name: str, reset_succeeded: bool):
        """Remet une interface en disponibilité dans ZRotate (succès ou échec)."""
        if self.zrotate_proxy_server and getattr(
            self.zrotate_proxy_server, "proxy_server", None
        ):
            qm = getattr(self.zrotate_proxy_server.proxy_server, "quota_manager", None)
            loop = getattr(self.zrotate_proxy_server, "loop", None)
            if qm and loop and loop.is_running():
                try:
                    asyncio.run_coroutine_threadsafe(
                        qm.release_interface_after_reset(name, reset_succeeded),
                        loop,
                    )
                except Exception as e:
                    print(f"[RESET] ⚠️ Erreur notification ZRotate: {e}")

    def _interface_has_reset(self, name: str) -> bool:
        if name in self._remote_interfaces:
            return bool(self._remote_iface_reset_script(name))
        iface_cfg = self.config.get("interface_proxies", {}).get(name)
        reset_script, is_explicit = resolve_interface_reset_script(
            iface_cfg,
            self.config.get("reset_script_default", DEFAULT_RESET_SCRIPT),
        )
        if is_explicit:
            return True
        script_path = resolve_reset_script_path(reset_script, get_app_dir())
        return is_playwright_reset_script(script_path)

    def _is_general_console_noise(self, message: str) -> bool:
        stripped = message.strip()
        if stripped.startswith("[RESET] runtime:"):
            return True
        if stripped.startswith("Call log:"):
            return True
        if stripped.startswith("- waiting for"):
            return True
        if stripped.startswith("- navigating to"):
            return True
        if stripped.startswith("- clicking"):
            return True
        if stripped.startswith("- filling"):
            return True
        return False

    def _resolve_log_interface(self, message: str) -> str | None:
        iface = self._interface_for_log_message(message)
        if iface:
            return iface
        if len(self._reset_in_progress) == 1:
            return next(iter(self._reset_in_progress))
        return None

    def _sync_manual_list_item_height(self, name: str) -> None:
        widget = self.interface_widgets.get(name)
        if not widget:
            return
        for row in range(self.manual_list.count()):
            item = self.manual_list.item(row)
            if self.manual_list.itemWidget(item) is widget:
                item.setSizeHint(widget.sizeHint())
                break

    def _record_reset_duration(self, name: str, elapsed_s: float) -> None:
        if elapsed_s <= 0:
            return
        self._reset_duration_sums[name] = (
            self._reset_duration_sums.get(name, 0.0) + elapsed_s
        )
        self._reset_duration_counts[name] = (
            self._reset_duration_counts.get(name, 0) + 1
        )

    def _update_reset_avg_ui(self, name: str) -> None:
        widget = self.interface_widgets.get(name)
        if not widget:
            return
        if not self._interface_has_reset(name):
            widget.set_reset_avg(None)
            return
        count = self._reset_duration_counts.get(name, 0)
        if count <= 0:
            widget.set_reset_avg(None)
            return
        avg = self._reset_duration_sums[name] / count
        widget.set_reset_avg(avg)
        self._sync_manual_list_item_height(name)

    @Slot(str, int, float)
    def _on_reset_completed(self, name: str, returncode: int, elapsed_s: float):
        """Appelé sur le thread Qt principal après la fin du script de reset (évite les crashes)."""
        self._reset_in_progress.discard(name)
        try:
            widget = self.interface_widgets.get(name)
            if widget:
                widget.set_reset_loading(False)
        except Exception as e:
            print(f"[RESET] ⚠️ Erreur mise à jour widget: {e}")

        if returncode == -1:
            try:
                QMessageBox.warning(
                    self,
                    "Reset impossible",
                    "Le script de reset est introuvable. Vérifiez la configuration (reset_script) ou placez le script dans le même dossier que l'application.",
                )
            except Exception as e:
                print(f"[RESET] ⚠️ Erreur affichage message: {e}")
        elif returncode == -2:
            print(
                f"[RESET] ⏱️ Timeout pour l'interface '{name}' — pas de remise dans le pool ZRotate"
            )
        elif returncode == 0:
            print(f"[RESET] ✅ Reset réussi pour l'interface '{name}'")
        else:
            print(
                f"[RESET] ❌ Reset échoué pour l'interface '{name}' (code {returncode}) — pas de remise dans le pool ZRotate"
            )

        if elapsed_s > 0 and returncode not in (-1, -2, -3):
            self._record_reset_duration(name, elapsed_s)
            self._update_reset_avg_ui(name)

        if returncode == 0 and name in self._remote_interfaces:
            self._remote_public_ip_last_ok.pop(name, None)
            QTimer.singleShot(
                2000,
                lambda n=name: self._refresh_remote_public_ip(n, force=True),
            )
            QTimer.singleShot(
                6000,
                lambda n=name: self._refresh_remote_public_ip(n, force=True),
            )

        try:
            self._release_interface_to_zrotate(name, returncode == 0)
        except Exception as e:
            print(f"[RESET] ⚠️ Erreur release ZRotate: {e}")
        # Un seul refresh 2s après le dernier reset (évite 6 refresh en rafale)
        self._refresh_after_reset_timer.start(2000)

    @Slot(str)
    def _on_reset_log(self, message: str):
        """Affiche les logs reset dans la console ZRotate (utile en .exe sans console)."""
        try:
            self._zrotate_log(message)
        except Exception:
            pass

    @Slot(str)
    def on_interface_reset_requested(self, name: str):
        """Reset manuel ou ZRotate : un thread par interface, en parallèle."""
        if name in self._remote_interfaces and not self._remote_iface_reset_script(name):
            QMessageBox.information(
                self,
                "Reset distant",
                "Aucun script de reset configuré pour cette interface distante.\n"
                "Clic-droit → Éditer pour en définir un.",
            )
            return
        if name in self._reset_in_progress:
            print(f"[RESET] ⏳ '{name}' déjà en cours, ignoré.")
            return

        interface_info = self._get_all_interfaces().get(name)
        if not interface_info:
            QMessageBox.warning(
                self, "Reset impossible", f"Interface '{name}' introuvable."
            )
            return

        widget = self.interface_widgets.get(name)
        proxy_port = None
        iface_cfg = self.config.get("interface_proxies", {}).get(name)
        if iface_cfg:
            proxy_port = iface_cfg.get("port")
        if not proxy_port and widget:
            port_text = widget.port_edit.text().strip()
            if port_text:
                try:
                    proxy_port = int(port_text)
                except ValueError:
                    pass

        if not proxy_port:
            QMessageBox.warning(
                self,
                "Reset impossible",
                f"Aucun port proxy configuré pour l'interface '{name}'.",
            )
            return

        reset_script, _ = resolve_interface_reset_script(
            iface_cfg,
            self.config.get("reset_script_default", DEFAULT_RESET_SCRIPT),
        )
        app_dir = get_app_dir()
        script_path = resolve_reset_script_path(reset_script, app_dir)

        self._reset_in_progress.add(name)
        if widget:
            widget.set_reset_loading(True)

        print(
            f"[RESET] Reset de l'interface '{name}' via {script_path.name} (port {proxy_port})..."
        )

        def run_reset():
            is_playwright_reset = is_playwright_reset_script(script_path)
            if not script_path.exists() and not is_playwright_reset:
                self.reset_completed.emit(name, -1, 0.0)
                return

            def ui_log(msg: str):
                self.reset_log.emit(msg)

            reset_options = extract_interface_reset_options(iface_cfg)
            gw = reset_options.get("modem_gateway")
            ui_log(
                f"[RESET] 🚀 Lancement {script_path.name} pour '{name}' (port {proxy_port}"
                + (f", gateway {gw})" if gw else ")")
            )
            try:
                t0 = time.time()
                return_code = run_reset_script(
                    script_path,
                    proxy_port,
                    120,
                    log_fn=ui_log,
                    reset_options=reset_options or None,
                )
                elapsed = time.time() - t0
                ui_log(
                    f"[RESET] ⏱️ Fin reset '{name}' (port {proxy_port}) en {elapsed:.1f}s, code={return_code}"
                )
                self.reset_completed.emit(name, return_code, elapsed)
            except subprocess.TimeoutExpired:
                ui_log(
                    f"[RESET] ⏱️ Timeout reset '{name}' (port {proxy_port}) après 120s"
                )
                self.reset_completed.emit(name, -2, 120.0)
            except Exception as e:
                ui_log(f"[RESET] 💥 Exception reset '{name}' (port {proxy_port}): {e}")
                traceback.print_exc()
                self.reset_completed.emit(name, -3, 0.0)

        threading.Thread(target=run_reset, daemon=True).start()

    @Slot(str)
    def on_interface_settings_requested(self, name: str):
        # Ouvre le panneau des connexions réseau Windows (ncpa.cpl)
        # L'utilisateur peut ensuite ouvrir les propriétés de l'interface voulue.
        try:
            subprocess.Popen(
                ["control.exe", "ncpa.cpl"],
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as e:
            QMessageBox.warning(
                self,
                "Ouverture des paramètres",
                f"Impossible d'ouvrir les paramètres réseau Windows : {e}",
            )

    @Slot(str)
    def on_interface_rename_requested(self, name: str):
        # Renomme le VRAI nom de l'interface Windows via netsh
        current = name
        new_name, ok = QInputDialog.getText(
            self,
            "Renommer l'interface",
            "Nouveau nom Windows :",
            QLineEdit.Normal,
            current,
        )
        new_name = new_name.strip()
        if not ok or not new_name or new_name == current:
            return

        try:
            # Utilise la forme positionnelle : set interface "<nom>" newname="<NouveauNom>"
            completed = subprocess.run(
                [
                    "netsh",
                    "interface",
                    "set",
                    "interface",
                    current,
                    f"newname={new_name}",
                ],
                capture_output=True,
                text=True,
                shell=False,
                creationflags=CREATE_NO_WINDOW,
            )
            if completed.returncode != 0:
                err = completed.stderr.strip() or completed.stdout.strip()
                QMessageBox.warning(
                    self,
                    "Renommage impossible",
                    "Netsh a refusé de renommer l'interface.\n\n"
                    f'Commande : netsh interface set interface "{current}" newname={new_name}\n\n'
                    f"Sortie : {err or 'Aucune sortie.'}\n\n"
                    "Assure-toi que ProxyZ est lancé en administrateur et que ce nom est valide.",
                )
                return
        except Exception as e:
            QMessageBox.warning(self, "Renommage impossible", str(e))
            return

        # Mettre à jour la config (ports/proxies) pour refléter le nouveau nom
        iface_cfgs = self.config.setdefault("interface_proxies", {})
        if current in iface_cfgs and new_name not in iface_cfgs:
            iface_cfgs[new_name] = iface_cfgs.pop(current)

        # Arrêter tout proxy associé à l'ancien nom
        old_thread = self.proxy_threads.pop(current, None)
        if old_thread:
            try:
                old_thread.stop()
                old_thread.wait(2000)
            except Exception:
                traceback.print_exc()

        self._save_interface_proxy_rename(current, new_name)
        # Forcer un refresh des interfaces pour récupérer le nouveau nom
        self._last_zrotate_structure_sig = None
        self.interface_manager.request_immediate_refresh()

    @Slot(str)
    def on_metrics_update_failed(self, message: str):
        QMessageBox.warning(self, "Métriques non appliquées", message)
        self._append_log(message)

    @Slot(list)
    def on_manual_order_changed(self, ordered_names: list):
        self._append_log(
            "Nouvel ordre manuel des interfaces: " + ", ".join(ordered_names)
        )
        self.interface_manager.apply_manual_order(ordered_names)

    # --- Proxy management ---
    @Slot(str, bool, int)
    def on_proxy_toggled(self, name: str, enabled: bool, port: int):
        widget = self.interface_widgets.get(name)
        if not widget:
            return

        if enabled:
            # On ne vérifie le conflit que par rapport aux ProxyThread connus,
            # on ne teste plus le port au niveau OS (l'utilisateur a indiqué
            # que seule cette application utilise ces ports).
            for other_name, thread in self.proxy_threads.items():
                if other_name == name:
                    continue
                if (
                    getattr(thread, "config", None)
                    and thread.config.port == port
                    and thread.running
                ):
                    QMessageBox.warning(
                        self,
                        "Port déjà utilisé",
                        f"Le port {port} est déjà utilisé par un autre proxy dans ProxyZ.",
                    )
                    widget.set_proxy_running(False)
                    return
            self._start_proxy_for_widget(widget, port, auto=False)
        else:
            self._stop_proxy_for_widget(widget)

    def _start_proxy_for_widget(self, widget: InterfaceWidget, port: int, auto: bool):
        name = widget.interface_name
        info = self._get_all_interfaces().get(name)
        if not info:
            widget.set_proxy_running(False)
            return

        if info.is_remote:
            if not info.upstream_host or not info.upstream_port:
                QMessageBox.warning(
                    self,
                    "Relais impossible",
                    "Proxy amont manquant pour cette interface distante.",
                )
                widget.set_proxy_running(False)
                return
        elif not info.local_ip:
            QMessageBox.warning(
                self,
                "Proxy impossible",
                "Cette interface n'a pas d'IPv4 locale valide.",
            )
            widget.set_proxy_running(False)
            return

        # Stopper un éventuel proxy existant sur cette interface (sans toucher au widget)
        existing = self.proxy_threads.get(name)
        if existing:
            try:
                existing.stop()
                existing.wait(2000)
            except Exception:
                traceback.print_exc()
            self.proxy_threads.pop(name, None)
            # S'assurer que ce proxy n'est plus comptabilisé comme actif
            if name in self._running_proxies:
                self._running_proxies.remove(name)
                self.active_proxies = len(self._running_proxies)
                self._update_window_title()

        if info.is_remote:
            config = ProxyConfig(
                name=name,
                bind_ip="",
                port=port,
                interface_name=name,
                is_remote=True,
                upstream_host=info.upstream_host,
                upstream_port=info.upstream_port,
            )
        else:
            config = ProxyConfig(
                name=name,
                bind_ip=info.local_ip,
                port=port,
                interface_name=name,
            )
        thread = ProxyThread(config)
        # Ne pas capturer directement le widget dans le slot, on utilise le nom
        thread.status_changed.connect(
            lambda running, iface=name, p=port: self._on_thread_status_changed(
                iface, running, p
            )
        )
        self.proxy_threads[name] = thread
        thread.start()

        # Mettre à jour config persistée (on n'écrit jamais reset_script : à ajouter à la main dans le JSON si besoin)
        iface_cfg = self.config.setdefault("interface_proxies", {}).setdefault(name, {})
        iface_cfg["port"] = port
        iface_cfg["enabled"] = True
        if info.is_remote:
            proxy_entry, remote_entry = self._build_remote_proxy_entries(name)
            proxy_entry["port"] = port
            proxy_entry["enabled"] = True
            remote_entry["port"] = port
            remote_entry["enabled"] = True
            self._apply_remote_proxy_entries(name, proxy_entry, remote_entry)
            self._save_remote_interfaces_config()
        self._save_interface_proxy_config(name)

        if not auto:
            if info.is_remote:
                self._append_log(
                    f"Relais démarré sur {name} "
                    f"(127.0.0.1:{port} → {info.upstream_host}:{info.upstream_port})"
                )
            else:
                self._append_log(
                    f"Proxy démarré sur {name} (127.0.0.1:{port}, source {info.local_ip})"
                )

        if info.is_remote:
            self._refresh_remote_public_ip(name)

    def _stop_proxy_for_widget(self, widget: InterfaceWidget, silent: bool = False):
        name = widget.interface_name
        thread = self.proxy_threads.get(name)
        if thread:
            try:
                thread.stop()
                thread.wait(2000)
            except Exception:
                pass
            self.proxy_threads.pop(name, None)
        # Retirer immédiatement ce proxy des actifs pour garder le compteur cohérent
        if name in self._running_proxies:
            self._running_proxies.remove(name)
            self.active_proxies = len(self._running_proxies)
            self._update_window_title()
        widget.set_proxy_running(False)

        iface_cfg = self.config.setdefault("interface_proxies", {}).setdefault(name, {})
        iface_cfg["enabled"] = False
        if name in self._remote_interfaces:
            proxy_entry, remote_entry = self._build_remote_proxy_entries(name)
            proxy_entry["enabled"] = False
            remote_entry["enabled"] = False
            self._apply_remote_proxy_entries(name, proxy_entry, remote_entry)
            self._save_remote_interfaces_config()
        self._save_interface_proxy_config(name)

        if not silent:
            self._append_log(f"Proxy arrêté pour {name}")

    def _restore_initial_proxies(self):
        """Démarre les proxies qui étaient actifs lors du dernier arrêt."""
        if self._initial_proxies_restored:
            return
        self._initial_proxies_restored = True

        iface_cfgs = self.config.get("interface_proxies", {})
        for name, cfg in iface_cfgs.items():
            if not cfg.get("enabled"):
                continue
            port = cfg.get("port")
            if not port:
                continue
            widget = self.interface_widgets.get(name)
            info = self._get_all_interfaces().get(name)
            if not widget or not info:
                continue
            if info.is_remote:
                if not info.upstream_host or not info.upstream_port:
                    continue
            elif not info.local_ip:
                continue
            # Démarrage silencieux en mode auto (pas de pop-up)
            self._start_proxy_for_widget(widget, port, auto=True)

    def _on_thread_status_changed(self, iface_name: str, running: bool, port: int):
        # Maintenir une vue cohérente des proxys effectivement actifs,
        # même en cas d'arrêts forcés ou d'erreurs de thread.
        if running:
            if iface_name not in self._running_proxies:
                self._running_proxies.add(iface_name)
        else:
            if iface_name in self._running_proxies:
                self._running_proxies.remove(iface_name)
        self.active_proxies = len(self._running_proxies)
        widget = self.interface_widgets.get(iface_name)
        if widget:
            widget.set_proxy_running(running, port if running else None)
        self._update_window_title()

    # --- ZRotate ---
    def _update_zrotate_interfaces_list(self):
        """Met à jour la liste des interfaces dans le panel ZRotate (rebuild complet, stable)."""
        prev_selected = set(self.zrotate_selected_interfaces)
        visible_in_rows = set(getattr(self, "_zrotate_interface_rows", {}) or {})
        current_selections = self._zrotate_selection_from_ui()
        if visible_in_rows:
            self.zrotate_selected_interfaces = (
                (self.zrotate_selected_interfaces - visible_in_rows)
                | current_selections
            )

        all_interfaces = self._get_all_interfaces()
        available_interfaces = {
            name for name, info in all_interfaces.items() if interface_is_usable(info)
        }
        self.zrotate_selected_interfaces &= available_interfaces

        visible_infos = [
            (name, info)
            for name, info in sorted(all_interfaces.items())
            if interface_is_usable(info)
        ]

        scroll_bar = self.zrotate_interfaces_list.verticalScrollBar()
        saved_scroll_value = scroll_bar.value()

        # Rebuild complet pour éviter tout pointeur Qt invalide après hot-plug interface.
        self.zrotate_interfaces_list.clear()
        self._zrotate_interface_rows = {}
        self._zrotate_interface_items = {}

        header_item = QListWidgetItem()
        header_widget = ZRotateInterfacesHeaderRow()
        self.zrotate_interfaces_list.addItem(header_item)
        self.zrotate_interfaces_list.setItemWidget(header_item, header_widget)
        header_item.setSizeHint(header_widget.sizeHint())
        self._zrotate_header_item = header_item
        self._zrotate_header_widget = header_widget

        for name, info in visible_infos:
            public_ip = info.public_ip or "-"
            display_name = self._get_interface_display_name(name)
            item = QListWidgetItem()
            row_widget = ZRotateInterfaceRow(name, public_ip, display_name=display_name)
            row_widget.set_checked(name in self.zrotate_selected_interfaces)
            row_widget.toggled.connect(self._on_zrotate_interface_toggled)
            self.zrotate_interfaces_list.addItem(item)
            self.zrotate_interfaces_list.setItemWidget(item, row_widget)
            item.setSizeHint(row_widget.sizeHint())
            self._zrotate_interface_rows[name] = row_widget
            self._zrotate_interface_items[name] = item

        self._sync_zrotate_row_pool_styles()

        def _restore_zrotate_list_scroll():
            sb = self.zrotate_interfaces_list.verticalScrollBar()
            sb.setValue(min(saved_scroll_value, sb.maximum()))

        QTimer.singleShot(0, _restore_zrotate_list_scroll)

        if self.zrotate_selected_interfaces != prev_selected:
            self._save_zrotate_selection_config()

    def _refresh_zrotate_row_public_ip(self, name: str):
        """Met à jour l'IP affichée sur une ligne ZRotate sans reconstruire toute la liste."""
        rows = getattr(self, "_zrotate_interface_rows", None) or {}
        row_widget = rows.get(name)
        if row_widget is None:
            return
        info = self._get_all_interfaces().get(name)
        if not info:
            return
        try:
            row_widget.set_public_ip(info.public_ip or "-")
        except RuntimeError:
            pass

    def _sync_zrotate_row_pool_styles(self):
        """Applique les couleurs d'état pool (runtime) aux lignes ZRotate."""
        rows = getattr(self, "_zrotate_interface_rows", None) or {}
        state = getattr(self, "_last_zrotate_pool_state", None) or {}
        if state == self._last_pool_state_for_ui:
            return
        self._last_pool_state_for_ui = (
            dict(state) if isinstance(state, dict) else {}
        )
        if not self.zrotate_running:
            for row in rows.values():
                try:
                    row.set_live_pool_state(False, None)
                except RuntimeError:
                    pass
            return

        selected = self.zrotate_selected_interfaces
        for name, row in rows.items():
            try:
                checked = row.is_checked() and name in selected
                snap = state.get(name)
                if checked:
                    # Cochée dans le pool : même brillance que les autres (pas de gris « hors session »)
                    if snap is None:
                        snap = {"in_pool": True}
                    row.set_live_pool_state(True, snap)
                elif snap is not None:
                    row.set_live_pool_state(True, snap)
                else:
                    row.set_live_pool_state(True, {"not_in_session": True})
            except RuntimeError:
                continue

    @Slot(object)
    def _on_pool_state_updated(self, state_obj: object):
        """Snapshot du pool (~1 Hz) depuis le thread ZRotate."""
        state = dict(state_obj) if isinstance(state_obj, dict) else {}
        if state == self._last_zrotate_pool_state:
            return
        self._last_zrotate_pool_state = state
        self._sync_zrotate_row_pool_styles()

    def _on_zrotate_interface_toggled(self, interface_name: str, state: int):
        """Gère le changement d'état d'une checkbox d'interface"""
        self._mark_user_interaction()  # Marquer l'interaction pour éviter les mises à jour pendant la sélection
        row_widget = getattr(self, "_zrotate_interface_rows", {}).get(interface_name)
        if isinstance(row_widget, ZRotateInterfaceRow):
            row_widget._apply_checked_visual_state()
        if state == Qt.Checked:
            self.zrotate_selected_interfaces.add(interface_name)
        else:
            self.zrotate_selected_interfaces.discard(interface_name)
        self._last_pool_state_for_ui = None
        self._sync_zrotate_row_pool_styles()

        # Si ZRotate est en cours d'exécution, redémarrer pour prendre en compte les changements
        if self.zrotate_running:
            self._zrotate_log(
                "⚠️ Redémarrage de ZRotate pour prendre en compte les changements..."
            )
            self._stop_zrotate()
            # Redémarrer après un court délai
            QTimer.singleShot(500, self._start_zrotate)

        self._save_zrotate_selection_config()

    def _clear_zrotate_console(self):
        """Efface la console actuellement affichée."""
        self._ensure_console_buffer(self._console_view)
        self._console_lines[self._console_view] = []
        self.zrotate_log_box.clear()

    def _zrotate_log(self, message: str):
        """Route les logs vers la console affichée (générale ou interface)."""
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        iface = self._resolve_log_interface(message)
        buffer_key = iface if iface else None
        if buffer_key is None and self._is_general_console_noise(message):
            return
        self._ensure_console_buffer(buffer_key)
        self._console_lines[buffer_key].append(line)
        if self._console_view == buffer_key:
            self.zrotate_log_box.append(line)
            self._scroll_console_to_bottom()

    @Slot(int, int, int)
    def _on_zrotate_stats_updated(self, total: int, successful: int, rejected: int):
        """Met à jour le label de stats ZRotate"""
        if rejected == 0:
            text = f"{total} requête{'s' if total != 1 else ''} · {successful} OK"
        else:
            text = (
                f"{total} requête{'s' if total != 1 else ''} · "
                f"{successful} OK · {rejected} rejetée{'s' if rejected != 1 else ''}"
            )
        self.zrotate_stats_label.setText(text)

    def _on_quota_stats_updated(self, stats: dict):
        """Met à jour les badges GET/CONNECT sans recréer la liste."""
        rows = getattr(self, "_zrotate_interface_rows", None)
        if not rows:
            return
        for name, data in stats.items():
            row_widget = rows.get(name)
            if row_widget is None:
                continue
            if not row_widget.is_checked():
                continue
            g_used, g_max = data.get("get", (0, 2))
            c_used, c_max = data.get("connect", (0, 2))
            try:
                row_widget.set_quota_values(g_used, g_max, c_used, c_max)
            except RuntimeError:
                continue

    def _set_quarantine_ui_stopped(self):
        """ZRotate arrêté : pas de données live côté quota manager."""
        self._last_quarantine_names = None
        self.zrotate_quarantine_list.clear()
        self.zrotate_quarantine_status.setText(
            "ZRotate est arrêté — démarrez-le pour afficher la quarantaine."
        )
        self.zrotate_quarantine_status.show()

    @Slot(object)
    def _on_quarantine_updated(self, names_obj: object):
        """Liste des interfaces en quarantaine (émise ~1x/s par le thread ZRotate)."""
        if not getattr(self, "zrotate_running", False):
            self._set_quarantine_ui_stopped()
            return
        names: list[str] = (
            list(names_obj) if names_obj is not None else []
        )
        names_key = tuple(sorted(names))
        if names_key == self._last_quarantine_names:
            return
        self._last_quarantine_names = names_key
        self.zrotate_quarantine_status.hide()
        self.zrotate_quarantine_list.clear()
        if not names:
            item = QListWidgetItem("Aucune clé en quarantaine")
            item.setFlags(
                item.flags()
                & ~Qt.ItemFlag.ItemIsSelectable
                & ~Qt.ItemFlag.ItemIsEnabled
            )
            self.zrotate_quarantine_list.addItem(item)
        else:
            for n in names:
                self.zrotate_quarantine_list.addItem(QListWidgetItem(n))

    def _update_zrotate_button_state(self):
        """Met à jour l'état et la couleur du bouton ZRotate"""
        if self.zrotate_running:
            self.zrotate_start_button.setText("Arrêter ZRotate")
            self.zrotate_start_button.setProperty("stopped", False)
        else:
            self.zrotate_start_button.setText("Démarrer ZRotate")
            self.zrotate_start_button.setProperty("stopped", True)
        # Forcer la mise à jour du style
        self.zrotate_start_button.style().unpolish(self.zrotate_start_button)
        self.zrotate_start_button.style().polish(self.zrotate_start_button)

    def _on_playwright_warmup_changed(self, state: int):
        self.playwright_warmup_enabled = state == Qt.CheckState.Checked
        self.config["playwright_warmup_enabled"] = self.playwright_warmup_enabled
        self._save_playwright_warmup_config()

    def _on_zrotate_auto_start_changed(self, state: int):
        """Gère le changement de l'option démarrage automatique"""
        self._save_zrotate_auto_start_config()

    def _auto_start_zrotate(self):
        """Démarre ZRotate automatiquement au lancement"""
        if not self.zrotate_running and self.zrotate_selected_interfaces:
            self._zrotate_log("🔄 Démarrage automatique de ZRotate...")
            self._start_zrotate()

    def on_zrotate_toggle(self):
        """Démarre ou arrête le serveur ZRotate"""
        if self.zrotate_running:
            self._stop_zrotate()
        else:
            self._start_zrotate()

    def _start_zrotate(self):
        """Démarre le serveur ZRotate"""
        # Vérifier que le serveur n'est pas déjà en cours d'exécution
        if self.zrotate_running:
            self._zrotate_log("⚠️ ZRotate est déjà en cours d'exécution")
            return

        # S'assurer que l'ancien serveur est complètement arrêté
        if self.zrotate_proxy_server:
            if self.zrotate_proxy_server.isRunning():
                self._zrotate_log("⚠️ Arrêt de l'ancien serveur en cours...")
                self.zrotate_proxy_server.stop()
                self.zrotate_proxy_server.wait(3000)
            self.zrotate_proxy_server = None

        # Lire directement depuis les lignes ZRotate pour avoir l'état actuel
        selected_interfaces = set()
        for row_widget in getattr(self, "_zrotate_interface_rows", {}).values():
            if isinstance(row_widget, ZRotateInterfaceRow) and row_widget.is_checked():
                selected_interfaces.add(row_widget.interface_name)

        # Mettre à jour self.zrotate_selected_interfaces avec les sélections actuelles
        self.zrotate_selected_interfaces = selected_interfaces
        self._save_zrotate_selection_config()

        if not self.zrotate_selected_interfaces:
            QMessageBox.warning(
                self,
                "ZRotate",
                "Veuillez sélectionner au moins une interface (clé Huawei).",
            )
            return

        # Extraire les IPs locales, ports et scripts de reset des interfaces sélectionnées
        egress_configs = []
        missing_ips = []
        app_dir = get_app_dir()
        default_reset = self.config.get("reset_script_default", DEFAULT_RESET_SCRIPT)

        for iface_name in self.zrotate_selected_interfaces:
            interface_info = self._get_all_interfaces().get(iface_name)
            if not interface_info:
                missing_ips.append(f"{iface_name} (interface non trouvée)")
                continue

            iface_cfg = self.config.get("interface_proxies", {}).get(iface_name, {})
            proxy_port = iface_cfg.get("port")
            if proxy_port is None:
                widget = self.interface_widgets.get(iface_name)
                if widget and widget.port_edit.text().strip():
                    try:
                        proxy_port = int(widget.port_edit.text().strip())
                    except ValueError:
                        pass

            if interface_info.is_remote:
                if not interface_info.upstream_host or not interface_info.upstream_port:
                    missing_ips.append(f"{iface_name} (proxy amont manquant)")
                    continue
                if not proxy_port:
                    missing_ips.append(f"{iface_name} (port relais local manquant)")
                    continue
                widget = self.interface_widgets.get(iface_name)
                if widget and iface_name not in self._running_proxies:
                    self._start_proxy_for_widget(widget, int(proxy_port), auto=True)
                cfg = {
                    "name": iface_name,
                    "ip": "127.0.0.1",
                    "remote": True,
                    "proxy_port": int(proxy_port),
                    "upstream_host": interface_info.upstream_host,
                    "upstream_port": interface_info.upstream_port,
                }
                reset_script = self._remote_iface_reset_script(iface_name)
                if reset_script:
                    cfg["reset_script_path"] = str(
                        resolve_reset_script_path(reset_script, app_dir)
                    )
                egress_configs.append(cfg)
                continue

            if not interface_info.local_ip:
                missing_ips.append(f"{iface_name} (IP locale manquante)")
                continue

            if not interface_info.is_up:
                missing_ips.append(f"{iface_name} (interface inactive)")
                continue

            reset_script, _ = resolve_interface_reset_script(iface_cfg, default_reset)
            reset_script_path = str(resolve_reset_script_path(reset_script, app_dir))

            cfg = {"name": iface_name, "ip": interface_info.local_ip}
            if proxy_port is not None:
                cfg["proxy_port"] = proxy_port
            cfg["reset_script_path"] = reset_script_path
            reset_options = extract_interface_reset_options(iface_cfg)
            if reset_options:
                cfg["reset_options"] = reset_options
            egress_configs.append(cfg)

        # Ne mettre dans le pool que les clés qui ont une IP (déjà fait ci-dessus).
        # Si certaines n'ont pas d'IP, on les ignore et on démarre avec les autres.
        if len(egress_configs) < 1:
            QMessageBox.warning(
                self,
                "ZRotate",
                "Aucune interface valide (locale ou distante).\n"
                + (
                    "Interfaces ignorées (sans IP ou inactives):\n"
                    + "\n".join(missing_ips)
                    if missing_ips
                    else ""
                ),
            )
            return
        if missing_ips:
            self._zrotate_log(
                "⚠️ Clés non ajoutées au pool (pas d'IP ou inactives): "
                + ", ".join(missing_ips)
            )

        # Parser l'URL du serveur (source unique: proxy_configs.json -> zrotate.server_url)
        url_text = str(
            self.config.get("zrotate", {}).get(
                "server_url", getattr(self, "zrotate_server_url", "http://127.0.0.1:9999")
            )
        ).strip()
        if not url_text:
            url_text = "http://127.0.0.1:9999"
        self.zrotate_server_url = url_text

        try:
            from urllib.parse import urlparse

            parsed = urlparse(url_text)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or 9999
        except Exception:
            host = "127.0.0.1"
            port = 9999

        # Charger max_requests_per_quota et quota_timeout depuis proxy_configs.json (min 2)
        zrotate_cfg = self.config.get("zrotate", {})
        max_requests = zrotate_cfg.get("max_requests_per_quota", 2)
        if not isinstance(max_requests, int) or max_requests < 2:
            max_requests = 2
        quota_timeout = zrotate_cfg.get("quota_timeout_seconds", 60.0)
        close_haapi_after = float(
            zrotate_cfg.get("close_haapi_tunnel_after_seconds", 0.0)
        )
        if close_haapi_after < 0:
            close_haapi_after = 0.0

        # Créer et démarrer le serveur proxy avec les egress IPs
        self.zrotate_proxy_server = ZRotateProxyServer(
            egress_configs=egress_configs,
            host=host,
            port=port,
            max_requests_per_quota=max_requests,
            quota_timeout_seconds=quota_timeout,
            close_haapi_tunnel_after_seconds=close_haapi_after,
        )
        self.zrotate_proxy_server.log_message.connect(self._zrotate_log)
        # Connecter le signal de reset pour déclencher le reset avec animation
        self.zrotate_proxy_server.reset_interface_requested.connect(
            self.on_interface_reset_requested
        )
        # Badge RESET → "In use" quand la clé a une requête/connexion en cours
        self.zrotate_proxy_server.interface_usage_changed.connect(
            self._on_interface_usage_changed
        )
        # Stats ZRotate
        self.zrotate_proxy_server.stats_updated.connect(self._on_zrotate_stats_updated)
        self.zrotate_proxy_server.quota_stats_updated.connect(
            self._on_quota_stats_updated
        )
        self.zrotate_proxy_server.quarantine_updated.connect(
            self._on_quarantine_updated
        )
        self.zrotate_proxy_server.pool_state_updated.connect(
            self._on_pool_state_updated
        )

        # Démarrer le serveur
        self.zrotate_proxy_server.start()
        self.zrotate_running = True

        # Mettre à jour le bouton avec la bonne couleur
        self._update_zrotate_button_state()

        self._zrotate_log(f"✅ ZRotate démarré sur {host}:{port}")
        self._zrotate_log(
            f"   Max requêtes/IP (GET+CONNECT): {max_requests} | Timeout quotas partiels: {quota_timeout}s"
        )
        self._zrotate_log(f"   {len(egress_configs)} clé(s) Huawei configurée(s):")
        for cfg in egress_configs:
            self._zrotate_log(f"      - {cfg['name']}: {cfg['ip']}")
        # État quarantaine jusqu'au 1er tick du thread (~1s)
        self._on_quarantine_updated([])
        self._last_pool_state_for_ui = None
        self._sync_zrotate_row_pool_styles()

    def _stop_zrotate(self, wait_timeout_ms: int = 500):
        """Arrête le serveur ZRotate.

        wait_timeout_ms contrôle le temps maximum (en ms) pendant lequel
        on attend l'arrêt propre du thread avant de le tuer de force.
        Par défaut on garde cette valeur très basse pour éviter de bloquer l'UI
        quand l'utilisateur clique sur le bouton Arrêter ZRotate.
        """
        if not self.zrotate_running:
            return  # Déjà arrêté

        self.zrotate_running = False  # Marquer comme arrêté immédiatement
        self._last_zrotate_pool_state = {}
        self._last_pool_state_for_ui = None
        self._last_quarantine_names = None
        self._sync_zrotate_row_pool_styles()

        # Mettre à jour le bouton immédiatement
        self._update_zrotate_button_state()

        if self.zrotate_proxy_server:
            thread = self.zrotate_proxy_server
            # Demander un arrêt propre du serveur (non bloquant)
            try:
                thread.stop()
            except Exception:
                pass

            # Attendre un court instant pour laisser le temps au thread
            # de s'arrêter sans geler l'UI, puis le tuer si nécessaire.
            if thread.isRunning():
                try:
                    if not thread.wait(wait_timeout_ms):
                        thread.terminate()
                        thread.wait(1000)
                except Exception:
                    # En cas de problème, on tente quand même de forcer l'arrêt
                    try:
                        thread.terminate()
                        thread.wait(1000)
                    except Exception:
                        pass
            self.zrotate_proxy_server = None

        self._set_quarantine_ui_stopped()

        # Remettre les badges à RESET pour les interfaces ZRotate
        for name in self.zrotate_selected_interfaces:
            w = self.interface_widgets.get(name)
            if w:
                w.set_reset_badge_in_use(False)

        self._zrotate_log("⏹️ ZRotate arrêté")

    def changeEvent(self, event):
        if event.type() == QEvent.Type.WindowStateChange:
            minimized = bool(self.windowState() & Qt.WindowState.WindowMinimized)
            if minimized != self._was_minimized:
                self._was_minimized = minimized
                self.interface_manager.notify_window_minimized(minimized)
        super().changeEvent(event)

    # --- Fermeture ---
    def closeEvent(self, event):
        print("[SHUTDOWN] closeEvent reçu, arrêt de l'application...")
        self._refresh_after_reset_timer.stop()

        # Arrêter tous les proxies proprement
        print("[SHUTDOWN] Arrêt des ProxyThread...")
        for name, thread in list(self.proxy_threads.items()):
            try:
                print(f"[SHUTDOWN] Arrêt proxy pour interface '{name}'...")
                thread.stop()
                # Attendre que le thread se termine pour éviter "QThread: Destroyed while thread is still running"
                if not thread.wait(2000):
                    print(
                        f"[SHUTDOWN] ⚠️ Thread proxy '{name}' n'a pas terminé dans les 2s, attente supplémentaire..."
                    )
                    thread.wait(1000)  # Attente supplémentaire
            except Exception:
                traceback.print_exc()
        self.proxy_threads.clear()

        # Arrêter ZRotate si actif (AVANT InterfaceManager pour éviter les conflits)
        if self.zrotate_running or self.zrotate_proxy_server:
            print("[SHUTDOWN] Arrêt du serveur ZRotate...")
            try:
                # Pendant la fermeture de l'application, on accepte d'attendre
                # un peu plus longtemps pour un arrêt plus propre.
                self._stop_zrotate(wait_timeout_ms=5000)
            except Exception:
                traceback.print_exc()

        # Arrêter proprement InterfaceManager (timers + PublicIpWorker)
        try:
            self.interface_manager.shutdown()
        except Exception:
            traceback.print_exc()

        print("[SHUTDOWN] Fermeture de la fenêtre principale.")
        super().closeEvent(event)


if __name__ == "__main__":
    try:
        ensure_local_build_id_file()
        if handle_startup_update_check():
            sys.exit(0)

        app = QApplication(sys.argv)
        app.setWindowIcon(QIcon("Z icon.ico"))
        app.setStyle("Fusion")

        # Raccourci propre pour Ctrl+C dans le terminal : on déclenche juste app.quit()
        signal.signal(signal.SIGINT, lambda *args: app.quit())

        window = MainWindow()
        window.show()
        sys.exit(app.exec())
    except KeyboardInterrupt:
        print("[SHUTDOWN] KeyboardInterrupt reçu dans main, arrêt immédiat.")
        sys.exit(0)
    except Exception:
        print("[FATAL] Exception non interceptée dans le main :")
        traceback.print_exc()
        # Forcer un code de retour non nul
        sys.exit(1)
