package com.freeriders.recompiled;

import android.app.Activity;
import android.app.ActivityManager;
import android.app.ApplicationExitInfo;
import android.os.Build;
import android.util.Log;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.List;

// Why the game's process ended, as only the system knows it.
//
// The game runs in a process of its own (android:process=":game") and the
// launcher survives it. The runtime's own crash reporter
// (src/crash_report.cpp) explains a death by signal from inside the process;
// nothing in the process can explain the other deaths, and game.log then just
// stops, mid-work, with no reason:
//
//   - the low-memory killer taking it (a tablet with 4 GB of memory and a
//     512 MB guest at 60 frames a second is exactly where that happens),
//   - the graphics driver aborting the process after a GPU fault,
//   - the system stopping it, or the process being judged excessive.
//
// Android 11 (API 30) and newer keep all of them: the reason, the exit status,
// the memory the process last held and, for a native crash, the tombstone's own
// text. This writes them to exit-report.txt in the app's files directory; the
// launcher appends that to game.log and shows it on its stopped page, so the
// one file a player sends says why the game died.
final class GameExitReport {
    private GameExitReport() {
    }

    // Writes the report for a `:game` process that ended after `started_at`,
    // and answers where it went, or null when there is nothing to write.
    static String capture(Activity activity, long started_at) {
        if (Build.VERSION.SDK_INT < 30) return null;
        File directory = activity.getExternalFilesDir(null);
        if (directory == null) return null;
        try {
            ActivityManager manager = (ActivityManager) activity.getSystemService(Activity.ACTIVITY_SERVICE);
            if (manager == null) return null;
            List<ApplicationExitInfo> exits =
                manager.getHistoricalProcessExitReasons(activity.getPackageName(), 0, 4);
            ApplicationExitInfo newest = null;
            for (ApplicationExitInfo exit : exits) {
                if (exit == null) continue;
                if (!(activity.getPackageName() + ":game").equals(exit.getProcessName())) continue;
                // Only this launcher session's death: the system keeps days of
                // them, and an old one would be shown as this run's.
                if (exit.getTimestamp() < started_at) continue;
                if (newest == null || exit.getTimestamp() > newest.getTimestamp()) newest = exit;
            }
            if (newest == null) return null;
            StringBuilder report = new StringBuilder();
            // The tombstone (a native crash) or the trace (an ANR), first: the
            // summary below is the last line, which is what a viewer showing
            // the end of the log shows.
            try (InputStream trace = newest.getTraceInputStream()) {
                if (trace != null) {
                    byte[] buffer = new byte[8192];
                    for (int total = 0, read; total < 64 * 1024 && (read = trace.read(buffer)) > 0; total += read)
                        report.append(new String(buffer, 0, read, StandardCharsets.UTF_8));
                    if (report.length() > 0 && report.charAt(report.length() - 1) != '\n') report.append('\n');
                }
            } catch (Exception noTrace) {
                // No trace for this kind of death, or it was already consumed.
            }
            report.append("EXIT reason=").append(reason_name(newest.getReason()))
                  .append(" status=").append(newest.getStatus());
            if (newest.getReason() == ApplicationExitInfo.REASON_SIGNALED
                || newest.getReason() == ApplicationExitInfo.REASON_CRASH_NATIVE)
                report.append(" signal=").append(signal_name(newest.getStatus()));
            report.append(" timestamp=").append(newest.getTimestamp());
            long rss = newest.getRss(), pss = newest.getPss();
            if (rss > 0) report.append(" rss_mb=").append(rss >> 20);
            if (pss > 0) report.append(" pss_mb=").append(pss >> 20);
            String description = newest.getDescription();
            if (description != null && !description.isEmpty())
                report.append(" description=").append(description.trim().replace('\n', ' '));
            report.append('\n');
            File file = new File(directory, "exit-report.txt");
            File partial = new File(directory, "exit-report.txt.partial");
            try (OutputStream out = new FileOutputStream(partial)) {
                out.write(report.toString().getBytes(StandardCharsets.UTF_8));
            } catch (Exception error) {
                partial.delete();
                return null;
            }
            if (!partial.renameTo(file)) {
                partial.delete();
                return null;
            }
            return file.getAbsolutePath();
        } catch (Exception error) {
            // A launcher that cannot read the reasons still launches the game.
            Log.w("FreeRiders", "cannot read the exit reasons", error);
            return null;
        }
    }

    // ApplicationExitInfo's reason codes (API 30). Spelled out rather than
    // read from the class, which does not exist before API 30: these are the
    // same values, and the line is what a player sends.
    private static String reason_name(int reason) {
        switch (reason) {
            case ApplicationExitInfo.REASON_EXIT_SELF: return "EXIT_SELF";
            case ApplicationExitInfo.REASON_SIGNALED: return "SIGNALED";
            case ApplicationExitInfo.REASON_LOW_MEMORY: return "LOW_MEMORY";
            case ApplicationExitInfo.REASON_CRASH: return "CRASH";
            case ApplicationExitInfo.REASON_CRASH_NATIVE: return "CRASH_NATIVE";
            case ApplicationExitInfo.REASON_ANR: return "ANR";
            case ApplicationExitInfo.REASON_INITIALIZATION_FAILURE: return "INITIALIZATION_FAILURE";
            case ApplicationExitInfo.REASON_PERMISSION_CHANGE: return "PERMISSION_CHANGE";
            case ApplicationExitInfo.REASON_EXCESSIVE_RESOURCE_USAGE: return "EXCESSIVE_RESOURCE_USAGE";
            case ApplicationExitInfo.REASON_USER_REQUESTED: return "USER_REQUESTED";
            case ApplicationExitInfo.REASON_USER_STOPPED: return "USER_STOPPED";
            case ApplicationExitInfo.REASON_DEPENDENCY_DIED: return "DEPENDENCY_DIED";
            case ApplicationExitInfo.REASON_OTHER: return "OTHER";
            case ApplicationExitInfo.REASON_FREEZER: return "FREEZER";
            case ApplicationExitInfo.REASON_PACKAGE_STATE_CHANGE: return "PACKAGE_STATE_CHANGE";
            case ApplicationExitInfo.REASON_PACKAGE_UPDATED: return "PACKAGE_UPDATED";
            default: return "UNKNOWN(" + reason + ")";
        }
    }

    // The signal a process died of, when the system says which. The kernel's
    // numbers are the same on every Android ABI.
    private static String signal_name(int signal) {
        switch (signal) {
            case 4: return "SIGILL(" + signal + ")";
            case 5: return "SIGTRAP(" + signal + ")";
            case 6: return "SIGABRT(" + signal + ")";
            case 7: return "SIGBUS(" + signal + ")";
            case 8: return "SIGFPE(" + signal + ")";
            case 9: return "SIGKILL(" + signal + ")";
            case 11: return "SIGSEGV(" + signal + ")";
            case 13: return "SIGPIPE(" + signal + ")";
            case 15: return "SIGTERM(" + signal + ")";
            case 31: return "SIGSYS(" + signal + ")";
            default: return "(" + signal + ")";
        }
    }
}
