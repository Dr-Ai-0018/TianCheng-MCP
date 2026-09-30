#!/usr/bin/env bash
# Offline Pi filesystem-boundary prototype for Ubuntu WSL with bubblewrap.
# Run inside WSL: bash scripts/probe_pi_wsl_isolation.sh /path/to/pi/dist/bundle
set -euo pipefail

bundle=${1:?Pass the compiled Pi dist/bundle directory}
test -f "$bundle/cli.js"
pi_source=$(realpath "$bundle/../../../..")
test -d "$pi_source/node_modules"
command -v bwrap >/dev/null
command -v node >/dev/null

scratch=$(mktemp -d -p /tmp tc-pi-probe.XXXXXXXX)
case "$scratch" in /tmp/tc-pi-probe.*) ;; *) echo 'Unsafe scratch path' >&2; exit 1 ;; esac
trap 'rm -rf -- "$scratch"' EXIT
mkdir "$scratch/workspace"
printf 'allowed\n' > "$scratch/workspace/allowed.txt"
printf 'outside-canary\n' > "$scratch/outside.txt"
ln -s "$scratch/outside.txt" "$scratch/workspace/outside-link"
ln -s "$scratch" "$scratch/workspace/escape-dir"

common=(
  --unshare-user --unshare-pid --unshare-ipc --unshare-uts --unshare-net
  --die-with-parent --clearenv
  --setenv PATH /usr/bin:/bin --setenv HOME /tmp
  --setenv PI_CLI_ENTRY "$bundle/cli.js"
  --setenv OUTSIDE_CANARY "$scratch/outside.txt"
  --ro-bind /usr /usr --ro-bind /lib /lib --ro-bind /lib64 /lib64
  # Local compiled Pi still resolves workspace package symlinks to its source.
  # This extra read-only mount is acceptable for this offline probe only.
  --ro-bind "$pi_source" "$pi_source"
  --dev /dev --tmpfs /tmp --chdir /workspace
)

bwrap "${common[@]}" --ro-bind "$scratch/workspace" /workspace \
  /usr/bin/sh -ec '
    test "$(cat /workspace/allowed.txt)" = allowed
    test ! -e /mnt/c && test ! -e /mnt/e/0software
    test ! -e /proc/self/environ
    test ! -e "$OUTSIDE_CANARY"
    if cat "$OUTSIDE_CANARY" >/dev/null 2>&1; then exit 10; fi
    if cat /workspace/outside-link >/dev/null 2>&1; then exit 14; fi
    test -r "$PI_CLI_ENTRY"
    if (printf blocked > /workspace/forbidden.txt) 2>/dev/null; then exit 11; fi
    if (printf blocked > "$PI_CLI_ENTRY") 2>/dev/null; then exit 12; fi
  '
test ! -e "$scratch/workspace/forbidden.txt"
test "$(cat "$scratch/outside.txt")" = outside-canary

bwrap "${common[@]}" --bind "$scratch/workspace" /workspace \
  /usr/bin/sh -ec '
    test ! -e /mnt/c && test ! -e /mnt/e/0software
    test ! -e /proc/self/environ
    test ! -e "$OUTSIDE_CANARY"
    if cat /workspace/outside-link >/dev/null 2>&1; then exit 15; fi
    printf permitted > /workspace/created.txt
    if (printf blocked > /workspace/escape-dir/from-inside.txt) 2>/dev/null; then exit 16; fi
    if (printf blocked > "$PI_CLI_ENTRY") 2>/dev/null; then exit 13; fi
  '
test "$(cat "$scratch/workspace/created.txt")" = permitted
test "$(cat "$scratch/outside.txt")" = outside-canary

# Verify the actual compiled Pi entry starts with only the pinned bundle and
# the Linux runtime visible. No API key, model request, or network is involved.
bwrap "${common[@]}" --ro-bind "$scratch/workspace" /workspace \
  /usr/bin/node "$bundle/cli.js" --help >/dev/null

printf '%s\n' 'Pi WSL filesystem probe: PASS (read-only, write-scoped, unrelated host paths hidden, Pi starts)'
