"""
Reset modem Xproxy via requête HTTP distante (sans Playwright).
Envoie GET http://<serveur>/reset?proxy=p:<port> puis attend le changement d'IP.
"""

import logging
import os
import re
import sys
import time
from datetime import datetime

import httpx
import certifi
import ssl
import threading
from pathlib import Path

# Cache TLS en mémoire propre à ce processus ; aucune connexion réseau conservée.
# Garder ce bloc autonome dans les scripts reset (pas de dépendance à ProxyZ/Qt).
# Redémarrer le processus après modification du contenu des certificats.
_contexts: dict[tuple, ssl.SSLContext] = {}
_context_lock = threading.Lock()


def get_httpx_tls_context() -> ssl.SSLContext:
    """Politique HTTPX verify=True/trust_env=True, chargée une seule fois.

    SSL_CERT_FILE conserve la priorité sur SSL_CERT_DIR, puis sur certifi.
    Un capath reste géré par OpenSSL (chargement différé des certificats du
    répertoire), pour préserver la sémantique de SSL_CERT_DIR.
    """
    cafile = os.environ.get("SSL_CERT_FILE")
    capath = os.environ.get("SSL_CERT_DIR")
    if cafile:
        source, path = "pem", os.path.abspath(cafile)
    elif capath:
        source, path = "directory", os.path.abspath(capath)
    else:
        source, path = "pem", certifi.where()
    key = ("httpx", source, path)
    with _context_lock:
        context = _contexts.get(key)
        if context is None:
            if source == "pem":
                pem = Path(path).read_text(encoding="ascii")
                context = ssl.create_default_context(cadata=pem)
            else:
                context = ssl.create_default_context(capath=path)
            _contexts[key] = context
        return context


DEFAULT_RESET_SERVER = "192.168.1.157"
IP_VERIFY_UNCHANGED_LIMIT = 5
IP_VERIFY_INTERVAL_SECONDS = 0.9
IP_VERIFY_INITIAL_DELAY_SECONDS = 2.8
RESET_ATTEMPTS_ON_STALE_IP = 2
RESET_REQUEST_TIMEOUT_SECONDS = 30.0

_PORT_RESET_SERVER: dict[int, str] = {}


def _log(message: str, proxy_port: int | None = None) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prefix = f"[XPRO-DIST][{ts}]"
    if proxy_port is not None:
        prefix += f" [port {proxy_port}]"
    print(f"{prefix} {message}", flush=True)


def _default_reset_server() -> str:
    return os.environ.get("RESET_XPRO_DIST_SERVER", DEFAULT_RESET_SERVER).strip()


def reset_server_for_port(proxy_port: int) -> str:
    return _PORT_RESET_SERVER.get(int(proxy_port)) or _default_reset_server()


def configure_port_options(port_options: dict[int, dict] | None) -> None:
    if not port_options:
        return
    for raw_port, opts in port_options.items():
        port = int(raw_port)
        server = (opts or {}).get("reset_server") or (opts or {}).get("modem_gateway")
        if server:
            _PORT_RESET_SERVER[port] = str(server).strip()


def set_port_modem_gateway(proxy_port: int, modem_gateway: str | None) -> None:
    port = int(proxy_port)
    if modem_gateway:
        _PORT_RESET_SERVER[port] = str(modem_gateway).strip()
    else:
        _PORT_RESET_SERVER.pop(port, None)


def _reset_url(proxy_port: int) -> str:
    server = reset_server_for_port(proxy_port)
    if server.startswith("http://") or server.startswith("https://"):
        base = server.rstrip("/")
        return f"{base}/reset?proxy=p:{int(proxy_port)}"
    return f"http://{server}/reset?proxy=p:{int(proxy_port)}"


def _get_modem_ip(proxy_port: int) -> str | None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    services = ["https://icanhazip.com", "https://api.ipify.org"]
    for service in services:
        try:
            with httpx.Client(
                proxy=proxy_url, timeout=10.0, verify=get_httpx_tls_context()
            ) as client:
                response = client.get(service)
                if response.status_code == 200:
                    ip = response.text.strip()
                    if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
                        _log(f"IP détectée via {service}: {ip}", proxy_port)
                        return ip
                    _log(
                        f"Réponse non-IP depuis {service} (ignorée): "
                        f"{response.text[:80]!r}",
                        proxy_port,
                    )
        except Exception as e:
            _log(f"Échec {service}: {e}", proxy_port)
    return None


def _trigger_reset(proxy_port: int) -> bool:
    url = _reset_url(proxy_port)
    _log(f"Requête reset: GET {url}", proxy_port)
    try:
        with httpx.Client(
            timeout=RESET_REQUEST_TIMEOUT_SECONDS, verify=get_httpx_tls_context()
        ) as client:
            response = client.get(url)
        _log(f"Réponse reset: HTTP {response.status_code}", proxy_port)
        if response.status_code >= 400:
            _log(f"Corps réponse: {response.text[:200]!r}", proxy_port)
            return False
        return True
    except Exception as e:
        _log(f"Erreur requête reset: {e}", proxy_port)
        return False


