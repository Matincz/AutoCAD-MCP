using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using AcApplication = Autodesk.AutoCAD.ApplicationServices.Core.Application;

namespace AutoCADMcp.Plugin;

internal sealed class NativeDocumentState
{
    internal string DocId { get; } = $"acad-{Guid.NewGuid():N}";
    internal long Revision { get; set; }
    internal int SuppressEvents { get; set; }
    // Database.SaveAs writes the file but does not rename the open document.
    internal string? SavedPath { get; set; }
    // Kept so the handlers can be detached before the document is destroyed.
    internal Database? Database { get; set; }
    internal ObjectEventHandler? OnAppended { get; set; }
    internal ObjectEventHandler? OnModified { get; set; }
    internal ObjectErasedEventHandler? OnErased { get; set; }
}

internal sealed class DocumentRegistry
{
    private readonly object gate = new();
    private readonly Dictionary<Document, NativeDocumentState> documents = new();

    internal string SessionId { get; } = $"native-{Guid.NewGuid():N}";

    internal DocumentRegistry()
    {
        AcApplication.DocumentManager.DocumentCreated += (_, args) => Guard(() => Register(args.Document));
        AcApplication.DocumentManager.DocumentToBeDestroyed += (_, args) => Guard(() => Unregister(args.Document));
        foreach (Document document in AcApplication.DocumentManager)
        {
            Register(document);
        }
    }

    internal NativeDocumentState Register(Document document)
    {
        lock (gate)
        {
            if (documents.TryGetValue(document, out NativeDocumentState? current))
            {
                return current;
            }
            var state = new NativeDocumentState();
            documents[document] = state;
            Database database = document.Database;
            state.Database = database;
            state.OnAppended = (_, _) => Touch(state);
            state.OnModified = (_, _) => Touch(state);
            state.OnErased = (_, _) => Touch(state);
            database.ObjectAppended += state.OnAppended;
            database.ObjectModified += state.OnModified;
            database.ObjectErased += state.OnErased;
            return state;
        }
    }

    internal Document Resolve(string? docId)
    {
        lock (gate)
        {
            if (docId is null)
            {
                return AcApplication.DocumentManager.MdiActiveDocument
                    ?? throw new CadProtocolException("E_NO_ACTIVE_DOCUMENT", "AutoCAD has no active document");
            }
            foreach ((Document document, NativeDocumentState state) in documents)
            {
                if (state.DocId == docId)
                {
                    return document;
                }
            }
        }
        throw new CadProtocolException("E_DOCUMENT_ID_MISMATCH", $"Unknown document id: {docId}");
    }

    internal NativeDocumentState State(Document document) => Register(document);

    private static void Guard(Action action)
    {
        try
        {
            action();
        }
        catch (System.Exception error)
        {
            System.Diagnostics.Trace.WriteLine($"AutoCAD-MCP: document event handler failed: {error}");
        }
    }

    // Called from DocumentToBeDestroyed.  The database keeps raising object
    // events while the document is torn down; a handler that still referenced
    // the document re-registered it and dereferenced the disposed wrapper,
    // crashing AutoCAD with a null-pointer access violation.
    private void Unregister(Document document)
    {
        NativeDocumentState? state;
        lock (gate)
        {
            if (!documents.Remove(document, out state))
            {
                return;
            }
        }
        try
        {
            if (state.Database is { IsDisposed: false } database)
            {
                database.ObjectAppended -= state.OnAppended;
                database.ObjectModified -= state.OnModified;
                database.ObjectErased -= state.OnErased;
            }
        }
        catch (System.Exception error)
        {
            System.Diagnostics.Trace.WriteLine($"AutoCAD-MCP: detaching document events failed: {error}");
        }
        state.Database = null;
    }

    internal void Validate(Document document, RpcRequest request)
    {
        NativeDocumentState state = State(document);
        if (request.SessionId is not null && request.SessionId != SessionId)
        {
            throw new CadProtocolException(
                "E_SESSION_GENERATION_MISMATCH",
                "The native AutoCAD session changed",
                recommendedAction: "read_document_context_and_retry");
        }
        if (request.DocId is not null && request.DocId != state.DocId)
        {
            throw new CadProtocolException("E_DOCUMENT_ID_MISMATCH", "The requested document is not active");
        }
        if (!ReferenceEquals(AcApplication.DocumentManager.MdiActiveDocument, document))
        {
            throw new CadProtocolException(
                "E_DOCUMENT_ID_MISMATCH",
                "The requested document is not the active AutoCAD document",
                recommendedAction: "activate_the_requested_document_and_retry",
                details: Context(document));
        }
        if (request.ExpectedRevision is null || request.ExpectedRevision.Value != state.Revision)
        {
            throw new CadProtocolException(
                "E_DOCUMENT_REVISION_MISMATCH",
                "The requested document revision is stale",
                details: Context(document));
        }
    }

    internal Dictionary<string, object?> Context(Document document)
    {
        NativeDocumentState state = State(document);
        string activePath = state.SavedPath
            ?? (string.IsNullOrWhiteSpace(document.Database.Filename)
                ? document.Name
                : document.Database.Filename);
        return new Dictionary<string, object?>
        {
            ["sessionId"] = SessionId,
            ["workerGeneration"] = Environment.ProcessId,
            ["workerProcessId"] = Environment.ProcessId,
            ["docId"] = state.DocId,
            ["activeDocId"] = state.DocId,
            ["revision"] = state.Revision,
            ["requestedPath"] = string.IsNullOrWhiteSpace(activePath) ? null : activePath,
            ["activePath"] = string.IsNullOrWhiteSpace(activePath) ? null : activePath,
            ["documentName"] = document.Name,
            ["revisionSource"] = "native-database-events",
        };
    }

    internal void RecordSavedPath(Document document, string path)
    {
        lock (gate)
        {
            State(document).SavedPath = path;
        }
    }

    internal void BeginMutation(Document document)
    {
        lock (gate)
        {
            State(document).SuppressEvents += 1;
        }
    }

    internal void EndMutation(Document document, bool committed)
    {
        lock (gate)
        {
            NativeDocumentState state = State(document);
            state.SuppressEvents = Math.Max(0, state.SuppressEvents - 1);
            if (committed)
            {
                state.Revision += 1;
            }
        }
    }

    // Runs inside AutoCAD's database reactors: never touch the Document or
    // Database here and never let an exception escape into AutoCAD.
    private void Touch(NativeDocumentState state)
    {
        try
        {
            lock (gate)
            {
                if (state.Database is not null && state.SuppressEvents == 0)
                {
                    state.Revision += 1;
                }
            }
        }
        catch (System.Exception error)
        {
            System.Diagnostics.Trace.WriteLine($"AutoCAD-MCP: revision update failed: {error}");
        }
    }
}
