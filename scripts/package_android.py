"""Packages the Android APK from the prebuilt native libraries without Gradle.

scripts/build_android.sh builds libmain.so and libSDL2.so into
android/app/src/main/jniLibs/<abi>/ and then runs this. Only the installed
Android SDK (build-tools, a platform) and a JDK are used, so nothing is
downloaded.

The APK is signed with the local debug key (~/.android/debug.keystore, created
with the standard debug-key settings when missing). Another key can be named
with --keystore or SFR_ANDROID_KEYSTORE (optionally with
SFR_ANDROID_KEYSTORE_PASSWORD, SFR_ANDROID_KEY_ALIAS, SFR_ANDROID_KEY_PASSWORD):
a build replaces an installed app only when both are signed with the same key.

Usage: python scripts/package_android.py [--output out/android/FreeRidersRecompiled.apk] [--abi ABI]... [--pack FILE]
       [--min-sdk N] [--version-name NAME] [--version-code N]
       [--keystore FILE] [--keystore-password PASSWORD] [--key-alias NAME] [--key-password PASSWORD]
"""
import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT = ROOT / 'android' / 'app' / 'src' / 'main'
SDL_JAVA = ROOT / 'tools' / 'SDL' / 'android-project' / 'app' / 'src' / 'main' / 'java'
MIN_SDK, TARGET_SDK = 28, 35
VERSION_CODE, VERSION_NAME = 3, '0.1.2'


def version_key(path):
    return [int(part) if part.isdigit() else 0 for part in path.name.replace('-', '.').split('.')]


def sdk_root():
    for name in ('ANDROID_HOME', 'ANDROID_SDK_ROOT'):
        if os.environ.get(name):
            return Path(os.environ[name])
    for candidate in (Path.home() / 'AppData/Local/Android/Sdk', Path.home() / 'Android/Sdk'):
        if candidate.is_dir():
            return candidate
    sys.exit('No Android SDK: set ANDROID_HOME')


def tool(directory, name):
    for suffix in ('', '.exe', '.bat'):
        path = directory / (name + suffix)
        if path.exists():
            return str(path)
    sys.exit('missing %s in %s' % (name, directory))


def jdk_tool(name):
    home = os.environ.get('JAVA_HOME')
    if home:
        return tool(Path(home) / 'bin', name)
    found = shutil.which(name)
    if not found:
        sys.exit('No JDK: set JAVA_HOME')
    return found


def run(*command):
    subprocess.run([str(part) for part in command], check=True)