def _wait_for_ip_change(
    proxy_port: int,
    old_ip: str | None,
    *,
    unchanged_limit: int = IP_VERIFY_UNCHANGED_LIMIT,
    interval_s: float = IP_VERIFY_INTERVAL_SECONDS,
) -> tuple[bool, str | None]:
    unchanged_streak = 0
    last_seen: str | None = None

    for attempt in range(1, unchanged_limit + 1):
        if attempt > 1:
            time.sleep(interval_s)
        new_ip = _get_modem_ip(proxy_port)
        if not new_ip:
            unchanged_streak = 0
            _log(
                f"Vérif IP {attempt}/{unchanged_limit}: IP non récupérée",
                proxy_port,
            )
            continue

        last_seen = new_ip
        # Ne confirmer un changement QUE si l'on a une IP de référence valide.
        # Sinon (old_ip inconnue) on ne peut PAS affirmer une rotation : ce serait
        # un faux succès qui réintègre une IP potentiellement brûlée dans le pool.
        if old_ip is not None and new_ip != old_ip:
            _log(f"IP changée: {old_ip} -> {new_ip}", proxy_port)
            return True, new_ip

        if old_ip is None:
            _log(
                f"Vérif IP {attempt}/{unchanged_limit}: IP lue ({new_ip}) mais "
                f"changement non vérifiable (IP avant reset inconnue)",
                proxy_port,
            )
            continue

        unchanged_streak += 1
        _log(
            f"Vérif IP {attempt}/{unchanged_limit}: inchangée ({new_ip}) "
            f"[{unchanged_streak}/{unchanged_limit}]",
            proxy_port,
        )
        if unchanged_streak >= unchanged_limit:
            return False, new_ip

    return False, last_seen


def initialize_browser_service(
    ports=None,
    port_options=None,
) -> bool:
    """Compatibilité ProxyZ — aucun navigateur à initialiser."""
    if port_options:
        configure_port_options(port_options)
    return True


def schedule_prepare_after_reset(proxy_port: int) -> None:
    """Compatibilité ProxyZ — no-op."""


def shutdown_browser_service() -> None:
    """Compatibilité ProxyZ — no-op."""


def reset_modem_by_port(proxy_port: int, modem_gateway: str | None = None) -> bool:
    if modem_gateway:
        set_port_modem_gateway(proxy_port, modem_gateway)

    _log(
        f"Démarrage reset HTTP ({reset_server_for_port(proxy_port)})",
        proxy_port,
    )
    # Lire l'IP avant reset avec quelques tentatives : une baseline fiable évite
    # de conclure à tort à un changement (ou à une absence de changement).
    old_ip = None
    for _ in range(3):
        old_ip = _get_modem_ip(proxy_port)
        if old_ip:
            break
        time.sleep(0.6)
    if old_ip:
        _log(f"IP avant reset: {old_ip}", proxy_port)
    else:
        _log(
            "IP avant reset: indisponible — le changement sera vérifié à partir "
            "de la 1re IP observée après reset",
            proxy_port,
        )

    for reset_try in range(1, RESET_ATTEMPTS_ON_STALE_IP + 1):
        if reset_try > 1:
            _log(
                f"Relance reset ({reset_try}/{RESET_ATTEMPTS_ON_STALE_IP}) "
                "après IP inchangée/non vérifiée",
                proxy_port,
            )

        if not _trigger_reset(proxy_port):
            if reset_try < RESET_ATTEMPTS_ON_STALE_IP:
                continue
            return False

        time.sleep(IP_VERIFY_INITIAL_DELAY_SECONDS)
        ip_ok, seen = _wait_for_ip_change(proxy_port, old_ip)
        if ip_ok:
            return True

        # Sans baseline, adopter la 1re IP observée comme référence pour pouvoir
        # confirmer (ou non) un changement à la tentative suivante.
        if old_ip is None and seen:
            old_ip = seen

        if reset_try < RESET_ATTEMPTS_ON_STALE_IP:
            _log("IP toujours inchangée / non vérifiée, nouvelle tentative reset", proxy_port)
            continue

    _log("IP inchangée ou non vérifiée après reset (tentatives épuisées)", proxy_port)
    return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python reset_XPro_dist.py <proxy_port> [reset_server]")
        print("Exemple: python reset_XPro_dist.py 4001")
        print("Exemple: python reset_XPro_dist.py 4001 192.168.1.158")
        print("Serveur par défaut: RESET_XPRO_DIST_SERVER ou 192.168.1.157")
        sys.exit(1)

    port = int(sys.argv[1])
    server = sys.argv[2] if len(sys.argv) > 2 else None
    success = reset_modem_by_port(port, modem_gateway=server)
    sys.exit(0 if success else 1)
