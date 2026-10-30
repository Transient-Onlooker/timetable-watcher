#!/usr/bin/env python3
"""Lightweight multi-school timetable watcher. Python stdlib only."""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.cookiejar
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BASE = "https://timetable.s2h9.dev"  # Student-facing viewer link shown in messages.
# Live data comes from the original Comci server (comci.net), not the viewer:
# the viewer's CDN challenges non-browser clients with a bot check (HTTP 403).
COMCI_BASE = "http://comci.net:4082"
COMCI_MAIN_ROUTE = "36179"       # Extracted from the Comci /st page; rotates occasionally.
COMCI_TIMETABLE_ROUTE = "73629"  # Keep in sync with the routes the /st page serves.
MAX_COMCI_BYTES = 16 * 1024 * 1024
DEFAULT_SCHOOL_CODE = "0"  # Unconfigured: select your own school via environment or CLI
KST = timezone(timedelta(hours=9))
DAY_NAMES = "월화수목금토일"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36")
MAX_BOOTSTRAP_BYTES = 2 * 1024 * 1024
MAX_TIMETABLE_BYTES = 4 * 1024 * 1024
MAX_CHANGE_DETAILS = 3  # 4+ changed periods: show only the updated timetable


class TimetableError(Exception):
    pass


def read_bounded(response: Any, limit: int, description: str) -> bytes:
    """Reject unexpectedly large replies before they exhaust a small host."""
    body = response.read(limit + 1)
    if len(body) > limit:
        raise TimetableError(f"{description} response exceeds {limit} bytes")
    return body


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            raise TimetableError("Truncated protobuf varint")
        b = data[pos]
        pos += 1
        value |= (b & 127) << shift
        if not b & 128:
            return value, pos
    raise TimetableError("Protobuf varint too long")


def wire_fields(data: bytes) -> list[tuple[int, int, Any]]:
    """Minimal protobuf wire parser: varint, fixed32, fixed64, length-delimited."""
    out = []
    pos = 0
    while pos < len(data):
        tag, pos = read_varint(data, pos)
        field, wire = tag >> 3, tag & 7
        if field <= 0:
            raise TimetableError("Invalid protobuf field number")
        if wire == 0:
            value, pos = read_varint(data, pos)
        elif wire in (1, 5):
            n = 8 if wire == 1 else 4
            value = data[pos:pos+n]
            pos += n
            if len(value) != n:
                raise TimetableError("Truncated protobuf fixed-width field")
        elif wire == 2:
            n, pos = read_varint(data, pos)
            if n > len(data) - pos:
                raise TimetableError("Truncated protobuf length-delimited field")
            value = data[pos:pos+n]
            pos += n
        else:
            raise TimetableError(f"Unsupported protobuf wire type: {wire}")
        out.append((field, wire, value))
    return out


def one(fields: list, field: int, default: Any = None) -> Any:
    return next((value for n, _, value in fields if n == field), default)


def many(fields: list, field: int) -> list:
    return [value for n, _, value in fields if n == field]


def dict_string(dictionary: list[str], index: int) -> str:
    if not isinstance(index, int) or not 0 <= index < len(dictionary):
        raise TimetableError(f"Invalid string reference {index}")
    return dictionary[index]


def parse_week_range(text: str) -> tuple[date, date]:
    m = re.fullmatch(r"(\d{2}-\d{2}-\d{2})\s*~\s*(\d{2}-\d{2}-\d{2})", text)
    if not m:
        raise TimetableError(f"Unexpected week label: {text!r}")
    return tuple(datetime.strptime(x, "%y-%m-%d").date() for x in m.groups())


@dataclass
class Decoded:
    week_start: date
    week_end: date
    last_update: str
    classes: dict[tuple[int, int], dict[str, list[dict[str, Any]]]]


