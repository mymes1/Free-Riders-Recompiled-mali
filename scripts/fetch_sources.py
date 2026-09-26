#!/usr/bin/env python3
"""Fetch the generated game code and shaders.pack from a link, and check them.

The android apk workflow builds the game from C++ generated on the player's own
machine (scripts/prepare_recomp.py) and from a shaders.pack collected there
(scripts/pack_shaders.py). scripts/package_sources.py puts both in one zip, and
a link to that zip -- a Google Drive share link, or any direct https link -- is
the easiest way to hand them to a runner. This downloads one, checks that it
really is a zip (and optionally that its SHA-256 is the one expected), extracts
it, finds the tree inside it and checks the layout the build reads, naming
whatever is missing: a run that starts without them fails an hour later, in the
middle of CMake.

Google Drive needs some care. A share link is an HTML page, not a file; and a
file large enough for Drive to scan it is only handed over after a second
request carrying the scan's confirm token, which is what the retry here does.
A link to a folder, or to a file that is not shared with anyone who has the
link, is answered with a page instead -- in which case this says so rather than
extracting a zip made of HTML.

Usage: python scripts/fetch_sources.py --url 'https://drive.google.com/file/d/<id>/view'
       [--sha256 HEX] [--output sources]
       [--game-directory out/recomp/diagnostic] [--shaders-pack out/shaders/shaders.pack]
       [--retries 3]

A local zip works too (--url out/sources/sfr-sources.zip), which is how the
tests round-trip it through scripts/package_sources.py.
"""
import argparse
import hashlib
from http.cookiejar import CookieJar
import os
from pathlib import Path
import re
import shutil
import sys
import urllib.parse
import urllib.request
import zipfile

# What the build reads: CMakeLists.txt globs the generated functions, the rest
# is named (docs/building.md), and the pack is what the launcher copies out
# (docs/android.md).
REQUIRED_FILES = ('report.json', 'ppc_recomp_shared.h', 'ppc_context.h', 'ppc_config.h',
                  'ppc_func_mapping.cpp', 'imports.cpp')
FUNCTION_GLOB = 'ppc_recomp.*.cpp'
PACK_MAGIC = b'SFRSHPK1'
CHUNK = 1 << 20
USER_AGENT = 'sfr-fetch-sources/1.0'
# Drive's identifier, from /file/d/<id>/..., /open?id=<id> or /uc?...&id=<id>.
IDENTIFIER = re.compile(r'(?:/d/|[?&]id=)([A-Za-z0-9_-]{10,})')
# The token on the page Drive answers with when it has scanned the file.
CONFIRM = re.compile(rb'confirm=([0-9A-Za-z_-]{4,})|name="confirm"\s+value="([0-9A-Za-z_-]{4,})"')
DRIVE_HOSTS = ('drive.google.com', 'docs.google.com', 'drive.usercontent.google.com')


def drive_download_url(identifier, confirm=None):
    """The download link for a Drive file, with the scan's token once there is one."""
    return ('https://drive.usercontent.google.com/download?id=%s&export=download&confirm=%s'
            % (identifier, confirm or 't'))


def direct_url(url):
    """A Drive share link becomes a download link; anything else is left alone."""
    host = urllib.parse.urlsplit(url).hostname or ''
    if not any(host == name or host.endswith('.' + name) for name in DRIVE_HOSTS):
        return url
    match = IDENTIFIER.search(url)
    if match is None:
        sys.exit('That is not a link to a file on Drive: %s\n'
                 'Open the file, choose Share, and copy the link (.../file/d/<id>/view...).' % url)
    return drive_download_url(match.group(1))


def with_confirm(url, confirm):
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query))
    query['confirm'] = confirm
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def confirm_token(page):
    match = CONFIRM.search(page)
    if match is None:
        return None
    return (match.group(1) or match.group(2)).decode()


def local_path(url):
    """A file:// URL or a plain path, so a zip on this machine can be checked too."""
    split = urllib.parse.urlsplit(url)
    if split.scheme == 'file':
        return Path(urllib.request.url2pathname(split.path))
    if split.scheme in ('', 'file') and not url.startswith(('http://', 'https://')):
        return Path(url)
    return None


def fetch(url, path, retries=3):
    """Stream one link into path, following Drive's scan confirmation."""
    source = local_path(url)
    if source is not None:
        if not source.is_file():
            sys.exit('No such file: %s' % source)
        shutil.copyfile(source, path)
        return
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    opener.addheaders = [('User-Agent', USER_AGENT)]
    tried = set()
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        with opener.open(request) as response, open(path, 'wb') as out:
            head = response.read(65536)
            start = head.lstrip()[:32].lower()
            if start.startswith((b'<!doctype html', b'<html')):
                page = head + response.read(1 << 20)
                token = confirm_token(page)
                if token and token not in tried and attempt < retries:
                    # Drive scanned the file: ask again, carrying the token.
                    tried.add(token)
                    url = with_confirm(url, token)
                    continue
                sys.exit('Drive answered with a page instead of the file. Either the link is not '
                         'shared with anyone who has it (Share -> General access -> Anyone with '
                         'the link, Viewer), or it points at a folder rather than the zip.')
            out.write(head)
            written = len(head)
            shutil.copyfileobj(response, out, CHUNK)
        return
    sys.exit('Drive would not hand over the file after %d attempts.' % (retries + 1))


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(CHUNK), b''):
            digest.update(block)
    return digest.hexdigest()


