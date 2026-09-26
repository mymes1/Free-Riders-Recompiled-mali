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


def run_steps(text):
    """Every `run: |` body of the workflow, dedented, keyed by its step's name.

    Blocks are picked by what a step is called rather than by a word that
    happens to be in it: several steps talk about the same tools.
    """
    lines = text.splitlines()
    steps = {}
    name = 'the step before any name'
    index = 0
    while index < len(lines):
        named = re.match(r'^\s*- name: (.+)$', lines[index])
        if named is not None:
            name = named.group(1).strip()
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
        steps[name] = '\n'.join(line[margin:] for line in body)
    return steps


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
        self.steps = run_steps(self.text)
        self.blocks = list(self.steps.values())

    def test_the_workflow_is_a_manual_build(self):
        self.assertIn('workflow_dispatch', self.text)
        self.assertIn('scripts/build_android.sh', self.text)
        for path in (BUILD_SCRIPT, PACKAGING, DOCS):
            self.assertTrue(path.exists(), path)
        self.assertIn('scripts/bootstrap.py', self.text)
        self.assertTrue((ROOT / 'scripts/bootstrap.py').exists())

    def test_build_options_are_ones_the_build_script_parses(self):
        build = self.steps['Build the APK']
        wanted = set()
        for group in re.findall(r'args(?:\+=|=)\((.*?)\)', build, re.S):
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
        block = self.steps['Check the game code that arrived']
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
        block = self.steps['Check the APK']
        libraries = ('libmain.so', 'liblauncher.so', 'libSDL2.so', 'libc++_shared.so')
        entries = ['classes.dex', 'assets/shaders.pack']
        entries += ['lib/arm64-v8a/' + library for library in libraries]
        values = {'inputs.sources_url': '', 'inputs.sources_repo': 'someone/sources',
                  'inputs.sources_ref': '', 'inputs.game_directory': 'out/recomp/diagnostic',
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

    def test_the_runner_installs_what_it_builds_with(self):
        # Nothing is taken for granted from the image: the SDK, its components,
        # the NDK and the build tools are all installed by these steps.
        for action in ('android-actions/setup-android@', 'actions/setup-java@', 'actions/setup-python@'):
            self.assertIn(action, self.text)
        for step in ('Install CMake, Ninja and ccache', 'Install the SDK components and the NDK'):
            self.assertIn(step, self.steps, step)

    @unittest.skipUnless(SHELL, 'the steps are shell scripts')
    def test_the_build_tools_step_installs_only_what_is_missing(self):
        block = self.steps['Install CMake, Ninja and ccache']
        cases = [
            # (what PATH holds, cmake version, what has to be installed)
            ({'cmake': '3.31.6', 'ninja': None, 'ccache': None, 'unzip': None}, None),
            ({'cmake': '3.22.1', 'ninja': None, 'ccache': None, 'unzip': None}, None),
            # CMakeLists.txt asks for 3.20, so an older one is replaced.
            ({'cmake': '3.18.4', 'ninja': None, 'ccache': None, 'unzip': None}, 'cmake'),
            ({'ninja': None, 'ccache': None, 'unzip': None}, 'cmake'),
            ({'cmake': '3.31.6', 'ccache': None, 'unzip': None}, 'ninja-build'),
            ({'cmake': '3.31.6', 'ninja': None, 'unzip': None}, 'ccache'),
        ]
        for present, install in cases:
            with self.subTest(present=sorted(present), install=install), \
                    tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / 'bin'
                path.mkdir()
                for tool, version in present.items():
                    executable = path / tool
                    if version:
                        executable.write_text('#!/bin/sh\necho "%s version %s"\n' % (tool, version))
                    else:
                        executable.write_text('#!/bin/sh\nexit 0\n')
                    executable.chmod(0o755)
                # apt-get is never run here: sudo records what it was asked for.
                sudo = path / 'sudo'
                sudo.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "%s/packages.txt"\n' % ('%s', root))
                sudo.chmod(0o755)
                result = bash_step(block, directory, {},
                                   dict(os.environ, PATH=str(path) + os.pathsep + os.environ['PATH']))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                asked = (root / 'packages.txt').read_text() if (root / 'packages.txt').exists() else ''
                if install is None:
                    self.assertEqual(asked, '', asked)
                else:
                    self.assertIn('apt-get install -y', asked)
                    for package in install.split():
                        self.assertIn(package, asked)
                    for package in ('ninja-build', 'ccache', 'unzip', 'cmake'):
                        if package not in install.split():
                            self.assertNotIn(package, asked.split('apt-get install -y')[1])

    def test_the_sdk_and_ndk_versions_are_the_ones_the_packaging_needs(self):
        # package_android.py targets SDK 35 and reads android.jar from the
        # newest platform there, so the workflow has to install one.
        packaged = PACKAGING.read_text(encoding='utf-8')
        target = re.search(r'^MIN_SDK, TARGET_SDK\s*=\s*\d+,\s*(\d+)', packaged, re.M).group(1)
        block = self.steps['Install the SDK components and the NDK']
        platform = re.search(r'^(\s*)platform=(\S+)', block, re.M)
        self.assertIsNotNone(platform, 'the step does not name a platform')
        self.assertEqual(platform.group(2), target)
        tools = re.search(r'^tools=(\S+)', block, re.M).group(1)
        self.assertTrue(tools.startswith(target + '.'), tools)
        # The pinned NDK is the one docs/android.md says was verified.
        ndk = re.search(r'ndk="\$\{WANTED_NDK:-(\S+)\}"', block).group(1)
        self.assertRegex(ndk, r'^2[7-9]\.\d+\.\d+$', ndk)
        self.assertIn(ndk, (ROOT / 'docs/android.md').read_text(encoding='utf-8'))

    def test_the_sources_arrive_either_as_a_link_or_from_a_repository(self):
        """One of the two ways in, each gated on its own input."""
        self.assertIn("if: inputs.sources_url != ''", self.text)
        self.assertIn("if: inputs.sources_repo != ''", self.text)
        download = self.steps['Download the game code']
        self.assertIn('scripts/fetch_sources.py', download)
        # The link opens the zip to anyone who has it, and this repository's
        # run logs can be read by anyone too: it is masked, never echoed.
        self.assertIn('::add-mask::${{ inputs.sources_url }}', download)
        self.assertNotIn('echo "${{ inputs.sources_url }}"', download)

    @unittest.skipUnless(SHELL, 'the steps are shell scripts')
    def test_the_workflow_says_when_no_game_code_was_given(self):
        block = self.steps['Decide where the game code comes from']
        cases = [
            ({'inputs.sources_url': '', 'inputs.sources_repo': ''}, False),
            ({'inputs.sources_url': 'https://drive.google.com/file/d/1ABC/view',
              'inputs.sources_repo': ''}, True),
            ({'inputs.sources_url': '', 'inputs.sources_repo': 'someone/sources'}, True),
        ]
        for values, accepted in cases:
            with self.subTest(values=values), tempfile.TemporaryDirectory() as directory:
                result = bash_step(block, directory, values)
                if accepted:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                else:
                    self.assertEqual(result.returncode, 1)
                    self.assertIn('No game code', result.stdout)
                    self.assertIn('sources_url', result.stdout)

    @unittest.skipUnless(SHELL, 'the steps are shell scripts')
    def test_the_download_step_hands_fetch_sources_the_link_and_the_digest(self):
        block = self.steps['Download the game code']
        link = 'https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUv/view?usp=sharing'
        for digest in ('', 'a' * 64):
            with self.subTest(sha256=bool(digest)), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / 'bin'
                path.mkdir()
                # fetch_sources.py is not run here: what it is given is.
                (path / 'python').write_text(
                    '#!/bin/sh\nprintf "%s\\n" "$*" > "$PWD/arguments.txt"\n')
                (path / 'python').chmod(0o755)
                (root / 'scripts').mkdir()
                result = bash_step(block, directory,
                                   {'inputs.sources_url': link, 'inputs.sources_sha256': digest},
                                   dict(os.environ, PATH=str(path) + os.pathsep + os.environ['PATH'],
                                        GITHUB_WORKSPACE=str(root)))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('::add-mask::' + link, result.stdout)
                arguments = (root / 'arguments.txt').read_text().split()
                self.assertEqual(arguments[:5],
                                 ['scripts/fetch_sources.py', '--url', link,
                                  '--output', str(root / 'sources')])
                self.assertEqual(arguments[5:], ['--sha256', digest] if digest else [])

    @unittest.skipUnless(SHELL, 'the steps are shell scripts')
    def test_the_sdk_step_installs_what_the_scripts_read(self):
        """The step against a stub sdkmanager: it installs, then checks each tool."""
        block = self.steps['Install the SDK components and the NDK']
        stub = r'''#!/bin/sh
# A stand-in for sdkmanager: it records every package asked for and writes the
# files a real install would leave behind.
printf '%s\n' "$*" >> "${ANDROID_HOME:?}/installed.txt"
[ "$1" = --install ] || exit 0
shift
for package in "$@"; do
  case "$package" in
    platforms\;android-*)
      directory="$ANDROID_HOME/${package%;*}/${package#*;}"
      mkdir -p "$directory" && : > "$directory/android.jar";;
    build-tools\;*)
      directory="$ANDROID_HOME/${package%;*}/${package#*;}"
      mkdir -p "$directory"
      for tool in aapt2 d8 zipalign apksigner; do
        : > "$directory/$tool" && chmod +x "$directory/$tool"
      done;;
    ndk\;*)
      directory="$ANDROID_HOME/${package%;*}/${package#*;}"
      mkdir -p "$directory/build/cmake" "$directory/toolchains/llvm/prebuilt/linux-x86_64/bin"
      : > "$directory/build/cmake/android.toolchain.cmake"
      : > "$directory/toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-strip"
      chmod +x "$directory/toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-strip"
      for triple in aarch64-linux-android x86_64-linux-android; do
        mkdir -p "$directory/toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/lib/$triple"
        : > "$directory/toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/lib/$triple/libc++_shared.so"
      done
      printf 'Pkg.Revision = 29.0.13599879\n' > "$directory/source.properties";;
    *) echo "unexpected package $package" >&2; exit 1;;
  esac
done
'''
        for case, on_path in (('every tool is installed', True), ('cmake is missing', False)):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                sdk = root / 'sdk'
                (sdk / 'cmdline-tools/latest/bin').mkdir(parents=True)
                manager = sdk / 'cmdline-tools/latest/bin/sdkmanager'
                manager.write_text(stub)
                manager.chmod(0o755)
                path = root / 'bin'
                path.mkdir()
                # Only the tools the step looks for on PATH; coreutils and the
                # shell's own built-ins are the real ones.
                for tool in ('java', 'javac', 'keytool', 'python', 'cmake', 'ninja', 'unzip', 'git'):
                    if tool == 'cmake' and not on_path:
                        continue
                    executable = path / tool
                    executable.write_text('#!/bin/sh\nexit 0\n')
                    executable.chmod(0o755)
                environment = dict(os.environ, PATH=str(path) + os.pathsep + os.environ['PATH'],
                                   ANDROID_HOME=str(sdk), WANTED_NDK='',
                                   GITHUB_ENV=str(root / 'github_env'))
                result = bash_step(block, directory, {}, environment)
                if not on_path:
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertIn('were not installed', result.stdout)
                    self.assertIn('cmake (on PATH)', result.stdout)
                    continue
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                installed = (sdk / 'installed.txt').read_text()
                for package in ('platforms;android-35', 'build-tools;35.0.0', 'ndk;29.0.13599879'):
                    self.assertIn(package, installed)
                self.assertIn('NDK 29.0.13599879', result.stdout)
                environment_lines = (root / 'github_env').read_text()
                self.assertIn('ANDROID_HOME=%s' % sdk, environment_lines)
                self.assertIn('SFR_ANDROID_NDK=%s/ndk/29.0.13599879' % sdk, environment_lines)

    @unittest.skipUnless(SHELL, 'the steps are shell scripts')
    def test_the_sdk_step_stops_when_there_is_no_sdkmanager(self):
        block = self.steps['Install the SDK components and the NDK']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = dict(os.environ, ANDROID_HOME=str(root / 'empty-sdk'), WANTED_NDK='',
                               PATH=str(root / 'nowhere') + os.pathsep + '/usr/bin:/bin',
                               GITHUB_ENV=str(root / 'github_env'))
            result = bash_step(block, directory, {}, environment)
            self.assertEqual(result.returncode, 1)
            self.assertIn('No Android sdkmanager', result.stdout)

    @unittest.skipUnless(SHELL, 'the steps are shell scripts')
    def test_the_build_step_hands_the_build_script_its_inputs(self):
        block = self.steps['Build the APK']
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