def decode_timetable(blob: bytes, period: int) -> Decoded:
    """Decoded from supplied 2026-10-22 TimetableResponse samples; version-sensitive."""
    root = wire_fields(blob)
    strings = [v.decode("utf-8") for v in many(root, 15)]
    if len(strings) < 20:
        raise TimetableError("Missing timetable string table; API format may have changed")

    labels = {}
    for raw in many(root, 2):
        fields = wire_fields(raw)
        labels[one(fields, 1)] = dict_string(strings, one(fields, 2))
    if period not in labels:
        raise TimetableError(f"Period {period} not in response")
    week_start, week_end = parse_week_range(labels[period])
    if week_end < week_start + timedelta(days=4):
        raise TimetableError("Week range doesn't cover 5 weekdays")

    last_update = dict_string(strings, one(root, 4))
    # Response schoolInfo.classCount is packed varints:
    # [total classes, grade 1, grade 2, grade 3]. This is an observed
    # protocol shape, not a public API contract. Some grades may have 0 classes.
    school_info = one(root, 1)
    if not isinstance(school_info, bytes):
        raise TimetableError("Missing school metadata")
    class_counts = one(wire_fields(school_info), 4)
    if not isinstance(class_counts, bytes):
        raise TimetableError("Unrecognized school class-count metadata")
    counts = []
    pos = 0
    while pos < len(class_counts):
        count, pos = read_varint(class_counts, pos)
        counts.append(count)
    if (len(counts) != 4 or counts[0] == 0 or
            sum(counts[1:]) != counts[0] or any(count > 500 for count in counts)):
        raise TimetableError("Unrecognized school class-count metadata")
    expected_classes = {
        (grade, room)
        for grade, count in enumerate(counts[1:], start=1)
        for room in range(1, count + 1)
    }
    classes: dict[tuple[int, int], dict[str, list[dict[str, Any]]]] = {}
    for class_raw in many(root, 5):
        fields = wire_fields(class_raw)
        grade, classroom = one(fields, 1), one(fields, 2)
        if not isinstance(grade, int) or not isinstance(classroom, int):
            raise TimetableError("Malformed grade/class identifiers")
        if (grade, classroom) not in expected_classes:
            raise TimetableError(f"Unexpected class {grade}-{classroom} in response")
        blocks = many(fields, 3)
        if len(blocks) != 5:
            raise TimetableError(f"Class {grade}-{classroom} has {len(blocks)} weekday blocks")
        week_days = {}
        for weekday, day_raw in enumerate(blocks):
            day = week_start + timedelta(days=weekday)
            lessons = []
            for lesson_raw in many(wire_fields(day_raw), 1):
                lesson_fields = wire_fields(lesson_raw)
                ordinal = one(lesson_fields, 8)
                actual_weekday = one(lesson_fields, 7)
                if actual_weekday != weekday + 1:
                    raise TimetableError(f"Unexpected weekday {actual_weekday} in {grade}-{classroom}")
                if not isinstance(ordinal, int) or not 1 <= ordinal <= 12:
                    raise TimetableError("Missing/invalid lesson ordinal")
                old_subject = None
                old_teacher = None
                original = one(lesson_fields, 5)
                if original is not None:
                    previous = wire_fields(original)
                    # The original message has nested subject/teacher references.
                    prev_sub = one(previous, 2)
                    prev_teacher = one(previous, 3)
                    if prev_sub is not None:
                        sub_ref = one(wire_fields(prev_sub), 2)
                        if sub_ref is not None:
                            old_subject = dict_string(strings, sub_ref)
                    if prev_teacher is not None:
                        teacher_ref = one(wire_fields(prev_teacher), 2)
                        if teacher_ref is not None:
                            old_teacher = dict_string(strings, teacher_ref)
                subject_idx = one(lesson_fields, 1)
                teacher_idx = one(lesson_fields, 2)
                if (subject_idx is None) != (teacher_idx is None):
                    raise TimetableError("Subject and teacher references disagree")
                lessons.append({
                    "period": ordinal,
                    "subject": dict_string(strings, subject_idx) if subject_idx is not None else "",
                    "teacher": dict_string(strings, teacher_idx) if teacher_idx is not None else "",
                    # field 4 is the site's changed flag; field 5 may be absent even when changed
                    "site_changed": bool(one(lesson_fields, 4, 0)),
                    "original_subject": old_subject,
                    "original_teacher": old_teacher,
                })
            lessons.sort(key=lambda x: x["period"])
            if len({c["period"] for c in lessons}) != len(lessons):
                raise TimetableError(f"Duplicate period in {grade}-{classroom}, {day}")
            week_days[day.isoformat()] = lessons
        key = (grade, classroom)
        if key in classes:
            raise TimetableError(f"Duplicate class {key}")
        classes[key] = week_days
    missing = expected_classes - classes.keys()
    if missing:
        preview = ", ".join(f"{g}-{c}" for g, c in sorted(missing)[:8])
        raise TimetableError(f"Incomplete class data: missing {len(missing)} classes ({preview})")
    return Decoded(week_start, week_end, last_update, classes)


