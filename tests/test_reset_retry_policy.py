"""Scénarios de panne sans Qt, réseau, attente réelle ni modem."""
import ast
import asyncio
import json
import math
from pathlib import Path
import types
import threading
import unittest
from unittest.mock import AsyncMock, Mock


def load_policy():
    path = Path(__file__).resolve().parents[1] / "ProxyZ.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    methods = {"__init__", "cancel_interface_reset", "prepare_manual_reset", "defer_interface_reset",
               "_release_interface_after_reset", "_retry_reset_loop", "start_retry_reset_task",
               "_request_interface_reset", "_interface_available_event_clear_if_empty",
               "_interface_available_event_set"}
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in
                {"MAX_CONSECUTIVE_RESET_FAILURES", "RESET_RETRY_DELAY_SECONDS", "RESET_POOL_RETURN_DELAY_SECONDS", "AUTH_QUOTA_SECONDS"} for t in n.targets):
            nodes.append(n)
        if isinstance(n, ast.ClassDef) and n.name in {"ResetRetryPolicy", "AuthQuotaState"}:
            nodes.append(n)
        if isinstance(n, ast.ClassDef) and n.name == "InterfaceQuotaManager":
            n.body = [m for m in n.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name in methods]
            nodes.append(n)
        if isinstance(n, ast.ClassDef) and n.name == "MainWindow":
            for method in n.body:
                if isinstance(method, ast.FunctionDef) and method.name in {"_start_reset", "_save_reset_blocks"}:
                    nodes.append(method)
    module = types.ModuleType("retry")
    module.__dict__.update(asyncio=asyncio, json=json, math=math, threading=threading, logger=Mock(), _atomic_write_json=Mock(),
                           time=types.SimpleNamespace(monotonic=Mock(return_value=100.0)))
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(path), "exec"), module.__dict__)
    return module


class RetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.code = load_policy()
        self.policy = self.code.ResetRetryPolicy()
        self.qm = self.code.InterfaceQuotaManager([{"name": "A"}, {"name": "B"}])
        self.qm._reset_callback = Mock()
        self.qm.start_retry_reset_task = AsyncMock()

    async def tick(self):
        # Une seule itération du véritable ordonnanceur, sans attendre 30 s.
        fake_asyncio = types.SimpleNamespace(
            sleep=AsyncMock(side_effect=[None, asyncio.CancelledError()]),
            CancelledError=asyncio.CancelledError,
        )
        original = self.code.asyncio
        self.code.asyncio = fake_asyncio
        try:
            await self.qm._retry_reset_loop()
        finally:
            self.code.asyncio = original

    async def test_ten_failures_then_no_retry_even_days_later(self):
        for attempt in range(1, 11):
            now = attempt * 100.0
            self.code.time.monotonic.return_value = now
            self.assertTrue(self.policy.can_start("A", now))
            self.qm.resetting_interfaces.add("A")
            self.policy.finish("A", False, now)
            await self.qm._release_interface_after_reset("A", False)
            self.assertEqual(self.qm._consecutive_reset_failures["A"], attempt)
            self.qm._reset_callback.reset_interface.assert_not_called()
        self.code.time.monotonic.return_value = 1e9
        await self.tick()
        self.assertFalse(self.policy.can_start("A", 1e9))
        self.assertIn("A", self.qm._quarantine_interfaces)
        self.assertNotIn("A", self.qm._keys_removed_from_pool)
        self.qm._request_interface_reset("A", "quota")
        self.qm._reset_callback.reset_interface.assert_not_called()
        self.assertTrue(self.policy.can_start("B", 1e9))

    async def test_retry_waits_thirty_seconds_after_failure(self):
        self.policy.finish("A", False, 100)
        await self.qm._release_interface_after_reset("A", False)
        self.code.time.monotonic.return_value = 129.9
        await self.tick()
        self.assertFalse(self.policy.can_start("A", 129.9))
        self.qm._reset_callback.reset_interface.assert_not_called()
        self.code.time.monotonic.return_value = 130
        await self.tick()
        self.assertTrue(self.policy.can_start("A", 130))
        self.qm._reset_callback.reset_interface.assert_called_once_with("A")
        await self.tick()
        self.qm._reset_callback.reset_interface.assert_called_once()  # pas de doublon en cours

    async def test_cancel_pending_retry_is_permanent(self):
        self.policy.finish("A", False, 100)
        await self.qm._release_interface_after_reset("A", False)
        self.policy.cancel("A")
        await self.qm.cancel_interface_reset("A")
        self.code.time.monotonic.return_value = 1e9
        await self.tick()
        self.assertFalse(self.policy.can_start("A", 1e9))
        self.qm._reset_callback.reset_interface.assert_not_called()
        self.assertEqual([p["name"] for p in self.qm.available_interfaces], ["B"])

    async def test_cancel_quarantined_interface_does_not_recover(self):
        self.qm._quarantine_interfaces.add("A")
        await self.qm.cancel_interface_reset("A")
        self.code.time.monotonic.return_value = 1e9
        await self.tick()
        self.assertIn("A", self.qm._quarantine_interfaces)
        self.qm._reset_callback.reset_interface.assert_not_called()

    async def test_late_success_or_failure_cannot_undo_cancel(self):
        self.policy.cancel("A")
        await self.qm.cancel_interface_reset("A")
        for success in (True, False):
            self.policy.finish("A", success, 1000)
            await self.qm._release_interface_after_reset("A", success)
            self.assertFalse(self.policy.can_start("A", 1e9))
            self.assertNotIn("A", [p["name"] for p in self.qm.available_interfaces])
        self.qm.start_retry_reset_task.assert_not_awaited()

    async def test_manual_rearm_restores_full_budget_and_success_pool(self):
        for _ in range(10):
            self.policy.finish("A", False, 100)
        await self.qm.cancel_interface_reset("A")
        self.policy.rearm("A")
        await self.qm.prepare_manual_reset("A")
        self.assertTrue(self.policy.can_start("A", 100))
        self.assertNotIn("A", self.qm._quarantine_interfaces)
        self.policy.finish("A", False, 100)
        await self.qm._release_interface_after_reset("A", False)
        self.assertEqual(self.policy.failures["A"], 1)
        self.assertEqual(self.qm._consecutive_reset_failures["A"], 1)
        self.policy.finish("A", True, 200)
        await self.qm._release_interface_after_reset("A", True)
        self.assertNotIn("A", self.policy.failures)
        self.assertNotIn("A", self.qm._consecutive_reset_failures)
        self.assertIn("A", [p["name"] for p in self.qm.available_interfaces])

    async def test_premature_queued_request_is_deferred_without_extra_failure(self):
        await self.qm._release_interface_after_reset("A", False)
        self.qm.resetting_interfaces.add("A")
        await self.qm.defer_interface_reset("A", 135)
        self.assertNotIn("A", self.qm.resetting_interfaces)
        self.assertEqual(self.qm._consecutive_reset_failures["A"], 1)
        self.code.time.monotonic.return_value = 135
        await self.tick()
        self.qm._reset_callback.reset_interface.assert_called_once_with("A")

    async def test_restart_zrotate_preserves_blocked_interfaces(self):
        qm = self.code.InterfaceQuotaManager([{"name": "A", "reset_blocked": True}, {"name": "B"}])
        self.assertEqual(qm.available_interfaces, [{"name": "B"}])
        self.assertIn("A", qm._quarantine_interfaces)

    def test_automatic_http_or_queued_request_cannot_rearm(self):
        window = types.SimpleNamespace(
            _closing=False, _reset_cleaning=False, _reset_in_progress=set(),
            _reset_retry_policy=self.policy, _cancel_interface_in_zrotate=Mock(),
            _defer_reset_in_zrotate=Mock(),
        )
        self.policy.cancel("A")
        self.assertFalse(self.code._start_reset(window, "A", interactive=False))
        window._cancel_interface_in_zrotate.assert_called_once_with("A")
        self.policy.rearm("A")
        self.policy.finish("A", False, 100)
        self.assertFalse(self.code._start_reset(window, "A", interactive=False))
        window._defer_reset_in_zrotate.assert_called_once_with("A")
        self.assertEqual(self.policy.failures["A"], 1)

    def test_block_persisted_only_on_state_change_and_other_config_preserved(self):
        path = Mock()
        path.is_file.return_value = True
        path.read_text.return_value = '{"interface_proxies":{"A":{"port":4001}}}'
        window = types.SimpleNamespace(
            _reset_retry_policy=self.policy, config={}, _config_path=Mock(return_value=path),
            _zrotate_log=Mock(),
        )
        self.code._save_reset_blocks(window)
        self.code._atomic_write_json.assert_not_called()
        self.policy.cancel("A")
        self.code._save_reset_blocks(window)
        self.code._save_reset_blocks(window)
        self.code._atomic_write_json.assert_called_once()
        saved = self.code._atomic_write_json.call_args.args[1]
        self.assertEqual(saved["reset_manual_required"], ["A"])
        self.assertEqual(saved["interface_proxies"]["A"]["port"], 4001)
        self.policy.rearm("A")
        self.code._save_reset_blocks(window)
        self.assertEqual(self.code._atomic_write_json.call_args.args[1]["reset_manual_required"], [])

    def test_remote_driver_does_not_hide_a_second_reset_attempt(self):
        path = Path(__file__).resolve().parents[1] / "reset_XPro_dist.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        nodes = [n for n in tree.body if
                 (isinstance(n, ast.FunctionDef) and n.name == "reset_modem_by_port") or
                 (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "RESET_ATTEMPTS_ON_STALE_IP" for t in n.targets))]
        trigger = Mock(return_value=False)
        env = dict(_get_modem_ip=Mock(return_value="1.2.3.4"), _trigger_reset=trigger,
                   _log=Mock(), reset_server_for_port=Mock(return_value="server"))
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), env)
        self.assertFalse(env["reset_modem_by_port"](4001))
        trigger.assert_called_once_with(4001)



