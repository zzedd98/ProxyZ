"""
Reset modem Xproxy XH22 via Playwright Python avec thread dédié et browser persistant.
Interface web 192.168.100.1 : login admin, rotation IP publique (cellRotate).
"""

import atexit
import os
import queue
import re
import select
import socket
import sys
import threading
import time
from concurrent.futures import Future
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import httpx
from playwright.sync_api import sync_playwright

DEFAULT_MODEM_GATEWAY = "192.168.100.1"
MODEM_WEB_URL = f"http://{DEFAULT_MODEM_GATEWAY}/"
MODEM_LOGIN_USER = "admin"
MODEM_LOGIN_PASSWORD = "admin"
MODEM_APP_READY_TIMEOUT_MS = 60_000
IDLE_BROWSER_CLOSE_SECONDS = 15 * 60
IP_VERIFY_UNCHANGED_LIMIT = 5
IP_VERIFY_INTERVAL_SECONDS = 0.9
IP_VERIFY_INITIAL_DELAY_SECONDS = 2.8
RESET_ATTEMPTS_ON_STALE_IP = 2
HEADLESS = os.environ.get("RESET_XPRO_HEADLESS", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)


_PORT_OPTIONS: dict[int, dict] = {}
_PORT_OPTIONS_LOCK = threading.Lock()


def _normalize_modem_url(gateway: str) -> str:
    value = (gateway or "").strip()
    if not value:
        return MODEM_WEB_URL
    if value.startswith("http://") or value.startswith("https://"):
        return value if value.endswith("/") else value + "/"
    return f"http://{value}/"


def configure_port_options(port_options: dict[int, dict] | None) -> None:
    """Enregistre les options par port (ex. modem_gateway) avant warmup/reset."""
    if not port_options:
        return
    with _PORT_OPTIONS_LOCK:
        for raw_port, opts in port_options.items():
            port = int(raw_port)
            merged = dict(_PORT_OPTIONS.get(port, {}))
            merged.update(opts or {})
            _PORT_OPTIONS[port] = merged


def set_port_modem_gateway(proxy_port: int, modem_gateway: str | None) -> None:
    port = int(proxy_port)
    with _PORT_OPTIONS_LOCK:
        if modem_gateway:
            _PORT_OPTIONS.setdefault(port, {})["modem_gateway"] = str(modem_gateway).strip()
        elif port in _PORT_OPTIONS:
            _PORT_OPTIONS[port].pop("modem_gateway", None)
            if not _PORT_OPTIONS[port]:
                _PORT_OPTIONS.pop(port, None)


def modem_web_url_for_port(proxy_port: int) -> str:
    with _PORT_OPTIONS_LOCK:
        gateway = (_PORT_OPTIONS.get(int(proxy_port)) or {}).get("modem_gateway")
    if gateway:
        return _normalize_modem_url(str(gateway))
    env_gw = os.environ.get("RESET_MODEM_GATEWAY", "").strip()
    if env_gw:
        return _normalize_modem_url(env_gw)
    return MODEM_WEB_URL


def _log(message: str, proxy_port: int | None = None) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prefix = f"[XPRO][{ts}]"
    if proxy_port is not None:
        prefix += f" [port {proxy_port}]"
    print(f"{prefix} {message}", flush=True)


def _get_modem_ip(proxy_port: int) -> str | None:
    """IP publique via le proxy local. ifconfig.me renvoie souvent du HTML (page blocage)."""
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    services = ["https://icanhazip.com", "https://api.ipify.org"]
    for service in services:
        try:
            with httpx.Client(proxy=proxy_url, timeout=10.0) as client:
                response = client.get(service)
                if response.status_code == 200:
                    ip = response.text.strip()
                    if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
                        _log(f"IP détectée via {service}: {ip}", proxy_port)
                        return ip
                    _log(
                        f"Réponse non-IP depuis {service} (ignorée, essai suivant): "
                        f"{response.text[:80]!r}",
                        proxy_port,
                    )
        except Exception as e:
            _log(f"Échec {service}: {e}", proxy_port)
            continue
    return None


