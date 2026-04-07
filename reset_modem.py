"""
Reset modem via Playwright Python avec thread dédié et browser persistant.
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
from playwright.sync_api import sync_playwright

MODEM_WEB_URL = "http://192.168.8.1/#/mobileconnection"
IDLE_BROWSER_CLOSE_SECONDS = 15 * 60


def _get_modem_ip(proxy_port: int) -> str | None:
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    services = ["https://api.ipify.org", "https://ifconfig.me", "https://icanhazip.com"]
    for service in services:
        try:
            with httpx.Client(proxy=proxy_url, timeout=10.0) as client:
                response = client.get(service)
                if response.status_code == 200:
                    ip = response.text.strip()
                    if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
                        return ip
        except Exception:
            continue
    return None


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

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._ready.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def warmup(self, timeout_s: float = 20.0) -> bool:
        self.start()
        return self._ready.wait(timeout=timeout_s)

    def submit(self, timeout_s: float = 120.0) -> bool:
        self.start()
        fut: Future = Future()
        self._queue.put(("reset", None, fut))
        return bool(fut.result(timeout=timeout_s))

    def shutdown(self) -> None:
        self._queue.put(("shutdown", None, None))

    def _ensure_browser(self) -> None:
        if self._browser is not None:
            return
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        print("[RESET] Browser Playwright lancé (thread dédié persistant).")

    def _restart_browser(self) -> None:
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

    def _run_reset_sequence(self) -> None:
        context = None
        try:
            self._ensure_browser()
            context = self._browser.new_context(
                proxy={"server": f"http://127.0.0.1:{self.proxy_port}"}
            )
            page = context.new_page()
            for attempt in range(1, 4):
                try:
                    page.goto(MODEM_WEB_URL, timeout=30000, wait_until="domcontentloaded")
                    break
                except Exception as goto_err:
                    if attempt < 3 and (
                        "ERR_EMPTY_RESPONSE" in str(goto_err)
                        or "Timeout" in str(goto_err)
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
            page.get_by_role("button", name="Save").click()
            page.wait_for_timeout(2000)
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass

    def _close_browser_if_idle(self) -> None:
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
        print(f"[RESET] Browser port {self.proxy_port} auto-fermé après inactivité.")

    def _run(self) -> None:
        try:
            self._ensure_browser()
            self._last_activity = time.time()
            self._ready.set()
        except Exception as e:
            print(f"[RESET] Échec warmup browser: {e}")
            self._ready.set()
        while True:
            try:
                action, payload, fut = self._queue.get(timeout=20.0)
            except queue.Empty:
                self._close_browser_if_idle()
                continue
            if action == "shutdown":
                break
            if action != "reset":
                continue
            try:
                self._run_reset_sequence()
                self._last_activity = time.time()
                if fut:
                    fut.set_result(True)
            except Exception as e:
                try:
                    self._restart_browser()
                    self._run_reset_sequence()
                    self._last_activity = time.time()
                    if fut:
                        fut.set_result(True)
                except Exception as e2:
                    if fut:
                        fut.set_exception(e2 if e2 else e)
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

    def warmup_ports(self, ports: list[int], timeout_s: float = 20.0) -> bool:
        if not ports:
            return True
        ok = True
        for p in sorted({int(x) for x in ports if int(x) > 0}):
            worker = self._get_worker(p)
            if not worker.warmup(timeout_s=timeout_s):
                ok = False
        return ok

    def submit(self, proxy_port: int, timeout_s: float = 120.0) -> bool:
        worker = self._get_worker(proxy_port)
        return worker.submit(timeout_s=timeout_s)

    def shutdown(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.shutdown()


_POOL = _PlaywrightWorkerPool()


def initialize_browser_service(ports: Optional[list[int]] = None) -> bool:
    """
    Pré-initialise le pool Playwright.
    - ports fournis: un browser persistant par port.
    - sinon: lazy init au premier reset.
    """
    return _POOL.warmup_ports(ports or [], timeout_s=20.0)


def shutdown_browser_service() -> None:
    _POOL.shutdown()


atexit.register(shutdown_browser_service)


def reset_modem_by_port(proxy_port: int) -> bool:
    print(f"[RESET] Démarrage reset Playwright port {proxy_port}...")
    old_ip = _get_modem_ip(proxy_port)
    if old_ip:
        print(f"[RESET] IP avant reset (port {proxy_port}): {old_ip}")

    try:
        _POOL.submit(proxy_port, timeout_s=120.0)
    except Exception as e:
        print(f"[RESET] Erreur browser reset (port {proxy_port}): {e}")
        return False

    for attempt in range(1, 13):
        time.sleep(5)
        new_ip = _get_modem_ip(proxy_port)
        if not new_ip:
            continue
        if old_ip is None or new_ip != old_ip:
            print(f"[RESET] IP changée (port {proxy_port}): {old_ip} -> {new_ip}")
            return True
        print(f"[RESET] Tentative {attempt}/12: IP inchangée ({new_ip})")
    print(f"[RESET] IP inchangée après reset (port {proxy_port}).")
    return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python reset_modem.py <proxy_port>")
        print("Exemple: python reset_modem.py 101")
        sys.exit(1)

    port = int(sys.argv[1])
    success = reset_modem_by_port(port)
    sys.exit(0 if success else 1)
