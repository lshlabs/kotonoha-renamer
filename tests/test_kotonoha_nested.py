"""Real-process recovery and Windows locking for nested output."""

import ctypes
import os
import subprocess
import sys
import unittest

import kotonoha_nested as nested
import kotonoha_storage as storage
from tests import fixtures


class NestedTests(unittest.TestCase):
    setUp = fixtures.Tests.setUp
    tearDown = fixtures.Tests.tearDown

    def prepare(self):
        root = fixtures.tree(self.parent)
        return (
            root,
            [op for op in fixtures.plan(root) if op["type"] != "root"],
            nested.content_entries(root),
        )

    def test_apply_undo_and_old_cli_dispatch(self):
        root, plan, entries = self.prepare()
        final, log, data = nested.apply(root, plan, "작품", entries)
        self.assertEqual(final, root / "작품")
        self.assertTrue(root.exists())
        self.assertEqual((final / "바깥/안쪽/01-1　제목.flac").read_bytes(), b"unaltered payload")
        self.assertEqual(data["status"], "completed")
        restored, log = storage.undo(log, lambda _: True)
        self.assertEqual(restored, root)
        self.assertEqual(
            (root / "外側/内側/01-1　タイトル.flac").read_bytes(), b"unaltered payload"
        )
        self.assertFalse(final.exists())
        storage.undo(log, lambda _: True)

    def test_collision_and_stale_scan(self):
        root, plan, entries = self.prepare()
        (root / "작품").mkdir()
        with self.assertRaises(RuntimeError):
            nested.apply(root, plan, "작품", entries)
        (root / "작품").rmdir()
        (root / "new.txt").write_bytes(b"new")
        with self.assertRaises(RuntimeError):
            nested.apply(root, plan, "작품", entries)
        self.assertTrue((root / "外側").exists())
        self.assertFalse((root / storage.LOG_NAME).exists())

    def test_undo_preserves_new_user_files(self):
        root, plan, entries = self.prepare()
        final, log, _ = nested.apply(root, plan, "작품", entries)
        (final / "added.txt").write_bytes(b"keep")
        nested.undo(log, lambda _: True)
        self.assertEqual((final / "added.txt").read_bytes(), b"keep")
        self.assertTrue((root / "外側").exists())

    def test_cancel_resume_and_journal_fallback(self):
        root, plan, entries = self.prepare()
        calls = iter([False, False, True])
        with self.assertRaises(nested.Cancelled):
            nested.apply(root, plan, "작품", entries, cancel=lambda: next(calls))
        log = root / storage.LOG_NAME
        log.unlink()
        final, log, data = storage.resume(log, lambda _: True)
        self.assertEqual(data["status"], "completed")
        self.assertEqual(final, root / "작품")
        nested.undo(log, lambda _: True)

    def test_forced_exit_boundaries(self):
        for direction in ("apply", "undo"):
            for stage in ("before_rename", "after_rename", "after_commit"):
                for index in range(1, 6):
                    with self.subTest(direction=direction, stage=stage, index=index):
                        root, plan, entries = self.prepare()
                        if direction == "undo":
                            nested.apply(root, plan, "작품", entries)
                        code = (
                            "from pathlib import Path; import sys; import kotonoha_nested as n; "
                            "from tests.fixtures import plan; r=Path(sys.argv[1]); "
                            + (
                                "n.apply(r,[o for o in plan(r) if o['type']!='root'],'작품',n.content_entries(r))"
                                if direction == "apply"
                                else "n.undo(r/'rename-log.json',lambda _: True)"
                            )
                        )
                        env = {
                            **os.environ,
                            "KOTONOHA_DATA_DIR": str(self.data),
                            "PYTHONUTF8": "1",
                            "KOTONOHA_TEST_CRASH": f"{direction}:{stage}:{index}",
                        }
                        result = subprocess.run(
                            [sys.executable, "-c", code, str(root)],
                            env=env,
                            capture_output=True,
                            encoding="utf-8",
                        )
                        self.assertEqual(result.returncode, 99, result.stderr)
                        nested.undo(root / storage.LOG_NAME, lambda _: True)
                        self.assertEqual(
                            (root / "外側/内側/01-1　タイトル.flac").read_bytes(),
                            b"unaltered payload",
                        )
                        self.assertFalse((root / "작품").exists())
                        self.stack.close()
                        self.temp.cleanup()
                        self.setUp()

    @unittest.skipUnless(os.name == "nt", "Windows directory sharing")
    def test_locked_original_directory_never_renamed(self):
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        api.CreateFileW.restype = ctypes.c_void_p
        api.CloseHandle.argtypes = [ctypes.c_void_p]
        root, plan, entries = self.prepare()
        handle = api.CreateFileW(str(root), 0x80000000, 3, None, 3, 0x02000000, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        root_identity = storage.identity(root)
        try:
            final, log, _ = nested.apply(root, plan, "작품", entries)
            self.assertEqual(storage.identity(root), root_identity)
            self.assertTrue((final / "바깥").exists())
            nested.undo(log, lambda _: True)
            self.assertEqual(storage.identity(root), root_identity)
        finally:
            api.CloseHandle(handle)