def _launch_chromium(playwright) -> object:
    launch_args = [
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--no-sandbox",
        "--ignore-certificate-errors",
    ]
    modes = [
        ("chromium headless", {"headless": HEADLESS, "args": launch_args}),
        ("chrome channel", {"channel": "chrome", "headless": HEADLESS, "args": launch_args}),
    ]
    if not HEADLESS:
        modes.insert(0, ("chromium visible", {"headless": False, "args": launch_args}))

    last_err: Exception | None = None
    for label, kwargs in modes:
        try:
            _log(f"Lancement navigateur ({label}, headless={kwargs.get('headless', HEADLESS)})")
            return playwright.chromium.launch(**kwargs)
        except Exception as e:
            last_err = e
            _log(f"Échec lancement ({label}): {e}")
    raise RuntimeError(f"Impossible de lancer Chromium: {last_err}")


def _rewrite_proxy_form_request(request: bytes) -> bytes:
    """Playwright envoie GET http://host/ ; le modem attend GET / (origin-form)."""
    head, sep, body = request.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    if not lines:
        return request

    first = lines[0].decode("latin-1", errors="ignore")
    parts = first.split()
    if len(parts) < 3:
        return request

    method, target, version = parts[0], parts[1], parts[2]
    if not (target.startswith("http://") or target.startswith("https://")):
        return request

    parsed = urlparse(target)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    host_header = host if port in (80, 443) else f"{host}:{port}"
    lines[0] = f"{method} {path} {version}".encode("latin-1")

    new_lines = [lines[0]]
    host_written = False
    for line in lines[1:]:
        lower = line.lower()
        if lower.startswith(b"host:"):
            new_lines.append(f"Host: {host_header}".encode("latin-1"))
            host_written = True
        elif lower.startswith(b"proxy-connection:"):
            continue
        else:
            new_lines.append(line)
    if not host_written:
        new_lines.insert(1, f"Host: {host_header}".encode("latin-1"))

    return b"\r\n".join(new_lines) + sep + body


