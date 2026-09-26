"""Fetching the generated game code and shaders.pack from a link.

The workflow hands a runner a zip of the two things only the player's own
machine can make; scripts/package_sources.py makes it and
scripts/fetch_sources.py downloads, unpacks and checks it. These tests drive
both: the Drive link forms, the scan-confirmation retry, the refusal of a zip
that writes outside where it is unpacked, the layout check's report, and one
round trip (make a zip, fetch it, build from it).
"""
import http.server
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import fetch_sources
import package_sources

GAME = 'out/recomp/diagnostic'
PACK = 'out/shaders/shaders.pack'


def write_sources(directory, shaders=468, functions=3, skip=(), bad_pack=False):
    """A tree like the one the zip holds, complete unless skip names what is not."""
    root = Path(directory)
    game = root / GAME
    game.mkdir(parents=True)
    for name in fetch_sources.REQUIRED_FILES:
        if name not in skip:
            (game / name).write_text('')
    for index in range(functions):
        (game / ('ppc_recomp.%d.cpp' % index)).write_text('// generated\n')
    (root / 'out/shaders').mkdir(parents=True)
    magic = b'WRONG' if bad_pack else fetch_sources.PACK_MAGIC
    (root / PACK).write_bytes(magic + struct.pack('<I', shaders))
    return root


class DriveLinkTests(unittest.TestCase):
    def test_share_links_become_download_links(self):
        identifier = '1AbCdEfGhIjKlMnOpQrStUvWxYz012345'
        cases = [
            'https://drive.google.com/file/d/%s/view?usp=sharing' % identifier,
            'https://drive.google.com/file/d/%s/view' % identifier,
            'https://drive.google.com/open?id=%s' % identifier,
            'https://docs.google.com/uc?export=download&id=%s' % identifier,
            'https://drive.google.com/file/d/%s/view?resourcekey=0-abc' % identifier,
        ]
        for link in cases:
            with self.subTest(link=link):
                self.assertEqual(fetch_sources.direct_url(link),
                                 fetch_sources.drive_download_url(identifier))

    def test_other_links_are_left_alone(self):
        for link in ('https://example.com/sfr-sources.zip',
                     'https://github.com/someone/sources/archive/refs/heads/main.zip'):
            self.assertEqual(fetch_sources.direct_url(link), link)

    def test_a_link_without_a_file_is_refused(self):
        for link in ('https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUv',
                     'https://drive.google.com/drive/my-drive'):
            with self.subTest(link=link):
                with self.assertRaises(SystemExit) as raised:
                    fetch_sources.direct_url(link)
                self.assertIn('not a link to a file', str(raised.exception))

    def test_the_scan_confirmation_is_followed(self):
        page = (b'<!DOCTYPE html><html><head></head><body>'
                b'<form action="/download" method="get">'
                b'<input type="hidden" name="id" value="x">'
                b'<input type="hidden" name="confirm" value="t7Qz9">'
                b'<input type="submit" value="Download anyway"></form></body></html>')
        self.assertEqual(fetch_sources.confirm_token(page), 't7Qz9')
        self.assertEqual(fetch_sources.confirm_token(b'...&amp;confirm=AbC123&amp;...'), 'AbC123')
        self.assertIsNone(fetch_sources.confirm_token(b'<!DOCTYPE html><html>no form here'))
        link = fetch_sources.drive_download_url('1AbCdEfGhIjKlMnOpQrStUvWxYz012345')
        confirmed = fetch_sources.with_confirm(link, 't7Qz9')
        self.assertIn('confirm=t7Qz9', confirmed)
        self.assertNotIn('confirm=t&', confirmed)


