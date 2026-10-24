#!/usr/bin/env bash
# Run on a trusted, clean Raspberry Pi OS/Ubuntu installation.
set -euo pipefail

MODE="${1:-prepare}"
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="$HOME/timetable-watcher"
CONFIG_DIR="$HOME/.config/timetable-watcher"
UNIT_DIR="$HOME/.config/systemd/user"
INSTANCE="myclass"
CONFIG="$CONFIG_DIR/$INSTANCE.env"

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
value_from_config() {
    # EnvironmentFile is owned by the current user; read simple KEY=value entries
    # without evaluating its contents as shell commands.
    local value
    value="$(sed -n "s/^$1=//p" "$CONFIG" | tail -n 1)"
    # Both systemd EnvironmentFile and run.sh support single-quoted URLs.
    # Parse only surrounding quotes; never eval or print the webhook secret.
    if [[ "$value" == \'*\' && "$value" == *\' ]]; then
        value="${value:1:${#value}-2}"
    elif [[ "$value" == \"*\" && "$value" == *\" ]]; then
        value="${value:1:${#value}-2}"
    fi
    printf '%s' "$value"
}
check_software() {
    command -v python3 >/dev/null || fail "python3 missing (sudo apt install python3)"
    command -v systemctl >/dev/null || fail "systemd required"
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || fail "Python 3.10+ required"
    [[ "$PROJECT_DIR" == "$TARGET_DIR" ]] || fail "Expected checkout at $TARGET_DIR (found $PROJECT_DIR)"
}
next_school_day() {
    python3 - <<'PY'
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
now = datetime.now(ZoneInfo("Asia/Seoul")).date()
if now.weekday() >= 5:
    now += timedelta(days=7 - now.weekday())
print(now.isoformat())
PY
}

check_software
case "$MODE" in
    prepare)
        cd "$PROJECT_DIR"
        python3 -m unittest discover -s tests -q
        mkdir -p "$CONFIG_DIR" "$UNIT_DIR"
        chmod 700 "$CONFIG_DIR"
        if [[ ! -f "$CONFIG" ]]; then
            cp deploy/example.env "$CONFIG"
            chmod 600 "$CONFIG"
            echo "Created $CONFIG; edit Google Chat webhook URL and school/class before activation."
        else
            echo "Existing config preserved: $CONFIG"
        fi
        cp deploy/timetable-watcher@.service deploy/timetable-watcher@.timer "$UNIT_DIR/"
        systemctl --user daemon-reload
        echo "Prepared systemd units (NOT activated)."
        echo "To activate after editing config: bash deploy/install-rpi.sh enable"
        ;;
    enable)
        [[ -f "$CONFIG" ]] || fail "Missing $CONFIG; run prepare first"
        chmod 600 "$CONFIG"
        for key in GOOGLE_CHAT_WEBHOOK_URL TIMETABLE_SCHOOL_CODE TIMETABLE_GRADE TIMETABLE_CLASSROOM; do
            val="$(value_from_config "$key")"
            [[ -n "$val" && "$val" != replace-* ]] || fail "Configure $key in $CONFIG"
        done
        webhook="$(value_from_config GOOGLE_CHAT_WEBHOOK_URL)"
        # Validate without printing the bearer credential, before enabling a timer.
        [[ "$webhook" == https://chat.googleapis.com/v1/spaces/*/messages\?key=*\&token=* ]] ||
            fail "GOOGLE_CHAT_WEBHOOK_URL does not look like a Google Chat webhook"
        code="$(value_from_config TIMETABLE_SCHOOL_CODE)"
        grade="$(value_from_config TIMETABLE_GRADE)"
        room="$(value_from_config TIMETABLE_CLASSROOM)"
        target="$(next_school_day)"
        echo "Checking live timetable for $target (school $code, class $grade-$room)."
        python3 "$PROJECT_DIR/timetable_watcher.py" inspect --date "$target" \
            --school-code "$code" --grade "$grade" --classroom "$room"
        echo "Previewing message without DB writes or Google Chat delivery."
        python3 "$PROJECT_DIR/timetable_watcher.py" morning --dry-run --date "$target" \
            --school-code "$code" --grade "$grade" --classroom "$room" \
            --school-name "$(value_from_config TIMETABLE_SCHOOL_NAME)"
        sudo loginctl enable-linger "$USER"
        systemctl --user daemon-reload
        systemctl --user enable --now "timetable-watcher@$INSTANCE.timer"
        systemctl --user list-timers --all "timetable-watcher@$INSTANCE.timer"
        echo "Enabled. No message is sent until the next scheduled time."
        ;;
    status)
        systemctl --user status "timetable-watcher@$INSTANCE.timer" --no-pager || true
        systemctl --user list-timers --all "timetable-watcher@$INSTANCE.timer"
        ;;
    *) fail "Usage: $0 {prepare|enable|status}" ;;
esac
