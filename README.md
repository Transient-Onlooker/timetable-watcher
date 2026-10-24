# Timetable Watcher

A lightweight, school-configurable timetable change watcher with Google Chat incoming-webhook notifications. Python 3.10+ standard library only.

The **public repository contains no real school codes, school names, teacher names, personal timetable data or collected timetable binary fixtures**. Never commit `state/`, configuration secrets, API captures or logs.

## Set up

```bash
git clone https://github.com/Transient-Onlooker/timetable-watcher.git
cd timetable-watcher
python3 -m unittest discover -s tests -q
bash deploy/install-rpi.sh prepare
```

Edit `~/.config/timetable-watcher/myclass.env` **locally**, setting your school code, optional school name, grade, classroom and Google Chat incoming webhook URL. Keep it readable only by your user (`chmod 600`). School codes come from the timetable website URL's `?sc=` value.

```bash
python3 timetable_watcher.py inspect --school-code SCHOOL_CODE --grade 1 --classroom 1
python3 timetable_watcher.py morning --school-code SCHOOL_CODE --grade 1 --classroom 1 --dry-run
bash deploy/install-rpi.sh enable
bash deploy/install-rpi.sh status
```

The installed systemd user timer runs Monday–Friday at **09:00 Asia/Seoul**, retrying at 09:05 and 09:10 if the morning message was not previously delivered. SQLite avoids duplicate sends. Google Chat posts are JSON `text` messages.

**Messages:** when there are up to three changes, show detailed original → current lesson information followed by the full timetable. At four or more changes, omit the detailed list and send only the timetable, with changed periods marked.

Changes include subject, teacher, added/deleted lessons and site change markers; classroom location is intentionally ignored. Use `watch` separately if monitoring changes after the scheduled morning delivery.

**Deployment note:** upgrading from releases that used a different Python filename changes the systemd unit command. Pull the latest source, run `bash deploy/install-rpi.sh prepare`, and reload the user daemon before checking timers. Existing per-user config and SQLite data should be backed up before migration. Older database rows without a school_code are assigned `0` by the generic migration: reconcile them offline if you need historical alert deduplication.

**Limitations:** the Protobuf decoder is based on an observed, unofficial website format and may need updates if upstream changes. Actual compatibility must be checked at each school with `inspect`. Avoid excessive polling. Live Google Chat delivery requires your private webhook URL.
