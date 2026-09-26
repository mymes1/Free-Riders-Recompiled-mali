// The crash reporter and the startup environment line (crash_report.h).
//
// This runs where nothing else does: from a signal handler, with the process
// already broken. It allocates nothing, takes no lock and calls nothing that
// could block, so the report is built in one static buffer with plain stores
// and written with write(2). backtrace/dladdr are the two unavoidable
// exceptions; both are safe enough on a broken process, and if they are not,
// what was written before them has already been decided.

#include "crash_report.h"

#include <atomic>
#include <cerrno>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <io.h>
#else
#include <dlfcn.h>
#include <execinfo.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <ucontext.h>
#include <unistd.h>
#endif

#if defined(__ANDROID__)
#include <android/log.h>
#include <sys/system_properties.h>
#endif

namespace {

// ---------------------------------------------------------------- the buffer
constexpr size_t report_capacity = 16 * 1024;
char report[report_capacity];
size_t report_size = 0;

void append(const char* text, size_t length) {
    if (report_size >= report_capacity) return;
    if (length > report_capacity - report_size) length = report_capacity - report_size;
    std::memcpy(report + report_size, text, length);
    report_size += length;
}

void append(const char* text) { append(text, std::strlen(text)); }

void append_hex(uint64_t value, unsigned digits = 0) {
    char text[16];
    unsigned length = 0;
    do {
        text[length++] = "0123456789abcdef"[value & 0xF];
        value >>= 4;
    } while (value != 0);
    while (length < digits) text[length++] = '0';
    while (length) append(&text[--length], 1);
}

void append_uint(uint64_t value) {
    char text[24];
    unsigned length = 0;
    do {
        text[length++] = char('0' + value % 10);
        value /= 10;
    } while (value != 0);
    while (length) append(&text[--length], 1);
}

void append_field(const char* name, const char* value) {
    append(" ");
    append(name);
    append("=");
    append(value && *value ? value : "?");
}

void append_hex_field(const char* name, uint64_t value) {
    append(" ");
    append(name);
    append("=0x");
    append_hex(value);
}

#ifndef _WIN32
// A write that a signal handler can make: no stdio, no allocation, retried only
// where the kernel says it was interrupted. Nothing is written on Windows,
// where the reporter installs no handler (report_environment is all there is).
void write_all(int descriptor, const char* data, size_t length) {
    while (length) {
        const ssize_t written = ::write(descriptor, data, length);
        if (written < 0) {
            if (errno == EINTR) continue;
            return;
        }
        if (written == 0) return;
        data += written;
        length -= size_t(written);
    }
}
#endif

const char* program_name = "?";

#ifndef _WIN32
// ------------------------------------------------------------------ POSIX
std::atomic<int> reporting{0};

const char* signal_name(int number) {
    switch (number) {
        case SIGSEGV: return "SIGSEGV";
        case SIGBUS: return "SIGBUS";
        case SIGILL: return "SIGILL";
        case SIGFPE: return "SIGFPE";
        case SIGABRT: return "SIGABRT";
        case SIGSYS: return "SIGSYS";
        case SIGTRAP: return "SIGTRAP";
        default: return "SIGNAL";
    }
}

// The faulting instruction's own account: the address for a data fault (guest
// memory is one host mapping, so for the guest's own faults this is a guest
// address), the opcode for an illegal instruction, the operand for a division.
void append_fault_detail(int number, siginfo_t* info, void* context) {
    if (number == SIGSEGV || number == SIGBUS) {
        append_hex_field("address", uint64_t(reinterpret_cast<uintptr_t>(info->si_addr)));
        if (number == SIGSEGV) {
            if (info->si_code == SEGV_MAPERR) append_field("kind", "unmapped");
            else if (info->si_code == SEGV_ACCERR) append_field("kind", "no-permission");
        } else {
            if (info->si_code == BUS_ADRALN) append_field("kind", "misaligned");
            else if (info->si_code == BUS_ADRERR) append_field("kind", "no-such-address");
            else if (info->si_code == BUS_OBJERR) append_field("kind", "object-error");
        }
    } else if (number == SIGILL && context) {
        auto* const cpu = static_cast<ucontext_t*>(context);
#if defined(__aarch64__)
        append_hex_field("instruction", *reinterpret_cast<const uint32_t*>(cpu->uc_mcontext.pc));
#elif defined(__x86_64__)
        append_hex_field("instruction", *reinterpret_cast<const uint32_t*>(cpu->uc_mcontext.gregs[REG_RIP]));
#endif
    } else if (number == SIGFPE) {
        append_field("kind", info->si_code == FPE_INTDIV ? "integer-divide-by-zero"
                            : info->si_code == FPE_INTOVF ? "integer-overflow"
                            : info->si_code == FPE_FLTDIV ? "float-divide-by-zero" : "arithmetic");
    }
}

void append_guest_line() {
    append("CRASH guest_id=");
#if defined(SFR_CRASH_GUEST_STATE)
    append_uint(sfr::guest_identity());
    append_field("function", sfr::guest_function());
    append(" entry=0x");
    append_hex(sfr::guest_address());
    append(" lr=0x");
    append_hex(sfr::guest_return_address());
#else
    append("(no guest code)");
#endif
    append("\n");
}

[[noreturn]] void die_of(int number) {
    // The default action, so the exit status, the tombstone and the launcher's
    // view of the death are the ones the system would have given.
    ::signal(number, SIG_DFL);
    ::raise(number);
    ::_exit(128 + number);  // a signal that somehow does not kill the process
}

void handler(int number, siginfo_t* info, void* context) {
    if (reporting.exchange(1) != 0) die_of(number);  // a second fault, or one inside this handler
    report_size = 0;
    // The trace is buffered (1 MiB, flushed twice a second by a thread of its
    // own), so what it holds is written out first: the report must be the last
    // thing in the log, not in front of lines that were older than the crash.
    // fflush takes the stream's lock, which the flush thread holds only while
    // it writes; if that ever blocked, the log would say nothing at all, which
    // is what it does today.
    std::fflush(stderr);
    append("CRASH program=");
    append(program_name);
    append_field("signal", signal_name(number));
    append("(0x");
    append_hex(uint64_t(number));
    append(")");
    if (info) {
        append(" code=0x");
        append_hex(uint64_t(info->si_code));
        append_fault_detail(number, info, context);
    }
    append("\n");
    append_guest_line();

    // Every frame, outermost first: the system's and the JVM's come first and
    // ours last, so the interesting end is the tail. module+offset is what
    // addr2line and ndk-stack need; a release build keeps our own symbol names,
    // and the thirty thousand generated functions are named by the guest line
    // above instead.
    void* frames[64];
    const int count = ::backtrace(frames, int(sizeof frames / sizeof frames[0]));
    for (int index = 0; index < count; ++index) {
        const uintptr_t address = reinterpret_cast<uintptr_t>(frames[index]);
        append("CRASH frame ");
        append_uint(uint64_t(index));
        append(" +0x");
        append_hex(address);
        Dl_info symbol{};
        if (::dladdr(frames[index], &symbol) && symbol.dli_fname) {
            const char* const slash = std::strrchr(symbol.dli_fname, '/');
            append_field("module", slash ? slash + 1 : symbol.dli_fname);
            append_hex_field("base", uint64_t(reinterpret_cast<uintptr_t>(symbol.dli_fbase)));
            append_hex_field("offset", uint64_t(address) - uint64_t(reinterpret_cast<uintptr_t>(symbol.dli_fbase)));
            if (symbol.dli_sname) append_field("symbol", symbol.dli_sname);
        } else {
            append_field("module", "?");
        }
        append("\n");
    }
    // The launcher's stopped page shows the last lines of the log, so the
    // reason is written once more at the end, where they are read.
    append("CRASH end signal=");
    append(signal_name(number));
    append("(0x");
    append_hex(uint64_t(number));
    append(")");
#if defined(SFR_CRASH_GUEST_STATE)
    append(" function=");
    append(sfr::guest_function());
    append(" entry=0x");
    append_hex(sfr::guest_address());
    append(" lr=0x");
    append_hex(sfr::guest_return_address());
#endif
    append("\n");
    write_all(2, report, report_size);
#if defined(__ANDROID__)
    __android_log_write(ANDROID_LOG_FATAL, "FreeRiders", report);
#endif
    die_of(number);
}
#endif

}  // namespace