class PoolReturnDelayTests(unittest.TestCase):
    def run_worker_result(self, result, cancel_during_wait=False):
        path = Path(__file__).resolve().parents[1] / "ProxyZ.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        window_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MainWindow")
        start = next(n for n in window_class.body if isinstance(n, ast.FunctionDef) and n.name == "_start_reset")
        worker = next(n for n in start.body if isinstance(n, ast.FunctionDef) and n.name == "run_reset")
        run_try = next(n for n in worker.body if isinstance(n, ast.Try))
        delay = next(n.value.value for n in tree.body if isinstance(n, ast.Assign) and
                     any(isinstance(t, ast.Name) and t.id == "RESET_POOL_RETURN_DELAY_SECONDS" for t in n.targets))
        events = []
        window = types.SimpleNamespace(_reset_cancelled=set(), _reset_in_progress={"A"},
                                       _register_reset_proc=Mock(),
                                       reset_completed=types.SimpleNamespace(emit=lambda *args: events.append(args)))
        def sleep(seconds):
            self.assertEqual(seconds, 5.0)
            self.assertEqual(events, [])  # Aucune notification de remise au pool avant le repos.
            self.assertIn("A", window._reset_in_progress)
            if cancel_during_wait:
                window._reset_cancelled.add("A")
        fake_time = types.SimpleNamespace(time=Mock(side_effect=[100, 102]), sleep=Mock(side_effect=sleep))
        env = dict(self=window, name="A", proxy_port=4001, script_path=Path("reset.py"),
                   reset_options={}, ui_log=Mock(), run_reset_script=Mock(return_value=result),
                   time=fake_time, RESET_POOL_RETURN_DELAY_SECONDS=delay)
        exec(compile(ast.Module(body=run_try.body, type_ignores=[]), str(path), "exec"), env)
        return fake_time.sleep, events

    def test_success_not_released_until_five_seconds_after_driver(self):
        sleep, events = self.run_worker_result(0)
        sleep.assert_called_once_with(5.0)
        self.assertEqual(events, [("A", 0, 2)])  # Mesure du reset sans gonfler sa durée de repos.

    def test_failure_keeps_existing_retry_policy_without_success_pause(self):
        sleep, events = self.run_worker_result(1)
        sleep.assert_not_called()
        self.assertEqual(events, [("A", 1, 2)])

    def test_clean_during_rest_prevents_return_to_pool(self):
        sleep, events = self.run_worker_result(0, cancel_during_wait=True)
        sleep.assert_called_once_with(5.0)
        self.assertEqual(events, [("A", -4, 2)])


if __name__ == "__main__":
    unittest.main()
