"""
Reset modem via Playwright Python avec thread dédié et browser persistant.
Warmup : navigation jusqu'à la page prête (avant Save).
Reset : clic Save sur la page préparée, réutilisée telle quelle entre chaque reset.
En cas d'erreur UI : redémarrage browser + séquence complète pour retrouver la page.
"""

import atexit
import queue
import re
import sys
import threading
import time
from concurrent.futures import Future
from typing import Optional

import httpx
import certifi
import os
import ssl
from pathlib import Path
from playwright.sync_api import sync_playwright

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


MODEM_WEB_URL = "http://192.168.8.1/#/mobileconnection"
IDLE_BROWSER_CLOSE_SECONDS = 15 * 60
IP_VERIFY_UNCHANGED_LIMIT = 2
IP_VERIFY_INTERVAL_SECONDS = 5
# ProxyZ espace et plafonne les reprises : une seule tentative par exécution.
RESET_ATTEMPTS_ON_STALE_IP = 1


def _log(message: str, proxy_port: int | None = None) -> None:
    prefix = "[HUAWEI][RESET]"
    if proxy_port is not None:
        prefix += f" [port {proxy_port}]"
    print(f"{prefix} {message}", flush=True)


def _get_modem_ip(proxy_port: int) -> str | None:
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    services = ["https://api.ipify.org", "https://ifconfig.me", "https://icanhazip.com"]
    for service in services:
        try:
            with httpx.Client(
                proxy=proxy_url, timeout=10.0, verify=get_httpx_tls_context()
            ) as client:
                response = client.get(service)
                if response.status_code == 200:
                    ip = response.text.strip()
                    if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
                        return ip
        except Exception:
            continue
    return None


def _navigate_to_reset_page(page, proxy_port: int) -> None:
    for attempt in range(1, 4):
        try:
            page.goto(MODEM_WEB_URL, timeout=30000, wait_until="domcontentloaded")
            break
        except Exception as goto_err:
            if attempt < 3 and (
                "ERR_EMPTY_RESPONSE" in str(goto_err) or "Timeout" in str(goto_err)
            ):
                page.wait_for_timeout(2500)
                continue
            raise
    page.wait_for_timeout(2000)
    page.locator("tr.table-data:nth-child(3)").click()
    page.wait_for_timeout(1000)
    page.evaluate(
        """() => {
        const cb = document.querySelector('#defaultProfile input[type="checkbox"]');
        if (cb) cb.click();
    }"""
    )
    page.wait_for_timeout(500)
    _log("Page reset prête (avant Save)", proxy_port)


def _trigger_save(page, proxy_port: int) -> None:
    _log("Clic Save (rotation)", proxy_port)
    page.get_by_role("button", name="Save").click()
    page.wait_for_timeout(2000)


