"""Routage domaines/jeu et changement de rôle, sans Qt ni réseau."""
import asyncio
import types
import unittest
from unittest.mock import AsyncMock, Mock

from test_auth_quota import load_code


class DomainRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.code = load_code()
        self.state = self.code.AuthQuotaState()
        self.state.set_dedicated_interface("A")
        self.qm = self.code.InterfaceQuotaManager(
            [{"name": "A", "ip": "127.0.0.2"}, {"name": "B", "ip": "127.0.0.3"}],
            auth_state=self.state,
        )
        self.qm._reset_callback = Mock()
        self.qm.start_cleanup_task = AsyncMock()
        self.qm.start_retry_reset_task = AsyncMock()

    async def request(self, host, cid=1, method="CONNECT"):
        return await self.qm.get_interface_for_request(method, host, 443, cid)

    async def test_user_auth_sequence_always_uses_dedicated_key_then_game_pool(self):
        hosts = ["hunt.ankabot.dev", "auth.ankama.com", "auth.ankama.com",
                 "3f38f7f4f368.edge.sdk.awswaf.com", "haapi.ankama.com", "avatar.ankama.com"]
        for i, host in enumerate(hosts):
            result = await self.request(host, i)
            self.assertEqual(result["name"], "A")
            self.assertTrue(result["auth_protected"])
        self.assertEqual((await self.request("52.30.61.61", 20))["name"], "B")
        self.assertEqual(self.state.snapshot(), {})  # Aucun lock AUTH de 120 s.
        self.assertNotIn("A", self.qm.quotas)
        self.qm._reset_callback.reset_interface.assert_not_called()

    async def test_domains_work_when_game_pool_is_exhausted(self):
        self.qm.available_interfaces = [self.qm.egress_configs[0]]
        self.qm.resetting_interfaces.add("B")
        self.assertIsNone(await self.request("52.30.61.61"))
        results = await asyncio.gather(*(self.request("auth.ankama.com", i) for i in range(40)))
        self.assertTrue(all(r["name"] == "A" for r in results))
        self.assertEqual(len(self.qm.available_interfaces), 1)

    async def test_no_game_fallback_if_dedicated_key_unavailable(self):
        self.qm.available_interfaces = [self.qm.egress_configs[1]]
        self.assertIsNone(await self.request("haapi.ankama.com"))
        self.assertEqual((await self.request("108.128.247.72", 2))["name"], "B")

    async def test_http_domains_route_to_dedicated_but_ipv6_literals_do_not(self):
        self.assertEqual((await self.request("example.com", method="GET"))["name"], "A")
        self.assertEqual((await self.request("2001:db8::1", 2))["name"], "B")

    async def test_automatic_resets_blocked_manual_reset_allowed(self):
        self.qm._request_interface_reset("A", "quota plein")
        await self.qm._reset_interface_direct("A")
        self.qm._reset_callback.reset_interface.assert_not_called()
        self.assertFalse(self.state.begin_reset("A", automatic=True))
        self.assertTrue(self.state.begin_reset("A", automatic=False))
        self.assertIsNone(await self.request("auth.ankama.com"))
        self.state.finish_reset("A")
        self.assertEqual((await self.request("auth.ankama.com", 2))["name"], "A")

    async def test_switch_role_drains_old_tunnels_before_game_or_automatic_reset(self):
        await self.request("auth.ankama.com", 1)
        self.assertTrue(await self.qm.set_domain_interface("B"))
        self.assertTrue(await self.qm.set_domain_interface("A", False))
        self.assertEqual((await self.request("haapi.ankama.com", 2))["name"], "B")
        self.assertIsNone(await self.request("52.30.61.61", 3))
        self.assertFalse(self.state.begin_reset("A", automatic=True))
        await self.qm.complete_request(1, True)
        self.assertEqual((await self.request("52.30.61.61", 4))["name"], "A")
        self.assertFalse(self.state.domain_reserved("A"))

    async def test_disable_role_restores_routing_after_existing_tunnel_closes(self):
        await self.request("auth.ankama.com", 1)
        self.assertTrue(await self.qm.set_domain_interface("A", False))
        self.assertEqual((await self.request("haapi.ankama.com", 2))["name"], "B")
        await self.qm.complete_request(1, False)
        self.assertFalse(self.state.domain_reserved("A"))
        self.assertEqual((await self.request("52.30.61.61", 3))["name"], "A")

    async def test_cannot_designate_key_with_queued_reset_or_quarantine(self):
        self.qm.resetting_interfaces.add("B")
        self.assertFalse(await self.qm.set_domain_interface("B"))
        self.qm.resetting_interfaces.clear()
        self.qm._quarantine_interfaces.add("B")
        self.assertFalse(await self.qm.set_domain_interface("B"))
        self.assertEqual(self.state.dedicated_interfaces(), {"A"})

    async def test_health_checks_skip_dedicated_key(self):
        self.code.async_check_egress_public_internet = AsyncMock(return_value=True)
        self.code.local_ipv4_assigned_on_host = Mock()
        self.code.asyncio = types.SimpleNamespace(
            sleep=AsyncMock(side_effect=[None, asyncio.CancelledError()]),
            CancelledError=asyncio.CancelledError, to_thread=AsyncMock(return_value=True),
        )
        await self.qm._pool_health_loop()
        self.code.async_check_egress_public_internet.assert_awaited_once_with("127.0.0.3", timeout=10.0)

    async def test_late_failed_health_check_cannot_remove_new_dedicated_key(self):
        async def check(*args, **kwargs):
            self.assertTrue(await self.qm.set_domain_interface("B"))
            return False
        self.code.async_check_egress_public_internet = AsyncMock(side_effect=check)
        self.code.local_ipv4_assigned_on_host = Mock()
        self.code.asyncio = types.SimpleNamespace(
            sleep=AsyncMock(side_effect=[None, asyncio.CancelledError()]),
            CancelledError=asyncio.CancelledError, to_thread=AsyncMock(return_value=True),
        )
        await self.qm._pool_health_loop()
        self.assertIn("B", [i["name"] for i in self.qm.available_interfaces])
        self.qm._reset_callback.reset_interface.assert_not_called()

    def test_config_load_disables_legacy_auth_and_restores_dedicated_key(self):
        disk = {"zrotate": {"auth_quota_enabled": True, "domain_interface": "B", "other": 42}}
        window = types.SimpleNamespace(_auth_quota_state=self.state,
                                       _merge_write_config_disk=lambda fn: fn(disk))
        self.code._load_domain_routing_config(window, dict(disk["zrotate"]))
        self.assertFalse(disk["zrotate"]["auth_quota_enabled"])
        self.assertEqual(disk["zrotate"]["other"], 42)
        self.assertEqual(self.state.dedicated_interfaces(), {"B"})

    def test_role_result_updates_badges_and_persists_without_restarting_server(self):
        disk = {"zrotate": {"other": 42}}
        rows = {"A": Mock(), "B": Mock()}
        window = types.SimpleNamespace(_auth_quota_state=self.state, config={},
                                       _zrotate_interface_rows=rows,
                                       _merge_write_config_disk=lambda fn: fn(disk))
        self.code._finish_domain_role_change(window, True)
        rows["A"].set_domain_role.assert_called_once_with(True)
        rows["B"].set_domain_role.assert_called_once_with(False)
        self.assertEqual(disk["zrotate"], {"other": 42, "domain_interfaces": ["A"], "auth_quota_enabled": False})

    async def test_server_cleanup_releases_only_its_domain_tunnels(self):
        await self.request("auth.ankama.com", 1)
        self.state.acquire_domain("A", (999, 1))
        self.state.set_dedicated_interface("A", False)
        self.state.release_domain_owner(id(self.qm))
        self.assertTrue(self.state.domain_reserved("A"))
        self.state.release_domain_owner(999)
        self.assertFalse(self.state.domain_reserved("A"))

    async def test_old_game_quota_resets_only_after_role_and_tunnels_released(self):
        self.state.set_dedicated_interface("A", False)
        await self.request("52.30.61.61", 10)
        await self.request("52.30.61.61", 11)
        self.assertTrue(await self.qm.set_domain_interface("A"))
        await self.request("auth.ankama.com", 1)
        await self.qm.complete_request(10, True)
        await self.qm.complete_request(11, True)
        self.qm._reset_callback.reset_interface.assert_not_called()
        await self.qm.set_domain_interface("A", False)
        self.qm._reset_callback.reset_interface.assert_not_called()
        await self.qm.complete_request(1, True)
        self.qm._reset_callback.reset_interface.assert_called_once_with("A")

    async def test_multiple_dedicated_keys_balance_domains_and_exclude_game(self):
        self.assertTrue(await self.qm.set_domain_interface("B"))
        self.assertEqual(self.state.dedicated_interfaces(), {"A", "B"})
        results = await asyncio.gather(*(self.request("auth.ankama.com", i) for i in range(40)))
        self.assertEqual(sum(r["name"] == "A" for r in results), 20)
        self.assertEqual(sum(r["name"] == "B" for r in results), 20)
        self.assertIsNone(await self.request("52.30.61.61", 100))
        for name in ("A", "B"):
            self.assertFalse(self.state.begin_reset(name, automatic=True))

    async def test_removing_one_role_keeps_other_and_drains_old_connections(self):
        await self.request("auth.ankama.com", 1)
        await self.qm.set_domain_interface("B")
        await self.qm.set_domain_interface("A", False)
        self.assertEqual(self.state.dedicated_interfaces(), {"B"})
        self.assertTrue(self.state.domain_reserved("A"))
        self.assertEqual((await self.request("haapi.ankama.com", 2))["name"], "B")
        await self.qm.complete_request(1, True)
        self.assertFalse(self.state.domain_reserved("A"))
        self.assertEqual((await self.request("52.30.61.61", 3))["name"], "A")

    async def test_manual_reset_of_one_key_routes_domains_to_other(self):
        await self.qm.set_domain_interface("B")
        self.assertTrue(self.state.begin_reset("A"))
        self.assertEqual((await self.request("auth.ankama.com"))["name"], "B")
        self.assertTrue(self.state.begin_reset("B"))
        self.assertIsNone(await self.request("auth.ankama.com", 2))

    def test_buttons_add_and_remove_only_clicked_role_and_persist_both(self):
        disk = {"zrotate": {"other": 42}}
        rows = {"A": Mock(), "B": Mock()}
        window = types.SimpleNamespace(
            _auth_quota_state=self.state, config={}, _zrotate_interface_rows=rows,
            _merge_write_config_disk=lambda fn: fn(disk), zrotate_selected_interfaces={"A", "B"},
            zrotate_running=False, _reset_retry_policy=types.SimpleNamespace(blocked=set()),
        )
        window._finish_domain_role_change = types.MethodType(self.code._finish_domain_role_change, window)
        self.code._on_domain_role_toggled(window, "B", True)
        self.assertEqual(disk["zrotate"]["domain_interfaces"], ["A", "B"])
        rows["A"].set_domain_role.assert_called_with(True)
        rows["B"].set_domain_role.assert_called_with(True)
        self.code._on_domain_role_toggled(window, "A", False)
        self.assertEqual(disk["zrotate"]["domain_interfaces"], ["B"])
        self.assertEqual(self.state.dedicated_interfaces(), {"B"})

    def test_multi_config_restored_and_explicit_empty_overrides_legacy(self):
        window = types.SimpleNamespace(_auth_quota_state=self.state, _merge_write_config_disk=Mock())
        self.code._load_domain_routing_config(window, {"domain_interfaces": ["B", "A", "B", None]})
        self.assertEqual(self.state.dedicated_interfaces(), {"A", "B"})
        self.code._load_domain_routing_config(window, {"domain_interfaces": [], "domain_interface": "A"})
        self.assertEqual(self.state.dedicated_interfaces(), set())


if __name__ == "__main__":
    unittest.main()
