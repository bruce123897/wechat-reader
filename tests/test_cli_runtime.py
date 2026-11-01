import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wechat_reader.archive import Archive
from wechat_reader.cli import cache_voice, main
from wechat_reader import runtime


BASE = runtime.DATA / "test-cli"
BASE.mkdir(parents=True, exist_ok=True)


class CliRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=BASE)
        self.root = Path(self.tmp.name)
        self.db = self.root / "archive.db"
        self.patch = patch.object(runtime, "DATA", self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def call(self, *args):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = main(["--db", str(self.db), *args])
        return code, json.loads(stream.getvalue())

    def test_import_query_export_error_exit_and_private_errors(self):
        fixture = Path(__file__).resolve().parents[1] / "examples/synthetic.json"
        self.assertEqual(self.call("import", str(fixture))[0], 0)
        code, data = self.call("search", "项目", "--limit", "1")
        self.assertEqual(code, 0)
        self.assertEqual(len(data["messages"]), 1)
        self.assertEqual(self.call("doctor")[1]["ok"], True)
        export = self.root / "export.json"
        self.assertEqual(self.call("export", "--output", str(export))[1]["exported"], 2)
        self.assertEqual(len(json.loads(export.read_text(encoding="utf-8"))), 2)
        code, data = self.call("msg", "99999")
        self.assertEqual(code, 2)
        self.assertFalse(data["ok"])
        self.assertNotIn(str(self.db), json.dumps(data))
        self.assertEqual(self.call("read", "--since", "1700000000", "--until", "1700000001")[0], 0)
        self.assertEqual(self.call("read", "--cursor", "invalid")[0], 2)

    def test_partial_sync_resume_and_failure_detected(self):
        first = {"account": "synthetic", "source": "a", "cursor": "1", "messages": [dict(chat="peer",local_id="1",ts=1,text="one")]}
        second = dict(first, source="b")

        class Source:
            account = "synthetic"

            def __init__(self, fail):
                self.fail = fail

            def scan(self, cursors=None):
                for b in (first, second):
                    if self.fail and b["source"] == "b":
                        raise OSError("private error must not be stored")
                    if (cursors or {}).get(b["source"]) != b["cursor"]:
                        yield b

        with Archive(self.db) as a:
            with self.assertRaises(OSError):
                runtime.sync_once(a, Source(True))
            self.assertFalse(a.doctor()["ok"])
            self.assertEqual(a.cursors("synthetic"), {"a": "1"})
            self.assertEqual(runtime.sync_once(a, Source(False))["inserted"], 1)
            self.assertTrue(a.doctor()["ok"])
            self.assertEqual(a.status()["messages"], 2)
            self.assertEqual(runtime.sync_once(a, Source(False))["batches"], 0)
            with Archive(self.root / "once.db") as other:
                runtime.sync_once(other, Source(False))
                self.assertEqual(a.query()["messages"], other.query()["messages"])

    def test_lock_output_boundary_and_empty_stale_archive(self):
        with runtime.lock("sync"):
            with self.assertRaises(RuntimeError):
                with runtime.lock("sync"):
                    pass
        with self.assertRaises(ValueError):
            runtime.output_path(runtime.ROOT / "README.md")
        with Archive(self.db) as a:
            self.assertFalse(a.doctor(max_age=120)["ok"])

    def test_concurrent_voice_cache_for_identical_payload(self):
        from concurrent.futures import ThreadPoolExecutor

        class Source:
            def read_voice(self, *args):
                return b"synthetic audio" * 10000

        with ThreadPoolExecutor(max_workers=4) as pool:
            paths = list(pool.map(lambda _: cache_voice(Source(), {"server_id": "1", "chat": "synthetic"}), range(8)))
        self.assertEqual(len(set(paths)), 1)
        self.assertEqual(paths[0].read_bytes(), b"synthetic audio" * 10000)
        self.assertEqual(list(paths[0].parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