class _PlaywrightPortWorker:
    def __init__(self, proxy_port: int) -> None:
        self.proxy_port = int(proxy_port)
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._playwright = None
        self._browser = None
        self._last_activity = time.time()
        self._prepared = False
        self._prepared_context = None
        self._prepared_page = None

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._ready.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def is_prepared(self) -> bool:
        return self._prepared and self._prepared_page is not None

    def prepare(self, timeout_s: float = 120.0) -> bool:
        self.start()
        if not self._ready.wait(timeout=10.0):
            return False
        fut: Future = Future()
        self._queue.put(("prepare", None, fut))
        try:
            return bool(fut.result(timeout=timeout_s))
        except Exception as e:
            _log(f"Échec préparation: {e}", self.proxy_port)
            return False

    def schedule_prepare(self) -> None:
        self.start()
        self._queue.put(("prepare", None, None))

    def submit(self, timeout_s: float = 120.0) -> bool:
        self.start()
        fut: Future = Future()
        self._queue.put(("reset", None, fut))
        return bool(fut.result(timeout=timeout_s))

    def shutdown(self) -> None:
        self._queue.put(("shutdown", None, None))

    def join_shutdown(self, timeout_s: float = 15.0) -> None:
        self.shutdown()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout_s)

    def _ensure_browser(self) -> None:
        if self._browser is not None:
            return
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        _log("Browser Playwright lancé (thread dédié persistant).", self.proxy_port)

    def _restart_browser(self) -> None:
        self._discard_prepared_session()
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        self._browser = None
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        self._playwright = None
        self._ensure_browser()

    def _discard_prepared_session(self) -> None:
        self._prepared = False
        self._prepared_page = None
        if self._prepared_context is not None:
            try:
                self._prepared_context.close()
            except Exception:
                pass
        self._prepared_context = None

    def _new_modem_page(self):
        self._ensure_browser()
        context = self._browser.new_context(
            proxy={"server": f"http://127.0.0.1:{self.proxy_port}"}
        )
        return context, context.new_page()

    def _prepare_session(self) -> bool:
        if self.is_prepared():
            _log("Page reset déjà prête, réutilisation", self.proxy_port)
            self._last_activity = time.time()
            return True
        self._discard_prepared_session()
        context, page = self._new_modem_page()
        try:
            _navigate_to_reset_page(page, self.proxy_port)
            self._prepared_context = context
            self._prepared_page = page
            self._prepared = True
            self._last_activity = time.time()
            return True
        except Exception:
            try:
                context.close()
            except Exception:
                pass
            raise

    def _run_reset_click(self) -> None:
        if self.is_prepared():
            _log("Reset rapide via page préparée", self.proxy_port)
            _trigger_save(self._prepared_page, self.proxy_port)
            return

        _log("Page non préparée, séquence complète", self.proxy_port)
        context, page = self._new_modem_page()
        _navigate_to_reset_page(page, self.proxy_port)
        _trigger_save(page, self.proxy_port)
        self._prepared_context = context
        self._prepared_page = page
        self._prepared = True
        _log("Page reset prête après séquence complète", self.proxy_port)

    def _close_browser_if_idle(self) -> None:
        if self._prepared:
            return
        if self._browser is None:
            return
        idle_for = time.time() - self._last_activity
        if idle_for < IDLE_BROWSER_CLOSE_SECONDS:
            return
        try:
            self._browser.close()
        except Exception:
            pass
        self._browser = None
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        self._playwright = None
        _log(f"Browser auto-fermé après {int(idle_for)}s d'inactivité", self.proxy_port)

    def _handle_prepare(self, fut: Future | None) -> None:
        try:
            ok = self._prepare_session()
            if fut:
                fut.set_result(ok)
        except Exception as e:
            _log(f"Préparation échouée: {e}", self.proxy_port)
            if fut:
                fut.set_exception(e)

    def _handle_reset(self, fut: Future | None) -> None:
        try:
            self._run_reset_click()
            self._last_activity = time.time()
            if fut:
                fut.set_result(True)
            _log("Reset UI réussi (page conservée pour prochain reset)", self.proxy_port)
        except Exception as e:
            _log(f"Reset UI échoué, retry après restart browser: {e}", self.proxy_port)
            self._discard_prepared_session()
            try:
                self._restart_browser()
                self._run_reset_click()
                self._last_activity = time.time()
                if fut:
                    fut.set_result(True)
                _log(
                    "Reset UI réussi après retry (page re-préparée)",
                    self.proxy_port,
                )
            except Exception as e2:
                self._discard_prepared_session()
                if fut:
                    fut.set_exception(e2 if e2 else e)

    def _run(self) -> None:
        self._ready.set()
        while True:
            try:
                action, payload, fut = self._queue.get(timeout=20.0)
            except queue.Empty:
                self._close_browser_if_idle()
                continue
            if action == "shutdown":
                break
            if action == "prepare":
                self._handle_prepare(fut)
            elif action == "reset":
                self._handle_reset(fut)
        self._discard_prepared_session()
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        self._browser = None
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        self._playwright = None


