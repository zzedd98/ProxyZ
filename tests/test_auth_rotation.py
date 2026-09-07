"""Rotation AUTH : seuil, drainage, relais et reprises simulés sans réseau."""
import types
import unittest
from unittest.mock import AsyncMock, Mock

from test_auth_quota import load_code


class AuthRotationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.code = load_code()
        self.state = self.code.AuthQuotaState()
        self.state.configure_request_limit(2)
        for name in ("A", "B"):
            self.state.set_dedicated_interface(name)
        self.qm = self.code.InterfaceQuotaManager(
            [{"name": n, "ip": f"127.0.0.{i}"} for i, n in enumerate(("A", "B", "G"), 2)],
            auth_state=self.state,
        )
        self.qm._reset_callback = Mock()
        self.qm.start_retry_reset_task = AsyncMock()
        self.qm.start_cleanup_task = AsyncMock()

    async def request(self, cid, host="auth.ankama.com"):
        return await self.qm.get_interface_for_request("CONNECT", host, 443, cid)

    async def fill_a(self):
        self.assertEqual((await self.request(1))["name"], "A")
        self.assertEqual((await self.request(2))["name"], "B")
        self.assertEqual((await self.request(3))["name"], "A")
        self.assertEqual(self.state.auth_rotation(), "A")

    async def test_threshold_drains_and_preserves_request_that_reached_limit(self):
        await self.fill_a()
        self.qm._reset_callback.reset_interface.assert_not_called()
        self.assertEqual((await self.request(4))["name"], "B")
        await self.qm.complete_request(1, True)
        self.qm._reset_callback.reset_interface.assert_not_called()
        await self.qm.complete_request(3, True)
        self.qm._reset_callback.reset_interface.assert_called_once_with("A")
        self.assertEqual(self.state.auth_counts()["A"], 2)

    async def test_only_one_rotation_even_when_all_keys_reach_limit(self):
        await self.fill_a()
        await self.request(4)
        for cid in (1, 2, 3, 4):
            await self.qm.complete_request(cid, True)
        self.qm._reset_callback.reset_interface.assert_called_once_with("A")
        self.assertFalse(self.state.begin_reset("B", automatic=True))
        self.assertFalse(self.state.begin_reset("B", automatic=False))
        self.assertTrue(self.state.begin_reset("A", automatic=True))
        self.assertEqual((await self.request(5))["name"], "B")

    async def test_next_key_waits_for_successful_reset_and_pool_return(self):
        await self.fill_a()
        await self.request(4)
        for cid in (1, 2, 3, 4):
            await self.qm.complete_request(cid, True)
        self.state.begin_reset("A", automatic=True)
        # Une notification hors du gestionnaire ne rétablit pas le relais dans le pool.
        self.state.finish_reset("A")
        self.qm._service_auth_rotations()
        self.qm._reset_callback.reset_interface.assert_called_once_with("A")
        await self.qm.release_interface_after_reset("A", True)
        self.qm._service_auth_rotations()
        self.assertEqual([call.args[0] for call in self.qm._reset_callback.reset_interface.call_args_list], ["A", "B"])
        self.assertNotIn("A", self.state.auth_counts())
        self.assertEqual((await self.request(10))["name"], "A")

    async def test_single_auth_key_keeps_serving_above_limit(self):
        await self.qm.set_domain_interface("B", False)
        for cid in range(8):
            self.assertEqual((await self.request(cid))["name"], "A")
            await self.qm.complete_request(cid, True)
        self.assertEqual(self.state.auth_counts()["A"], 8)
        self.assertIsNone(self.state.auth_rotation())
        self.qm._reset_callback.reset_interface.assert_not_called()
        self.assertFalse(self.state.begin_reset("A", automatic=False))

    async def test_failed_reset_preserves_last_key_and_retries_after_backoff(self):
        await self.fill_a()
        await self.qm.complete_request(1, True)
        await self.qm.complete_request(3, True)
        await self.qm.release_interface_after_reset("A", False)
        self.qm._service_auth_rotations()
        self.qm._reset_callback.reset_interface.assert_called_once_with("A")
        self.assertEqual(self.state.auth_counts()["A"], 2)
        self.code.time.monotonic.return_value = 130
        self.qm._service_auth_rotations()
        self.assertEqual([c.args[0] for c in self.qm._reset_callback.reset_interface.call_args_list], ["A", "A"])
        self.assertEqual((await self.request(5))["name"], "B")

    async def test_ten_reset_failures_quarantine_without_rotating_last_key(self):
        await self.fill_a()
        for cid in (1, 3):
            await self.qm.complete_request(cid, True)
        for attempt in range(10):
            await self.qm.release_interface_after_reset("A", False)
            self.code.time.monotonic.return_value += 30
            self.qm._service_auth_rotations()
        self.assertIn("A", self.qm._quarantine_interfaces)
        self.assertEqual(self.qm._reset_callback.reset_interface.call_count, 10)
        for cid in range(10, 15):
            self.assertEqual((await self.request(cid))["name"], "B")
            await self.qm.complete_request(cid, True)
        self.assertIsNone(self.state.auth_rotation())

    async def test_failed_connect_counts_once_and_releases_drain(self):
        self.state.configure_request_limit(1)
        await self.request(1)
        self.assertEqual(self.state.auth_counts(), {"A": 1})
        await self.qm.complete_request(1, False)
        await self.qm.complete_request(1, False)
        self.assertEqual(self.state.auth_counts(), {"A": 1})
        self.qm._reset_callback.reset_interface.assert_called_once_with("A")

    async def test_other_domains_normal_first_then_auth_fallback_counts(self):
        for cid, host in enumerate(("hunt.ankabot.dev", "haapi.ankama.com", "avatar.ankama.com")):
            self.assertEqual((await self.request(cid, host))["name"], "G")
        self.assertEqual(self.state.auth_counts(), {})
        self.qm.resetting_interfaces.add("G")
        self.qm.available_interfaces = [i for i in self.qm.available_interfaces if i["name"] != "G"]
        self.assertEqual((await self.request(10, "haapi.ankama.com"))["name"], "A")
        self.assertEqual(self.state.auth_counts(), {"A": 1})
        self.assertIsNone(await self.request(11, "52.30.61.61"))

    async def test_disappearing_spare_aborts_drain_before_reset(self):
        await self.fill_a()
        self.qm.available_interfaces = [i for i in self.qm.available_interfaces if i["name"] != "B"]
        self.qm._service_auth_rotations()
        self.assertIsNone(self.state.auth_rotation())
        self.assertEqual((await self.request(4))["name"], "A")
        self.qm._reset_callback.reset_interface.assert_not_called()

    async def test_cannot_disable_last_relay_during_rotation(self):
        await self.fill_a()
        self.assertFalse(await self.qm.set_domain_interface("B", False))
        self.assertFalse(await self.qm.set_domain_interface("A", False))
        self.assertEqual(self.state.dedicated_interfaces(), {"A", "B"})

    async def test_zero_disables_rotation(self):
        self.state.configure_request_limit(0)
        for cid in range(10):
            await self.request(cid)
            await self.qm.complete_request(cid, True)
        self.assertIsNone(self.state.auth_rotation())
        self.qm._reset_callback.reset_interface.assert_not_called()

    def test_json_threshold_validation_and_host_matching(self):
        for value, expected in ((25, 25), (0, 0), (-1, 100), (2.5, 100), (True, 100), ("bad", 100)):
            window = types.SimpleNamespace(_auth_quota_state=self.state, _merge_write_config_disk=Mock())
            self.code._load_domain_routing_config(window, {"auth_reset_request_limit": value})
            self.assertEqual(self.state.auth_reset_request_limit, expected)
        for host in ("auth.ankama.com", "AUTH.ANKAMA.COM.", "x.edge.sdk.awswaf.com"):
            self.assertTrue(self.code._requires_auth_interface(host))
        for host in ("haapi.ankama.com", "auth.ankama.com.evil.test", "notawswaf.com"):
            self.assertFalse(self.code._requires_auth_interface(host))


if __name__ == "__main__":
    unittest.main()