def validate_school_code(value: str) -> str:
    if not re.fullmatch(r"[0-9]{1,10}", value):
        raise TimetableError("School code must contain 1-10 ASCII digits")
    return value


def _comci_code_parts(code: Any) -> tuple[int, int, bool]:
    """Split a Comci lesson code into (teacher idx, subject idx, changed).

    Codes pack teacher/subject dictionary indexes as trailing digit groups;
    a leading '>' marks a lesson changed from the original timetable.
    """
    s = str(code)
    changed = s.startswith(">")
    if changed:
        s = s[1:]
    parts = re.findall(r"\d{1,3}(?=(?:\d{3})+(?!\d))|\d{1,3}$", s)
    if len(parts) < 2:
        raise TimetableError(f"Unrecognized Comci lesson code: {code!r}")
    ma = [int(x) for x in reversed(parts)]
    teacher = ma[0]
    subject = ma[1] + (ma[2] if len(ma) > 2 else 0) * 1000
    return teacher, subject, changed


def _comci_original(orig_codes: Any, period: int,
                    subjects: list, teachers: list) -> tuple[str | None, str | None]:
    """Pre-change (subject, teacher) for a period; best-effort decoration."""
    if not isinstance(orig_codes, list) or period >= len(orig_codes):
        return None, None
    code = orig_codes[period]
    if not code or str(code).lstrip(">") == "0":
        return None, None
    try:
        teacher_idx, subject_idx, _ = _comci_code_parts(code)
    except TimetableError:
        return None, None
    subject = subjects[subject_idx] if 0 <= subject_idx < len(subjects) else ""
    teacher = teachers[teacher_idx] if 0 <= teacher_idx < len(teachers) else ""
    return subject, teacher


def _comci_day_lessons(day_codes: Any, orig_codes: Any,
                       subjects: list, teachers: list) -> list[dict[str, Any]]:
    if not isinstance(day_codes, list) or not day_codes:
        return []
    total = day_codes[0]
    if not isinstance(total, int) or not 0 < total <= 12:
        return []
    lessons = []
    for period in range(1, total + 1):
        code = day_codes[period] if period < len(day_codes) else 0
        if not code:
            continue
        if str(code).lstrip(">") == "0":
            # '>0' marks a lesson removed from the original timetable.
            if not str(code).startswith(">"):
                continue
            original_subject, original_teacher = _comci_original(
                orig_codes, period, subjects, teachers)
            lessons.append({
                "period": period, "subject": "", "teacher": "",
                "site_changed": True,
                "original_subject": original_subject,
                "original_teacher": original_teacher,
            })
            continue
        teacher_idx, subject_idx, changed = _comci_code_parts(code)
        original_subject = None
        original_teacher = None
        if changed:
            original_subject, original_teacher = _comci_original(
                orig_codes, period, subjects, teachers)
        lessons.append({
            "period": period,
            "subject": subjects[subject_idx] if 0 <= subject_idx < len(subjects) else "",
            "teacher": teachers[teacher_idx] if 0 <= teacher_idx < len(teachers) else "",
            "site_changed": changed,
            "original_subject": original_subject,
            "original_teacher": original_teacher,
        })
    return lessons


