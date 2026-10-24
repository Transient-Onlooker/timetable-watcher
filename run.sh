#!/bin/sh
set -eu
BASE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ENV_FILE="$HOME/.config/timetable-watcher/myclass.env"
if [ -f "$ENV_FILE" ]; then
    # This file is user-owned/trusted (chmod 600). Shell syntax requires quotes
    # around webhook URLs containing '&', e.g. GOOGLE_CHAT_WEBHOOK_URL='https://...'.
    set -a
    . "$ENV_FILE"
    set +a
fi
exec /usr/bin/python3 "$BASE_DIR/timetable_watcher.py" "$@"