def _read_http_request(sock: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(8192)
        if not chunk:
            break
        data += chunk
    if not data:
        return b""

    header_end = data.index(b"\r\n\r\n") + 4
    headers = data[:header_end].decode("latin-1", errors="ignore")
    content_length = 0
    for line in headers.split("\r\n"):
        if line.lower().startswith("content-length:"):
            try:
                content_length = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
            break

    body = data[header_end:]
    while len(body) < content_length:
        chunk = sock.recv(8192)
        if not chunk:
            break
        body += chunk
    return data[:header_end] + body[:content_length]


def _relay_sockets(a: socket.socket, b: socket.socket) -> None:
    sockets = [a, b]
    a.settimeout(120.0)
    b.settimeout(120.0)
    while True:
        try:
            readable, _, _ = select.select(sockets, [], [], 120.0)
        except OSError:
            break
        if not readable:
            break
        for sock in readable:
            try:
                chunk = sock.recv(8192)
            except OSError:
                return
            if not chunk:
                return
            other = b if sock is a else a
            try:
                other.sendall(chunk)
            except OSError:
                return


class _ModemAdminForwardProxy:
    """
    Proxy local : réécrit proxy-form -> origin-form puis transmet au proxy ProxyZ
    du port (bind sur la bonne interface). Contourne le 400 du modem sans toucher ProxyZ.
    """

    def __init__(self, upstream_proxy_port: int) -> None:
        self.upstream_proxy_port = int(upstream_proxy_port)
        self.listen_port: int | None = None
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> int:
        with self._lock:
            if self.listen_port is not None:
                return self.listen_port
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", 0))
            server.listen(8)
            self.listen_port = server.getsockname()[1]
            self._server = server
            self._thread = threading.Thread(target=self._serve, daemon=True)
            self._thread.start()
            return self.listen_port

    def stop(self) -> None:
        with self._lock:
            if self._server is not None:
                try:
                    self._server.close()
                except OSError:
                    pass
                self._server = None
            self.listen_port = None

    def _serve(self) -> None:
        server = self._server
        if server is None:
            return
        while True:
            try:
                client, _ = server.accept()
            except OSError:
                break
            threading.Thread(
                target=self._handle_client,
                args=(client,),
                daemon=True,
            ).start()

    def _handle_client(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            request = _read_http_request(client)
            if not request:
                return

            first_line = request.split(b"\r\n", 1)[0].decode("latin-1", errors="ignore")
            upstream = socket.create_connection(
                ("127.0.0.1", self.upstream_proxy_port), timeout=30
            )

            if first_line.upper().startswith("CONNECT "):
                upstream.sendall(request)
                reply = upstream.recv(4096)
                if reply:
                    client.sendall(reply)
                if reply and b"200" in reply.split(b"\r\n", 1)[0]:
                    _relay_sockets(client, upstream)
                return

            outbound = _rewrite_proxy_form_request(request)
            upstream.sendall(outbound)
            _relay_sockets(client, upstream)
        except Exception as e:
            _log(f"Mini-proxy erreur: {e}", self.upstream_proxy_port)
        finally:
            try:
                client.close()
            except OSError:
                pass
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass


def _dismiss_quick_setup_modal(page, proxy_port: int | None) -> None:
    skip_btn = page.locator("#btnSkip")
    try:
        if skip_btn.is_visible(timeout=2000):
            _log("Fermeture modal Quick Setup", proxy_port)
            skip_btn.click(timeout=5000)
            page.wait_for_timeout(500)
    except Exception:
        pass


def _wait_modem_app_ready(page, proxy_port: int | None) -> None:
    """Attend login ou dashboard (SPA jQuery, chargement asynchrone)."""
    _log("Attente interface modem (login ou dashboard)", proxy_port)
    page.wait_for_function(
        """() => {
            const rotate = document.getElementById('cellRotate');
            const username = document.getElementById('tbarouter_username');
            const welcome = document.getElementById('lableWelcome');
            const visible = (el) => el && (el.offsetParent !== null || el.getClientRects().length > 0);
            return visible(rotate) || visible(username) || visible(welcome);
        }""",
        timeout=MODEM_APP_READY_TIMEOUT_MS,
    )


def _is_dashboard_ready(page) -> bool:
    rotate = page.locator("#cellRotate")
    if rotate.count() == 0:
        return False
    try:
        return rotate.is_visible(timeout=2000)
    except Exception:
        return False


def _perform_login(page, proxy_port: int | None) -> None:
    _log("Connexion admin", proxy_port)
    page.locator("#tbarouter_username").fill(MODEM_LOGIN_USER, timeout=15000)
    page.locator("#tbarouter_password").fill(MODEM_LOGIN_PASSWORD, timeout=15000)
    page.evaluate(
        """() => {
        const username = document.getElementById('tbarouter_username');
        const password = document.getElementById('tbarouter_password');
        if (username) username.value = 'admin';
        if (password) password.value = 'admin';
        if (typeof Login === 'function') {
            Login();
            return;
        }
        const btn = document.getElementById('btnSignin');
        if (btn) btn.click();
    }"""
    )
    page.wait_for_selector("#cellRotate", state="attached", timeout=MODEM_APP_READY_TIMEOUT_MS)
    _dismiss_quick_setup_modal(page, proxy_port)


def _ensure_dashboard(page, proxy_port: int | None) -> None:
    _wait_modem_app_ready(page, proxy_port)
    if _is_dashboard_ready(page):
        _log("Dashboard déjà affiché", proxy_port)
        _dismiss_quick_setup_modal(page, proxy_port)
        return

    username = page.locator("#tbarouter_username")
    if username.count() > 0:
        _perform_login(page, proxy_port)
    else:
        _log("Dashboard en chargement, attente #cellRotate", proxy_port)
        page.wait_for_selector("#cellRotate", state="visible", timeout=MODEM_APP_READY_TIMEOUT_MS)

    if not _is_dashboard_ready(page):
        raise RuntimeError("Dashboard XH22 non prêt (#cellRotate introuvable après connexion)")


def _trigger_ip_rotation(page, proxy_port: int | None) -> None:
    _log("Déclenchement Rotate Public IP (CellularRotate)", proxy_port)
    page.evaluate(
        """() => {
        const rotate = document.getElementById('cellRotate');
        if (!rotate) {
            throw new Error('cellRotate introuvable');
        }
        if (typeof CellularRotate === 'function') {
            CellularRotate();
            return;
        }
        rotate.click();
    }"""
    )


class _PlaywrightPortWorker:
    def __init__(self, proxy_port: int) -> None:
        self.proxy_port = int(proxy_port)
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._playwright = None
        self._browser = None
        self._admin_proxy: _ModemAdminForwardProxy | None = None
        self._last_activity = time.time()
        self._prepared = False
        self._prepared_context = None
        self._prepared_page = None
        self._modem_web_url = modem_web_url_for_port(self.proxy_port)

    def _sync_modem_url(self) -> None:
        url = modem_web_url_for_port(self.proxy_port)
        if url != self._modem_web_url:
            _log(f"Passerelle modem: {self._modem_web_url} -> {url}", self.proxy_port)
            self._modem_web_url = url
            self._discard_prepared_session()
        else:
            self._modem_web_url = url

    def _playwright_proxy_url(self) -> str:
        if self._admin_proxy is None:
            self._admin_proxy = _ModemAdminForwardProxy(self.proxy_port)
        listen = self._admin_proxy.start()
        return f"http://127.0.0.1:{listen}"

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
        _log(f"Préparation page reset (timeout={timeout_s}s)", self.proxy_port)
        self.start()
        if not self._ready.wait(timeout=10.0):
            _log("Timeout: thread worker non prêt", self.proxy_port)
            return False
        fut: Future = Future()
        self._queue.put(("prepare", None, fut))
        try:
            return bool(fut.result(timeout=timeout_s))
        except Exception as e:
            _log(f"Échec préparation page reset: {e}", self.proxy_port)
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
        _log("Initialisation Playwright", self.proxy_port)
        self._playwright = sync_playwright().start()
        self._browser = _launch_chromium(self._playwright)
        _log("Browser Playwright prêt (thread dédié persistant).", self.proxy_port)

    def _restart_browser(self) -> None:
        _log("Redémarrage browser", self.proxy_port)
        self._discard_prepared_session()
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception as e:
            _log(f"Erreur fermeture browser: {e}", self.proxy_port)
        self._browser = None
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception as e:
            _log(f"Erreur arrêt Playwright: {e}", self.proxy_port)
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
        playwright_proxy = self._playwright_proxy_url()
        _log(
            f"Proxy Playwright -> mini-proxy -> port {self.proxy_port} ({playwright_proxy})",
            self.proxy_port,
        )
        context = self._browser.new_context(
            proxy={"server": playwright_proxy},
            viewport={"width": 1280, "height": 720},
            locale="en-US",
            ignore_https_errors=True,
        )
        page = context.new_page()
        return context, page

    def _navigate_to_reset_page(self, page) -> None:
        self._sync_modem_url()
        modem_url = self._modem_web_url
        for attempt in range(1, 4):
            try:
                _log(f"Navigation {modem_url} (tentative {attempt}/3)", self.proxy_port)
                page.goto(modem_url, timeout=30000, wait_until="domcontentloaded")
                _log(f"Page chargée: {page.url}", self.proxy_port)
                break
            except Exception as goto_err:
                _log(f"goto échoué: {goto_err}", self.proxy_port)
                if attempt < 3 and (
                    "ERR_EMPTY_RESPONSE" in str(goto_err) or "Timeout" in str(goto_err)
                ):
                    page.wait_for_timeout(2500)
                    continue
                raise
        page.wait_for_timeout(1500)
        _ensure_dashboard(page, self.proxy_port)

    def _prepare_session(self) -> bool:
        self._discard_prepared_session()
        context, page = self._new_modem_page()
        try:
            self._navigate_to_reset_page(page)
            self._prepared_context = context
            self._prepared_page = page
            self._prepared = True
            self._last_activity = time.time()
            _log("Page reset prête (#cellRotate)", self.proxy_port)
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
            page = self._prepared_page
            _trigger_ip_rotation(page, self.proxy_port)
            page.wait_for_timeout(3000)
            _log("Rotation déclenchée (page préparée)", self.proxy_port)
            return

        _log("Page non préparée, séquence complète (navigation + rotation)", self.proxy_port)
        context, page = self._new_modem_page()
        try:
            self._navigate_to_reset_page(page)
            _trigger_ip_rotation(page, self.proxy_port)
            page.wait_for_timeout(3000)
        finally:
            try:
                context.close()
            except Exception:
                pass

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
        _log("Reset reçu dans la file", self.proxy_port)
        try:
            self._run_reset_click()
            self._last_activity = time.time()
            if fut:
                fut.set_result(True)
            _log("Reset UI réussi", self.proxy_port)
        except Exception as e:
            _log(f"Reset UI échoué, retry après restart browser: {e}", self.proxy_port)
            try:
                self._restart_browser()
                self._run_reset_click()
                self._last_activity = time.time()
                if fut:
                    fut.set_result(True)
                _log("Reset UI réussi après retry", self.proxy_port)
            except Exception as e2:
                _log(f"Reset UI échoué définitivement: {e2}", self.proxy_port)
                if fut:
                    fut.set_exception(e2 if e2 else e)
        finally:
            self._discard_prepared_session()

    def _run(self) -> None:
        _log("Démarrage thread worker", self.proxy_port)
        self._ready.set()
        while True:
            try:
                action, payload, fut = self._queue.get(timeout=20.0)
            except queue.Empty:
                self._close_browser_if_idle()
                continue
            if action == "shutdown":
                _log("Arrêt worker demandé", self.proxy_port)
                break
            if action == "prepare":
                self._handle_prepare(fut)
                continue
            if action == "reset":
                self._handle_reset(fut)
                continue
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
        if self._admin_proxy is not None:
            self._admin_proxy.stop()
            self._admin_proxy = None
        _log("Thread worker terminé", self.proxy_port)


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

    def schedule_prepare_ports(
        self, ports: list[int], port_options: dict[int, dict] | None = None
    ) -> None:
        if port_options:
            configure_port_options(port_options)
        for p in sorted({int(x) for x in ports if int(x) > 0}):
            worker = self._get_worker(p)
            worker._sync_modem_url()
            worker.schedule_prepare()

    def prepare_ports(self, ports: list[int], timeout_s: float = 120.0) -> bool:
        if not ports:
            return True
        ok = True
        for p in sorted({int(x) for x in ports if int(x) > 0}):
            worker = self._get_worker(p)
            if not worker.prepare(timeout_s=timeout_s):
                ok = False
        return ok

    def schedule_prepare(self, proxy_port: int) -> None:
        worker = self._get_worker(proxy_port)
        worker._sync_modem_url()
        worker.schedule_prepare()

    def submit(self, proxy_port: int, timeout_s: float = 120.0) -> bool:
        worker = self._get_worker(proxy_port)
        worker._sync_modem_url()
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


def initialize_browser_service(
    ports: Optional[list[int]] = None,
    port_options: Optional[dict[int, dict]] = None,
) -> bool:
    """
    Lance la préparation des pages reset en arrière-plan (non bloquant).
    port_options: {101: {"modem_gateway": "192.168.100.1"}, ...}
    """
    ports_list = [int(p) for p in (ports or []) if int(p) > 0]
    if port_options:
        configure_port_options(port_options)
    _log(f"Préparation navigateurs en arrière-plan pour ports={ports_list}")
    if ports_list:
        _POOL.schedule_prepare_ports(ports_list, port_options=port_options)
    return True


def schedule_prepare_after_reset(proxy_port: int) -> None:
    """Re-prépare un navigateur en arrière-plan après un reset réussi."""
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
    """
    Vérifie l'IP après reset. Échec si `unchanged_limit` lectures consécutives
    donnent la même IP que avant reset.
    """
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


def reset_modem_by_port(proxy_port: int, modem_gateway: str | None = None) -> bool:
    if modem_gateway:
        set_port_modem_gateway(proxy_port, modem_gateway)
    _log(
        f"Démarrage reset Playwright (passerelle {modem_web_url_for_port(proxy_port)})",
        proxy_port,
    )
    old_ip = _get_modem_ip(proxy_port)
    if old_ip:
        _log(f"IP avant reset: {old_ip}", proxy_port)
    else:
        _log("IP avant reset: indisponible", proxy_port)

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

        time.sleep(IP_VERIFY_INITIAL_DELAY_SECONDS)
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
        print("Usage: python reset_XPro.py <proxy_port> [modem_gateway]")
        print("Exemple: python reset_XPro.py 101 192.168.100.1")
        print("Navigateur visible: set RESET_XPRO_HEADLESS=0")
        sys.exit(1)

    port = int(sys.argv[1])
    gateway = sys.argv[2] if len(sys.argv) > 2 else None
    success = reset_modem_by_port(port, modem_gateway=gateway)
    sys.exit(0 if success else 1)
