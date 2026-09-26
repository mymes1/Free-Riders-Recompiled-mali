"""The Android APK workflow against what it calls.

The workflow lives at `.github/workflows/android-apk.yml`; where that file
cannot be pushed (GitHub refuses a token without the workflows permission) the
same bytes are kept at `packaging/android-apk.workflow.yml`, and this reads
whichever is there.

The workflow is YAML, so this reads it as text: what it builds with are
`scripts/build_android.sh` and `scripts/package_android.py`, and those are what
it checks -- the options the build script parses, the environment variables the
two scripts read, and the secrets and paths its own documentation names. A
rename on either side fails here instead of in a run that takes an hour.
"""
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
# The live workflow. It is also kept under packaging/ because a token without
# the workflows permission cannot push .github/workflows (docs say how to put
# it in place); the two must not drift apart.
INSTALLED_WORKFLOW = ROOT / '.github/workflows/android-apk.yml'
WORKFLOW = INSTALLED_WORKFLOW if INSTALLED_WORKFLOW.exists() else ROOT / 'packaging/android-apk.workflow.yml'
DOCS = ROOT / 'docs/android-apk-workflow.md'
BUILD_SCRIPT = ROOT / 'scripts/build_android.sh'
PACKAGING = ROOT / 'scripts/package_android.py'


def run_blocks(text):
    """Every `run: |` body of the workflow, dedented, in order."""
    lines = text.splitlines()
    blocks = []
    index = 0
    while index < len(lines):
        match = re.match(r'^(\s*)run: \|$', lines[index])
        if match is None:
            index += 1
            continue
        indent = len(match.group(1))
        index += 1
        body = []
        while index < len(lines):
            line = lines[index]
            if line.strip() and len(line) - len(line.lstrip()) <= indent:
                break
            body.append(line)
            index += 1
        margin = min(len(line) - len(line.lstrip()) for line in body if line.strip())
        blocks.append('\n'.join(line[margin:] for line in body))
    return blocks


SHELL = shutil.which('bash') if os.name != 'nt' else None


def bash_step(block, directory, values, environment=None):
    """One step's shell, with the workflow's `${{ ... }}` filled in from values."""
    script = re.sub(r'\$\{\{\s*([^}]*?)\s*\}\}', lambda match: str(values[match.group(1).strip()]), block)
    path = Path(directory) / 'step.sh'
    path.write_text(script, encoding='utf-8')
    return subprocess.run([SHELL, '-e', '-o', 'pipefail', str(path)], cwd=directory, env=environment,
                          text=True, capture_output=True)


class AndroidApkWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding='utf-8')
        self.blocks = run_blocks(self.text)

    def test_the_workflow_is_a_manual_build(self):
        self.assertIn('workflow_dispatch', self.text)
        self.assertIn('scripts/build_android.sh', self.text)
        for path in (BUILD_SCRIPT, PACKAGING, DOCS):
            self.assertTrue(path.exists(), path)
        self.assertIn('scripts/bootstrap.py', self.text)
        self.assertTrue((ROOT / 'scripts/bootstrap.py').exists())

    def test_build_options_are_ones_the_build_script_parses(self):
        build = [block for block in self.blocks if 'build_android.sh' in block]
        self.assertEqual(len(build), 1)
        wanted = set()
        for group in re.findall(r'args(?:\+=|=)\((.*?)\)', build[0], re.S):
            wanted |= set(re.findall(r'(--[a-z][a-z-]+)', group))
        self.assertEqual(wanted, {'--diagnostic', '--pack', '--abi', '--validation', '--cmake-argument'})
        parsed = set(re.findall(r'^\s+(--[a-z][a-z-]+)\)', BUILD_SCRIPT.read_text(encoding='utf-8'), re.M))
        self.assertFalse(wanted - parsed, wanted - parsed)

    def test_environment_variables_are_ones_the_scripts_read(self):
        names = set(re.findall(r'\bSFR_[A-Z0-9_]+', self.text))
        self.assertIn('SFR_ANDROID_API', names)
        self.assertIn('SFR_ANDROID_NDK', names)
        scripts = BUILD_SCRIPT.read_text(encoding='utf-8') + PACKAGING.read_text(encoding='utf-8')
        self.assertFalse(names - set(re.findall(r'\bSFR_[A-Z0-9_]+', scripts)), names)

    def test_secrets_and_inputs_are_documented(self):
        secrets = set(re.findall(r'secrets\.([A-Z0-9_]+)', self.text))
        self.assertIn('SOURCE_REPO_TOKEN', secrets)
        docs = DOCS.read_text(encoding='utf-8')
        self.assertFalse(secrets - set(re.findall(r'[A-Z0-9_]{4,}', docs)), secrets)
        inputs = set(re.findall(r'^\s{6}([a-z_]+):$', self.text, re.M))
        self.assertIn('sources_repo', inputs)
        self.assertTrue(inputs <= set(re.findall(r'[a-z_]{3,}', docs)), inputs)

    def test_paths_the_workflow_defaults_to_are_documented(self):
        docs = DOCS.read_text(encoding='utf-8')
        for path in ('out/recomp/diagnostic', 'out/shaders/shaders.pack'):
            self.assertIn(path, self.text)
            self.assertIn(path, docs)

    def test_the_packaged_copy_matches_the_installed_workflow(self):
        packaged = ROOT / 'packaging/android-apk.workflow.yml'
        self.assertTrue(packaged.exists(), packaged)
        if not INSTALLED_WORKFLOW.exists():
            self.skipTest('the workflow is not installed at %s' % INSTALLED_WORKFLOW)
        self.assertEqual(packaged.read_bytes(), INSTALLED_WORKFLOW.read_bytes())

    def test_the_artifact_carries_the_apk(self):
        self.assertIn('out/android/FreeRidersRecompiled.apk', self.text)
        self.assertIn('actions/upload-artifact', self.text)
        self.assertIn('if-no-files-found: error', self.text)

    @unittest.skipUnless(SHELL, 'the steps are shell scripts')
    def test_the_sources_check_reads_the_layout_it_documents(self):
        block = next(block for block in self.blocks if 'shaders.pack' in block and 'missing' in block)
        values = {'inputs.game_directory': 'out/recomp/diagnostic',
                  'inputs.shaders_pack': 'out/shaders/shaders.pack'}
        with tempfile.TemporaryDirectory() as directory:
            game = Path(directory) / 'sources/out/recomp/diagnostic'
            game.mkdir(parents=True)
            for name in ('report.json', 'ppc_recomp_shared.h', 'ppc_context.h', 'ppc_config.h',
                         'ppc_func_mapping.cpp', 'imports.cpp', 'ppc_recomp.1.cpp'):
                (game / name).write_text('')
            pack = Path(directory) / 'sources/out/shaders'
            pack.mkdir(parents=True)
            (pack / 'shaders.pack').write_bytes(b'SFRSHPK1' + struct.pack('<I', 468))
            result = bash_step(block, directory, values)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertIn('shaders.pack: 468 shaders', result.stdout)
            # A tree the workflow would have built from is named as broken first.
            (game / 'imports.cpp').unlink()
            (pack / 'shaders.pack').write_bytes(b'NOPE')
            result = bash_step(block, directory, values)
            self.assertEqual(result.returncode, 1)
            self.assertIn('sources/out/recomp/diagnostic/imports.cpp', result.stdout)

    @unittest.skipUnless(SHELL, 'the steps are shell scripts')
    def test_the_apk_check_reads_a_packaged_apk(self):
        block = next(block for block in self.blocks if 'aapt2' in block)
        libraries = ('libmain.so', 'liblauncher.so', 'libSDL2.so', 'libc++_shared.so')
        entries = ['classes.dex', 'assets/shaders.pack']
        entries += ['lib/arm64-v8a/' + library for library in libraries]
        values = {'inputs.sources_repo': 'someone/sources', 'inputs.sources_ref': '',
                  'inputs.game_directory': 'out/recomp/diagnostic',
                  'inputs.shaders_pack': 'out/shaders/shaders.pack', 'inputs.abi': 'arm64-v8a',
                  'inputs.api': '28', 'inputs.validation': 'false', 'inputs.compiler_cache': 'true'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build_tools = root / 'sdk/build-tools/35.0.0'
            build_tools.mkdir(parents=True)
            for tool, output in (('aapt2', "package: name='com.freeriders.recompiled' versionCode='3'"),
                                 ('apksigner', 'Signer #1 certificate SHA-256 digest: 0000')):
                stub = build_tools / tool
                stub.write_text('#!/bin/sh\necho "%s"\n' % output)
                stub.chmod(0o755)
            apk = root / 'out/android/FreeRidersRecompiled.apk'
            apk.parent.mkdir(parents=True)
            with zipfile.ZipFile(apk, 'w') as archive:
                for entry in entries:
                    archive.writestr(entry, b'')
            subprocess.run(['git', 'init', '-q'], cwd=directory, check=True)
            subprocess.run(['git', '-c', 'user.email=t@e', '-c', 'user.name=t', 'commit', '-q',
                            '--allow-empty', '-m', 'x'], cwd=directory, check=True)
            summary = root / 'summary.md'
            environment = dict(os.environ, ANDROID_HOME=str(root / 'sdk'),
                               GITHUB_STEP_SUMMARY=str(summary))
            result = bash_step(block, directory, values, environment)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertIn('sha256=', (root / 'out/android/apk-info.txt').read_text())
            self.assertIn('lib/arm64-v8a/libmain.so', summary.read_text())
            # An APK without one of the game's libraries is not uploaded.
            with zipfile.ZipFile(apk, 'w') as archive:
                for entry in entries:
                    if not entry.endswith('liblauncher.so'):
                        archive.writestr(entry, b'')
            result = bash_step(block, directory, values, environment)
            self.assertEqual(result.returncode, 1)
            self.assertIn('has no lib/arm64-v8a/liblauncher.so', result.stdout)

    @unittest.skipUnless(SHELL, 'the steps are shell scripts')
    def test_the_build_step_hands_the_build_script_its_inputs(self):
        block = next(block for block in self.blocks if 'build_android.sh' in block)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            shutil.copy(PACKAGING, root / 'scripts/package_android.py')
            stub = root / 'scripts/build_android.sh'
            stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$GITHUB_WORKSPACE/arguments"\n'
                            'printf "%s\\n" "$SFR_ANDROID_VERSION_NAME" > "$GITHUB_WORKSPACE/version"\n')
            stub.chmod(0o755)
            (root / 'sources/out/recomp/diagnostic').mkdir(parents=True)
            (root / 'sources/out/shaders').mkdir(parents=True)
            (root / 'sources/out/shaders/shaders.pack').write_bytes(b'')
            subprocess.run(['git', 'init', '-q'], cwd=directory, check=True)
            subprocess.run(['git', '-c', 'user.email=t@e', '-c', 'user.name=t', 'commit', '-q',
                            '--allow-empty', '-m', 'x'], cwd=directory, check=True)
            values = {'inputs.sources_repo': 'someone/sources', 'inputs.sources_ref': '',
                      'inputs.game_directory': 'out/recomp/diagnostic',
                      'inputs.shaders_pack': 'out/shaders/shaders.pack', 'inputs.abi': 'arm64-v8a',
                      'inputs.api': '28', 'inputs.validation': 'false', 'inputs.compiler_cache': 'true',
                      'github.run_number': '7'}
            environment = dict(os.environ, GITHUB_WORKSPACE=directory, RUNNER_TEMP=directory)
            result = bash_step(block, directory, values, environment)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            arguments = (root / 'arguments').read_text().splitlines()
            self.assertEqual(arguments, [
                '--diagnostic', str(root / 'sources/out/recomp/diagnostic'),
                '--pack', str(root / 'sources/out/shaders/shaders.pack'),
                '--abi', 'arm64-v8a',
                '--cmake-argument', '-DCMAKE_C_COMPILER_LAUNCHER=ccache',
                '--cmake-argument', '-DCMAKE_CXX_COMPILER_LAUNCHER=ccache',
            ])
            version = (root / 'version').read_text().strip()
            self.assertTrue(version.startswith('0.1.2+ci.7.'), version)
            # Both ABIs at once, with validation and without the compiler cache.
            values.update({'inputs.abi': 'both', 'inputs.validation': 'true', 'inputs.compiler_cache': 'false'})
            result = bash_step(block, directory, values, environment)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertEqual((root / 'arguments').read_text().splitlines(), [
                '--diagnostic', str(root / 'sources/out/recomp/diagnostic'),
                '--pack', str(root / 'sources/out/shaders/shaders.pack'),
                '--validation',
            ])

    @unittest.skipUnless(SHELL, 'bash is needed to parse the shell')
    def test_the_shell_of_every_step_parses(self):
        for block in self.blocks:
            with tempfile.NamedTemporaryFile('w', suffix='.sh') as script:
                script.write(block)
                script.flush()
                result = subprocess.run(['bash', '-n', script.name], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, '%s\n%s' % (block, result.stderr))


if __name__ == '__main__':
    unittest.main()
