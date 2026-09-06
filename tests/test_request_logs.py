"""Requêtes simulées : aucun socket réseau ni démarrage de Qt."""
import ast
import asyncio
import json
import logging
from pathlib import Path
import re
import types
import unittest
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlparse


def load_logs():
    path = Path(__file__).resolve().parents[1] / "ProxyZ.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in {
            "log_zrotate_request", "parse_connect_request", "parse_http_proxy_request", "_host_is_ip_only"
        }:
            nodes.append(n)
        if isinstance(n, ast.ClassDef):
            if n.name == "LogHandler":
                nodes.append(n)
            elif n.name in {"MainWindow", "ZRotateSingleProxyServer"}:
                for method in n.body:
                    if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name in {
                        "_handle_client", "_zrotate_log", "_ensure_console_buffer"
                    }:
                        nodes.append(method)
    module = types.ModuleType("request_logs")
    module.__dict__.update(
        json=json, re=re, logging=logging, urlparse=urlparse,
        logger=logging.Logger("request-test", logging.INFO),
        time=types.SimpleNamespace(strftime=Mock(return_value="12:00:00")),
        CONSOLE_MAX_LINES=2,
        asyncio=types.SimpleNamespace(
            CancelledError=asyncio.CancelledError, TimeoutError=asyncio.TimeoutError, wait_for=asyncio.wait_for,
            open_connection=AsyncMock(side_effect=OSError("upstream unavailable")),
        ),
        read_until_double_crlf=AsyncMock(), rebuild_http_request=Mock(return_value=b"GET / HTTP/1.1\r\n\r\n"),
        open_connection_with_bind=AsyncMock(side_effect=OSError("upstream unavailable")),
    )
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(path), "exec"), module.__dict__)
    return module


class RequestLogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.code = load_logs()
        self.signal = Mock()
        self.code.logger.addHandler(self.code.LogHandler(self.signal))

    async def request(self, first_line, interface):
        self.code.read_until_double_crlf.return_value = (first_line + "\r\n\r\n").encode()
        server = types.SimpleNamespace(
            _connection_counter=0, _use_quotas=True, total_requests=0, rejected_requests=0,
            _close_haapi_tunnel_after_seconds=30,
            quota_manager=types.SimpleNamespace(
                acquire_interface_for_request=AsyncMock(return_value=interface),
                complete_request=AsyncMock(),
            ),
        )
        writer = types.SimpleNamespace(get_extra_info=Mock(return_value=("127.0.0.1", 1000)),
            write=Mock(), drain=AsyncMock(), close=Mock(), wait_closed=AsyncMock())
        await self.code._handle_client(server, object(), writer)
        return writer

    async def test_auth_tunnel_is_not_closed_by_hostname_timer(self):
        upstream = types.SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
        self.code.open_connection_with_bind = AsyncMock(return_value=(object(), upstream))
        self.code.relay_tunnel = AsyncMock()
        scheduled = []
        def schedule(coro):
            scheduled.append(coro)
            coro.close()  # Aucun timer réel dans ce test.
        self.code.asyncio.create_task = schedule
        await self.request("CONNECT auth.example.com:443 HTTP/1.1",
                           {"name": "A", "ip": "127.0.0.2", "auth_protected": True})
        self.code.relay_tunnel.assert_awaited_once()
        self.assertEqual(scheduled, [])
        # Option désactivée : conserver la fermeture habituelle des hostnames.
        await self.request("CONNECT auth.example.com:443 HTTP/1.1",
                           {"name": "A", "ip": "127.0.0.2"})
        self.assertEqual(len(scheduled), 1)

    async def test_connect_destination_logged_once_even_if_upstream_fails(self):
        writer = await self.request("CONNECT auth.example.com:443 HTTP/1.1", {"name": "Clé 1", "ip": "127.0.0.2"})
        self.code.open_connection_with_bind.assert_awaited_once_with("auth.example.com", 443, "127.0.0.2")
        writer.write.assert_any_call(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        self.signal.emit.assert_called_once_with('[REQ] CONNECT → auth.example.com:443 | interface: "Clé 1"')

    async def test_http_logs_method_and_host_without_path_query_or_credentials(self):
        await self.request("GET http://example.com/login?token=secret HTTP/1.1", {"name": "Clé 1", "ip": "127.0.0.2"})
        self.code.open_connection_with_bind.assert_awaited_once_with("example.com", 80, "127.0.0.2")
        self.signal.emit.assert_called_once_with('[REQ] GET → example.com:80 | interface: "Clé 1"')

    async def test_request_without_interface_remains_visible(self):
        await self.request("CONNECT auth.example.com:443 HTTP/1.1", None)
        self.signal.emit.assert_called_once_with('[REQ] CONNECT → auth.example.com:443 | interface: null')

    def test_technical_logs_do_not_emit_qt_signals(self):
        self.code.logger.info("[QUOTA] some technical details")
        self.code.logger.warning("Connection error")
        self.signal.emit.assert_not_called()

    def window(self, view=None):
        window = types.SimpleNamespace(
            logs_panel_enabled=True, _console_lines={None: []}, _console_view=view,
            zrotate_log_box=Mock(), _scroll_console_to_bottom=Mock(),
            _reset_in_progress={"another interface"},
        )
        window._ensure_console_buffer = types.MethodType(self.code._ensure_console_buffer, window)
        return window

    def test_request_visible_in_general_and_exact_interface(self):
        window = self.window()
        self.code._zrotate_log(window, '[REQ] CONNECT → auth.example.com:443 | interface: "Clé 10"')
        expected = '[12:00:00] CONNECT → auth.example.com:443 | interface: "Clé 10"'
        self.assertEqual(window._console_lines[None], [expected])
        self.assertEqual(window._console_lines["Clé 10"], [expected])
        self.assertNotIn("Clé 1", window._console_lines)
        self.assertNotIn("another interface", window._console_lines)
        window.zrotate_log_box.append.assert_called_once_with(expected)

    def test_filtered_view_and_bounded_buffers(self):
        window = self.window(view="Clé 1")
        for _ in range(3):
            self.code._zrotate_log(window, '[REQ] CONNECT → 192.0.2.1:5555 | interface: "Clé 1"')
        self.code._zrotate_log(window, '[REQ] CONNECT → other.example:443 | interface: "Clé 2"')
        self.assertEqual(len(window._console_lines[None]), 2)
        self.assertEqual(len(window._console_lines["Clé 1"]), 2)
        self.assertEqual(window.zrotate_log_box.append.call_count, 3)

    def test_rejected_request_general_only_and_no_reset_noise(self):
        window = self.window()
        self.code._zrotate_log(window, "[RESET] debug details")
        self.code._zrotate_log(window, '[REQ] CONNECT → auth.example:443 | interface: null')
        self.assertEqual(list(window._console_lines), [None])
        self.assertIn("aucune (503)", window._console_lines[None][0])
        window.zrotate_log_box.append.assert_called_once()

    def test_disabled_logs_skip_request_formatting(self):
        self.code.logger.setLevel(logging.CRITICAL + 1)
        self.code.log_zrotate_request("CONNECT", "auth.example", 443, "Clé 1")
        self.signal.emit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
