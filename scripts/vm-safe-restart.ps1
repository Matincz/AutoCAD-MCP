# Restart only the AutoCAD-MCP HTTP listener; never touch AutoCAD.
# Run as SYSTEM (prlctl exec without --current-user): a non-elevated user cannot
# read the elevated server's command line. Never use taskkill /T here: AutoCAD's
# dead launcher PID can be reused by the server, so a tree kill may take AutoCAD.
$ErrorActionPreference = 'Stop'
$task = 'AutoCAD-MCP HTTP'
$procs = @{}
Get-CimInstance Win32_Process | ForEach-Object { $procs[[int]$_.ProcessId] = $_ }
$listener = @(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)
if ($listener.Count -gt 1) { throw "more than one listener on 8765" }
if ($listener.Count -eq 1) {
    $listenerPid = [int]$listener[0].OwningProcess
    if ($procs[$listenerPid].CommandLine -notmatch '(?i)autocad-mcp') { throw "listener $listenerPid is not autocad-mcp" }
    foreach ($cad in ($procs.Values | Where-Object Name -eq 'acad.exe')) {
        $cursor = [int]$cad.ProcessId; $seen = @{}
        while ($procs.ContainsKey($cursor) -and -not $seen[$cursor]) {
            $seen[$cursor] = 1
            if ($cursor -eq $listenerPid) { throw "acad $($cad.ProcessId) descends from listener; refusing" }
            $cursor = [int]$procs[$cursor].ParentProcessId
        }
    }
} else { $listenerPid = 0 }
$cadBefore = @(Get-Process acad -ErrorAction SilentlyContinue | ForEach-Object { "$($_.Id)@$($_.StartTime.Ticks)" }) -join ','
schtasks /end /tn $task | Out-Null
if ($listenerPid) { Stop-Process -Id $listenerPid -Force -ErrorAction SilentlyContinue }
for ($i = 0; $i -lt 20 -and (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue); $i++) { Start-Sleep 1 }
if (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue) { throw "port 8765 still listening" }
schtasks /run /tn $task | Out-Null
$cadAfter = @(Get-Process acad -ErrorAction SilentlyContinue | ForEach-Object { "$($_.Id)@$($_.StartTime.Ticks)" }) -join ','
if ($cadBefore -ne $cadAfter) { throw "AutoCAD processes changed: before=$cadBefore after=$cadAfter" }
"RESTARTED old_listener=$listenerPid cad=$cadAfter"
