"""Archive mutations use synthetic data, never statutory source acceptance."""
import gzip
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from publisher import safeio
from publisher.consumer import consume_publication
from publisher.protocol import canonical, sha256
from fixtures import archive, fixture


class ConsumerControls(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.request, self.machine, self.main, self.assets = fixture(self.root)

    def consume(self):
        return consume_publication(request=self.request, machine_manifest=self.machine,
                                   main_archive=self.root / 'main.tar.gz', assets_archive=self.root / 'assets.tar.gz',
                                   output_dir=self.root / 'stage')

    def rebind_archive(self):
        self.request['assetsArchiveSha256'] = sha256((self.root / 'assets.tar.gz').read_bytes())
        identity = {key: value for key, value in self.request.items() if key != 'requestId'}
        self.request['requestId'] = sha256(('usct.static-publication-request/v1\n' + canonical(identity)).encode())

    def test_accepts_complete_deterministic_archive_closure(self):
        result = self.consume()
        self.assertEqual(result['generationId'], self.request['locator']['generationId'])
        self.assertEqual((self.root / 'stage/main/index.html').read_bytes(), self.main['index.html'])
        for path, data in self.assets.items():
            self.assertEqual((self.root / 'stage/assets' / path).read_bytes(), data)

    def test_rejects_traversal_links_devices_directories_and_duplicates(self):
        for kind in ['../escape', '/escape', 'g/../escape', 'link', 'device', 'directory', 'duplicate', 'sparse']:
            with self.subTest(kind=kind):
                name = next(iter(self.assets)) if kind == 'duplicate' else kind
                item = tarfile.TarInfo(name)
                if kind == 'link':
                    item.type = tarfile.SYMTYPE
                    item.linkname = '/tmp/foreign'
                elif kind == 'device':
                    item.type = tarfile.CHRTYPE
                elif kind == 'directory':
                    item.type = tarfile.DIRTYPE
                elif kind == 'sparse':
                    item.type = tarfile.GNUTYPE_SPARSE
                archive(self.root / 'assets.tar.gz', self.assets, (item, None))
                self.rebind_archive()
                with self.assertRaises(ValueError):
                    self.consume()
                self.assertFalse((self.root / 'escape').exists())
                self.assertFalse((self.root / 'stage/private-success.json').exists())
                self.root.joinpath('stage').rename(self.root / ('failed-' + kind.replace('/', '_')))

    def test_rejects_missing_extra_corrupt_and_truncated_payload(self):
        original = dict(self.assets)
        for kind in ['missing', 'extra', 'corrupt', 'truncated']:
            with self.subTest(kind=kind):
                files = dict(original)
                first = next(iter(files))
                if kind == 'missing':
                    del files[first]
                elif kind == 'extra':
                    files['g/extra'] = b'extra'
                elif kind == 'corrupt':
                    files[first] = b'wrong length and hash'
                archive(self.root / 'assets.tar.gz', files)
                if kind == 'truncated':
                    value = (self.root / 'assets.tar.gz').read_bytes()
                    (self.root / 'assets.tar.gz').write_bytes(value[:-8])
                self.rebind_archive()
                with self.assertRaises(ValueError):
                    self.consume()
                self.root.joinpath('stage').rename(self.root / ('failed-' + kind))

    def test_rejects_pax_header_before_reading_attacker_declared_payload(self):
        item = tarfile.TarInfo('pax')
        item.type = tarfile.XHDTYPE
        item.size = 900000000
        payload = item.tobuf(format=tarfile.USTAR_FORMAT)
        (self.root / 'assets.tar.gz').write_bytes(gzip.compress(payload, mtime=0))
        self.rebind_archive()
        with self.assertRaisesRegex(ValueError, 'special archive member'):
            self.consume()

    def test_rejects_concatenated_gzip_members_even_when_trailing_text_is_zero(self):
        path = self.root / 'assets.tar.gz'
        path.write_bytes(path.read_bytes() + gzip.compress(b'\0' * 512, mtime=0))
        self.rebind_archive()
        with self.assertRaisesRegex(ValueError, 'Multiple gzip'):
            self.consume()

    def test_refuses_existing_output_and_external_manifest_mismatch(self):
        stage = self.root / 'stage'
        stage.mkdir()
        (stage / 'foreign').write_text('preserve')
        with self.assertRaises(FileExistsError):
            self.consume()
        self.assertEqual((stage / 'foreign').read_text(), 'preserve')
        self.machine += b' '
        with self.assertRaisesRegex(ValueError, 'manifest hash'):
            self.consume()


    def test_space_and_inode_shortfalls_hold_before_stage_creation(self):
        for measured in [SimpleNamespace(f_frsize=4096, f_bsize=4096, f_bavail=(2 * 1024 ** 3) // 4096, f_favail=100000),
                         SimpleNamespace(f_frsize=4096, f_bsize=4096, f_bavail=(8 * 1024 ** 3) // 4096, f_favail=10000)]:
            with self.subTest(measured=measured), patch('publisher.safeio.os.fstatvfs', return_value=measured):
                with self.assertRaisesRegex(ValueError, 'free bytes/inodes'):
                    self.consume()
            self.assertFalse((self.root / 'stage').exists())

    def test_midwrite_capacity_failure_preserves_partial_stage_and_original_archives(self):
        before = (self.root / 'main.tar.gz').read_bytes()
        original = safeio.capacity
        calls = 0
        def gate(fd, needed_bytes=0, needed_inodes=0):
            nonlocal calls
            calls += 1
            if calls > 9:
                raise ValueError('injected capacity shortfall')
            return original(fd, needed_bytes, needed_inodes)
        with patch('publisher.safeio.capacity', side_effect=gate):
            with self.assertRaisesRegex(ValueError, 'Incomplete publication stage retained'):
                self.consume()
        self.assertTrue((self.root / 'stage').is_dir())
        self.assertEqual((self.root / 'main.tar.gz').read_bytes(), before)

    def test_transport_symlink_and_hardlink_never_bypass_digest_pin(self):
        original = self.root / 'main.tar.gz'
        saved = self.root / 'original.tar.gz'
        original.rename(saved)
        original.symlink_to(saved)
        with self.assertRaises((ValueError, OSError)):
            self.consume()
        (self.root / 'stage').rename(self.root / 'failed-symlink')
        original.unlink()
        original.hardlink_to(saved)
        with self.assertRaisesRegex(ValueError, 'single-link'):
            self.consume()


    def test_closes_transport_descriptor_when_destination_open_refuses(self):
        target = self.root / 'not-a-directory'
        target.write_text('foreign')
        before = len(os.listdir('/proc/self/fd'))
        with self.assertRaises(OSError):
            safeio.extract_ustar(self.root / 'main.tar.gz', target, {}, self.request['mainArchiveSha256'])
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)


    def test_final_closure_rejects_late_same_length_changes_and_extra_files(self):
        original = safeio.extract_ustar
        def altered(path, destination, table, digest):
            original(path, destination, table, digest)
            if Path(destination).name == 'assets':
                (Path(destination).parent / 'main/index.html').write_bytes(b'<h1>Tamper</h1>')
        with patch('publisher.safeio.extract_ustar', side_effect=altered):
            with self.assertRaisesRegex(ValueError, 'Staged file digest'):
                self.consume()


    def test_public_stage_modes_do_not_depend_on_operator_umask(self):
        original = os.umask(0o077)
        try:
            self.consume()
        finally:
            os.umask(original)
        self.assertEqual((self.root / 'stage/assets').stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.root / 'stage/main/index.html').stat().st_mode & 0o777, 0o644)
