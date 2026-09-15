#!/bin/bash
# Prompt for a Gmail App Password, sanitize it, and REPLACE the line in .env.
# Replaces rather than appends so repeated attempts cannot stack duplicates.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

echo "SMTP_USER in .env is: $(grep '^SMTP_USER=' .env | cut -d= -f2)"
echo "The App Password must belong to THAT Google account."
echo

read -rs -p "App password (input hidden): " RAW
echo

CLEAN="$(printf '%s' "$RAW" | tr -d '[:space:]')"
LEN=${#CLEAN}

# A double-paste produces the same 16 characters twice; keep one copy.
if [ "$LEN" -eq 32 ] && [ "${CLEAN:0:16}" = "${CLEAN:16:16}" ]; then
  CLEAN="${CLEAN:0:16}"
  echo "note: input was pasted twice, using one copy"
  LEN=16
fi

if [ "$LEN" -ne 16 ]; then
  echo "WARNING: got $LEN characters; Gmail App Passwords are 16. Saving anyway."
fi

grep -v '^SMTP_PASSWORD=' .env > .env.tmp
printf 'SMTP_PASSWORD=%s\n' "$CLEAN" >> .env.tmp
mv .env.tmp .env
unset RAW CLEAN

echo "saved. SMTP_PASSWORD lines in .env: $(grep -c '^SMTP_PASSWORD=' .env)"
