"""Tests isolés du nettoyage : aucun accès aux modems ni import de l'UI."""
import ast
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

import psutil


def load_cleanup():
    path = Path(__file__).resolve().parents[1] / "ProxyZ.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = {"_terminate_process_tree", "_find_reset_helpers", "_run_subprocess_tree"}
    methods = {"_register_reset_proc", "_start_reset", "_on_reset_completed", "closeEvent"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in functions]
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "MainWindow":
            for method in node.body:
                if isinstance(method, ast.FunctionDef) and method.name in methods:
                    method.decorator_list = []
                    nodes.append(method)
    module = types.ModuleType("cleanup")
    module.__dict__.update(
        psutil=psutil, subprocess=subprocess, threading=threading, os=os, Path=Path,
        re=re, ThreadPoolExecutor=ThreadPoolExecutor,
        _RESET_SPAWN_LOCK=threading.Lock(), _RESET_SHUTTING_DOWN=threading.Event(),
    )
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), module.__dict__)
    return module


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.code = load_cleanup()
        self.logs = []

    def sleeper(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        def finish():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
        self.addCleanup(finish)
        return proc

    def test_real_process_killed_when_psutil_access_denied(self):
        proc = self.sleeper()
        with patch.object(psutil, "Process", side_effect=psutil.AccessDenied(proc.pid)):
            result = self.code._terminate_process_tree(proc, timeout=0.1, log_fn=self.logs.append)
        self.assertIsNotNone(proc.poll())
        self.assertFalse(result)  # Parent mort, mais descendants non vérifiables.
        self.assertTrue(any("inaccessible" in line for line in self.logs))

    def test_real_parent_and_child_terminated(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child.pid"
            child_code = "import time; time.sleep(60)"
            parent_code = (
                "import subprocess,sys,time,pathlib; "
                f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}],creationflags={getattr(subprocess, 'CREATE_NO_WINDOW', 0)}); "
                f"pathlib.Path({str(marker)!r}).write_text(str(p.pid)); time.sleep(60)"
            )
            parent = subprocess.Popen([sys.executable, "-c", parent_code],
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            child = None
            try:
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(marker.exists())
                child = psutil.Process(int(marker.read_text()))
                self.assertTrue(self.code._terminate_process_tree(parent, timeout=0.2, log_fn=self.logs.append))
                self.assertIsNotNone(parent.poll())
                self.assertFalse(child.is_running())
            finally:
                if parent.poll() is None:
                    parent.kill()
                parent.wait(timeout=5)
                if child is not None and child.is_running():
                    child.kill()
                    child.wait(timeout=5)

    def test_exited_popen_does_not_lookup_reused_pid(self):
        proc = self.sleeper()
        proc.kill()
        proc.wait(timeout=5)
        with patch.object(psutil, "Process") as lookup:
            self.assertTrue(self.code._terminate_process_tree(proc))
        lookup.assert_not_called()

    def test_survivor_reported_not_claimed_dead(self):
        proc = Mock(pid=99999)
        proc.children.return_value = []
        proc.terminate.side_effect = psutil.AccessDenied(proc.pid)
        proc.kill.side_effect = psutil.AccessDenied(proc.pid)
        with patch.object(psutil, "wait_procs", return_value=([], [proc])):
            self.assertFalse(self.code._terminate_process_tree(proc, log_fn=self.logs.append))
        self.assertTrue(any("Encore actifs" in line for line in self.logs))

    def test_discovered_parent_killed_even_if_children_inaccessible(self):
        proc = Mock(pid=99999)
        proc.children.side_effect = psutil.AccessDenied(proc.pid)
        with patch.object(psutil, "wait_procs", return_value=([proc], [])):
            self.assertFalse(self.code._terminate_process_tree(proc, log_fn=self.logs.append))
        proc.terminate.assert_called_once()

    def test_discovery_exact_script_and_ownership(self):
        root = Path(__file__).resolve().parents[1]
        allowed = root / "reset_XPro_dist.py"
        def process(pid, command, parent):
            p = Mock(pid=pid)
            p.cmdline.return_value = command
            p.parent.return_value = parent
            p.cwd.return_value = str(root)
            return p
        ours = process(101, ["pythonw.exe", str(allowed), "4001"], Mock(pid=os.getpid()))
        orphan = process(102, ["python.exe", "reset_XPro_dist.py", "4002"], None)
        other_app = process(103, ["pythonw.exe", str(root / "other.py")], None)
        other_copy = process(104, ["pythonw.exe", str(root / "elsewhere" / allowed.name)], None)
        live_parent = process(105, ["pythonw.exe", str(allowed)], Mock(pid=12345))
        impostor = process(106, ["other.exe", str(allowed)], None)
        with patch.object(psutil, "process_iter", return_value=[ours, orphan, other_app, other_copy, live_parent, impostor]):
            found = self.code._find_reset_helpers([allowed], self.logs.append)
        self.assertEqual([p.pid for p in found], [101, 102])

    def test_inaccessible_process_is_skipped_and_reported(self):
        proc = Mock(pid=123)
        proc.cmdline.side_effect = psutil.AccessDenied(proc.pid)
        with patch.object(psutil, "process_iter", return_value=[proc]):
            self.assertEqual(self.code._find_reset_helpers([], self.logs.append), [])
        self.assertTrue(any("non inspectable" in line for line in self.logs))

    def test_timeout_reaps_real_helper(self):
        captured = []
        with self.assertRaises(subprocess.TimeoutExpired):
            self.code._run_subprocess_tree(
                [sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.1,
                cwd=str(Path.cwd()), on_process_start=captured.append,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), log_fn=self.logs.append,
            )
        self.assertIsNotNone(captured[0].poll())

    def test_shutdown_prevents_spawn(self):
        self.code._RESET_SHUTTING_DOWN.set()
        with patch.object(subprocess, "Popen") as popen:
            with self.assertRaises(RuntimeError):
                self.code._run_subprocess_tree([], 120, str(Path.cwd()))
        popen.assert_not_called()

    def test_registration_failure_does_not_orphan_helper(self):
        captured = []
        def reject(proc):
            captured.append(proc)
            raise RuntimeError("registration failed")
        with self.assertRaisesRegex(RuntimeError, "registration failed"):
            self.code._run_subprocess_tree(
                [sys.executable, "-c", "import time; time.sleep(60)"], 120, str(Path.cwd()),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                on_process_start=reject, log_fn=self.logs.append,
            )
        self.assertIsNotNone(captured[0].poll())

    def test_late_registration_after_cancel_kills_helper(self):
        proc = self.sleeper()
        window = types.SimpleNamespace(
            _reset_procs_lock=threading.Lock(), _reset_procs={},
            _reset_cancelled={"modem"}, _closing=False,
            reset_log=types.SimpleNamespace(emit=self.logs.append),
        )
        self.code._register_reset_proc(window, "modem", proc)
        self.assertIsNotNone(proc.poll())

    def test_no_reset_during_clean_or_shutdown(self):
        for closing in (True, False):
            window = types.SimpleNamespace(_closing=closing, _reset_cleaning=not closing,
                                           _cancel_interface_in_zrotate=Mock())
            self.assertFalse(self.code._start_reset(window, "modem"))
            window._cancel_interface_in_zrotate.assert_called_once_with("modem")

    def test_shutdown_completion_cannot_restart_reset(self):
        window = types.SimpleNamespace(_closing=True)
        self.code._on_reset_completed(window, "modem", -2, 120)

    def test_duplicate_reset_does_not_cancel_running_interface(self):
        window = types.SimpleNamespace(_closing=False, _reset_cleaning=False,
                                       _reset_in_progress={"modem"},
                                       _cancel_interface_in_zrotate=Mock())
        self.assertFalse(self.code._start_reset(window, "modem"))
        window._cancel_interface_in_zrotate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
