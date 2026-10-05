"""Isolated filesystem and engine fixtures for GUI tests."""

import contextlib
import io
import tempfile
from pathlib import Path
from unittest.mock import patch

import kotonoha_engine as engine
import kotonoha_storage as storage


def tree(parent):
    root = parent / "作品"
    (root / "外側/内側").mkdir(parents=True)
    (root / "外側/内側/01-1　タイトル.flac").write_bytes(b"unaltered payload")
    return root


def plan(root):
    pairs = [
        ("file", "外側/内側/01-1　タイトル.flac", "外側/内側/01-1　제목.flac"),
        ("folder", "外側/内側", "外側/안쪽"),
        ("folder", "外側", "바깥"),
    ]
    result = [
        {
            "type": kind,
            "source": root / old,
            "target": root / new,
            "identity": storage.identity(root / old),
        }
        for kind, old, new in pairs
    ]
    result.append(
        {
            "type": "root",
            "source": root,
            "target": root.with_name("작품"),
            "identity": storage.identity(root),
        }
    )
    return result


class Tests:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kotonoha-한글-space ")
        self.parent = Path(self.temp.name)
        self.data = self.parent / "data"
        self.stack = contextlib.ExitStack()
        for module, attribute, value in (
            (storage, "DATA_DIR", self.data),
            (engine, "DATA_DIR", self.data),
            (engine, "PREFERENCES_PATH", self.data / "translation_preferences.json"),
            (engine, "PROPER_NAME_CACHE_PATH", self.data / "proper_name_cache.json"),
            (engine, "CACHE_PATH", self.data / "translation-cache.json"),
        ):
            self.stack.enter_context(patch.object(module, attribute, value))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        storage.DATA_DIR.mkdir(parents=True, exist_ok=True)
        storage.atomic_json(engine.PREFERENCES_PATH, {"version": "2.0", "terms": {}})
        storage.atomic_json(
            engine.PROPER_NAME_CACHE_PATH, {"version": "1.1", "confirmed": {}, "learned": {}}
        )
        self.stack.enter_context(patch.object(engine, "PREFERENCES", engine.load_preferences()))
        self.stack.enter_context(
            patch.object(
                engine,
                "PREFERENCES_HASH",
                engine.sha256_bytes(engine.PREFERENCES_PATH.read_bytes()),
            )
        )

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()