def setting(argument, variable, default):
    """One build setting: the command line first, then the environment, then the default."""
    return argument if argument is not None else os.environ.get(variable) or default


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default=str(ROOT / 'out' / 'android' / 'FreeRidersRecompiled.apk'))
    parser.add_argument('--abi', action='append', help='only these ABIs (default: every built one)')
    parser.add_argument('--pack', help='a shaders.pack to carry as an asset (LauncherActivity copies it out)')
    parser.add_argument('--min-sdk', type=int, default=MIN_SDK,
                        help='minimum API level, matching the native build (default: 28)')
    parser.add_argument('--version-name', help='versionName to record (default: %s)' % VERSION_NAME)
    parser.add_argument('--version-code', type=int, help='versionCode to record (default: %s)' % VERSION_CODE)
    parser.add_argument('--keystore', help='signing keystore (default: ~/.android/debug.keystore)')
    parser.add_argument('--keystore-password', help='keystore password (default: android)')
    parser.add_argument('--key-alias', help='key alias (default: androiddebugkey)')
    parser.add_argument('--key-password', help='key password (default: the keystore password)')
    args = parser.parse_args()
    if args.min_sdk < MIN_SDK:
        parser.error('--min-sdk must be at least 28')
    version_name = setting(args.version_name, 'SFR_ANDROID_VERSION_NAME', VERSION_NAME)
    try:
        version_code = int(setting(args.version_code, 'SFR_ANDROID_VERSION_CODE', VERSION_CODE))
    except ValueError:
        sys.exit('the version code must be a number, not %r'
                 % (args.version_code if args.version_code is not None else os.environ['SFR_ANDROID_VERSION_CODE']))
    if version_code < 1:
        sys.exit('version code must be a positive number')
    keystore = Path(setting(args.keystore, 'SFR_ANDROID_KEYSTORE',
                            Path.home() / '.android' / 'debug.keystore'))
    store_password = setting(args.keystore_password, 'SFR_ANDROID_KEYSTORE_PASSWORD', 'android')
    key_alias = setting(args.key_alias, 'SFR_ANDROID_KEY_ALIAS', 'androiddebugkey')
    key_password = setting(args.key_password, 'SFR_ANDROID_KEY_PASSWORD', store_password)


    sdk = sdk_root()
    build_tools = max((sdk / 'build-tools').iterdir(), key=version_key)
    platform = max((p for p in (sdk / 'platforms').iterdir() if (p / 'android.jar').exists()), key=version_key)
    android_jar = platform / 'android.jar'
    libraries = PROJECT / 'jniLibs'
    abis = sorted(p.name for p in libraries.iterdir() if (p / 'libmain.so').exists()) if libraries.is_dir() else []
    if args.abi:
        abis = [abi for abi in abis if abi in args.abi]
    if not abis:
        sys.exit('No native libraries: run scripts/build_android.sh first')
    if not SDL_JAVA.is_dir():
        sys.exit('Run scripts/bootstrap.py to obtain the pinned SDL (tools/SDL)')

    work = ROOT / 'out' / 'build' / 'android-apk'
    shutil.rmtree(work, ignore_errors=True)
    (work / 'gen').mkdir(parents=True)
    (work / 'classes').mkdir()
    (work / 'dex').mkdir()

    aapt2 = tool(build_tools, 'aapt2')
    run(aapt2, 'compile', '--dir', PROJECT / 'res', '-o', work / 'resources.zip')
    run(aapt2, 'link', '-o', work / 'base.apk', '-I', android_jar, '--manifest', PROJECT / 'AndroidManifest.xml',
        '--java', work / 'gen', '--min-sdk-version', args.min_sdk, '--target-sdk-version', TARGET_SDK,
        '--version-code', version_code, '--version-name', version_name,
        '--auto-add-overlay', work / 'resources.zip')

    sources = [str(p) for p in SDL_JAVA.rglob('*.java')] + [str(p) for p in (PROJECT / 'java').rglob('*.java')] \
        + [str(p) for p in (work / 'gen').rglob('*.java')]
    run(jdk_tool('javac'), '-nowarn', '-Xlint:-options', '-source', '11', '-target', '11', '-encoding', 'UTF-8',
        '-classpath', android_jar, '-d', work / 'classes', *sources)
    classes = [str(p) for p in (work / 'classes').rglob('*.class')]
    run(tool(build_tools, 'd8'), '--release', '--min-api', args.min_sdk, '--lib', android_jar,
        '--output', work / 'dex', *classes)

    unsigned = work / 'unsigned.apk'
    shutil.copy(work / 'base.apk', unsigned)
    with zipfile.ZipFile(unsigned, 'a') as apk:
        apk.write(work / 'dex' / 'classes.dex', 'classes.dex', compress_type=zipfile.ZIP_DEFLATED)
        # Uncompressed so the loader maps them straight from the APK.
        for abi in abis:
            for library in sorted((libraries / abi).glob('*.so')):
                apk.write(library, 'lib/%s/%s' % (abi, library.name), compress_type=zipfile.ZIP_STORED)
        if args.pack:
            apk.write(args.pack, 'assets/shaders.pack', compress_type=zipfile.ZIP_DEFLATED)
    aligned = work / 'aligned.apk'
    run(tool(build_tools, 'zipalign'), '-f', '-P', '16', '4', unsigned, aligned)

    if not keystore.exists():
        if args.keystore or os.environ.get('SFR_ANDROID_KEYSTORE'):
            sys.exit('No keystore at %s' % keystore)
        keystore.parent.mkdir(parents=True, exist_ok=True)
        run(jdk_tool('keytool'), '-genkeypair', '-keystore', keystore, '-storepass', store_password,
            '-keypass', key_password, '-alias', key_alias, '-dname', 'CN=Android Debug,O=Android,C=US',
            '-keyalg', 'RSA', '-keysize', '2048', '-validity', '10000')
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    run(tool(build_tools, 'apksigner'), 'sign', '--ks', keystore, '--ks-pass', 'pass:' + store_password,
        '--key-pass', 'pass:' + key_password, '--ks-key-alias', key_alias, '--out', output, aligned)
    # The signer is printed because Android installs an update only when it is
    # signed with the same key as what is already there.
    run(tool(build_tools, 'apksigner'), 'verify', '--print-certs', str(output))
    print('APK: %s (%s, version %s (%d), signed with %s)'
          % (output, ', '.join(abis), version_name, version_code, keystore))


if __name__ == '__main__':
    main()