def decode_comci(payload: dict) -> Decoded:
    """Build a Decoded from the original Comci JSON payload.

    Field map (observed 2026-10-30): 시작일 week-start date, 자료244
    last-update timestamp, 자료147 current timetable codes, 자료481
    pre-change timetable codes, 자료446 teacher names, 자료492 subject
    names. 자료147/481 are [grade][class][weekday 1=Mon] arrays whose
    first element is the day's period count.
    """
    try:
        week_start = datetime.strptime(str(payload["시작일"]), "%Y-%m-%d").date()
        subjects = payload["자료492"]
        teachers = payload["자료446"]
        table = payload["자료147"]
        originals = payload.get("자료481") or []
        last_update = str(payload.get("자료244", ""))
    except (KeyError, TypeError, ValueError) as exc:
        raise TimetableError("Unrecognized Comci response; API format may have changed") from exc
    if not isinstance(table, list) or not isinstance(subjects, list) or not isinstance(teachers, list):
        raise TimetableError("Unrecognized Comci response; API format may have changed")
    classes: dict[tuple[int, int], dict[str, list[dict[str, Any]]]] = {}
    for grade, grade_rows in enumerate(table):
        if grade == 0 or not isinstance(grade_rows, list):
            continue
        orig_grade = originals[grade] if grade < len(originals) else []
        for classroom, class_rows in enumerate(grade_rows):
            if classroom == 0 or not isinstance(class_rows, list):
                continue
            orig_rows = orig_grade[classroom] if isinstance(orig_grade, list) and classroom < len(orig_grade) else []
            week_days = {}
            for weekday in range(5):
                day = week_start + timedelta(days=weekday)
                day_codes = class_rows[weekday + 1] if weekday + 1 < len(class_rows) else []
                orig_codes = orig_rows[weekday + 1] if isinstance(orig_rows, list) and weekday + 1 < len(orig_rows) else []
                week_days[day.isoformat()] = _comci_day_lessons(day_codes, orig_codes, subjects, teachers)
            classes[(grade, classroom)] = week_days
    if not classes:
        raise TimetableError("Comci response contained no classes")
    return Decoded(week_start, week_start + timedelta(days=4), last_update, classes)


