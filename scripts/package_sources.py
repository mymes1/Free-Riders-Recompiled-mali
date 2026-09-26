#!/usr/bin/env python3
"""Put the generated game code and shaders.pack in one zip for a build machine.

The android apk workflow builds on a runner that has no disc, so it is handed
the two things only the player's own machine can make:

  out/recomp/diagnostic     the generated C++ (scripts/prepare_recomp.py)
  out/shaders/shaders.pack  the translated shaders (scripts/pack_shaders.py)

This zips them, at the paths the build reads, ready to be uploaded (to Google
Drive, say) and linked from the workflow's sources_url input. Both are private
output: they are derived from your own disc and are never committed; a zip
uploaded anywhere publishes them to whoever has the link.

The layout is checked before anything is written, and the SHA-256 is printed:
paste it into the workflow's sources_sha256 input so a download that stops
halfway is caught instead of being unpacked.

Usage: python scripts/package_sources.py [--output out/sources/sfr-sources.zip]
       [--game-directory out/recomp/diagnostic] [--shaders-pack out/shaders/shaders.pack]
"""
import argparse
import hashlib
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--game-directory', default='out/recomp/diagnostic',
                        help='the generated code (default: out/recomp/diagnostic)')
    parser.add_argument('--shaders-pack', default='out/shaders/shaders.pack',
                        help='the shader pack (default: out/shaders/shaders.pack)')
    parser.add_argument('--output', default='out/sources/sfr-sources.zip',
                        help='the zip to write (default: out/sources/sfr-sources.zip)')
    parser.add_argument('--game-root', default='out/recomp',
                        help='the directory inside the zip the game code goes under '
                             '(default: out/recomp, so it lands at out/recomp/diagnostic)')
    parser.add_argument('--pack-root', default='out/shaders',
                        help='the directory inside the zip the pack goes under '
                             '(default: out/shaders, so it lands at out/shaders/shaders.pack)')
    args = parser.parse_args()

    game = Path(args.game_directory)
    pack = Path(args.shaders_pack)
    if not game.is_dir():
        sys.exit('No generated game code at %s: run scripts/prepare_recomp.py first.' % game)
    if not pack.is_file():
        sys.exit('No shader pack at %s: run scripts/pack_shaders.py first.' % pack)
    with open(pack, 'rb') as stream:
        if stream.read(8) != b'SFRSHPK1':
            sys.exit('%s is not a shaders.pack.' % pack)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix('.zip.partial')
    digest = hashlib.sha256()
    count = 0
    # Deflated: the generated C++ is text and shrinks to a fifth of itself,
    # which is the difference between a slow upload and a quick one.
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(game.rglob('*')):
            if not name.is_file():
                continue
            archive.write(name, '%s/%s' % (args.game_root, name.relative_to(game.parent)))
            count += 1
        archive.write(pack, '%s/%s' % (args.pack_root, pack.name))
        count += 1
    with open(temporary, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    shutil.move(temporary, output)
    size = output.stat().st_size
    print('zip: %s (%d files, %.1f MiB)' % (output, count, size / (1 << 20)))
    print('sha256: %s' % digest.hexdigest())
    print()
    print('Upload that zip, share it with anyone who has the link (Viewer), and put the link in the')
    print('workflow\'s sources_url input (the SHA-256 in sources_sha256, so a partial download is')
    print('caught before it is unpacked). docs/android-apk-workflow.md has the steps.')


if __name__ == '__main__':
    sys.exit(main())