class _PlaywrightWorkerPool:
    def __init__(self) -> None:
        self._workers: dict[int, _PlaywrightPortWorker] = {}
        self._lock = threading.Lock()

    def _get_worker(self, proxy_port: int) -> _PlaywrightPortWorker:
        port = int(proxy_port)
        with self._lock:
            worker = self._workers.get(port)
            if worker is None:
                worker = _PlaywrightPortWorker(port)
                self._workers[port] = worker
            return worker

    def schedule_prepare_ports(self, ports: list[int]) -> None:
        for p in sorted({int(x) for x in ports if int(x) > 0}):
            self._get_worker(p).schedule_prepare()

    def schedule_prepare(self, proxy_port: int) -> None:
        self._get_worker(proxy_port).schedule_prepare()

    def submit(self, proxy_port: int, timeout_s: float = 120.0) -> bool:
        worker = self._get_worker(proxy_port)
        return worker.submit(timeout_s=timeout_s)

    def restart_worker(self, proxy_port: int, timeout_s: float = 15.0) -> None:
        port = int(proxy_port)
        with self._lock:
            worker = self._workers.pop(port, None)
        if worker is None:
            return
        _log("Redémarrage thread worker (session navigateur fermée)", port)
        worker.join_shutdown(timeout_s=timeout_s)

    def shutdown(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.shutdown()


_POOL = _PlaywrightWorkerPool()


def initialize_browser_service(ports: Optional[list[int]] = None) -> bool:
    """Prépare les pages reset en arrière-plan (non bloquant)."""
    ports_list = [int(p) for p in (ports or []) if int(p) > 0]
    _log(f"Préparation navigateurs en arrière-plan pour ports={ports_list}")
    if ports_list:
        _POOL.schedule_prepare_ports(ports_list)
    return True


def schedule_prepare_after_reset(proxy_port: int) -> None:
    worker = _POOL._get_worker(proxy_port)
    if worker.is_prepared():
        _log("Page reset toujours prête, re-préparation ignorée", proxy_port)
        return
    _log("Re-préparation navigateur planifiée", proxy_port)
    _POOL.schedule_prepare(proxy_port)


def shutdown_browser_service() -> None:
    _POOL.shutdown()


atexit.register(shutdown_browser_service)


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
        if old_ip is None or new_ip != old_ip:
            _log(f"IP changée: {old_ip} -> {new_ip}", proxy_port)
            return True, new_ip

        unchanged_streak += 1
        _log(
            f"Vérif IP {attempt}/{unchanged_limit}: inchangée ({new_ip}) "
            f"[{unchanged_streak}/{unchanged_limit}]",
            proxy_port,
        )
        if unchanged_streak >= unchanged_limit:
            return False, new_ip

    return False, last_seen


def reset_modem_by_port(proxy_port: int) -> bool:
    _log("Démarrage reset Playwright", proxy_port)
    old_ip = _get_modem_ip(proxy_port)
    if old_ip:
        _log(f"IP avant reset: {old_ip}", proxy_port)

    for reset_try in range(1, RESET_ATTEMPTS_ON_STALE_IP + 1):
        if reset_try > 1:
            _log(
                f"Relance reset ({reset_try}/{RESET_ATTEMPTS_ON_STALE_IP}) "
                "après IP inchangée",
                proxy_port,
            )
            _POOL.restart_worker(proxy_port)

        try:
            _POOL.submit(proxy_port, timeout_s=120.0)
        except Exception as e:
            _log(f"Erreur browser reset: {e}", proxy_port)
            if reset_try < RESET_ATTEMPTS_ON_STALE_IP:
                _POOL.restart_worker(proxy_port)
                continue
            return False

        ip_ok, _ = _wait_for_ip_change(proxy_port, old_ip)
        if ip_ok:
            schedule_prepare_after_reset(proxy_port)
            return True

        if reset_try < RESET_ATTEMPTS_ON_STALE_IP:
            _log(
                "IP toujours inchangée, redémarrage worker + nouvelle tentative reset",
                proxy_port,
            )
            continue

    _log("IP inchangée après reset (tentatives épuisées)", proxy_port)
    return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python reset_huawei.py <proxy_port>")
        print("Exemple: python reset_huawei.py 101")
        sys.exit(1)

    port = int(sys.argv[1])
    success = reset_modem_by_port(port)
    sys.exit(0 if success else 1)