def _comci_fetch_json(school_code: str, period: int) -> dict:
    raw = f"{COMCI_TIMETABLE_ROUTE}_{school_code}_0_{period}"
    query = base64.b64encode(raw.encode()).decode()
    url = f"{COMCI_BASE}/{COMCI_MAIN_ROUTE}?{query}"
    last_err: Exception | None = None
    for _ in range(5):  # Comci sometimes truncates large responses mid-stream.
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=25) as response:
                if response.getcode() != 200:
                    raise TimetableError(f"Comci returned HTTP {response.getcode()}")
                body = read_bounded(response, MAX_COMCI_BYTES, "Comci")
            return json.loads(body.replace(b"\x00", b"").decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last_err = exc
            time.sleep(2)
    raise TimetableError(f"Comci request failed after retries: {last_err}") from None


def fetch_timetable(target: date, fixture_dir: Path | None = None,
                    school_code: str = DEFAULT_SCHOOL_CODE) -> tuple[Decoded, int]:
    school_code = validate_school_code(school_code)
    if fixture_dir is not None:
        for period in (1, 2):
            blob = (fixture_dir / f"period-{period}.bin").read_bytes()
            decoded = decode_timetable(blob, period)
            if decoded.week_start <= target <= decoded.week_end:
                return decoded, period
        raise TimetableError(f"Date {target} not in fixture's 2 weeks")

    for period in (1, 2):
        decoded = decode_comci(_comci_fetch_json(school_code, period))
        if decoded.week_start <= target <= decoded.week_end:
            return decoded, period
    raise TimetableError(f"Date {target} was not in Comci's current/next week")


def target_lessons(decoded: Decoded, day: date, grade: int, classroom: int) -> list[dict[str, Any]]:
    days = decoded.classes.get((grade, classroom))
    if not days:
        raise TimetableError(f"Class {grade}-{classroom} not found")
    lessons = days.get(day.isoformat())
    if lessons is None:
        raise TimetableError(f"Target day {day} is outside timetable response")
    if not lessons:
        raise TimetableError("No lessons returned for target day (holiday or missing data)")
    return lessons


def json_lessons(lessons: list[dict[str, Any]]) -> str:
    return json.dumps(lessons, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ensure_schema(db: sqlite3.Connection) -> None:
    # Migrate databases lacking school_code, using the configured school code.
    # For ambiguous multi-school legacy data, manually review before migration.
    db.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          school_code TEXT NOT NULL DEFAULT '0',
          target_day TEXT NOT NULL,
          grade INTEGER NOT NULL,
          classroom INTEGER NOT NULL,
          hash TEXT NOT NULL,
          observed_at TEXT NOT NULL,
          server_last_update TEXT NOT NULL,
          lessons_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sent (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          school_code TEXT NOT NULL DEFAULT '0',
          target_day TEXT NOT NULL,
          grade INTEGER NOT NULL,
          classroom INTEGER NOT NULL,
          kind TEXT NOT NULL,
          snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
          sent_at TEXT NOT NULL
        );
    """)
    for table in ("snapshots", "sent"):
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if "school_code" not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN school_code TEXT NOT NULL DEFAULT '0'")
    db.executescript("""
        CREATE INDEX IF NOT EXISTS idx_school_snaps
          ON snapshots(school_code, target_day, grade, classroom, id DESC);
        CREATE INDEX IF NOT EXISTS idx_school_sent
          ON sent(school_code, target_day, grade, classroom, id DESC);
    """)


def connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    ensure_schema(db)
    return db


def preview_db(path: Path) -> sqlite3.Connection:
    """Read existing state without writing; use memory for a fresh preview."""
    if not path.exists():
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        ensure_schema(db)
    else:
        uri = path.resolve().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True)
        db.row_factory = sqlite3.Row
        # A pre-multi-school DB has no school_code column. Keep dry-runs
        # non-destructive by upgrading an in-memory copy, never the original.
        columns = {row[1] for row in db.execute("PRAGMA table_info(snapshots)")}
        if "school_code" not in columns:
            upgrade = sqlite3.connect(":memory:")
            db.backup(upgrade)
            db.close()
            upgrade.row_factory = sqlite3.Row
            ensure_schema(upgrade)
            db = upgrade
        db.execute("PRAGMA query_only=ON")
    return db


def latest_snapshot(db: sqlite3.Connection, target: date, grade: int, classroom: int,
                    school_code: str = DEFAULT_SCHOOL_CODE):
    return db.execute("""
        SELECT * FROM snapshots WHERE school_code=? AND target_day=? AND grade=? AND classroom=?
        ORDER BY id DESC LIMIT 1
    """, (school_code, target.isoformat(), grade, classroom)).fetchone()


def latest_sent(db: sqlite3.Connection, target: date, grade: int, classroom: int,
                school_code: str = DEFAULT_SCHOOL_CODE):
    return db.execute("""
        SELECT s.*, t.hash, t.lessons_json FROM sent s
        JOIN snapshots t ON t.id=s.snapshot_id
        WHERE s.school_code=? AND s.target_day=? AND s.grade=? AND s.classroom=?
        ORDER BY s.id DESC LIMIT 1
    """, (school_code, target.isoformat(), grade, classroom)).fetchone()


def capture(db: sqlite3.Connection, target: date, grade: int, classroom: int,
            lessons: list[dict[str, Any]], last_update: str,
            school_code: str = DEFAULT_SCHOOL_CODE) -> tuple[int, bool]:
    js = json_lessons(lessons)
    digest = hashlib.sha256(js.encode()).hexdigest()
    previous = latest_snapshot(db, target, grade, classroom, school_code)
    if previous is not None and previous["hash"] == digest:
        return previous["id"], False
    cursor = db.execute("""
        INSERT INTO snapshots(school_code,target_day,grade,classroom,hash,observed_at,server_last_update,lessons_json)
        VALUES (?,?,?,?,?,?,?,?)
    """, (school_code, target.isoformat(), grade, classroom, digest,
          datetime.now(KST).isoformat(timespec="seconds"), last_update, js))
    db.commit()
    return cursor.lastrowid, True


def _subject_teacher(subject: str | None, teacher: str | None) -> str:
    if not subject:
        return "수업 없음"
    return f"{subject} {teacher}" if teacher else subject


def _lesson_compact(lesson: dict[str, Any]) -> str:
    return _subject_teacher(lesson.get("subject"), lesson.get("teacher"))


def _current_lesson(lesson: dict[str, Any]) -> str:
    if not lesson.get("subject"):
        return "수업 없음"
    if lesson.get("teacher"):
        return f"{lesson['subject']} · {lesson['teacher']}"
    return lesson["subject"]


def _substantive_change(a: dict[str, Any] | None,
                        b: dict[str, Any] | None) -> bool:
    if a is None or b is None:
        return a is not b
    return any(a.get(f) != b.get(f) for f in ("subject", "teacher"))


def _recent_change_lines(before: list[dict[str, Any]],
                         after: list[dict[str, Any]]) -> list[str]:
    old = {x["period"]: x for x in before}
    new = {x["period"]: x for x in after}
    result = []
    for period in sorted(old.keys() | new.keys()):
        a, b = old.get(period), new.get(period)
        if a is None:
            result.append(f"{period}교시 추가: {_lesson_compact(b)}")
        elif b is None:
            result.append(f"{period}교시 삭제: {_lesson_compact(a)}")
        elif (a.get("subject"), a.get("teacher")) != (b.get("subject"), b.get("teacher")):
            result.append(f"{period}교시 {_lesson_compact(a)} → {_lesson_compact(b)}")
        elif a.get("site_changed") != b.get("site_changed"):
            result.append(f"{period}교시 사이트 변동 표시 변경")
        elif (a.get("original_subject"), a.get("original_teacher")) != (b.get("original_subject"), b.get("original_teacher")):
            result.append(f"{period}교시 변경 전 수업 정보 갱신")
    return result


def changed_between(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[str]:
    """Public legacy-facing diff helper. Covers lessons and site metadata."""
    changes = _recent_change_lines(before, after)
    # Preserve legacy line syntax for callers expecting parenthesized teachers.
    old = {x["period"]: x for x in before}
    new = {x["period"]: x for x in after}
    for idx, line in enumerate(changes):
        match = re.match(r"(\d+)교시 ", line)
        if not match:
            continue
        period = int(match.group(1))
        a, b = old.get(period), new.get(period)
        if a and b and (a.get("subject"), a.get("teacher")) != (b.get("subject"), b.get("teacher")):
            old_text = f"{a['subject']} ({a['teacher']})" if a.get("subject") else "수업 없음"
            new_text = f"{b['subject']} ({b['teacher']})" if b.get("subject") else "수업 없음"
            changes[idx] = f"{period}교시: {old_text} → {new_text}"
    return changes


def _short_update(value: str) -> str:
    try:
        timestamp = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value
    return f"{timestamp.month}/{timestamp.day} {timestamp:%H:%M:%S}"


def format_message(target: date, grade: int, classroom: int, lessons: list[dict[str, Any]],
                   previous: list[dict[str, Any]] | None, last_update: str,
                   kind: str, school_code: str = DEFAULT_SCHOOL_CODE,
                   school_name: str | None = None) -> str:
    if not school_name:
        school_name = f"학교 {school_code}"
    flagged = [x for x in lessons if x.get("site_changed")]
    recent = _recent_change_lines(previous, lessons) if previous is not None else []
    comparison = {x["period"]: x for x in previous} if previous is not None else {}
    latest = {x["period"]: x for x in lessons}
    changed_periods = {
        period for period in comparison.keys() | latest.keys()
        if _substantive_change(comparison.get(period), latest.get(period))
    } if previous is not None else set()

    if kind == "morning":
        details = []
        for x in flagged:
            old_subject, old_teacher = x.get("original_subject"), x.get("original_teacher")
            if old_subject is None and old_teacher is None:
                details.append(f"{x['period']}교시 변경 (변경 전 정보 없음)")
            else:
                original = _subject_teacher(old_subject, old_teacher)
                details.append(f"{x['period']}교시 {original} → {_subject_teacher(x.get('subject'), x.get('teacher'))}")
        # Site flags describe original-vs-current, but an independent stored
        # Snapshot can detect an overnight subject/teacher change without flags.
        flagged_periods = {x["period"] for x in flagged}
        for line in recent:
            match = re.match(r"(\d+)교시", line)
            if match and int(match.group(1)) not in flagged_periods:
                details.append(line + " (직전 저장본 대비)")
    else:
        details = recent

    # A message should never contain more than three change-detail lines.
    # Count overlapping site/original and snapshot corrections separately:
    # both would otherwise appear as distinct details in the same message.
    additional = []
    if kind == "morning" and previous is not None:
        additional = [
            line for line in recent
            if (match := re.match(r"(\d+)교시\b", line))
            and int(match.group(1)) in flagged_periods
        ]
    suppress_details = len(details) + len(additional) > MAX_CHANGE_DETAILS

    title = "시간표 변경 알림" if kind == "morning" else "추가 시간표 변경 알림"
    lines = [title, f"{school_name} {grade}학년 {classroom}반", "",
             f"{target.month}/{target.day}({DAY_NAMES[target.weekday()]})"]
    if not suppress_details:
        lines.append("")
        if details:
            lines.extend(details)
            if additional:
                lines.extend(["", "직전 저장본 이후 변경", *additional])
        elif previous is None and not flagged:
            lines.append("사이트 변동 표시 없음 (이전 저장본 없음)")
        else:
            lines.append("변동 없음")
    # No detail bullets at all for 4+ changes; keep the final timetable and
    # per-period (변동됨) markers so the message is short but still useful.
    lines += ["", "변경된 시간표" if suppress_details or flagged or changed_periods else "오늘의 시간표", ""]
    for x in lessons:
        marker = " (변동됨)" if x.get("site_changed") or x["period"] in changed_periods else ""
        lines.append(f"{x['period']}교시 {_current_lesson(x)}{marker}")
    lines.extend(["", f"사이트 최종 업데이트: {_short_update(last_update)}", "",
                  f"{BASE}/?sc={school_code}"])
    return "\n".join(lines)


MAX_GOOGLE_CHAT_RESPONSE_BYTES = 64 * 1024
MAX_GOOGLE_CHAT_MESSAGE_BYTES = 32 * 1024


def send_google_chat(text: str, dry_run: bool) -> bool:
    """Post a text message to a Google Chat incoming webhook (stdlib only).

    The webhook URL embeds secret key/token query parameters. Never propagate
    URL-bearing exceptions or print the URL, even on HTTP errors.
    """
    if dry_run:
        print("\n--- 메시지 미리보기 (실제 발송하지 않음) ---\n" + text)
        return False

    url = os.getenv("GOOGLE_CHAT_WEBHOOK_URL", "").strip()
    if not url or url.startswith("replace-"):
        raise TimetableError("Google Chat webhook URL not configured; use --dry-run to preview")
    try:
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    except ValueError:
        raise TimetableError("Invalid Google Chat webhook URL") from None
    # A webhook URL is a bearer credential. Only send it to Chat's documented
    # endpoint, never to a user-controlled host or over plaintext HTTP.
    if (parsed.scheme != "https" or parsed.netloc != "chat.googleapis.com" or
            parsed.fragment or not re.fullmatch(r"/v1/spaces/[^/]+/messages", parsed.path) or
            any(len(query.get(field, [])) != 1 or not query[field][0]
                for field in ("key", "token"))):
        raise TimetableError("Invalid Google Chat webhook URL")
    if len(text.encode("utf-8")) > MAX_GOOGLE_CHAT_MESSAGE_BYTES:
        raise TimetableError("Google Chat message exceeds 32 KiB")

    payload = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method="POST", headers={
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if not 200 <= response.getcode() < 300:
                raise TimetableError(f"Google Chat webhook HTTP {response.getcode()}")
            body = read_bounded(response, MAX_GOOGLE_CHAT_RESPONSE_BYTES, "Google Chat")
    except urllib.error.HTTPError as exc:
        raise TimetableError(f"Google Chat webhook HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise TimetableError("Google Chat webhook connection failed") from None
    try:
        confirmation = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise TimetableError("Google Chat webhook returned invalid JSON") from None
    # Incoming webhooks return a Message object (name=spaces/.../messages/...).
    # A 2xx response without that acknowledgement must not be persisted as sent.
    if (not isinstance(confirmation, dict) or
            not isinstance(confirmation.get("name"), str) or
            not re.fullmatch(r"spaces/[^/]+/messages/[^/]+", confirmation["name"])):
        raise TimetableError("Google Chat webhook did not confirm message delivery")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "capture", "morning", "watch"))
    parser.add_argument("--school-code", default=os.getenv("TIMETABLE_SCHOOL_CODE", DEFAULT_SCHOOL_CODE),
                        help="Comci school code (required for real requests)")
    parser.add_argument("--school-name", default=os.getenv("TIMETABLE_SCHOOL_NAME"),
                        help="Display label for Google Chat messages (optional)")
    parser.add_argument("--grade", type=int, default=int(os.getenv("TIMETABLE_GRADE", "2")))
    parser.add_argument("--classroom", type=int, default=int(os.getenv("TIMETABLE_CLASSROOM", "11")))
    parser.add_argument("--target", choices=("today", "tomorrow"), default="today")
    parser.add_argument("--date", help="Override KST target date: YYYY-MM-DD")
    parser.add_argument("--db", type=Path, default=Path(__file__).resolve().parent/"state"/"watcher.sqlite3")
    parser.add_argument("--fixture-dir", type=Path, help="Use local period-{1,2}.bin instead of HTTPS")
    parser.add_argument("--dry-run", action="store_true", help="Read-only preview: no DB changes or Google Chat send")
    args = parser.parse_args(argv)
    args.school_code = validate_school_code(args.school_code)
    if args.school_code == "0":
        raise TimetableError("Set TIMETABLE_SCHOOL_CODE or --school-code")
    if args.school_name and (len(args.school_name) > 100 or
                             any(ch in args.school_name for ch in "\r\n\x00")):
        raise TimetableError("School name must be <=100 characters, one line")
    if not 1 <= args.grade <= 3 or not 1 <= args.classroom <= 500:
        raise TimetableError("Grade/classroom out of range")

    target = date.fromisoformat(args.date) if args.date else datetime.now(KST).date()
    if not args.date and args.target == "tomorrow":
        target += timedelta(days=1)
    if target.weekday() >= 5:
        print(f"{target}: 주말이므로 건너뜀")
        return 0

    if args.action == "inspect":
        # Inspecting the API response must not create or modify a database.
        decoded, period = fetch_timetable(target, args.fixture_dir, school_code=args.school_code)
        lessons = target_lessons(decoded, target, args.grade, args.classroom)
        print(f"{target} / school={args.school_code} / period={period} / {args.grade}-{args.classroom} / "
              f"{len(lessons)}교시 / last_update={decoded.last_update}")
        print(json.dumps(lessons, ensure_ascii=False, indent=2))
        return 0

    with closing(preview_db(args.db) if args.dry_run else connect_db(args.db)) as db:
        if args.action in ("morning", "watch") and not args.dry_run:
            last = latest_sent(db, target, args.grade, args.classroom, args.school_code)
            if args.action == "morning" and db.execute("""
                SELECT 1 FROM sent WHERE school_code=? AND target_day=? AND grade=? AND classroom=? AND kind='morning'
                LIMIT 1""", (args.school_code, target.isoformat(), args.grade, args.classroom)).fetchone():
                print("오늘 아침 시간표는 이미 발송했음")
                return 0
            if args.action == "watch" and last is None:
                print("아침 메시지 발송 내역이 없어 추가 변경 알림을 생략함")
                return 0

        decoded, period = fetch_timetable(target, args.fixture_dir, school_code=args.school_code)
        lessons = target_lessons(decoded, target, args.grade, args.classroom)
        print(f"{target} / school={args.school_code} / period={period} / {args.grade}-{args.classroom} / "
              f"{len(lessons)}교시 / last_update={decoded.last_update}")
        prior = latest_snapshot(db, target, args.grade, args.classroom, args.school_code)
        if args.dry_run:
            snap_id = None
            is_new = (prior is None or prior["hash"] !=
                      hashlib.sha256(json_lessons(lessons).encode()).hexdigest())
            print(f"snapshot=preview (not saved), content_changed={is_new}")
        else:
            snap_id, is_new = capture(db, target, args.grade, args.classroom,
                                      lessons, decoded.last_update, args.school_code)
            print(f"snapshot={snap_id}, content_changed={is_new}")
        if args.action == "capture":
            return 0

        last = latest_sent(db, target, args.grade, args.classroom, args.school_code)
        if args.action == "watch":
            if last is None:
                print("발송된 기준 메시지가 없음. 추가 변경 알림 생략")
                return 0
            if last["hash"] == hashlib.sha256(json_lessons(lessons).encode()).hexdigest():
                print("마지막 발송 이후 신규 변동 없음")
                return 0
            baseline = json.loads(last["lessons_json"])
            if not _recent_change_lines(baseline, lessons):
                print("실질적인 시간표 변동 없음 (저장 형식 변경만 감지됨)")
                return 0
        else:
            baseline = json.loads(prior["lessons_json"]) if prior else None
            flagged = [x for x in lessons if x.get("site_changed")]
            recent = _recent_change_lines(baseline, lessons) if baseline is not None else []
            if not flagged and not recent:
                print("변동 없어 아침 발송 생략")
                return 0

        message = format_message(target, args.grade, args.classroom, lessons,
                                 baseline, decoded.last_update, args.action,
                                 args.school_code, args.school_name)
        sent = send_google_chat(message, args.dry_run)
        if sent:
            db.execute("""
                INSERT INTO sent(school_code,target_day,grade,classroom,kind,snapshot_id,sent_at)
                VALUES (?,?,?,?,?,?,?)
            """, (args.school_code, target.isoformat(), args.grade, args.classroom, args.action, snap_id,
                  datetime.now(KST).isoformat(timespec="seconds")))
            db.commit()
            print("Google Chat 발송 완료, 중복 발송 방지 기록 저장")
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (TimetableError, urllib.error.URLError, OSError, ValueError, KeyError,
            json.JSONDecodeError, sqlite3.Error) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