namespace sfr {

void install_crash_reporter(const char* program, uint64_t guest_backing_budget) {
    static bool installed = false;
    program_name = program && *program ? program : "?";
    if (installed) return;
    installed = true;
    report_environment(program_name, guest_backing_budget);
#ifndef _WIN32
    struct sigaction action {};
    action.sa_sigaction = handler;
    // SA_ONSTACK: a stack overflow faults inside the handler unless it has a
    // stack of its own. SA_NODEFER: a fault inside the handler is delivered
    // again rather than blocked, and reporting then turns it into the default
    // death instead of a hang.
    action.sa_flags = SA_SIGINFO | SA_ONSTACK | SA_NODEFER;
    ::sigemptyset(&action.sa_mask);
    static char alternate_stack[64 * 1024];
    stack_t alternate{};
    alternate.ss_sp = alternate_stack;
    alternate.ss_size = sizeof alternate_stack;
    alternate.ss_flags = 0;
    ::sigaltstack(&alternate, nullptr);
    for (const int number : {SIGSEGV, SIGBUS, SIGILL, SIGFPE, SIGABRT, SIGSYS, SIGTRAP})
        ::sigaction(number, &action, nullptr);
#endif
}

void report_environment(const char* program, uint64_t guest_backing_budget) {
    char abi[16] = "unknown";
#if defined(__aarch64__)
    std::strcpy(abi, "arm64-v8a");
#elif defined(__arm__)
    std::strcpy(abi, "armeabi-v7a");
#elif defined(__x86_64__) || defined(_M_X64)
    std::strcpy(abi, "x86_64");
#elif defined(__i386__) || defined(_M_IX86)
    std::strcpy(abi, "x86");
#elif defined(__riscv) && __riscv_xlen == 64
    std::strcpy(abi, "riscv64");
#endif
    unsigned long page_size = 4096;
    unsigned long long memory_bytes = 0;
    int cpus = 0;
#if defined(__ANDROID__)
    char model[128] = "?", manufacturer[128] = "?";
    char api[16] = "";
    // The property calls are deprecated in the newest NDK and still the only
    // ones every API level has.
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wdeprecated-declarations"
    char value[PROP_VALUE_MAX + 1];
    if (__system_property_get("ro.product.model", value) > 0) std::snprintf(model, sizeof model, "%s", value);
    if (__system_property_get("ro.product.manufacturer", value) > 0)
        std::snprintf(manufacturer, sizeof manufacturer, "%s", value);
    if (__system_property_get("ro.build.version.sdk", value) > 0) std::snprintf(api, sizeof api, "%s", value);
#pragma GCC diagnostic pop
    std::fprintf(stderr, "NATIVE_DEVICE program=%s abi=%s api=%s model=%s manufacturer=%s",
                 program && *program ? program : "?", abi, api[0] ? api : "?", model, manufacturer);
#else
    std::fprintf(stderr, "NATIVE_DEVICE program=%s abi=%s", program && *program ? program : "?", abi);
#endif
    // What the runtime then asks of the machine: the host page size this
    // process got (the guest's ranges are aligned to it), the memory it may use
    // and the guest's own budget.
#if defined(_WIN32)
    SYSTEM_INFO info{};
    ::GetSystemInfo(&info);
    page_size = info.dwPageSize;
    cpus = int(info.dwNumberOfProcessors);
    MEMORYSTATUSEX status{sizeof(MEMORYSTATUSEX)};
    if (::GlobalMemoryStatusEx(&status)) memory_bytes = status.ullTotalPhys;
#else
    const long size = ::sysconf(_SC_PAGESIZE);
    if (size > 0) page_size = (unsigned long)size;
    const long processors = ::sysconf(_SC_NPROCESSORS_ONLN);
    if (processors > 0) cpus = int(processors);
    if (FILE* meminfo = std::fopen("/proc/meminfo", "r")) {
        char line[256];
        while (std::fgets(line, sizeof line, meminfo)) {
            unsigned long long kilobytes = 0;
            if (std::sscanf(line, "MemTotal: %llu kB", &kilobytes) == 1) {
                memory_bytes = kilobytes * 1024ull;
                break;
            }
        }
        std::fclose(meminfo);
    }
#endif
    std::fprintf(stderr, " page_size=%lu memory_mb=%llu cpus=%d guest_budget_mb=%llu\n", page_size,
                 memory_bytes / (1024ull * 1024ull), cpus, guest_backing_budget / (1024ull * 1024ull));
    if (page_size != 4096)
        std::fprintf(stderr, "NATIVE_PAGE_SIZE_NOTE page_size=%lu expected=4096"
                             " host_page_alignment=unsupported\n",
                     page_size);
    std::fflush(stderr);
}

}  // namespace sfr
