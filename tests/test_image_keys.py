"""Synthetic V2 pictures and fake process reads; never inspect a real account."""
import contextlib
import ctypes.wintypes as W
import io
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from wxkit import cli, imgkey, keygrab, keys, media


class ImageKeyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.acct = SimpleNamespace(path=str(self.root / 'wxid_fixture_e36e'),
                                    home=str(self.root / 'state'),
                                    keyfile=str(self.root / 'state/keys.bin'))
        self.seed = 1234567890
        self.key = imgkey._derive(self.seed, 'wxid_fixture')
        self.plain = b'\x89PNG\r\n\x1a\n' + b'x' * 24
        self.tail = b'fixture-tail\xff\xd9'
        xor = self.seed & 0xFF
        self.encoded = (b'\x07\x08V2\x08\x07' + struct.pack('<II', len(self.plain), len(self.tail))
                        + b'\0' + AES.new(self.key, AES.MODE_ECB).encrypt(pad(self.plain, 16))
                        + b'middle' + bytes(value ^ xor for value in self.tail))
        self.image = Path(self.acct.path) / 'msg/attach/fixture/2026-11/Img/image.dat'
        self.image.parent.mkdir(parents=True)
        self.image.write_bytes(self.encoded)

    def fake_memory(self, data=b''):
        kernel = SimpleNamespace(OpenProcess=mock.Mock(return_value=2**40),
                                 CloseHandle=mock.Mock())
        return SimpleNamespace(kernel32=kernel, PROCESS_VM_READ=16, PROCESS_QUERY_INFORMATION=1024,
                               find_wechat_pid=mock.Mock(return_value=999),
                               _rw_regions=mock.Mock(return_value=[(0, len(data))]),
                               _read_mem=mock.Mock(side_effect=lambda _handle, base, count: data[base:base + count]))

    def test_restore_capture_save_and_decrypt_roundtrip(self):
        output = io.StringIO()
        with mock.patch.object(imgkey, '_memory_api', return_value=self.fake_memory()), \
                mock.patch.object(imgkey, 'scan_seeds', return_value={self.seed, self.seed + 1}), \
                mock.patch.object(imgkey.sys, 'platform', 'win32'), \
                mock.patch.object(keys, 'import_any', side_effect=SystemExit('no database fixture')), \
                contextlib.redirect_stdout(output):
            self.assertEqual(keygrab.image_key_interactive(self.acct), (True, True))
        self.assertEqual(keys.load(self.acct)['img_aes'], self.key.decode())
        self.assertEqual(media.decrypt_dat(self.acct, self.encoded), self.plain + b'middle' + self.tail)
        self.assertNotIn(str(self.seed), output.getvalue())
        self.assertNotIn(self.key.decode(), output.getvalue())
        if sys.platform == 'win32':
            self.assertNotIn(self.key, Path(self.acct.keyfile).read_bytes())

    def test_wrong_xor_hint_falls_back_to_full_candidates(self):
        with mock.patch.object(imgkey, '_memory_api', return_value=self.fake_memory()), \
                mock.patch.object(imgkey, 'scan_seeds', return_value={self.seed, self.seed + 1}), \
                mock.patch.object(imgkey.sys, 'platform', 'win32'):
            result = imgkey.solve(self.acct, xor_hint=(self.seed + 1) & 255)
        self.assertEqual(result, (self.key, self.seed, self.seed & 255))

    def test_foreign_account_key_is_not_accepted(self):
        foreign = imgkey._derive(self.seed, 'wxid_other')
        self.assertFalse(imgkey._verify(foreign, imgkey._samples(self.acct)))

    def test_all_samples_must_pass(self):
        sample = imgkey._samples(self.acct)
        wrong = AES.new(b'0123456789abcdef', AES.MODE_ECB).encrypt(pad(self.plain, 16))
        self.assertFalse(imgkey._verify(self.key, sample + [(wrong, 32, 'other-account')]))

    def test_empty_and_truncated_samples_are_rejected(self):
        self.assertFalse(imgkey._verify(self.key, []))
        self.image.write_bytes(self.encoded[:30])
        self.assertEqual(imgkey._samples(self.acct), [])
        self.assertFalse(imgkey._verify(self.key, [(b'bad', 32, 'truncated')]))

    def test_full_image_magic_is_required(self):
        self.assertFalse(imgkey._head_ok(b'\x89PNGfake'))
        self.assertFalse(imgkey._head_ok(b'RIFF0000WAVE'))
        self.assertTrue(imgkey._head_ok(b'RIFF0000WEBP'))
        self.assertTrue(imgkey._head_ok(b'GIF89a' + b'x' * 10))

    def test_thumbnail_is_usable_and_original_is_preferred(self):
        thumb = self.image.with_name('image_t.dat')
        self.image.rename(thumb)
        self.assertEqual(len(imgkey._samples(self.acct)), 1)
        self.image.write_bytes(self.encoded)
        self.assertEqual(imgkey._samples(self.acct, limit=1)[0][2], str(self.image))

    def test_cached_key_skips_memory_scan(self):
        keys.save(self.acct, img_aes=self.key.decode(), img_xor='1')
        with mock.patch.object(media, 'guess_xor', return_value=None), \
                mock.patch.object(imgkey, 'solve') as solver, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(keygrab.image_key_interactive(self.acct), (True, True))
        solver.assert_not_called()

    def test_failed_refresh_preserves_cached_key_and_reports_failure(self):
        keys.save(self.acct, img_aes=self.key.decode(), img_xor='1')
        with mock.patch.object(media, 'guess_xor', return_value=None), \
                mock.patch.object(imgkey, 'solve', return_value=None), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(keygrab.image_key_interactive(self.acct, force=True), (False, True))
        self.assertEqual(keys.load(self.acct)['img_aes'], self.key.decode())

    def test_no_v2_sample_does_not_open_a_process(self):
        self.image.unlink()
        with mock.patch.object(imgkey, '_memory_api') as api, \
                mock.patch.object(imgkey.sys, 'platform', 'win32'):
            self.assertIsNone(imgkey.solve(self.acct))
        api.assert_not_called()

    def test_non_windows_does_not_load_memory_api(self):
        with mock.patch.object(imgkey.sys, 'platform', 'linux'), \
                mock.patch.object(imgkey, '_memory_api') as api:
            self.assertIsNone(imgkey.solve(self.acct))
        api.assert_not_called()

    def test_stopped_wechat_does_not_scan(self):
        memory = self.fake_memory()
        memory.find_wechat_pid.return_value = None
        with mock.patch.object(imgkey, '_memory_api', return_value=memory), \
                mock.patch.object(imgkey.sys, 'platform', 'win32'), \
                mock.patch.object(imgkey, 'scan_seeds') as scan:
            self.assertIsNone(imgkey.solve(self.acct))
        scan.assert_not_called()

    def test_chunk_boundary_and_long_numeric_run(self):
        data = b'x' * 12 + str(self.seed).encode() + b';' + b'7' * 30 + b';2345678901;'
        memory = self.fake_memory(data)
        with mock.patch.object(imgkey, '_memory_api', return_value=memory), \
                mock.patch.object(imgkey, 'CHUNK', 16):
            result = imgkey.scan_seeds(999)
        self.assertEqual(result, {self.seed, 2345678901})
        memory.kernel32.CloseHandle.assert_called_once_with(2**40)

    def test_handle_closes_when_memory_read_fails(self):
        memory = self.fake_memory(b'x')
        memory._read_mem.side_effect = OSError('synthetic read error')
        with mock.patch.object(imgkey, '_memory_api', return_value=memory):
            with self.assertRaises(OSError):
                imgkey.scan_seeds(999)
        memory.kernel32.CloseHandle.assert_called_once_with(2**40)

    def test_timeout_stops_reads_and_closes_handle(self):
        memory = self.fake_memory(b'x')
        with mock.patch.object(imgkey, '_memory_api', return_value=memory), \
                mock.patch.object(imgkey.time, 'monotonic', side_effect=[0, 2]):
            self.assertEqual(imgkey.scan_seeds(999, timeout=1), set())
        memory._read_mem.assert_not_called()
        memory.kernel32.CloseHandle.assert_called_once()

    @unittest.skipUnless(sys.platform == 'win32', 'Windows ctypes declarations')
    def test_windows_api_uses_full_width_handles(self):
        memory = imgkey._memory_api()
        self.assertIs(memory.kernel32.OpenProcess.restype, W.HANDLE)

    def test_cli_failure_has_nonzero_exit(self):
        args = SimpleNamespace(image=True, timeout=10, force=False)
        with mock.patch.object(cli.config, 'account', return_value=self.acct), \
                mock.patch.object(keygrab, 'image_key_interactive', return_value=(False, False)):
            with self.assertRaises(SystemExit) as result:
                cli.cmd_key(None, args)
        self.assertEqual(result.exception.code, 1)

    def test_invalid_timeout_does_not_scan(self):
        with mock.patch.object(imgkey, 'solve') as solver, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(keygrab.image_key_interactive(self.acct, timeout=0), (False, False))
        solver.assert_not_called()


if __name__ == '__main__':
    unittest.main()
