"""Synthetic tests only: no private WeChat content or accounts are stored here."""
from __future__ import annotations

import concurrent.futures
import hashlib
from contextlib import closing
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest

from wechat_reader.parsing import parse_message
from wechat_reader.media import MediaResolver, decode_image
from wechat_reader.voice import VoiceQueue, FasterWhisperTranscriber, ModelUnavailable


class ParsingTests(unittest.TestCase):
    def test_long_text_is_not_truncated(self):
        text = "中文 long text\n" * 10000
        self.assertEqual(parse_message(text, 1)["text"], text)

    def test_group_sender_prefix(self):
        result = parse_message("wxid_synthetic:\n原文", 1, chat="synthetic@chatroom")
        self.assertEqual((result["text"], result["extra"]["sender"]), ("原文", "wxid_synthetic"))
        self.assertEqual(parse_message("说明:\n原文", 1, chat="synthetic@chatroom")["text"], "说明:\n原文")

    def test_quote_keeps_reference_separate(self):
        result = parse_message('<msg><appmsg><title>回复</title><type>57</type><refermsg><svrid>123</svrid><displayname>合成人</displayname><content>被引原文</content></refermsg></appmsg></msg>', 49)
        self.assertEqual(result["kind"], "quote")
        self.assertEqual(result["text"], "回复")
        self.assertEqual(result["extra"]["reference"]["content"], "被引原文")

    def test_recall_does_not_invent_original(self):
        result = parse_message('<sysmsg type="revokemsg"><revokemsg><session>c</session><newmsgid>555</newmsgid></revokemsg></sysmsg>', 10002)
        self.assertTrue(result["recalled"])
        self.assertEqual(result["extra"]["target_server_id"], "555")
        self.assertIn("原文未采集", result["text"])

    def test_unknown_invalid_xml_and_entities(self):
        unknown = parse_message("unknown body", 999999)
        self.assertEqual((unknown["kind"], unknown["text"]), ("unknown", "unknown body"))
        self.assertEqual(parse_message("<msg>", 49)["extra"]["parse_error"], "invalid_xml")
        self.assertEqual(parse_message('<!DOCTYPE a [<!ENTITY x SYSTEM "file:///no">]><a>&x;</a>', 49)["extra"]["parse_error"], "xml_declaration_rejected")

    def test_file_and_image_clues(self):
        result = parse_message('<msg><appmsg><title>example.txt</title><type>6</type><appattach><md5>aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</md5><fileext>txt</fileext></appattach></appmsg></msg>', 49)
        self.assertEqual((result["kind"], result["extra"]["name"]), ("file", "example.txt"))
        image = parse_message('<msg><img md5="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"/></msg>', 3, packed=b"\x00cccccccccccccccccccccccccccccccc\x01")
        self.assertEqual(image["extra"]["packed_hashes"], ["cccccccccccccccccccccccccccccccc"])

    def test_zstd_and_invalid_bytes(self):
        import zstandard
        text = "合成压缩文本"
        self.assertEqual(parse_message(zstandard.ZstdCompressor().compress(text.encode()), 1)["text"], text)
        self.assertEqual(parse_message(b"\x28\xb5\x2f\xfdBAD", 1)["extra"]["parse_error"], "zstandard_invalid")
        self.assertEqual(parse_message(b"\xffbad", 1)["extra"]["parse_error"], "invalid_utf8")


class LocalTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / "data" / "test-media"
        parent.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=parent)
        self.base = Path(self.tmp.name)
        self.root, self.cache = self.base / "account", self.base / "cache"
        (self.root / "msg").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_media_missing_and_escape(self):
        resolver = MediaResolver(self.root, self.cache)
        self.assertEqual(resolver.resolve({"extra": {}})["state"], "missing")
        self.assertEqual(resolver.resolve({"extra": {"path": "../private.txt"}})["state"], "error")
        with self.assertRaises(ValueError):
            MediaResolver(self.root, self.root / "cache")

    def test_file_ambiguity_thumbnail_and_read(self):
        folder = self.root / "msg"
        (folder / "doc.txt").write_bytes(b"synthetic file")
        resolver = MediaResolver(self.root, self.cache)
        result, data = resolver.read({"kind": "file", "extra": {"name": "doc.txt"}})
        self.assertEqual((result["state"], data), ("available", b"synthetic file"))
        result, data = resolver.read({"extra": {"name": "doc.txt"}}, max_bytes=2)
        self.assertEqual((result["state"], data), ("error", None))
        (folder / "other").mkdir()
        (folder / "other" / "doc.txt").write_bytes(b"second")
        resolver.refresh()
        self.assertEqual(resolver.resolve({"extra": {"name": "doc.txt"}})["state"], "ambiguous")
        hash_id = "a" * 32
        (folder / (hash_id + "_t.png")).write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
        resolver.refresh()
        self.assertEqual(resolver.resolve({"extra": {"md5": hash_id}})["variant"], "thumbnail")

    def test_legacy_dat_decode_and_source_unchanged(self):
        original = b"\xff\xd8\xff" + b"synthetic content" * 20 + b"\xff\xd9"
        encoded = bytes(x ^ 0x72 for x in original)
        path = self.root / "msg" / ("b" * 32 + ".dat")
        path.write_bytes(encoded)
        resolver = MediaResolver(self.root, self.cache)
        result, data = resolver.read({"kind": "image", "extra": {"md5": "b" * 32}})
        self.assertEqual((result["state"], data), ("available", original))
        self.assertEqual(path.read_bytes(), encoded)
        self.assertTrue(Path(result["path"]).is_relative_to(self.cache))
        self.assertEqual(resolver.resolve({"extra": {"md5": "b" * 32}})["path"], result["path"])

    def test_file_hash_clue_falls_back_to_verified_filename(self):
        payload = b"synthetic file attachment"
        expected = hashlib.md5(payload).hexdigest()
        folder = self.root / "msg"
        (folder / "report.txt").write_bytes(payload)
        resolver = MediaResolver(self.root, self.cache)
        message = {"kind": "file", "extra": {"md5": expected, "name": "report.txt"}}
        self.assertEqual(resolver.read(message)[1], payload)
        (folder / "report.txt").write_bytes(b"different version")
        self.assertEqual(resolver.resolve(message)["detail"], "local_file_hash_mismatch")
        other = folder / "other"
        other.mkdir()
        (folder / "report.txt").write_bytes(payload)
        (other / "report.txt").write_bytes(payload)
        resolver.refresh()
        self.assertEqual(resolver.resolve(message)["state"], "ambiguous")

    def test_video_with_only_cover_is_explicitly_an_image_thumbnail(self):
        thumb_hash, video_hash = "c" * 32, "d" * 32
        cover = self.root / "msg" / (thumb_hash + ".jpg")
        cover.write_bytes(b"\xff\xd8\xffsynthetic cover\xff\xd9")
        message = {"kind": "video", "extra": {"md5": video_hash, "cdnthumbmd5": thumb_hash}}
        result = MediaResolver(self.root, self.cache).resolve(message)
        self.assertEqual((result["state"], result["variant"], result["media_kind"], result["format"]), ("available", "thumbnail", "image", "jpg"))
        self.assertTrue(result["source_media_missing"])
        self.assertEqual(result["detail"], "source_media_missing")

    def test_verified_video_wins_over_same_hash_jpeg_cover(self):
        video_hash = "e" * 32
        folder = self.root / "msg"
        (folder / (video_hash + ".jpg")).write_bytes(b"\xff\xd8\xffsynthetic cover\xff\xd9")
        # An ISO BMFF ftyp box is verified from bytes, including with a .dat suffix.
        payload = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42" + b"synthetic payload"
        video = folder / (video_hash + ".dat")
        video.write_bytes(payload)
        result, data = MediaResolver(self.root, self.cache).read({"kind": "video", "extra": {"packed_hashes": [video_hash]}})
        self.assertEqual((result["variant"], result["media_kind"], result["format"]), ("original", "video", "mp4"))
        self.assertFalse(result["source_media_missing"])
        self.assertEqual(data, payload)

    def test_jpeg_video_hash_never_becomes_original_and_extension_is_untrusted(self):
        value = "f" * 32
        folder = self.root / "msg"
        (folder / (value + ".mp4")).write_bytes(b"\xff\xd8\xffsynthetic cover\xff\xd9")
        result = MediaResolver(self.root, self.cache).resolve({"kind": "video", "extra": {"md5": value}})
        self.assertEqual((result["variant"], result["media_kind"]), ("thumbnail", "image"))
        (folder / (value + ".mp4")).write_bytes(b"not a verified video")
        self.assertEqual(MediaResolver(self.root, self.cache).resolve({"kind": "video", "extra": {"md5": value}})["detail"], "video_format_unverified")

    def test_image_primary_hash_wins_over_thumbnail_and_thumb_field_keeps_role(self):
        image_hash, thumb_hash = "1" * 32, "2" * 32
        folder = self.root / "msg"
        original = folder / (image_hash + ".png")
        original.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic original")
        (folder / (thumb_hash + ".png")).write_bytes(b"\x89PNG\r\n\x1a\nsynthetic thumb")
        resolver = MediaResolver(self.root, self.cache)
        message = {"kind": "image", "extra": {"md5": image_hash, "cdnthumbmd5": thumb_hash}}
        self.assertEqual(resolver.resolve(message)["path"], str(original))
        original.unlink()
        result = resolver.resolve(message)
        self.assertEqual((result["variant"], result["media_kind"]), ("thumbnail", "image"))
        self.assertTrue(result["source_media_missing"])

    def test_v2_decode_rejects_bad_key_and_bounds(self):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        key = b"0123456789abcdef"
        original = b"\xff\xd8\xff" + b"synthetic bytes" * 10 + b"\xff\xd9"
        head_size, tail_size, mask = 31, 8, 83
        head = original[:head_size] + bytes([1])
        enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        body = enc.update(head) + enc.finalize() + original[head_size:-tail_size] + bytes(x ^ mask for x in original[-tail_size:])
        container = b"\x07\x08V2\x08\x07" + struct.pack("<II", head_size, tail_size) + b"\x00" + body
        self.assertEqual(decode_image(container, image_key=key), (original, "jpg"))
        with self.assertRaises(LookupError):
            decode_image(container)
        with self.assertRaises(ValueError):
            decode_image(container, image_key=b"wrongwrongwrong!")
        with self.assertRaises(ValueError):
            decode_image(container[:20], image_key=key)

    def test_unsupported_image_is_explicit(self):
        p = self.root / "msg" / "unknown.dat"
        p.write_bytes(b"unsupported container" * 10)
        result = MediaResolver(self.root, self.cache).resolve({"extra": {"path": "msg/unknown.dat"}})
        self.assertEqual(result["state"], "unsupported")
        self.assertEqual(list(self.cache.glob("*")), [])

    def test_queue_missing_model_failure_retry_and_success(self):
        audio = self.root / "msg" / "audio.wav"
        audio.write_bytes(b"RIFF synthetic")
        queue = VoiceQueue(self.base / "voices.sqlite")
        queue.enqueue("missing", None)
        self.assertEqual(queue.run_once()["state"], "missing")
        queue.enqueue("voice", audio)
        self.assertEqual(queue.run_once()["state"], "model_unavailable")
        queue.enqueue("voice", audio)
        self.assertEqual(queue.status(), {"missing": 1, "model_unavailable": 1})
        queue.retry("voice")
        def fail(_):
            raise RuntimeError("private text must not appear")
        result = queue.run_once(fail)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["detail"], "transcription_failed:RuntimeError")
        queue.retry("voice")
        self.assertEqual(queue.run_once(lambda _: "合成转写")["transcript"], "合成转写")
        queue.enqueue("voice", audio)
        self.assertEqual(queue.get("voice")["attempts"], 3)

    def test_expired_worker_resumes_and_live_lease_is_exclusive(self):
        audio = self.root / "msg" / "audio.wav"
        audio.write_bytes(b"synthetic")
        now = [100.0]
        queue = VoiceQueue(self.base / "voices.sqlite", lease_seconds=10, clock=lambda: now[0])
        queue.enqueue("m", audio)
        with closing(sqlite3.connect(queue.path)) as db:
            db.execute("UPDATE voice_jobs SET state='running',owner='crashed',lease_until=110 WHERE message_id='m'")
            db.commit()
        self.assertIsNone(queue.run_once(lambda _: "done"))
        now[0] = 111
        self.assertEqual(queue.run_once(lambda _: "done")["state"], "done")

    def test_reconcile_keeps_completed_transcript_and_known_path(self):
        audio = self.root / "msg" / "audio.wav"
        audio.write_bytes(b"synthetic")
        now = [100.0]
        queue = VoiceQueue(self.base / "voices.sqlite", clock=lambda: now[0])
        queue.enqueue("done", audio)
        queue.run_once(lambda _: "已有转写", message_id="done")
        queue.enqueue("known", audio)
        before = {identity: queue.get(identity) for identity in ("done", "known")}
        now[0] = 200
        self.assertEqual(queue.ensure_many(["done", "known", "new", "new"]), 1)
        for identity in before:
            self.assertEqual(queue.get(identity), before[identity])
        self.assertEqual(queue.get("new")["state"], "pending")
        self.assertIsNone(queue.get("new")["media_path"])
        self.assertEqual(queue.ensure_many(["new", "known", "done"]), 0)

    def test_reconcile_input_failure_rolls_back_entire_batch(self):
        queue = VoiceQueue(self.base / "voices.sqlite")
        queue.ensure_many(["existing"])
        def interrupted_ids():
            yield "new"
            raise RuntimeError("controlled interrupted input")
        with self.assertRaises(RuntimeError):
            queue.ensure_many(interrupted_ids())
        self.assertIsNone(queue.get("new"))
        self.assertEqual(queue.status(), {"pending": 1})

    def test_selective_worker_only_claims_requested_pending_job(self):
        audio = self.root / "msg" / "audio.wav"
        audio.write_bytes(b"synthetic")
        queue = VoiceQueue(self.base / "voices.sqlite")
        queue.ensure_many(["a", "b"])
        self.assertIsNone(queue.run_once(message_id="absent"))
        result = queue.run_once(lambda _: "B", media_loader=lambda identity: audio if identity == "b" else None, message_id="b")
        self.assertEqual((result["message_id"], result["state"], result["transcript"]), ("b", "done", "B"))
        self.assertEqual(queue.get("a")["attempts"], 0)
        self.assertIsNone(queue.run_once(message_id="b"))

    def test_batch_jobs_keep_fifo_order_when_timestamps_are_equal(self):
        queue = VoiceQueue(self.base / "voices.sqlite", clock=lambda: 100)
        queue.ensure_many(["z-first", "a-second"])
        self.assertEqual(queue.run_once()["message_id"], "z-first")
        self.assertEqual(queue.run_once()["message_id"], "a-second")

    def test_loader_failure_retry_and_existing_path_bypasses_loader(self):
        queue = VoiceQueue(self.base / "voices.sqlite")
        queue.ensure_many(["new"])
        def fail(_):
            raise OSError("private source path must not appear")
        result = queue.run_once(lambda _: "unreachable", media_loader=fail)
        self.assertEqual((result["state"], result["detail"]), ("failed", "media_load_failed:OSError"))
        audio = self.root / "msg" / "loaded.wav"
        audio.write_bytes(b"synthetic")
        queue.retry("new")
        result = queue.run_once(lambda path: "loaded" if path == audio else "wrong", media_loader=lambda _: audio)
        self.assertEqual((result["state"], result["transcript"], result["attempts"]), ("done", "loaded", 2))
        self.assertEqual(result["media_path"], str(audio.resolve()))
        queue.enqueue("known", audio)
        def should_not_load(_):
            self.fail("a valid cached voice must not be loaded again")
        self.assertEqual(queue.run_once(lambda _: "cached", media_loader=should_not_load)["state"], "done")
        queue.ensure_many(["unavailable"])
        self.assertEqual(queue.run_once(media_loader=lambda _: None)["state"], "missing")

    def test_expired_loader_cannot_overwrite_new_worker_path_or_result(self):
        now = [100.0]
        queue = VoiceQueue(self.base / "voices.sqlite", lease_seconds=10, clock=lambda: now[0])
        queue.ensure_many(["job"])
        winning_audio = self.root / "msg" / "winner.wav"
        stale_audio = self.root / "msg" / "stale.wav"
        winning_audio.write_bytes(b"winning synthetic")
        stale_audio.write_bytes(b"stale synthetic")
        def delayed_loader(_):
            now[0] = 111
            replacement = VoiceQueue(queue.path, lease_seconds=10, clock=lambda: now[0])
            result = replacement.run_once(lambda _: "new worker result", media_loader=lambda _: winning_audio, message_id="job")
            self.assertEqual(result["state"], "done")
            return stale_audio
        result = queue.run_once(lambda _: self.fail("expired worker must not transcribe"), media_loader=delayed_loader)
        self.assertEqual((result["state"], result["transcript"], result["media_path"]), ("done", "new worker result", str(winning_audio.resolve())))
        self.assertEqual(result["attempts"], 2)

    def test_two_workers_claim_each_job_once(self):
        audio = self.root / "msg" / "audio.wav"
        audio.write_bytes(b"synthetic")
        queue = VoiceQueue(self.base / "voices.sqlite")
        for i in range(10):
            queue.enqueue(str(i), audio)
        def work(_):
            return queue.run_once(lambda _: "done")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            rows = list(executor.map(work, range(10)))
        self.assertEqual({r["message_id"] for r in rows}, {str(i) for i in range(10)})
        self.assertEqual(queue.status(), {"done": 10})
        self.assertTrue(all(r["attempts"] == 1 for r in rows))

    def test_no_model_is_explicit(self):
        backend = FasterWhisperTranscriber(None, cache_dir=self.cache)
        with self.assertRaises(ModelUnavailable):
            backend(self.root / "msg" / "audio.wav")


if __name__ == "__main__":
    unittest.main()
