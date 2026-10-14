using Autodesk.AutoCAD.Internal;
using AcApplication = Autodesk.AutoCAD.ApplicationServices.Core.Application;

namespace AutoCADMcp.Plugin;

// AutoCAD 2025 uploads every opened/saved file to the Autodesk "shared recent
// files" service from a thread-pool task.  Its first step, WSUtils.IsLoggedIn,
// can crash the whole process with an AccessViolationException in
// acrxLoadModule (observed repeatedly on open, save and close).  Replacing the
// callback with a no-op disables only that cloud upload; local recent files
// are unaffected.  Set AUTOCAD_MCP_KEEP_SHARED_RECENT_FILES=1 to keep it.
internal static class SharedRecentFilesGuard
{
    internal static void Install()
    {
        if (Environment.GetEnvironmentVariable("AUTOCAD_MCP_KEEP_SHARED_RECENT_FILES") == "1")
        {
            return;
        }
        Disable();
        // AcWindows registers its handler during its own initialization, which
        // can run after this plugin; replace it again once startup is idle.
        AcApplication.Idle += OnFirstIdle;
    }

    private static void OnFirstIdle(object? sender, EventArgs args)
    {
        AcApplication.Idle -= OnFirstIdle;
        Disable();
    }

    private static void Disable()
    {
        try
        {
            SharedRecentFiles.RecentFileCallback = (_, _) => { };
            SharedRecentFiles.GetRecentFilesCallback = (_, _) => { };
        }
        catch (System.Exception error)
        {
            System.Diagnostics.Trace.WriteLine(
                $"AutoCAD-MCP: could not disable shared recent files: {error}");
        }
    }
}
