"""Tests hors réseau : python -m unittest discover -s tests -v."""

import asyncio
import ast
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import ssl
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

import certifi
import httpx

def load_tls_section(filename):
    """Exécute les véritables helpers sans lancer Qt, Playwright ou les modems."""
    path = Path(__file__).resolve().parents[1] / filename
    tree = ast.parse(path.read_text(encoding="utf-8"))
    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in (
            "get_httpx_tls_context", "get_system_tls_context"
        ):
            selected.append(node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "_contexts":
                selected.append(node)
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "_context_lock" for t in node.targets):
                selected.append(node)
    module = types.ModuleType(filename)
    module.__dict__.update(os=os, Path=Path, ssl=ssl, threading=threading, certifi=certifi)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), module.__dict__)
    return module


class TLSContextTests(unittest.TestCase):
    source_name = "ProxyZ.py"

    def setUp(self):
        self.tls = load_tls_section(self.source_name)
        excluded = {
            "SSL_CERT_FILE", "SSL_CERT_DIR", "SSLKEYLOGFILE",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        }
        clean_env = {k: v for k, v in os.environ.items() if k.upper() not in excluded}
        self.env = patch.dict(os.environ, clean_env, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.tls._contexts.clear()
        self.addCleanup(self.tls._contexts.clear)

    def test_same_certificates_and_tls_policy_as_httpx_default(self):
        original = ssl.create_default_context(cafile=certifi.where())
        cached = self.tls.get_httpx_tls_context()
        self.assertEqual(
            set(original.get_ca_certs(binary_form=True)),
            set(cached.get_ca_certs(binary_form=True)),
        )
        self.assertGreater(len(cached.get_ca_certs()), 0)
        self.assertEqual(cached.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(cached.check_hostname)
        for attr in ("verify_flags", "minimum_version", "maximum_version", "options"):
            self.assertEqual(getattr(original, attr), getattr(cached, attr))
        self.assertEqual(original.get_ciphers(), cached.get_ciphers())

    def test_parallel_first_call_loads_pem_only_once(self):
        barrier = threading.Barrier(16)
        def get_context():
            barrier.wait(timeout=10)
            return self.tls.get_httpx_tls_context()
        with patch.object(
            self.tls.ssl, "create_default_context", wraps=ssl.create_default_context
        ) as create:
            with ThreadPoolExecutor(max_workers=16) as pool:
                contexts = list(pool.map(lambda _: get_context(), range(16)))
        self.assertTrue(all(c is contexts[0] for c in contexts))
        self.assertEqual(create.call_count, 1)
        self.assertIn("cadata", create.call_args.kwargs)
        self.assertNotIn("cafile", create.call_args.kwargs)

    def test_file_override_has_precedence_and_uses_only_selected_ca(self):
        default = self.tls.get_httpx_tls_context()
        one_ca = default.get_ca_certs(binary_form=True)[0]
        with tempfile.TemporaryDirectory() as folder:
            bundle = Path(folder) / "custom.pem"
            bundle.write_text(ssl.DER_cert_to_PEM_cert(one_ca), encoding="ascii")
            os.environ["SSL_CERT_FILE"] = str(bundle)
            os.environ["SSL_CERT_DIR"] = str(Path(folder) / "absent")
            custom = self.tls.get_httpx_tls_context()
            self.assertIsNot(custom, default)
            self.assertEqual(custom.get_ca_certs(binary_form=True), [one_ca])
            self.assertEqual(custom.verify_mode, ssl.CERT_REQUIRED)
            self.assertIs(custom, self.tls.get_httpx_tls_context())

    def test_directory_override_preserves_openssl_capath_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            os.environ["SSL_CERT_DIR"] = folder
            with patch.object(
                self.tls.ssl, "create_default_context", wraps=ssl.create_default_context
            ) as create:
                custom = self.tls.get_httpx_tls_context()
                self.assertIs(custom, self.tls.get_httpx_tls_context())
            create.assert_called_once_with(capath=os.path.abspath(folder))
            self.assertEqual(custom.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(custom.check_hostname)
            self.assertEqual(custom.get_ca_certs(), [])

    def test_invalid_override_fails_closed_and_is_not_cached(self):
        with tempfile.TemporaryDirectory() as folder:
            bundle = Path(folder) / "invalid.pem"
            bundle.write_text("not a certificate", encoding="ascii")
            os.environ["SSL_CERT_FILE"] = str(bundle)
            with self.assertRaises(ssl.SSLError):
                self.tls.get_httpx_tls_context()
            self.assertEqual(self.tls._contexts, {})
            bundle.write_bytes(Path(certifi.where()).read_bytes())
            self.assertGreater(len(self.tls.get_httpx_tls_context().get_ca_certs()), 0)

    def test_cached_context_never_rereads_bundle(self):
        context = self.tls.get_httpx_tls_context()
        with patch.object(Path, "read_text", side_effect=AssertionError("unexpected read")):
            for _ in range(100):
                self.assertIs(context, self.tls.get_httpx_tls_context())

    def test_system_trust_is_separate_and_cached(self):
        if not hasattr(self.tls, "get_system_tls_context"):
            self.skipTest("Le driver utilise uniquement la politique HTTPX.")
        http = self.tls.get_httpx_tls_context()
        with patch.object(
            self.tls.ssl, "create_default_context", wraps=ssl.create_default_context
        ) as create:
            system = self.tls.get_system_tls_context()
            self.assertIs(system, self.tls.get_system_tls_context())
        create.assert_called_once_with()
        self.assertIsNot(system, http)
        self.assertTrue(system.check_hostname)
        self.assertEqual(system.verify_mode, ssl.CERT_REQUIRED)

    def test_fresh_httpx_clients_reuse_tls_without_reloading_certificates(self):
        context = self.tls.get_httpx_tls_context()
        async def async_clients():
            async with httpx.AsyncClient(
                proxy="http://127.0.0.1:101", verify=context
            ) as first:
                async with httpx.AsyncClient(
                    proxy="http://127.0.0.1:102", verify=context
                ) as second:
                    self.assertIsNot(first, second)
        with patch.object(
            self.tls.ssl, "create_default_context", side_effect=AssertionError("TLS reloaded")
        ):
            for proxy in (None, "http://127.0.0.1:101", "http://127.0.0.1:102"):
                with httpx.Client(proxy=proxy, verify=context) as client:
                    self.assertFalse(client.is_closed)
            asyncio.run(async_clients())


class XProTLSContextTests(TLSContextTests):
    source_name = "reset_XPro.py"


class HuaweiTLSContextTests(TLSContextTests):
    source_name = "reset_huawei.py"


class RemoteTLSContextTests(TLSContextTests):
    source_name = "reset_XPro_dist.py"


if __name__ == "__main__":
    unittest.main()
