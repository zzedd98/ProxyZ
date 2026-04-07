import sys
import json
import math
import logging
import importlib
from PySide6.QtWidgets import *
from PySide6.QtCore import *
from PySide6.QtGui import *
import psutil
import select
import socket
from dataclasses import dataclass
import threading
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

# Sous Windows, empêche l'ouverture de consoles éphémères pour netsh / control.exe
if sys.platform.startswith("win"):
    CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
else:
    CREATE_NO_WINDOW = 0

# Script de reset par défaut (nom ou chemin relatif au dossier de l'app / exe)
DEFAULT_RESET_SCRIPT = "reset_modem.py"


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
        # Si on ne trouve pas Python, on utilise quand même sys.executable
        # mais cela ne fonctionnera probablement pas
        return sys.executable
    # En script Python, utiliser l'interpréteur actuel
    return sys.executable


def build_reset_command(script_path: Path, proxy_port: int) -> list[str]:
    """
    Construit la commande de reset Python.
    """
    if script_path.suffix.lower() != ".py":
        raise RuntimeError("Seuls les scripts reset Python (.py) sont supportés.")
    return [get_python_executable(), str(script_path), str(proxy_port)]


_RESET_MODEM_FUNC = None


def run_reset_script(script_path: Path, proxy_port: int, timeout_seconds: int = 120) -> int:
    """
    Exécute un reset:
    - `reset_modem.py` est appelé en-process (browser persistant réutilisable).
    - autres scripts Python restent en subprocess.
    Retourne un code process-like (0 succès, 1 échec).
    """
    global _RESET_MODEM_FUNC
    if script_path.name.lower() == "reset_modem.py":
        if _RESET_MODEM_FUNC is None:
            module = importlib.import_module("reset_modem")
            _RESET_MODEM_FUNC = getattr(module, "reset_modem_by_port")
        ok = bool(_RESET_MODEM_FUNC(proxy_port))
        return 0 if ok else 1

    cmd = build_reset_command(script_path, proxy_port)
    result = subprocess.run(cmd, timeout=timeout_seconds)
    return result.returncode


