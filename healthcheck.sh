#!/bin/sh
# Fail if healthcheck file is older than 60 seconds
FILE="/tmp/healthcheck"
[ -f "$FILE" ] || exit 1
LAST=$(cat "$FILE")
NOW=$(date +%s)
DIFF=$(echo "$NOW - ${LAST%.*}" | bc)
[ "$DIFF" -lt 60 ] || exit 1