class LayoutTests(unittest.TestCase):
    def test_a_complete_tree_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = write_sources(directory)
            problems, lines = fetch_sources.describe(root, GAME, PACK)
            self.assertEqual(problems, [])
            self.assertIn('generated functions: 3 files', lines)
            self.assertIn('shaders.pack: 468 shaders', ' '.join(lines))

    def test_what_is_missing_is_named(self):
        with tempfile.TemporaryDirectory() as directory:
            root = write_sources(directory, functions=0, skip=('imports.cpp',))
            problems, _ = fetch_sources.describe(root, GAME, PACK)
            self.assertIn(str(root / GAME / 'imports.cpp'), problems)
            self.assertIn(str(root / GAME / fetch_sources.FUNCTION_GLOB) + ' (the generated functions)',
                          problems)

    def test_a_file_that_is_not_a_pack_is_named(self):
        with tempfile.TemporaryDirectory() as directory:
            root = write_sources(directory, bad_pack=True)
            problems, _ = fetch_sources.describe(root, GAME, PACK)
            self.assertTrue(any('is not a shaders.pack' in problem for problem in problems), problems)

    def test_a_missing_tree_is_one_problem(self):
        with tempfile.TemporaryDirectory() as directory:
            problems, lines = fetch_sources.describe(Path(directory), GAME, PACK)
            self.assertEqual(len(problems), 1)
            self.assertIn('the generated game code', problems[0])
            self.assertEqual(lines, [])

    def test_the_root_is_found_under_one_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_sources(root / 'sfr-sources')
            self.assertEqual(fetch_sources.find_root(root, GAME), root / 'sfr-sources')
            write_sources(root / 'elsewhere')
            self.assertEqual(fetch_sources.find_root(root, GAME), root)


class ZipTests(unittest.TestCase):
    def test_a_zip_that_writes_outside_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'sources.zip'
            with zipfile.ZipFile(archive, 'w') as zip_file:
                zip_file.writestr('../escaped.txt', 'not in the tree')
            with self.assertRaises(SystemExit) as raised:
                fetch_sources.extract(archive, root / 'unpacked')
            self.assertIn('writes outside', str(raised.exception))

    def test_a_normal_zip_is_unpacked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'sources.zip'
            with zipfile.ZipFile(archive, 'w') as zip_file:
                zip_file.writestr('out/shaders/shaders.pack', b'SFRSHPK1' + struct.pack('<I', 1))
            target = root / 'unpacked'
            fetch_sources.extract(archive, target)
            self.assertTrue((target / 'out/shaders/shaders.pack').is_file())