@dataclass
class ProxyConfig:
    name: str
    bind_ip: str
    port: int
    interface_name: str


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
            print(
                f"[OK] Proxy écoute sur 127.0.0.1:{self.config.port}, envoi via l'IP source {self.config.bind_ip}"
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


class InterfaceManager(QObject):
    interfaces_updated = Signal(list)  # list[InterfaceInfo]
    public_ip_updated = Signal(str, str, bool)  # name, public_ip, online
    metrics_update_failed = Signal(str)  # message

    def __init__(self, parent=None):
        super().__init__(parent)
        self.interfaces: dict[str, InterfaceInfo] = {}
        # Threads Python pour IP publique (évite d'utiliser QThread qui peut crasher en natif)
        self._public_ip_threads: dict[str, threading.Thread] = {}

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(2000)
        self.refresh_timer.timeout.connect(self.refresh_interfaces)
        self.refresh_timer.start()

        self.public_ip_timer = QTimer(self)
        # IP publique plus réactive : toutes les 5 secondes
        self.public_ip_timer.setInterval(5000)
        self.public_ip_timer.timeout.connect(self.refresh_public_ips)
        self.public_ip_timer.start()

        # Première charge
        try:
            self.refresh_interfaces()
            # Lancer immédiatement une première résolution d'IP publique
            self.refresh_public_ips()
        except Exception:
            traceback.print_exc()

    def shutdown(self):
        """Arrête proprement les timers et attend la fin des threads IP publique."""
        try:
            self.refresh_timer.stop()
            self.public_ip_timer.stop()
        except Exception:
            traceback.print_exc()

        for name, th in list(self._public_ip_threads.items()):
            try:
                if th.is_alive():
                    th.join(timeout=2.0)
            except Exception:
                traceback.print_exc()
        self._public_ip_threads.clear()

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

    def refresh_interfaces(self):
        netsh_data = self._parse_netsh_interfaces()
        if not netsh_data:
            return

        try:
            addrs = psutil.net_if_addrs()
            stats = psutil.net_if_stats()
        except Exception:
            traceback.print_exc()
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

        self.interfaces = new_interfaces
        self.interfaces_updated.emit(list(self.interfaces.values()))

    # --- Public IP / connectivité ---
    def refresh_public_ips(self):
        for name, info in self.interfaces.items():
            if not info.is_up or not info.local_ip:
                continue
            # Ne pas lancer plusieurs threads en parallèle pour la même interface
            th = self._public_ip_threads.get(name)
            if th is not None and th.is_alive():
                continue
            # print(
            #     f"[REFRESH] Lancement thread IP publique pour interface '{name}' ({info.local_ip})"
            # )
            t = threading.Thread(
                target=self._public_ip_worker_thread,
                args=(name, info.local_ip),
                daemon=True,
            )
            self._public_ip_threads[name] = t
            t.start()

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
        # Mettre à jour l'état interne + émettre le signal (Qt dispatchera côté GUI)
        try:
            if name in self.interfaces:
                info = self.interfaces[name]
                info.public_ip = public_ip or info.public_ip
                info.online = online
                self.interfaces[name] = info
            self.public_ip_updated.emit(name, public_ip or "", online)
        except Exception:
            traceback.print_exc()

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
        self.refresh_interfaces()


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

    async def _retry_reset_loop(self):
        """Toutes les 30s, relance un reset pour les clés hors pool tant qu'elles n'ont pas été remises."""
        while True:
            try:
                await asyncio.sleep(30)
                async with self._lock:
                    for key_name in list(self._keys_removed_from_pool):
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

            # Trier par charge croissante (celles avec le moins de requêtes utilisées en premier)
            candidates.sort(key=lambda x: x[0])

            if candidates:
                _total_used, interface_info, quota_info, interface_name = candidates[0]
                quota_info.start_request()
                logger.info(
                    f"[QUOTA] 🚀 {interface_name}: Démarrage {request_type} {domain_key} → Temporaire: {quota_info.temporary_requests}, Complétée: {quota_info.completed_requests}/{quota_info.max_requests} (priorité charge)"
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

            if script_path.exists():
                result = await asyncio.to_thread(
                    run_reset_script, script_path, proxy_port, 120
                )

                if result == 0:
                    logger.info(f"[QUOTA] ✅ Reset réussi pour {interface_name}")
                else:
                    logger.warning(
                        f"[QUOTA] ⚠️ Reset échoué pour {interface_name} (code: {result})"
                    )
            else:
                logger.error(f"[QUOTA] ❌ Script reset introuvable: {script_path}")

            # Réinitialiser tous les quotas et remettre l'interface disponible
            await self._release_interface_after_reset(interface_name)

        except Exception as e:
            logger.error(f"[QUOTA] ❌ Erreur lors du reset de {interface_name}: {e}")
            import traceback

            logger.error(traceback.format_exc())
            await self._release_interface_after_reset(interface_name)

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

    async def release_interface_after_reset(self, interface_name: str):
        """Remet une interface en disponibilité après reset (appelé depuis ProxyZ)"""
        await self._release_interface_after_reset(interface_name)

    async def _release_interface_after_reset(self, interface_name: str):
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

            # Log de succès avec les nouveaux quotas (toujours 0/max après reset)
            if new_quotas_str:
                logger.info(
                    f"[QUOTA] ✅ Reset réussi pour {interface_name} - "
                    f"Nouveaux quotas: {', '.join(new_quotas_str)}"
                )
            else:
                logger.info(
                    f"[QUOTA] ✅ Reset réussi pour {interface_name} - Aucun quota à réinitialiser (déjà à zéro)"
                )

            # Retirer de la liste des interfaces en reset
            self.resetting_interfaces.discard(interface_name)

            # Vérifier la connectivité avant de remettre dans le pool (surtout après reset suite à 3 échecs)
            interface_info = next(
                (i for i in self.egress_configs if i["name"] == interface_name), None
            )
            if interface_info:
                names_in_available = [a["name"] for a in self.available_interfaces]
                if interface_name not in names_in_available:
                    self.available_interfaces.append(interface_info)
                    self._interface_available_event_set()
                    self._interface_failure_count[interface_name] = 0
                    self._keys_removed_from_pool.discard(interface_name)
                    logger.info(
                        f"[QUOTA] ✅ Interface {interface_name} remise en disponibilité"
                    )

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

            logger.info(
                f"[{connection_id}] Nouvelle connexion depuis {client_ip}:{client_port} "
                f"→ Egress: {egress_info['name']} ({egress_info['ip']})"
            )

            # Traiter la requête selon son type
            if request_type == "CONNECT":
                # Requête CONNECT (HTTPS)
                dest = parse_connect_request(first_line)
                if not dest:
                    logger.warning(
                        f"[{connection_id}] Requête CONNECT invalide: {first_line}"
                    )
                    writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                    await writer.drain()
                    # Libérer le quota temporaire si pris
                    # complete_request vérifie elle-même si la connexion est dans _active_connections avec le lock
                    if self._use_quotas:
                        await self.quota_manager.complete_request(
                            connection_id, success=False
                        )
                    return

                logger.info(f"[{connection_id}] CONNECT {dest_host}:{dest_port}")

                # Ouvrir connexion upstream avec bind source
                try:
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

                # Ouvrir connexion upstream avec bind source
                try:
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

            if egress_info:
                logger.info(
                    f"[{connection_id}] ✅ Connexion fermée (egress: {egress_info['name']})"
                )

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


class InterfaceWidget(QFrame):
    proxy_toggled = Signal(str, bool, int)  # name, enabled, port
    rename_requested = Signal(str)  # name
    settings_requested = Signal(str)  # ouverture des propriétés de la carte
    reset_requested = Signal(str)  # name
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
        self._build_ui()
        self.update_from_interface(interface)

    # --- UI ---
    def _build_ui(self):
        self.setFrameShape(QFrame.StyledPanel)
        self.setFrameShadow(QFrame.Raised)

        main_layout = QVBoxLayout(self)
        # Marges et espacements compacts pour réduire la hauteur tout en restant lisible
        main_layout.setContentsMargins(8, 3, 8, 3)
        main_layout.setSpacing(2)

        # Ligne titre + badges (sans bouton paramètres local)
        header = QHBoxLayout()
        header.setSpacing(8)

        self.name_label = QLabel()
        self.name_label.setObjectName("ifaceName")
        header.addWidget(self.name_label, 1)

        # Badge AUTO (métrique automatique) seulement, la métrique numérique est déplacée en bas
        self.auto_badge = QLabel("AUTO")
        self.auto_badge.setObjectName("autoBadge")
        self.auto_badge.setVisible(False)
        header.addWidget(self.auto_badge, 0, Qt.AlignLeft)

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

        self.metric_badge = QLabel()
        self.metric_badge.setObjectName("metricBadge")
        proxy_row.addWidget(self.metric_badge, 0, Qt.AlignRight)

        main_layout.addLayout(proxy_row)

        self.setStyleSheet(
            """
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
        QLabel#autoBadge {
            background-color: rgba(52, 152, 219, 0.18);
            color: #3498db;
            border-radius: 9px;
            padding: 2px 8px;
            font-size: 11px;
            font-weight: 600;
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
        )

        # Autoriser le renommage via double-clic sur le nom
        self.name_label.installEventFilter(self)

    # --- Mise à jour depuis InterfaceInfo ---
    def update_from_interface(self, interface: InterfaceInfo):
        self.interface = interface
        self.name_label.setText(interface.name)
        self.metric_badge.setText(f"Metric {interface.metric}")
        self.auto_badge.setVisible(interface.automatic)

        if interface.local_ip:
            self.local_ip_label.setText(f"IPv4 locale: {interface.local_ip}")
        else:
            self.local_ip_label.setText("IPv4 locale: -")

        if interface.public_ip:
            self.public_ip_header_label.setText(interface.public_ip)
        else:
            self.public_ip_header_label.setText("-")

        has_local = bool(interface.local_ip)
        # Si plus d'IP locale, forcer l'affichage en OFF (sans émettre de signal)
        if not has_local:
            self.set_proxy_running(False)

        # Appliquer l'état disconnected / connected pour la surbrillance globale
        self.setProperty("disconnected", not interface.is_up)
        self.setProperty("connected", bool(interface.online and interface.is_up))
        self.style().unpolish(self)
        self.style().polish(self)

    # --- Proxy ---
    def _on_proxy_button_clicked(self):
        port_text = self.port_edit.text().strip()
        # Activer / désactiver en fonction de l'état actuel du bouton
        want_enable = self.proxy_status_chip.objectName() != "proxyOnChip"

        if want_enable:
            if not self.interface.local_ip:
                QMessageBox.warning(
                    self,
                    "Proxy impossible",
                    "Aucune IPv4 locale valide pour cette interface.",
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
            self.proxy_status_chip.setText(f"PROXY ON · 127.0.0.1:{port}")
            self.proxy_status_chip.setObjectName("proxyOnChip")
        else:
            self.proxy_status_chip.setText("PROXY OFF")
            self.proxy_status_chip.setObjectName("proxyOffChip")

        # Rafraîchir le style du badge et du bouton
        self.style().unpolish(self.proxy_status_chip)
        self.style().polish(self.proxy_status_chip)

    def mark_disconnected(self, disconnected: bool):
        self._disconnected = disconnected
        self.setProperty("disconnected", disconnected)
        self.style().unpolish(self)
        self.style().polish(self)
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

    def set_reset_loading(self, loading: bool):
        """Active ou désactive l'animation de loading sur le bouton reset"""
        self._reset_loading = loading
        if loading:
            self.reset_badge.setProperty("loading", True)
            self.reset_badge.style().unpolish(self.reset_badge)
            self.reset_badge.style().polish(self.reset_badge)
            self._reset_loading_dots = 0
            self._reset_loading_timer.start(500)  # Mise à jour toutes les 500ms
        else:
            self._reset_loading_timer.stop()
            self.reset_badge.setProperty("loading", False)
            self.reset_badge.setText("In use" if self._reset_in_use else "RESET")
            self.reset_badge.style().unpolish(self.reset_badge)
            self.reset_badge.style().polish(self.reset_badge)

    def set_reset_badge_in_use(self, in_use: bool):
        """Affiche 'In use' si la clé a une requête/connexion en cours, sinon 'RESET'."""
        self._reset_in_use = in_use
        if not self._reset_loading:
            self.reset_badge.setText("In use" if in_use else "RESET")
            self.reset_badge.style().unpolish(self.reset_badge)
            self.reset_badge.style().polish(self.reset_badge)

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

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
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
            QListWidget::item:selected {
                background: transparent;
            }
            """
        )
        self.setFrameShape(QFrame.NoFrame)

    def mousePressEvent(self, event):
        self.user_interaction.emit()
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

    def __init__(self, interface_name: str, public_ip: str, parent=None):
        super().__init__(parent)
        self.interface_name = interface_name

        self.setObjectName("zrotateInterfaceRow")
        self.setMinimumHeight(34)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(10)

        self.checkbox = QCheckBox(interface_name)
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

        self.setStyleSheet(
            """
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
        )

    def set_checked(self, checked: bool):
        blocked = self.checkbox.blockSignals(True)
        self.checkbox.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self.checkbox.blockSignals(blocked)
        self._apply_checked_visual_state()

    def is_checked(self) -> bool:
        return self.checkbox.checkState() == Qt.Checked

    def set_public_ip(self, public_ip: str):
        value = public_ip or "-"
        if self.ip_chip.text() != value:
            self.ip_chip.setText(value)

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
        self.setProperty("poolEnabled", enabled)
        self.style().unpolish(self)
        self.style().polish(self)


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
    reset_completed = Signal(str, int)

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

        # Resets en parallèle (un thread par interface) ; suivi pour éviter doublons et refresh en rafale
        self._reset_in_progress: set[str] = set()  # interfaces en cours de reset
        self._refresh_after_reset_timer = QTimer(self)
        self._refresh_after_reset_timer.setSingleShot(True)
        self._refresh_after_reset_timer.timeout.connect(
            self.interface_manager.refresh_interfaces
        )

        self._build_ui()
        self._load_config()
        self._start_playwright_browser_warmup()

        self.interface_manager.interfaces_updated.connect(self.on_interfaces_updated)
        self.interface_manager.public_ip_updated.connect(self.on_public_ip_updated)
        self.interface_manager.metrics_update_failed.connect(
            self.on_metrics_update_failed
        )
        # Connexion du signal reset_completed pour arrêter l'animation après un reset
        self.reset_completed.connect(self._on_reset_completed)

        # Première sync
        self.on_interfaces_updated(list(self.interface_manager.interfaces.values()))
        self._restore_initial_proxies()
        self._update_zrotate_interfaces_list()

        # Démarrer ZRotate automatiquement si configuré
        zrotate_cfg = self.config.get("zrotate", {})
        if zrotate_cfg.get("auto_start", False):
            # Vérifier qu'il y a des interfaces sélectionnées
            if self.zrotate_selected_interfaces:
                # Attendre un peu que les proxies soient prêts
                QTimer.singleShot(2000, self._auto_start_zrotate)

    def _start_playwright_browser_warmup(self):
        """
        Pré-initialise reset_modem (thread dédié + browser persistant) en arrière-plan.
        Si indisponible, le premier reset fera le lazy init.
        """

        def _warmup():
            try:
                mod = importlib.import_module("reset_modem")
                init_fn = getattr(mod, "initialize_browser_service", None)
                if callable(init_fn):
                    ports: list[int] = []
                    for cfg in (self.config.get("interface_proxies", {}) or {}).values():
                        try:
                            p = int((cfg or {}).get("port", 0) or 0)
                            if p > 0:
                                ports.append(p)
                        except Exception:
                            continue
                    ok = bool(init_fn(ports))
                    if ok:
                        if ports:
                            print(
                                f"[RESET] Browsers Playwright pré-initialisés pour ports: {sorted(set(ports))}"
                            )
                        else:
                            print("[RESET] Service Playwright prêt (lazy init par port).")
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

        self.auto_container = QWidget()
        self.auto_layout = QVBoxLayout(self.auto_container)
        self.auto_layout.setContentsMargins(0, 0, 0, 0)
        self.auto_layout.setSpacing(6)
        # Largeur raisonnable des cartes pour un rendu équilibré
        self.auto_container.setMaximumWidth(620)
        left.addWidget(self.auto_container)

        self.manual_list = ManualInterfacesList()
        self.manual_list.order_changed.connect(self.on_manual_order_changed)
        self.manual_list.user_interaction.connect(self._mark_user_interaction)

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

        # Panel haut : Configuration ZRotate
        config_panel = QWidget()
        config_panel.setObjectName("zrotateConfigPanel")
        config_layout = QVBoxLayout(config_panel)
        config_layout.setContentsMargins(10, 10, 10, 10)
        config_layout.setSpacing(10)

        # Titre
        zrotate_title = QLabel("ZRotate - Rotation d'IP")
        zrotate_title.setObjectName("zrotateTitle")
        config_layout.addWidget(zrotate_title)

        # URL du serveur : configurée via proxy_configs.json (zrotate.server_url)

        # Liste des interfaces à cocher
        interfaces_label = QLabel("Interfaces pour le pool d'IP:")
        interfaces_label.setObjectName("zrotateLabel")
        config_layout.addWidget(interfaces_label)

        self.zrotate_interfaces_list = QListWidget()
        self.zrotate_interfaces_list.setObjectName("zrotateInterfacesList")
        # Augmenter la taille de la liste (partie de l'agrandissement de 50%)
        config_layout.addWidget(
            self.zrotate_interfaces_list, 2
        )  # Augmenté de 1 à 2 pour plus d'espace

        # Checkbox démarrage automatique
        self.zrotate_auto_start_checkbox = QCheckBox(
            "Démarrer automatiquement au lancement"
        )
        self.zrotate_auto_start_checkbox.setObjectName("zrotateAutoStartCheckbox")
        self.zrotate_auto_start_checkbox.stateChanged.connect(
            self._on_zrotate_auto_start_changed
        )
        config_layout.addWidget(self.zrotate_auto_start_checkbox)

        # Bouton démarrer/arrêter
        self.zrotate_start_button = QPushButton("Démarrer ZRotate")
        self.zrotate_start_button.setObjectName("zrotateStartButton")
        self.zrotate_start_button.setProperty(
            "stopped", True
        )  # Initialiser comme arrêté
        self.zrotate_start_button.clicked.connect(self.on_zrotate_toggle)
        # Appliquer le style initial
        self.zrotate_start_button.style().unpolish(self.zrotate_start_button)
        self.zrotate_start_button.style().polish(self.zrotate_start_button)
        config_layout.addWidget(self.zrotate_start_button)

        zrotate_layout.addWidget(
            config_panel, 2
        )  # Augmenté de 1 à 2 pour plus d'espace

        # Panel bas : Console de logs
        console_panel = QWidget()
        console_panel.setObjectName("zrotateConsolePanel")
        console_layout = QVBoxLayout(console_panel)
        console_layout.setContentsMargins(10, 10, 10, 10)
        console_layout.setSpacing(5)

        # Ligne titre + bouton clear
        console_title_row = QHBoxLayout()
        console_title_row.setSpacing(8)

        console_title = QLabel("Console ZRotate")
        console_title.setObjectName("zrotateTitle")
        console_title_row.addWidget(console_title)

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

        zrotate_layout.addWidget(
            console_panel, 2
        )  # Augmenté de 1 à 2 pour plus d'espace (répartition de l'agrandissement)

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
            QWidget#zrotateConsolePanel {
                background-color: #02172e;
                border-radius: 12px;
                border: 1px solid rgba(31, 41, 55, 0.9);
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
            QTextEdit#zrotateLogBox {
                background-color: #1a1a1a;
                border-radius: 6px;
                border: 1px solid rgba(255, 255, 255, 0.05);
                color: #ecf0f1;
                font-family: 'Consolas', 'Monaco', monospace;
                font-size: 11px;
            }
        """
        )

    # --- Logging / titre ---
    def _append_log(self, text: str):
        timestamp = time.strftime("%H:%M:%S")
        self.log_box.append(f"[{timestamp}] {text}")

    def _update_window_title(self):
        # Nombre de connexions actives :
        # interface up avec IPv4 locale ET IP publique résolue non vide
        try:
            online_count = sum(
                1
                for info in self.interface_manager.interfaces.values()
                if info.is_up and info.local_ip and info.public_ip
            )
        except Exception:
            online_count = 0

        self.setWindowTitle(f"ProxyZ - {online_count} Co / {self.active_proxies} Prox")

        self.global_status_label.setText(
            f"{online_count} connexion{'s' if online_count != 1 else ''} / "
            f"{self.active_proxies} proxy actif{'s' if self.active_proxies != 1 else ''}"
        )

    # --- Config / persistance ---
    def _load_config(self):
        try:
            with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
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
                }
                self.config.setdefault("reset_script_default", DEFAULT_RESET_SCRIPT)
                self._save_config()
            else:
                # Garder tout le JSON (dont reset_script_default) et s'assurer que les clés attendues existent
                self.config = dict(data)
                self.config.setdefault("interface_proxies", {})
                self.config.setdefault("ui", {})
                self.config.setdefault("interface_aliases", {})
                self.config.setdefault("zrotate", {})
        except FileNotFoundError:
            self.config = {
                "interface_proxies": {},
                "ui": {},
                "interface_aliases": {},
                "zrotate": {},
            }
            self.config.setdefault("reset_script_default", DEFAULT_RESET_SCRIPT)
        except Exception as e:
            print(f"Erreur de chargement config: {e}")
            self.config = {
                "interface_proxies": {},
                "ui": {},
                "interface_aliases": {},
                "zrotate": {},
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
        if "auto_start" in zrotate_cfg:
            self.zrotate_auto_start_checkbox.setCheckState(
                Qt.Checked if zrotate_cfg["auto_start"] else Qt.Unchecked
            )

        ui = self.config.get("ui", {})
        size = ui.get("last_window_size")
        if isinstance(size, list) and len(size) == 2:
            self.resize(size[0], size[1])
        else:
            # Taille de départ qui épouse le panneau central (agrandie de 50% en hauteur et 20% en largeur pour le panel droit)
            self.resize(1070, 920)  # Largeur: 400 + 540 + 20 espacement + marges ≈ 1070

    def _save_config(self):
        try:
            # Ne jamais écraser les clés globales (ex. reset_script_default)
            self.config.setdefault("reset_script_default", DEFAULT_RESET_SCRIPT)

            # Sauvegarder la configuration ZRotate ; ne jamais écrire max_requests_per_quota < 2
            zrotate_cfg = self.config.setdefault("zrotate", {})
            _max_req = zrotate_cfg.get("max_requests_per_quota", 2)
            if not isinstance(_max_req, int) or _max_req < 2:
                _max_req = 2
            zrotate_cfg["max_requests_per_quota"] = _max_req
            zrotate_cfg.setdefault("quota_timeout_seconds", 60.0)
            zrotate_cfg["server_url"] = getattr(
                self, "zrotate_server_url", "http://127.0.0.1:9999"
            )
            zrotate_cfg["selected_interfaces"] = list(self.zrotate_selected_interfaces)
            zrotate_cfg["running"] = self.zrotate_running
            zrotate_cfg["auto_start"] = (
                self.zrotate_auto_start_checkbox.checkState() == Qt.Checked
            )

            with open(self.CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            print(f"Erreur de sauvegarde config: {e}")

    # --- Gestion des interfaces depuis InterfaceManager ---
    @Slot(list)
    def on_interfaces_updated(self, interfaces: list):
        # Si l'utilisateur vient d'interagir (édition de port, drag, clic), on laisse
        # un petit délai sans update lourd pour ne pas annuler sa sélection.
        if time.time() - self.last_user_interaction < 2.5:
            return
        try:
            # Mapping nom -> InterfaceInfo renvoyé par InterfaceManager
            iface_by_name = {i.name: i for i in interfaces}

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

            auto_infos.sort(key=lambda x: (x.metric, x.name.lower()))
            manual_infos.sort(key=lambda x: (x.metric, x.name.lower()))

            # Créer les widgets manquants pour AUTO
            for info in auto_infos:
                if info.name not in self.interface_widgets:
                    widget = InterfaceWidget(info)
                    widget.proxy_toggled.connect(self.on_proxy_toggled)
                    widget.rename_requested.connect(self.on_interface_rename_requested)
                    widget.settings_requested.connect(
                        self.on_interface_settings_requested
                    )
                    widget.reset_requested.connect(self.on_interface_reset_requested)
                    widget.user_interaction.connect(self._mark_user_interaction)
                    self.interface_widgets[info.name] = widget

                    iface_cfg = self.config.get("interface_proxies", {}).get(info.name)
                    if iface_cfg:
                        port = iface_cfg.get("port")
                        if port:
                            widget.set_port(port)

                    self.auto_layout.addWidget(widget)

            # Créer les widgets manquants pour MANUEL
            for info in manual_infos:
                if info.name not in self.interface_widgets:
                    widget = InterfaceWidget(info)
                    widget.proxy_toggled.connect(self.on_proxy_toggled)
                    widget.rename_requested.connect(self.on_interface_rename_requested)
                    widget.settings_requested.connect(
                        self.on_interface_settings_requested
                    )
                    widget.reset_requested.connect(self.on_interface_reset_requested)
                    widget.user_interaction.connect(self._mark_user_interaction)
                    self.interface_widgets[info.name] = widget

                    iface_cfg = self.config.get("interface_proxies", {}).get(info.name)
                    if iface_cfg:
                        port = iface_cfg.get("port")
                        if port:
                            widget.set_port(port)

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

            # Resynchroniser l'état visuel des widgets avec les ProxyThread existants
            for name, thread in self.proxy_threads.items():
                if getattr(thread, "config", None) and thread.running:
                    w = self.interface_widgets.get(name)
                    if w:
                        w.set_proxy_running(True, thread.config.port)

            # Mettre à jour le titre / compteur dès qu'on a une nouvelle photo des interfaces
            self._update_window_title()
            # Mettre à jour la liste ZRotate seulement si pas d'interaction récente sur ZRotate
            # (pour éviter de perdre la sélection pendant que l'utilisateur coche/décoche)
            # Augmenter le délai à 3 secondes pour être sûr
            if time.time() - self.last_user_interaction >= 3.0:
                self._update_zrotate_interfaces_list()
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

    def _get_interface_display_name(self, name: str) -> str:
        # On retourne toujours le vrai nom Windows : plus d'alias d'affichage
        return name

    def _mark_user_interaction(self):
        self.last_user_interaction = time.time()

    @Slot(str, bool)
    def _on_interface_usage_changed(self, name: str, in_use: bool):
        """Met à jour le badge RESET → 'In use' ou 'RESET' selon l'état de la clé."""
        widget = self.interface_widgets.get(name)
        if widget:
            widget.set_reset_badge_in_use(in_use)

    def _release_interface_to_zrotate(self, name: str):
        """Remet une interface en disponibilité dans ZRotate (succès ou échec)."""
        if self.zrotate_proxy_server and getattr(
            self.zrotate_proxy_server, "proxy_server", None
        ):
            qm = getattr(self.zrotate_proxy_server.proxy_server, "quota_manager", None)
            loop = getattr(self.zrotate_proxy_server, "loop", None)
            if qm and loop and loop.is_running():
                try:
                    asyncio.run_coroutine_threadsafe(
                        qm.release_interface_after_reset(name),
                        loop,
                    )
                except Exception as e:
                    print(f"[RESET] ⚠️ Erreur notification ZRotate: {e}")

    @Slot(str, int)
    def _on_reset_completed(self, name: str, returncode: int):
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
            print(f"[RESET] ⏱️ Timeout pour l'interface '{name}' - remise dans le pool")
        elif returncode == 0:
            print(f"[RESET] ✅ Reset réussi pour l'interface '{name}'")
        else:
            print(
                f"[RESET] ❌ Reset échoué pour l'interface '{name}' (code {returncode}) - remise dans le pool"
            )

        try:
            self._release_interface_to_zrotate(name)
        except Exception as e:
            print(f"[RESET] ⚠️ Erreur release ZRotate: {e}")
        # Un seul refresh 2s après le dernier reset (évite 6 refresh en rafale)
        self._refresh_after_reset_timer.start(2000)

    @Slot(str)
    def on_interface_reset_requested(self, name: str):
        """Reset manuel ou ZRotate : un thread par interface, en parallèle."""
        if name in self._reset_in_progress:
            print(f"[RESET] ⏳ '{name}' déjà en cours, ignoré.")
            return

        interface_info = self.interface_manager.interfaces.get(name)
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

        reset_script = (iface_cfg or {}).get(
            "reset_script",
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
            if not script_path.exists():
                self.reset_completed.emit(name, -1)
                return
            print(
                f"[RESET] 🚀 Lancement {script_path.name} pour '{name}' (port {proxy_port})"
            )
            try:
                return_code = run_reset_script(script_path, proxy_port, 120)
                self.reset_completed.emit(name, return_code)
            except subprocess.TimeoutExpired:
                self.reset_completed.emit(name, -2)
            except Exception as e:
                print(f"[RESET] 💥 Exception: {e}")
                traceback.print_exc()
                self.reset_completed.emit(name, -3)

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

        self._save_config()
        # Forcer un refresh des interfaces pour récupérer le nouveau nom
        self.interface_manager.refresh_interfaces()

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
        info = self.interface_manager.interfaces.get(name)
        if not info or not info.local_ip:
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
        self._save_config()

        if not auto:
            self._append_log(
                f"Proxy démarré sur {name} (127.0.0.1:{port}, source {info.local_ip})"
            )

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
        self._save_config()

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
            info = self.interface_manager.interfaces.get(name)
            if not widget or not info or not info.local_ip:
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
        """Met à jour la liste des interfaces dans le panel ZRotate"""
        header_item = getattr(self, "_zrotate_header_item", None)
        header_widget = getattr(self, "_zrotate_header_widget", None)
        if header_item is None or header_widget is None:
            header_item = QListWidgetItem()
            header_widget = ZRotateInterfacesHeaderRow()
            self.zrotate_interfaces_list.insertItem(0, header_item)
            self.zrotate_interfaces_list.setItemWidget(header_item, header_widget)
            header_item.setSizeHint(header_widget.sizeHint())
            self._zrotate_header_item = header_item
            self._zrotate_header_widget = header_widget

        # Sauvegarder l'état actuel des lignes avant la mise à jour
        current_selections = set()
        for row_widget in getattr(self, "_zrotate_interface_rows", {}).values():
            if isinstance(row_widget, ZRotateInterfaceRow) and row_widget.is_checked():
                current_selections.add(row_widget.interface_name)

        # Fusionner les sélections actuelles avec celles sauvegardées (union au lieu d'intersection)
        # pour préserver toutes les sélections, y compris celles qui viennent d'être cochées
        self.zrotate_selected_interfaces.update(current_selections)
        # Retirer les interfaces qui n'existent plus
        available_interfaces = {
            name
            for name in self.interface_manager.interfaces.keys()
            if self.interface_manager.interfaces[name].is_up
            and self.interface_manager.interfaces[name].local_ip
        }
        self.zrotate_selected_interfaces &= available_interfaces

        rows = getattr(self, "_zrotate_interface_rows", None)
        row_items = getattr(self, "_zrotate_interface_items", None)
        if rows is None:
            self._zrotate_interface_rows = {}
            rows = self._zrotate_interface_rows
        if row_items is None:
            self._zrotate_interface_items = {}
            row_items = self._zrotate_interface_items

        visible_infos = []
        for name, info in sorted(self.interface_manager.interfaces.items()):
            if not info.is_up or not info.local_ip:
                continue
            visible_infos.append((name, info))

        visible_names = {name for name, _ in visible_infos}

        for old_name in list(rows.keys()):
            if old_name in visible_names:
                continue
            old_item = row_items.get(old_name)
            if old_item is not None:
                old_row = self.zrotate_interfaces_list.row(old_item)
                if old_row >= 0:
                    self.zrotate_interfaces_list.takeItem(old_row)
            old_widget = rows.pop(old_name, None)
            if old_widget is not None:
                old_widget.deleteLater()
            row_items.pop(old_name, None)

        for name, info in visible_infos:
            public_ip = info.public_ip or "-"
            row_widget = rows.get(name)
            if row_widget is None:
                item = QListWidgetItem()
                row_widget = ZRotateInterfaceRow(name, public_ip)
                row_widget.set_checked(name in self.zrotate_selected_interfaces)
                row_widget.toggled.connect(self._on_zrotate_interface_toggled)
                self.zrotate_interfaces_list.addItem(item)
                self.zrotate_interfaces_list.setItemWidget(item, row_widget)
                item.setSizeHint(row_widget.sizeHint())
                rows[name] = row_widget
                row_items[name] = item
            else:
                row_widget.set_public_ip(public_ip)
                row_widget.set_checked(name in self.zrotate_selected_interfaces)

        for target_index, (name, _info) in enumerate(visible_infos):
            target_index += 1  # index 0 réservé à l'en-tête fixe
            item = row_items.get(name)
            if item is None:
                continue
            current_index = self.zrotate_interfaces_list.row(item)
            if current_index == target_index:
                continue
            moved_item = self.zrotate_interfaces_list.takeItem(current_index)
            self.zrotate_interfaces_list.insertItem(target_index, moved_item)
            self.zrotate_interfaces_list.setItemWidget(target_index, rows[name])
            moved_item.setSizeHint(rows[name].sizeHint())

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

        # Si ZRotate est en cours d'exécution, redémarrer pour prendre en compte les changements
        if self.zrotate_running:
            self._zrotate_log(
                "⚠️ Redémarrage de ZRotate pour prendre en compte les changements..."
            )
            self._stop_zrotate()
            # Redémarrer après un court délai
            QTimer.singleShot(500, self._start_zrotate)

        self._save_config()

    def _clear_zrotate_console(self):
        """Efface le contenu de la console ZRotate"""
        self.zrotate_log_box.clear()

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
            row_widget.set_quota_values(g_used, g_max, c_used, c_max)

    def _zrotate_log(self, message: str):
        """Ajoute un message dans la console ZRotate"""
        timestamp = time.strftime("%H:%M:%S")
        self.zrotate_log_box.append(f"[{timestamp}] {message}")
        # Auto-scroll vers le bas pour afficher toujours la dernière ligne
        # Déplacer le curseur à la fin du document
        cursor = self.zrotate_log_box.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.zrotate_log_box.setTextCursor(cursor)
        # S'assurer que le curseur est visible (scrolle automatiquement)
        self.zrotate_log_box.ensureCursorVisible()
        # Forcer le scroll vers le maximum pour garantir l'affichage de la dernière ligne
        scrollbar = self.zrotate_log_box.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

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

    def _on_zrotate_auto_start_changed(self, state: int):
        """Gère le changement de l'option démarrage automatique"""
        self._save_config()

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
            interface_info = self.interface_manager.interfaces.get(iface_name)
            if not interface_info:
                missing_ips.append(f"{iface_name} (interface non trouvée)")
                continue

            if not interface_info.local_ip:
                missing_ips.append(f"{iface_name} (IP locale manquante)")
                continue

            if not interface_info.is_up:
                missing_ips.append(f"{iface_name} (interface inactive)")
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
            reset_script = iface_cfg.get("reset_script", default_reset)
            reset_script_path = str(resolve_reset_script_path(reset_script, app_dir))

            cfg = {"name": iface_name, "ip": interface_info.local_ip}
            if proxy_port is not None:
                cfg["proxy_port"] = proxy_port
            cfg["reset_script_path"] = reset_script_path
            egress_configs.append(cfg)

        # Ne mettre dans le pool que les clés qui ont une IP (déjà fait ci-dessus).
        # Si certaines n'ont pas d'IP, on les ignore et on démarre avec les autres.
        if len(egress_configs) < 1:
            QMessageBox.warning(
                self,
                "ZRotate",
                "Aucune interface valide avec IP locale.\n"
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
        self._save_config()

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

        # Remettre les badges à RESET pour les interfaces ZRotate
        for name in self.zrotate_selected_interfaces:
            w = self.interface_widgets.get(name)
            if w:
                w.set_reset_badge_in_use(False)

        self._zrotate_log("⏹️ ZRotate arrêté")
        self._save_config()

    # --- Fermeture ---
    def closeEvent(self, event):
        print("[SHUTDOWN] closeEvent reçu, arrêt de l'application...")
        # Sauvegarder taille fenêtre
        size = [self.width(), self.height()]
        self.config.setdefault("ui", {})["last_window_size"] = size
        self._save_config()

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
