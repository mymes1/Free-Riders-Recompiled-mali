// The crash reporter (src/crash_report.cpp): a process that dies of a signal
// leaves the reason, the guest function it was in and a stack trace in the log
// the launcher shows, and says once what the machine is.
//
// The report is checked by crashing a child of this test: the handler is
// installed there, stderr is the file the checks read, and the parent sees the
// death the way the system would (the child is killed by the same signal).
// Windows is not covered: install_crash_reporter documents what it does there.
#ifdef _WIN32

#include <cstdio>
int main() {
    std::puts("crash report: not covered on Windows (the device is the target)");
    return 0;
}

#else

#include "crash_report.h"

#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <stdexcept>
#include <string>
#include <sys/wait.h>
#include <unistd.h>

namespace sfr {
// What the game runtime answers in src/diagnostic_main.cpp, where the guest's
// entry state and the generated context are visible. The test supplies its own
// so the report's guest line is checked against known values.
uint32_t guest_identity() { return 7; }
const char* guest_function() { return "sub_8250C848"; }
uint32_t guest_address() { return 0x8250C848; }
uint32_t guest_return_address() { return 0x8250E6E8; }
}

namespace {
void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

std::string read_file(const std::string& path) {
    std::ifstream in(path, std::ios::binary);
    if (!in) throw std::runtime_error("the child left no log");
    return std::string(std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>());
}

size_t count_of(const std::string& text, const std::string& needle) {
    size_t count = 0, at = 0;
    while ((at = text.find(needle, at)) != std::string::npos) {
        ++count;
        at += needle.size();
    }
    return count;
}

// A child that logs the way the runtime does -- 1 MiB of buffering, a line
// written but not yet flushed when the signal arrives -- installs the reporter
// twice, and dies of SIGSEGV.
void crash_into(const std::string& path) {
    const pid_t child = fork();
    require(child >= 0, "cannot fork");
    if (child == 0) {
        if (!std::freopen(path.c_str(), "w", stderr)) ::_exit(90);
        static char buffer[1 << 16];
        std::setvbuf(stderr, buffer, _IOFBF, sizeof buffer);
        std::fputs("PENDING runtime line\n", stderr);
        sfr::install_crash_reporter("game", 0x20000000ull);
        sfr::install_crash_reporter("game", 0x20000000ull);  // once is enough
        ::raise(SIGSEGV);
        ::_exit(91);  // not reached: the reporter dies of the signal
    }
    int status = 0;
    require(::waitpid(child, &status, 0) == child, "cannot wait for the child");
    // The death is the system's own: the exit status the launcher and the
    // tombstone machinery see is unchanged.
    require(WIFSIGNALED(status), "the reporter must die of the signal it handled");
    require(WTERMSIG(status) == SIGSEGV, "and of the same signal");
}
}  // namespace

int main() {
    try {
        const std::string path = "/tmp/sfr-crash-report-" + std::to_string(::getpid()) + ".log";
        crash_into(path);
        const std::string text = read_file(path);
        std::remove(path.c_str());

        // What the run is on, once, before anything else.
        require(count_of(text, "NATIVE_DEVICE program=game") == 1, "the environment line is written once");
        require(text.find("abi=") != std::string::npos, "the environment line names the ABI");
        require(text.find("page_size=") != std::string::npos, "the environment line names the host page size");
        require(text.find("guest_budget_mb=512") != std::string::npos, "the environment line names the guest budget");

        // Why it died, and where the guest was.
        require(text.find("CRASH program=game signal=SIGSEGV(0xb)") != std::string::npos,
                "the report names the signal");
        require(text.find("code=0x") != std::string::npos, "the report names the signal's code");
        require(text.find("CRASH guest_id=7 function=sub_8250C848 entry=0x8250c848 lr=0x8250e6e8") != std::string::npos,
                "the report names the guest function the crashing thread was in");
        require(text.find("CRASH frame 0 +0x") != std::string::npos, "the report carries a stack trace");
        require(text.find("CRASH end signal=SIGSEGV(0xb)") != std::string::npos,
                "the report ends with the reason again, where a stopped page reads it");
        require(text.find("function=sub_8250C848 entry=0x8250c848 lr=0x8250e6e8") != std::string::npos,
                "the end line repeats the guest function");

        // What was buffered when the signal arrived is flushed first: the
        // report is the last thing in the log, not in front of older lines.
        require(text.find("PENDING runtime line") != std::string::npos, "the buffered trace survives the crash");
        require(text.find("PENDING runtime line") < text.find("CRASH program="),
                "the report follows the lines that were already logged");
        require(text.rfind("CRASH end") != std::string::npos, "the report is the last thing in the log");
    } catch (const std::exception& error) {
        std::fprintf(stderr, "crash report: %s\n", error.what());
        return 1;
    }
    std::puts("crash report: all checks passed");
    return 0;
}

#endif
