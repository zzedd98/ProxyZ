"""Protection AUTH : horloge simulée, aucun modem, réseau ou processus lancé."""
import ast
import asyncio
from datetime import datetime
import math
from pathlib import Path
import threading
import types
import unittest
from unittest.mock import AsyncMock, Mock


def load_code():
    path = Path(__file__).resolve().parents[1] / "ProxyZ.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    constants = {"AUTH_QUOTA_SECONDS", "RESET_POOL_RETURN_DELAY_SECONDS", "RESET_RETRY_DELAY_SECONDS",
                 "MAX_CONSECUTIVE_RESET_FAILURES", "GAME_SERVER_QUOTA_KEY", "GET_QUOTA_KEY"}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in node.targets):
            nodes.append(node)
        if isinstance(node, ast.ClassDef) and node.name in {"AuthQuotaState", "InterfaceQuotaManager", "QuotaInfo", "ResetRetryPolicy"}:
            nodes.append(node)
        if isinstance(node, ast.FunctionDef) and node.name == "_host_is_ip_only":
            nodes.append(node)
        if isinstance(node, ast.ClassDef) and node.name == "MainWindow":
            nodes.extend(m for m in node.body if isinstance(m, ast.FunctionDef)
                         and m.name in {"_start_reset", "_on_auth_quota_toggled"})
    code = types.ModuleType("auth_test")
    code.__dict__.update(asyncio=asyncio, threading=threading, datetime=datetime, math=math,
                         logger=Mock(), time=types.SimpleNamespace(monotonic=Mock(return_value=100.0)),
                         resolve_interface_reset_script=Mock(return_value=("reset.py", None)),
                         get_app_dir=Mock(), resolve_reset_script_path=Mock(),
                         DEFAULT_RESET_SCRIPT="reset.py", QMessageBox=Mock())
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(path), "exec"), code.__dict__)
    return code


class AuthQuotaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.code = load_code()
        self.qm = self.code.InterfaceQuotaManager(
            [{"name": "A", "ip": "127.0.0.2"}, {"name": "B", "ip": "127.0.0.3"}],
            auth_quota_enabled=True,
        )
        self.qm._reset_callback = Mock()
        self.qm.start_auth_task = Mock()
        self.qm.start_retry_reset_task = AsyncMock()

    async def tick(self):
        real_asyncio = self.code.asyncio
        self.code.asyncio = types.SimpleNamespace(sleep=AsyncMock(side_effect=asyncio.CancelledError()))
        try:
            with self.assertRaises(asyncio.CancelledError):
                await self.qm._auth_reset_loop()
        finally:
            self.code.asyncio = real_asyncio

    async def reserve(self, host="AUTH.example.com", method="CONNECT", cid=1):
        return await self.qm.get_interface_for_request(method, host, 443, cid)

    async def test_reservation_is_immediate_and_other_traffic_uses_other_key(self):
        result = await self.reserve()
        self.assertEqual(result["name"], "A")
        self.assertTrue(result["auth_protected"])
        self.assertEqual(self.qm.auth_state.remaining("A"), 120)
        self.assertEqual([x["name"] for x in self.qm.available_interfaces], ["B"])
        for i, host in enumerate(["haapi.example.com", "192.0.2.1"], 2):
            self.assertEqual((await self.reserve(host, cid=i))["name"], "B")

    async def test_deadline_single_reset_and_no_pool_return_until_success(self):
        await self.reserve()
        self.code.time.monotonic.return_value = 219.999
        await self.tick()
        self.qm._reset_callback.reset_interface.assert_not_called()
        self.code.time.monotonic.return_value = 220
        await self.tick()
        await self.tick()
        self.qm._reset_callback.reset_interface.assert_called_once_with("A")
        self.assertNotIn("A", [x["name"] for x in self.qm.available_interfaces])
        await self.qm.release_interface_after_reset("A", True)
        self.assertIn("A", [x["name"] for x in self.qm.available_interfaces])
        self.assertFalse(self.qm.auth_state.unavailable("A"))

    async def test_game_quota_completion_cannot_reset_reserved_key(self):
        # A has two game requests in progress when AUTH is assigned to it.
        await self.reserve("192.0.2.1", cid=10)
        await self.reserve("192.0.2.1", cid=11)
        self.qm.available_interfaces = [self.qm.egress_configs[0]]
        await self.reserve()
        await self.qm.complete_request(10, True)
        await self.qm.complete_request(11, True)
        self.qm._request_interface_reset("A", "timeout quota partiel")
        self.qm._reset_callback.reset_interface.assert_not_called()
        self.assertNotIn("A", self.qm.resetting_interfaces)

    async def test_failure_and_early_release_do_not_remove_protection(self):
        await self.reserve()
        await self.qm.complete_request(1, False)
        await self.qm.release_interface_after_reset("A", True)
        await self.qm.prepare_manual_reset("A")
        self.assertEqual(self.qm.auth_state.remaining("A"), 120)
        self.assertNotIn("A", [x["name"] for x in self.qm.available_interfaces])

    async def test_disabled_option_and_non_auth_destinations(self):
        self.qm.auth_quota_enabled = False
        self.assertNotIn("auth_protected", await self.reserve())
        self.assertEqual(len(self.qm.available_interfaces), 2)
        self.qm.auth_quota_enabled = True
        await self.reserve("haapi.ankama.com", cid=2)
        self.assertEqual(self.qm.auth_state.snapshot(), {})
        result = await self.reserve("oauth.example.com", method="GET", cid=3)
        self.assertTrue(result["auth_protected"])

    async def test_disable_preserves_existing_deadline(self):
        await self.reserve()
        self.qm.auth_quota_enabled = False
        result = await self.reserve(cid=2)
        self.assertEqual(result["name"], "B")
        self.assertNotIn("auth_protected", result)
        self.code.time.monotonic.return_value = 220
        await self.tick()
        self.qm._reset_callback.reset_interface.assert_called_once_with("A")

    async def test_concurrent_auths_cannot_share_reserved_key(self):
        results = await asyncio.gather(*(self.reserve(cid=i) for i in range(3)))
        self.assertEqual([r["name"] if r else None for r in results], ["A", "B", None])
        self.assertFalse(self.qm._interface_available_event.is_set())

    async def test_reset_failure_uses_existing_backoff_and_quarantine(self):
        await self.reserve()
        self.code.time.monotonic.return_value = 220
        await self.tick()
        await self.qm.release_interface_after_reset("A", False)
        self.assertNotIn("A", [x["name"] for x in self.qm.available_interfaces])
        self.assertEqual(self.qm._reset_next_retry["A"], 250)
        self.assertEqual(self.qm._consecutive_reset_failures["A"], 1)
        self.assertEqual(self.qm.auth_state.snapshot(), {})

    async def test_restart_zrotate_reuses_protection(self):
        await self.reserve()
        self.qm = self.code.InterfaceQuotaManager(self.qm.egress_configs, auth_state=self.qm.auth_state)
        self.qm._reset_callback = Mock()
        self.assertEqual([x["name"] for x in self.qm.available_interfaces], ["B"])
        self.code.time.monotonic.return_value = 220
        await self.tick()
        self.qm._reset_callback.reset_interface.assert_called_once_with("A")

    async def test_reset_claim_and_auth_claim_are_mutually_exclusive(self):
        self.assertTrue(self.qm.auth_state.begin_reset("A"))
        self.assertFalse(self.qm.auth_state.reserve("A"))
        self.assertEqual((await self.reserve())["name"], "B")
        self.assertFalse(self.qm.auth_state.begin_reset("B"))

    async def test_manual_and_http_reset_cannot_start_during_auth(self):
        await self.reserve()
        window = types.SimpleNamespace(
            _closing=False, _reset_cleaning=False, _reset_in_progress=set(),
            _reset_retry_policy=self.code.ResetRetryPolicy(), _reset_procs_lock=threading.Lock(),
            _reset_procs={}, _remote_interfaces={}, _get_all_interfaces=lambda: {"A": {"ip": "127.0.0.2"}},
            interface_widgets={}, config={"interface_proxies": {"A": {"port": 4001}}},
            _auth_quota_state=self.qm.auth_state,
        )
        for interactive in (True, False):
            self.assertFalse(self.code._start_reset(window, "A", interactive=interactive))
        self.assertEqual(window._reset_in_progress, set())
        self.assertEqual(self.qm.auth_state.remaining("A"), 120)

    async def test_three_connection_failures_cannot_reset_during_auth(self):
        await self.reserve("192.0.2.1", cid=10)
        await self.reserve("192.0.2.1", cid=11)
        self.qm.available_interfaces = [self.qm.egress_configs[0]]
        await self.reserve()
        self.qm._interface_failure_count["A"] = 2
        await self.qm.complete_request(10, False)
        await self.qm.complete_request(11, False)
        self.qm._reset_callback.reset_interface.assert_not_called()
        self.assertNotIn("A", self.qm.resetting_interfaces)
        self.code.time.monotonic.return_value = 220
        await self.tick()
        self.qm._reset_callback.reset_interface.assert_called_once_with("A")

    async def test_option_persisted_and_applied_without_restarting_server(self):
        await self.reserve()
        disk = {"zrotate": {"max_requests_per_quota": 3}, "other": 42}
        callbacks = []
        server = types.SimpleNamespace(
            auth_quota_enabled=True, proxy_server=types.SimpleNamespace(quota_manager=self.qm),
            loop=types.SimpleNamespace(is_running=lambda: True, call_soon_threadsafe=callbacks.append),
        )
        window = types.SimpleNamespace(
            auth_quota_checkbox=types.SimpleNamespace(isChecked=lambda: False), config={},
            zrotate_proxy_server=server, _merge_write_config_disk=lambda fn: fn(disk),
        )
        self.code._on_auth_quota_toggled(window, 0)
        self.assertFalse(disk["zrotate"]["auth_quota_enabled"])
        self.assertEqual(disk["zrotate"]["max_requests_per_quota"], 3)
        self.assertEqual(disk["other"], 42)
        callbacks[0]()
        self.assertFalse(self.qm.auth_quota_enabled)
        self.assertEqual(self.qm.auth_state.remaining("A"), 120)


if __name__ == "__main__":
    unittest.main()
