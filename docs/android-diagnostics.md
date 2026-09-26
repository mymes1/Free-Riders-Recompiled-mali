# Android diagnostics: what a device's log says

A run on a device that is not a known-good one used to end in silence. The
runtime's trace goes to `game.log`; a native crash there wrote nothing at all,
the log simply stopped mid-work, and the launcher's stopped page showed the last
lines of a run that never explained itself. This is what a run now says about
itself, and how to read it.

Everything below is written to `game.log` in the app's files directory
(`/sdcard/Android/data/<package>/files/`), which is the file to send with a
report. On Android the same lines also go to logcat under the `FreeRiders` tag.

## What the run is

The first line, written by `sfr::install_crash_reporter` before anything else
(`src/crash_report.cpp`):

```
NATIVE_DEVICE program=game abi=arm64-v8a api=35 model=SM-X110 manufacturer=samsung page_size=4096 memory_mb=3772 cpus=8 guest_budget_mb=512
```

Two more lines appear only when they matter:

```
NATIVE_PAGE_SIZE_NOTE page_size=16384 expected=4096 host_page_alignment=unsupported
NATIVE_GUEST_MEMORY source=environment megabytes=256
```

The guest's own pages are 4 KiB and its memory ranges are aligned to the host
page (`src/guest_memory.cpp`), so a device with 16 KiB pages is one this runtime
has not been run on; the note says so instead of leaving a stopped run to guess.
The guest memory knob is below.

## A death by signal

`install_crash_reporter` handles `SIGSEGV`, `SIGBUS`, `SIGILL`, `SIGFPE`,
`SIGABRT`, `SIGSYS` and `SIGTRAP` (and, through `abort`, an uncaught C++
exception). It writes the report to `game.log` and to logcat, and then dies of
the same signal, so the exit status, the tombstone and the launcher's view of
the death are the system's own:

```
CRASH program=game signal=SIGSEGV(0xb) code=0x1 address=0x7f8f3c2b10 kind=unmapped
CRASH guest_id=1 function=sub_824F29E8 entry=0x824f29e8 lr=0x8250e6e8
CRASH frame 0 +0x7f8f3c2b10 module=libmain.so base=0x7f8f00000000 offset=0x3c2b10
CRASH frame 1 +0x7f8f3c5c48 module=libmain.so base=0x7f8f00000000 offset=0x3c5c48 symbol=...
CRASH end signal=SIGSEGV(0xb) function=sub_824F29E8 entry=0x824f29e8 lr=0x8250e6e8
```

- **The signal and its detail.** `address` is the address the faulting
  instruction touched. Guest memory is one host mapping, so for a fault in the
  guest this is a guest address; `kind=unmapped` means the page was never
  reserved, `kind=no-permission` that protection refused the access. A
  `SIGILL` reports the opcode it could not execute.
- **The guest line** is the one that names the code: the generated function the
  crashing thread entered last, and where it was called from. It is the same
  pair the log's own `LAST_FUNCTION` shows on a stop -- the runtime keeps which
  function is running for the stop lines, and this reads it (plus the live
  context's LR) without the entry path paying for it. Without it, a fault in
  thirty thousand generated functions is anonymous.
- **The frames** are written outermost first, so the interesting end is the
  tail, and the last line repeats the reason because that is what the launcher's
  stopped page shows. `module`+`offset` (with the module's load `base`) is what
  `ndk-stack` and `addr2line` need:

  ```
  adb logcat | ndk-stack -sym out/build/android-arm64-v8a
  ```

- The runtime's own buffered trace is flushed first: the report is the last
  thing in the log, not in front of lines that were older than the crash.

## A death the system dealt

Nothing inside a process can explain the deaths the system deals out -- the
low-memory killer taking it (a 4 GB tablet running a 512 MB guest at 60 frames
a second is exactly where that happens), a graphics driver aborting it after a
GPU fault, the system stopping it. Android keeps those, and `GameExitReport.java`
copies them, as the launcher comes back in front, into `exit-report.txt`, which
the launcher appends to `game.log`:

```
<the tombstone's own text, for a native crash>
EXIT reason=LOW_MEMORY status=0 timestamp=1758900000000 rss_mb=180 pss_mb=150 description=...
```

- `reason` is Android's (`LOW_MEMORY`, `CRASH_NATIVE`, `SIGNALED`, `ANR`, ...);
  `signal=` names the signal for a death by signal, `rss_mb`/`pss_mb` what the
  process last held. Needs Android 11 (API 30); older devices show nothing.
- `Exit_self status=3` is a normal stop: the runtime explains that one itself
  with a `STOP` line earlier in the log.

## Guest memory

The guest's backing budget is committed on demand; the runtime's own peak is a
fraction of it (the title asks for far more than it touches). A device whose
neighbours need the memory can ask for less:

```
SFR_GUEST_MEMORY_MB=256
```

in `debug.env` in the app's files directory: the launcher reads `settings.env`,
`debug.env` and the game's defaults in that order, and it writes `settings.env`
itself at every start, so a hand-written line belongs in `debug.env` (the
launcher's settings write the same variable on the desktop). 64 to 2048 MB is
accepted; anything else is named and rejected on a `NATIVE_GUEST_MEMORY` line.
A guest that runs out of its budget stops with a named reason and a `STOP`
line, which is a report rather than a process the system took.

## What the device must provide

- Android 9 or later, arm64-v8a, Vulkan 1.1 (`android/…/AndroidManifest.xml`).
- `VK_EXT_robustness2`, `VK_KHR_sampler_mirror_clamp_to_edge` and
  `VK_EXT_descriptor_indexing`'s non-uniform indexing are used when present and
  worked around when not (`patches/plume-optional-extensions.patch`); a driver
  that lacks them is not a failure by itself.
- Block compression (BC1/2/3) is *not* required: a driver without it (Mali
  drivers from MediaTek do not expose it) gets the title's DXT textures decoded
  on the CPU (`src/native_formats.cpp`), which is slower and correct. The run
  says which path it took on its `NATIVE_TEXTURE_BC` line.
- 16 KiB host pages are not supported yet (see the note above).

## Reporting a crash

Send `game.log`. If it ends in `CRASH` lines, those are the report; if it ends
in an `EXIT` line, the system's reason is there; if it ends in nothing at all,
the run needs `debug.env` with `SFR_TRACE_GRAPHICS=1` and `SFR_TRACE_IMPORTS=1`
(the game's own settings do that on the desktop) so the log says where it got
to. A tombstone from logcat (`adb logcat -b crash`) helps when the report is
truncated by the death itself.
