# Building an APK on GitHub Actions

The Android APK can be built by a GitHub runner instead of a machine with the
NDK installed: [`.github/workflows/android-apk.yml`](../.github/workflows/android-apk.yml)
is a **Run workflow** button that leaves an installable APK in the run's
artifacts. (The same file is kept beside the licences at
[`packaging/android-apk.workflow.yml`](../packaging/android-apk.workflow.yml):
GitHub will not let an app push into `.github/workflows/`, which is how it is
put there when a machine cannot — see step 0 below.) It exists for test builds on a device that is being brought up (a
Mali tablet, say), and it is not how releases are made: it builds the game from
code that **you** generated on your own machine and pushed for it, and the disc
image itself never reaches CI ([releasing.md](releasing.md)).

What the runner needs from you:

| Input | What it is |
| --- | --- |
| the generated game code | `out/recomp/diagnostic` from `scripts/prepare_recomp.py` |
| `shaders.pack` | `out/shaders/shaders.pack` from `scripts/pack_shaders.py`, with SPIR-V in it |
| a repository | a private repository of yours holding both |
| a token | `SOURCE_REPO_TOKEN`, able to read that repository |
| a key (optional) | the keystore secrets below, so the APK replaces an installed one |

Everything else the runner installs for itself: the Android SDK and its command
line tools, platform 35 and build-tools 35.0.0 (aapt2, d8, zipalign,
apksigner), the NDK — `29.0.13599879` unless the `ndk` input names another —
plus CMake, Ninja, ccache and unzip, and Python and the JDK. Nothing has to be
installed on the runner image, and after installing the step checks each of
these and lists by name whatever it could not find.

## 0. Put the workflow in `.github/workflows/`

Nothing shows under **Actions** until the file is on the repository's default
branch. If this branch already carries it, skip to step 1; GitHub refuses to
let an app (a sandbox or CI acting for you) create anything in
`.github/workflows/`, so the copy may be waiting in `packaging/` instead. Put
it in place from a machine of your own, or in the web interface:

```bash
cp packaging/android-apk.workflow.yml .github/workflows/android-apk.yml
git add .github/workflows/android-apk.yml && git commit -m "Build an Android APK on a runner"
git push
```

Or: GitHub → **Add file → Create new file**, path
`.github/workflows/android-apk.yml`, contents of
`packaging/android-apk.workflow.yml`, **Commit changes** → commit directly to
the default branch.

## 1. Generate the game code

On the machine with the disc and Windows (see [building.md](building.md)):

```powershell
python scripts/rom_tool.py extract --iso "your disc.iso" --output private/game --path default.xex
python scripts/prepare_recomp.py                 # out/recomp/diagnostic
./scripts/build_tools.ps1 -Diagnostic            # play once, D3D12 and Vulkan
python scripts/pack_shaders.py                   # out/shaders/shaders.pack
```

Android draws from the pack alone, so it has to hold **SPIR-V**: run the game
under Vulkan at least once (`SFR_GRAPHICS=vulkan`, or the launcher's Advanced
tab) before packing, or the APK will boot to a screen the runtime cannot draw.

## 2. Put them in a private repository

The two paths keep their place in the repository, so the workflow's defaults
work as they are:

```
out/recomp/diagnostic/…      (report.json, ppc_recomp.*.cpp, ppc_func_mapping.cpp, imports.cpp, …)
out/shaders/shaders.pack
```

```bash
sources=$(mktemp -d)
mkdir -p "$sources/out/recomp" "$sources/out/shaders"
cp -r out/recomp/diagnostic "$sources/out/recomp/"
cp out/shaders/shaders.pack "$sources/out/shaders/"
cd "$sources"
git init -b main && git add . && git commit -m "generated game code and shaders"
git remote add origin https://github.com/<you>/<your-private-repo>.git
git push -u origin main
```

A fresh repository rather than a fork of this one: this project's `.gitignore`
ignores `out/`, so a fork would need `git add -f` everywhere. Keep it plain Git,
without LFS — the workflow reads the files as they are.

Push again whenever the code is regenerated. Nothing here is distributed: the
repository is private, and a public one would be publishing code derived from
the game.

## 3. Let the workflow read it

A token, not a credential in the build:

