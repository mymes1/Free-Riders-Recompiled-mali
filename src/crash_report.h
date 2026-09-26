#pragma once
#include <cstdint>

namespace sfr {
// What a death by signal looks like from inside the process, and what the
// machine is. Android runs the game in the launcher's app but a process of its
// own (android:process=":game"), and a native crash there writes nothing: the
// runtime's trace goes to game.log, and the log then simply stops, mid-work.
// The launcher shows the end of that log; the reason was only in a logcat
// buffer nobody reads.
//
// install_crash_reporter gives the process a report of its own, written
// straight to the log (stderr, which the Android entry points at game.log) and
// to logcat:
//
//   CRASH program=game signal=SIGSEGV(0xb) code=1 address=0x7f8f3c2b10 kind=unmapped
//   CRASH guest_id=1 function=sub_824F29E8 entry=0x824f29e8 lr=0x8250e6e8
//   CRASH frame 0 +0x7f8f3c2b10 module=libmain.so base=0x7f8f00000000 offset=0x3c2b10
//   ...
//   CRASH end signal=SIGSEGV(0xb) function=sub_824F29E8 entry=0x824f29e8 lr=0x8250e6e8
//
// The guest line names the guest function the crashing thread was in and where
// it was called from, which is what a native fault inside generated code or a
// hook needs to be understood; without it, a fault in thirty thousand assigned
// functions is anonymous.
//
// Call once, before the runtime starts, with the guest's memory budget
// (install() also writes the NATIVE_DEVICE line below). It is safe to call
// twice -- the Android entry calls it before the runtime's own main is reached,
// and main calls it again for the desktop runs -- and the second call only
// updates the program's name.
void install_crash_reporter(const char* program, uint64_t guest_backing_budget = 0);

// One line about the machine the runtime is on, written at startup, so a log a
// player sends says what it ran on:
//
//   NATIVE_DEVICE program=game abi=arm64-v8a api=35 model=SM-X110 manufacturer=samsung
//   page_size=4096 memory_mb=3772 cpus=8 guest_budget_mb=512
//
// `guest_backing_budget` is the guest memory budget in bytes, or zero where
// there is no guest memory. A host page size other than 4 KiB adds a warning:
// the guest's memory ranges are aligned to the host page, so a device whose
// page size differs is one this runtime has not been run on.
void report_environment(const char* program, uint64_t guest_backing_budget = 0);

#if defined(SFR_CRASH_GUEST_STATE)
// The game runtime's answers about the crashing thread, defined in
// src/diagnostic_main.cpp, where the guest's entry state and the generated
// context are visible: which guest this thread is, the function it entered
// last, and where that function was called from. The entry path pays nothing
// for them -- the function and its address are already kept there for the
// runtime's own stop lines (diagnostic_hooks.h), and the return address is the
// live context's LR. Targets without guest memory (the launcher) do not define
// SFR_CRASH_GUEST_STATE and report without the guest line.
uint32_t guest_identity();
const char* guest_function();
uint32_t guest_address();
uint32_t guest_return_address();
#endif
}
