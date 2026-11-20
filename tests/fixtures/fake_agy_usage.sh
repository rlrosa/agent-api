#!/usr/bin/env bash
set -e

if [ -n "$DBUS_SESSION_BUS_ADDRESS" ]; then
    echo "ERROR: DBUS_SESSION_BUS_ADDRESS leaked into agy_quota subprocess" >&2
    exit 2
fi

if [ "$SSH_CONNECTION" != "1" ]; then
    echo "ERROR: SSH_CONNECTION=1 not set in agy_quota subprocess" >&2
    exit 3
fi

if [ -n "$COUNTER_FILE" ]; then
    count=0
    if [ -f "$COUNTER_FILE" ]; then
        count=$(cat "$COUNTER_FILE")
    fi
    echo $((count + 1)) > "$COUNTER_FILE"
fi

if [ "$SIMULATE_FAIL" = "1" ]; then
    echo "Simulated agy failure" >&2
    exit 1
fi

cat << 'EOF'
Gemini Models	Weekly Limit Remaining	80%	2026-11-14T06:29:14Z
Gemini Models	Five Hour Limit Remaining	92%	2026-11-09T03:29:14Z
EOF