1. GitHub → **Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token**.
2. Repository access: **Only select repositories** → the private repository
   above. Permissions: **Contents → Read-only**. Nothing else.
3. In the **runtime** repository (this one) → **Settings → Secrets and
   variables → Actions → New repository secret**: name `SOURCE_REPO_TOKEN`,
   value the token.

The game code itself stays in the private repository, which this token only
reads. If the sources repository is public, the secret can be left out.

### Optional: the signing key

An APK only replaces an installed app when both are signed with the same key.
Without these secrets the runner makes a fresh debug key, and a device with an
older build has to be cleaned up first (step 5); with them, the new APK
installs over the old one and keeps the installed game and saves:

| Secret | Value |
| --- | --- |
| `ANDROID_KEYSTORE_BASE64` | your `debug.keystore`, base64: `base64 -w0 ~/.android/debug.keystore` (PowerShell: `[Convert]::ToBase64String([IO.File]::ReadAllBytes("$HOME\.android\debug.keystore"))`) |
| `ANDROID_KEYSTORE_PASSWORD` | its store password (`android` for the standard debug key) |
| `ANDROID_KEY_ALIAS` | `androiddebugkey` |
| `ANDROID_KEY_PASSWORD` | its key password (the store password when unset) |

## 4. Run it

The workflow appears under **Actions → android apk** once this file is on the
repository's default branch (merge this branch first). Then:

**Run workflow**, and fill in:

| Input | Meaning |
| --- | --- |
| `sources_repo` | `<you>/<your-private-repo>`; the only one without a default |
| `sources_ref` | a branch, tag or commit there (empty: its default branch) |
| `game_directory` | where the generated code is there (`out/recomp/diagnostic`) |
| `shaders_pack` | where the pack is there (`out/shaders/shaders.pack`) |
| `abi` | `arm64-v8a` for a tablet, `x86_64` for the emulator, `both` for both |
| `api` | minimum Android version: 28, or 29 and up |
| `ndk` | the NDK version to install (empty: the pinned `29.0.13599879`, the one [android.md](android.md) was verified with) |
| `validation` | build with the Vulkan validation layers, for a debugging run |
| `compiler_cache` | reuse compiled objects between runs; leave it on, the second run of the same code rebuilds only what changed |

The **Install the SDK components and the NDK** step's log lines say which NDK,
platform and build-tools it ended up with, and the **Install CMake, Ninja and
ccache** step's say which of those it had to install.

A build takes tens of minutes (the game is thirty thousand generated
functions). When it finishes, the run's summary lists what was built - commit,
ABIs, size, SHA-256, the signer's fingerprint and the shader count - and
**Artifacts** has `FreeRidersRecompiled-arm64-v8a-r<run>.zip` with the APK and
`apk-info.txt` beside it. The summary also prints the APK's contents, so a
missing `lib/arm64-v8a/libmain.so` or `assets/shaders.pack` is visible before
anything is installed.

## 5. Install it on the device

```bash
adb install -r out/android/FreeRidersRecompiled.apk
```

If the install is refused — `INSTALL_FAILED_UPDATE_INCOMPATIBLE` — the APK on
the device was signed with another key (a locally built one, or the runner's
fresh debug key). Android has to remove it, and removing it deletes the app's
files, which is where the installed game lives. Copy them off first:

```bash
adb pull /sdcard/Android/data/com.freeriders.recompiled/files ./fr-files
adb shell pm uninstall com.freeriders.recompiled
adb install out/android/FreeRidersRecompiled.apk
adb push ./fr-files/. /sdcard/Android/data/com.freeriders.recompiled/files/
adb shell chmod -R a+rwX /sdcard/Android/data/com.freeriders.recompiled/files
```

The launcher installs its way out of a missing `game/`: **Install** takes the
disc image again (an `.iso` on the device works the same as on Windows). `save/`
is worth keeping in either case.

Without adb, copy the APK to the device (a file manager, or a Download) and open
it; Android asks to allow installs from that app. Setting the key secrets above
avoids all of this after the first time.

## 6. What the build cannot tell you

The runner has no GPU of the class of your device and no emulator run here, so
the APK passing through the workflow only means it was built, signed, carries
its libraries and its pack. Whether it boots is answered by the device:
`game.log` now opens with what the device is and ends with why a run died
([android-diagnostics.md](android-diagnostics.md)) — the file to send back.