class RoundTripTests(unittest.TestCase):
    """package_sources.py writes what fetch_sources.py reads."""

    def run_script(self, script, *arguments):
        result = subprocess.run([sys.executable, str(SCRIPTS / script), *arguments],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_make_a_zip_then_fetch_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree = root / 'machine'
            write_sources(tree)
            zip_path = root / 'out/sources/sfr-sources.zip'
            out = self.run_script('package_sources.py',
                                  '--game-directory', str(tree / GAME),
                                  '--shaders-pack', str(tree / PACK),
                                  '--output', str(zip_path))
            self.assertIn('sha256:', out)
            digest = out.split('sha256:')[1].split()[0]
            # --game-root is the default out/recomp, so the code lands back at
            # out/recomp/diagnostic when it is unpacked.
            sources = root / 'sources'
            out = self.run_script('fetch_sources.py', '--url', str(zip_path),
                                  '--output', str(sources), '--sha256', digest)
            self.assertIn('shaders.pack: 468 shaders', out)
            self.assertIn('generated functions: 3 files', out)
            self.assertTrue((sources / GAME / 'imports.cpp').is_file())
            self.assertTrue((sources / PACK).is_file())

    def test_the_zip_is_refused_when_the_sha256_differs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_sources(root / 'tree')
            zip_path = root / 'sources.zip'
            self.run_script('package_sources.py',
                            '--game-directory', str(root / 'tree' / GAME),
                            '--shaders-pack', str(root / 'tree' / PACK),
                            '--output', str(zip_path))
            result = subprocess.run([sys.executable, str(SCRIPTS / 'fetch_sources.py'),
                                     '--url', str(zip_path), '--output', str(root / 'unpacked'),
                                     '--sha256', '0' * 64], text=True, capture_output=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn('not the', result.stderr)

    def test_packaging_stops_without_the_generated_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / PACK).parent.mkdir(parents=True)
            (root / PACK).write_bytes(b'SFRSHPK1' + struct.pack('<I', 1))
            result = subprocess.run([sys.executable, str(SCRIPTS / 'package_sources.py'),
                                     '--game-directory', str(root / GAME),
                                     '--shaders-pack', str(root / PACK),
                                     '--output', str(root / 'out.zip')],
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn('prepare_recomp.py', result.stderr)


class DownloadTests(unittest.TestCase):
    """fetch_sources.fetch against a local server that behaves like Drive."""

    def serve(self, responses):
        """A server that answers each request with the next of responses."""
        state = {'calls': 0}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                state['calls'] += 1
                body = responses[min(state['calls'] - 1, len(responses) - 1)]
                self.send_response(200)
                self.send_header('Content-Type', 'text/html' if body.lstrip()[:6] == b'<html>' or
                                 b'<!doctype' in body.lstrip()[:10].lower() else 'application/zip')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *arguments):
                pass

        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return 'http://127.0.0.1:%d/download?id=x' % server.server_address[1], state

    def test_a_direct_download_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            url, state = self.serve([b'PK\x03\x04zip bytes'])
            target = Path(directory) / 'file.zip'
            fetch_sources.fetch(url, target)
            self.assertEqual(target.read_bytes(), b'PK\x03\x04zip bytes')
            self.assertEqual(state['calls'], 1)

    def test_the_scan_page_is_followed(self):
        page = b'<!DOCTYPE html><html><body><input name="confirm" value="t0ken"></body></html>'
        with tempfile.TemporaryDirectory() as directory:
            url, state = self.serve([page, b'PK\x03\x04after the scan'])
            target = Path(directory) / 'file.zip'
            fetch_sources.fetch(url, target)
            self.assertEqual(target.read_bytes(), b'PK\x03\x04after the scan')
            self.assertEqual(state['calls'], 2)

    def test_a_share_link_is_converted_before_it_is_downloaded(self):
        identifier = '1AbCdEfGhIjKlMnOpQrStUvWxYz012345'
        seen = []
        original = fetch_sources.fetch

        def recording(url, path, retries=3):
            seen.append(url)
            path.write_bytes(b'not a zip')  # main() then stops, which is all this needs

        fetch_sources.fetch = recording
        try:
            with tempfile.TemporaryDirectory() as directory:
                sys.argv = ['fetch_sources.py',
                            '--url', 'https://drive.google.com/file/d/%s/view' % identifier,
                            '--output', str(Path(directory) / 'sources')]
                with self.assertRaises(SystemExit):
                    fetch_sources.main()
        finally:
            fetch_sources.fetch = original
        self.assertEqual(seen, [fetch_sources.drive_download_url(identifier)])

    def test_a_drive_download_is_unpacked_and_checked(self):
        """Scan page, then the zip: what Drive does with a file it scanned."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree = root / 'machine'
            write_sources(tree)
            archive = root / 'sources.zip'
            with zipfile.ZipFile(archive, 'w') as zip_file:
                for path in sorted(tree.rglob('*')):
                    if path.is_file():
                        zip_file.write(path, str(path.relative_to(tree)))
            page = b'<!DOCTYPE html><html><body><input name="confirm" value="t0ken"></body></html>'
            url, state = self.serve([page, archive.read_bytes()])
            result = subprocess.run([sys.executable, str(SCRIPTS / 'fetch_sources.py'),
                                     '--url', url, '--output', str(root / 'sources')],
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(state['calls'], 2)
            self.assertIn('shaders.pack: 468 shaders', result.stdout)
            self.assertTrue((root / 'sources' / GAME / 'imports.cpp').is_file())
            self.assertTrue((root / 'sources' / PACK).is_file())

    def test_a_page_that_never_offers_the_file_is_an_error(self):
        page = b'<!DOCTYPE html><html><body>Sign in to download</body></html>'
        with tempfile.TemporaryDirectory() as directory:
            url, _ = self.serve([page])
            with self.assertRaises(SystemExit) as raised:
                fetch_sources.fetch(url, Path(directory) / 'file.zip')
            self.assertIn('Anyone with the link', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