def extract(archive_path, destination):
    """Unpack the zip, refusing one that would write outside destination."""
    root = destination.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.namelist():
            target = (root / member).resolve()
            if target != root and root not in target.parents:
                sys.exit('The zip writes outside where it is unpacked: %s' % member)
        archive.extractall(destination)


def find_root(directory, game):
    """The directory the generated tree is under: the zip may hold one folder.

    Two or more are left alone rather than guessed between: the check that
    follows names what it cannot find, and a wrong guess would be worse.
    """
    directory = Path(directory)
    if (directory / game).is_dir():
        return directory
    candidates = [child for child in directory.iterdir()
                  if child.is_dir() and (child / game).is_dir()]
    return candidates[0] if len(candidates) == 1 else directory


def describe(directory, game, pack):
    """(problems, lines): what is missing, and what the tree turned out to hold."""
    directory = Path(directory)
    game_directory = directory / game
    problems = []
    lines = []
    if not game_directory.is_dir():
        problems.append('%s (the generated game code)' % game_directory)
        return problems, lines
    for name in REQUIRED_FILES:
        if not (game_directory / name).is_file():
            problems.append(str(game_directory / name))
    functions = sorted(game_directory.glob(FUNCTION_GLOB))
    if not functions:
        problems.append(str(game_directory / FUNCTION_GLOB) + ' (the generated functions)')
    lines.append('generated functions: %d files' % len(functions))
    lines.append('game code: %s' % size(game_directory))
    pack_path = directory / pack
    if not pack_path.is_file():
        problems.append('%s (the shader pack)' % pack_path)
        return problems, lines
    with open(pack_path, 'rb') as stream:
        magic, count = stream.read(8), stream.read(4)
    if magic != PACK_MAGIC:
        problems.append('%s is not a shaders.pack' % pack_path)
    else:
        import struct
        lines.append('shaders.pack: %d shaders (%s)'
                     % (struct.unpack('<I', count)[0], size(pack_path)))
    return problems, lines


def size(path):
    path = Path(path)
    if path.is_file():
        return '%s' % human(path.stat().st_size)
    total = sum(child.stat().st_size for child in path.rglob('*') if child.is_file())
    return '%s in %d files' % (human(total), sum(1 for child in path.rglob('*') if child.is_file()))


def human(count):
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if count < 1024 or unit == 'GiB':
            return '%.1f %s' % (count, unit) if unit != 'B' else '%d B' % count
        count /= 1024
    return '%d B' % count


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--url', required=True, help='a Drive share link, any direct link, or a path')
    parser.add_argument('--output', default='sources', help='where to unpack it (default: sources)')
    parser.add_argument('--sha256', help='the zip\'s SHA-256, checked after downloading')
    parser.add_argument('--game-directory', default='out/recomp/diagnostic',
                        help='the generated code inside the zip (default: out/recomp/diagnostic)')
    parser.add_argument('--shaders-pack', default='out/shaders/shaders.pack',
                        help='the pack inside the zip (default: out/shaders/shaders.pack)')
    parser.add_argument('--retries', type=int, default=3, help='Drive confirmations to follow')
    args = parser.parse_args()

    destination = Path(args.output)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    archive = destination.parent / (destination.name + '.zip')
    # A Drive share link is a page: it has to become a download link first.
    fetch(direct_url(args.url), archive, retries=args.retries)
    if not zipfile.is_zipfile(archive):
        sys.exit('That is not a zip: %s (%s). Drive links to a folder or to a page look like this; '
                 'link to the zip scripts/package_sources.py wrote.'
                 % (archive, human(archive.stat().st_size)))
    print('downloaded %s' % human(archive.stat().st_size))
    if args.sha256:
        found = sha256(archive)
        if found.lower() != args.sha256.strip().lower():
            sys.exit('The zip\'s SHA-256 is %s, not the %s you expected: it did not arrive whole, '
                     'or the link points at another file.' % (found, args.sha256.strip()))
        print('sha256: %s (as expected)' % found)
    extract(archive, destination)
    archive.unlink()
    root = find_root(destination, args.game_directory)
    problems, lines = describe(root, args.game_directory, args.shaders_pack)
    for line in lines:
        print(line)
    if problems:
        print('The zip does not hold what the build needs:', file=sys.stderr)
        for problem in problems:
            print('  %s' % problem, file=sys.stderr)
        print('Make it with scripts/package_sources.py, which puts the generated code and the pack '
              'at the paths the build expects.', file=sys.stderr)
        sys.exit(1)
    print('sources: %s (game code %s, pack %s)' % (root, args.game_directory, args.shaders_pack))
    return 0


if __name__ == '__main__':
    sys.exit(main())
