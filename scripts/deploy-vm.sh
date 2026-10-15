#!/bin/zsh
# Deploy changed source files from this Mac checkout to the Parallels VM and
# restart only the MCP listener. Usage:
#   scripts/deploy-vm.sh                 # files changed vs HEAD + untracked under src/ and lisp-code/
#   scripts/deploy-vm.sh path/a.py ...   # explicit repo-relative files
#   scripts/deploy-vm.sh --no-restart ...
# A hung `prlctl exec` blocks every later one, so the script refuses to start
# while one exists and bounds each VM call to 180 s.
set -euo pipefail
VM="Windows 11"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WIN_SRC='C:\Mac\Home\Desktop\Autocad mcp\AutoCAD-MCP'   # the same checkout via the Parallels share
WIN_DST='C:\Users\matincz\AutoCAD-MCP'
URL="http://10.211.55.4:8765/mcp"
restart=1
[[ "${1:-}" == "--no-restart" ]] && { restart=0; shift; }

stale=$(pgrep -fl 'prlctl exec' || true)
if [[ -n "$stale" ]]; then
  echo "stale prlctl exec processes would block the VM channel; kill them first (kill -9):" >&2
  print -r -- "$stale" >&2; exit 1
fi

cd "$REPO"
if (( $# )); then files=("$@"); else
  files=(${(f)"$( { git diff --name-only HEAD; git ls-files --others --exclude-standard; } \
    | grep -E '^(src/|lisp-code/)' | grep -Ev '__pycache__' | sort -u )"})
fi
(( ${#files} )) || { echo "nothing to deploy"; exit 0; }

# Run PowerShell in the VM as SYSTEM: it reaches C:\\Mac\\Home, can read the elevated
# server's command line, and avoids `prlctl --current-user`, which can hang
# indefinitely. Errors come back as CLIXML on stderr; surface them.
vmps() {
  local script="$1" enc out
  enc=$(printf '%s' "$script" | iconv -f utf-8 -t utf-16le | base64)
  out=$(perl -e 'alarm shift; exec @ARGV' 180 prlctl exec "$VM" powershell -NoProfile -EncodedCommand "$enc" 2>&1) || true
  [[ -n "$out" ]] || { echo "VM command produced no output (timed out?)" >&2; return 1; }
  print -r -- "$out" | grep -v '^#< CLIXML' | sed -e 's/<Objs.*//' -e '/^$/d'
  if print -r -- "$out" | grep -q '<S S="Error">'; then
    print -r -- "$out" | grep -o '<S S="Error">[^<]*' | sed 's/<S S="Error">//; s/_x000D__x000A_//g' | head -5 >&2
    return 1
  fi
}

t0=$SECONDS
stamp=$(date +%Y%m%d-%H%M%S)
# One VM call per file: -EncodedCommand arguments of a few KB already make prlctl hang.
bak="C:\\Users\\matincz\\AppData\\Local\\AutoCAD-MCP\\backup-$stamp"
for f in $files; do
  w=${f//\//\\}
  ps="\$ErrorActionPreference='Stop'; \$d='$WIN_DST\\$w'"
  ps+="; if (Test-Path \$d) { \$b=Join-Path '$bak' '$w'; New-Item -ItemType Directory -Force (Split-Path \$b) | Out-Null; Copy-Item \$d \$b }"
  ps+="; New-Item -ItemType Directory -Force (Split-Path \$d) | Out-Null; Copy-Item '$WIN_SRC\\$w' \$d -Force"
  ps+="; (Get-FileHash \$d -Algorithm SHA256).Hash.ToLower()"
  remote=$(vmps "$ps")
  [[ "$remote" == *"$(shasum -a 256 "$f" | cut -d' ' -f1)"* ]] || { echo "hash mismatch after copy: $f" >&2; exit 1; }
done
echo "copied ${#files} file(s), backup-$stamp  [$((SECONDS - t0))s]"

vmps "Set-Location '$WIN_DST'; & .\\.venv\\Scripts\\python.exe -c \"import autocad_mcp.server; print('IMPORT_OK')\"" | grep -q IMPORT_OK \
  || { echo "import check failed in VM; server NOT restarted" >&2; exit 1; }
echo "import check ok  [$((SECONDS - t0))s]"

(( restart )) || exit 0
# Run the restart script by path: its encoded text is too long for prlctl.
# (loaded as a script block, so the VM's execution policy stays untouched).
vmps "& ([scriptblock]::Create((Get-Content -Raw -Encoding UTF8 '$WIN_SRC\\scripts\\vm-safe-restart.ps1')))"
for i in {1..40}; do
  [[ "$(curl -s -m 3 -o /dev/null -w '%{http_code}' "$URL")" == 406 ]] && { echo "MCP up  [$((SECONDS - t0))s total]; reconnect the client (session expires)"; exit 0; }
  sleep 2
done
echo "MCP did not come back on $URL" >&2; exit 1
