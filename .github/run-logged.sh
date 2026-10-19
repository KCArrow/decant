#!/usr/bin/env bash
# Run a command. If it fails, repeat the end of its output as a GitHub error
# annotation, so the cause shows on the run summary and through the API
# without opening the full log.
log=$(mktemp)
"$@" 2>&1 | tee "$log"
rc=${PIPESTATUS[0]}
if [ "$rc" -ne 0 ]; then
    msg=$(tail -n 60 "$log" | sed -e 's/%/%25/g' -e 's/\r/%0D/g' | awk 'BEGIN { ORS = "%0A" } { print }')
    echo "::error title=$(basename "$1") failed with exit code $rc::$msg"
fi
exit "$rc"
