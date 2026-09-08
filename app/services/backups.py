from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ..models import BackupSettings
from typing import Iterable

from sqlalchemy.engine import make_url


@dataclass(frozen=True)
class BackupInfo:
    name: str
    path: Path
    created_at: datetime
    size: int
    automatic: bool


def backup_dir(path: str) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _stamp(now: datetime) -> str:
    return now.strftime("%Y%m%d-%H%M%S")


def backup_filename(now: datetime, *, automatic: bool, sqlite: bool = False) -> str:
    kind = "auto" if automatic else "manual"
    ext = "sqlite" if sqlite else "dump"
    return f"share-manager-{kind}-{_stamp(now)}.{ext}"


def list_backups(path: str) -> list[BackupInfo]:
    root = backup_dir(path)
    rows: list[BackupInfo] = []
    for item in root.iterdir():
        if not item.is_file() or not item.name.startswith("share-manager-") or item.suffix not in {".dump", ".sqlite"}:
            continue
        stat = item.stat()
        rows.append(
            BackupInfo(
                name=item.name,
                path=item,
                created_at=datetime.fromtimestamp(stat.st_mtime),
                size=stat.st_size,
                automatic="-auto-" in item.name,
            )
        )
    return sorted(rows, key=lambda row: row.created_at, reverse=True)


def _pg_env(url):
    env = os.environ.copy()
    if url.password:
        env["PGPASSWORD"] = url.password
    return env


def _pg_args(url) -> list[str]:
    return [
        "--host", url.host or "localhost",
        "--port", str(url.port or 5432),
        "--username", url.username or "postgres",
    ]


def create_backup(database_url: str, target_dir: str, *, automatic: bool = False, now: datetime | None = None) -> BackupInfo:
    now = now or datetime.utcnow()
    url = make_url(database_url)
    sqlite = url.get_backend_name() == "sqlite"
    root = backup_dir(target_dir)
    name = backup_filename(now, automatic=automatic, sqlite=sqlite)
    final_path = root / name
    temp_path = root / f".{name}.tmp"

    try:
        if sqlite:
            db_path = Path(url.database or "")
            if not db_path.exists():
                raise RuntimeError("SQLite database file not found")
            shutil.copy2(db_path, temp_path)
        else:
            cmd = [
                "pg_dump", "--format=custom", "--no-owner", "--no-privileges",
                *_pg_args(url), "--file", str(temp_path), url.database or "postgres",
            ]
            result = subprocess.run(cmd, env=_pg_env(url), capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError((result.stderr or "pg_dump failed").strip())
        os.replace(temp_path, final_path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)

    stat = final_path.stat()
    return BackupInfo(name=name, path=final_path, created_at=datetime.fromtimestamp(stat.st_mtime), size=stat.st_size, automatic=automatic)


def validate_backup(database_url: str, path: Path) -> tuple[bool, str]:
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        if path.suffix != ".sqlite":
            return False, "Expected a .sqlite backup for this database"
        return (path.stat().st_size > 0, "SQLite backup file is readable" if path.stat().st_size > 0 else "Backup is empty")
    if path.suffix != ".dump":
        return False, "Expected a PostgreSQL .dump backup"
    result = subprocess.run(["pg_restore", "--list", str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        return False, (result.stderr or "pg_restore could not read this backup").strip()
    if not result.stdout.strip():
        return False, "Backup contains no restorable objects"
    return True, "PostgreSQL custom-format dump validated successfully"


def restore_backup(database_url: str, path: Path) -> None:
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        db_path = Path(url.database or "")
        shutil.copy2(path, db_path)
        return

    env = _pg_env(url)
    db_name = url.database or "postgres"
    # Disconnect pooled/request sessions before pg_restore drops/recreates objects.
    terminate_sql = (
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = '{db_name.replace(chr(39), chr(39)*2)}' AND pid <> pg_backend_pid();"
    )
    terminate = subprocess.run(
        ["psql", *_pg_args(url), "--dbname", "postgres", "--command", terminate_sql],
        env=env, capture_output=True, text=True,
    )
    if terminate.returncode != 0:
        raise RuntimeError((terminate.stderr or "Could not disconnect active database sessions").strip())

    result = subprocess.run(
        [
            "pg_restore", "--clean", "--if-exists", "--no-owner", "--no-privileges", "--exit-on-error",
            *_pg_args(url), "--dbname", db_name, str(path),
        ],
        env=env, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "pg_restore failed").strip())


def retention_keep_set(backups: Iterable[BackupInfo], *, daily: int, weekly: int, monthly: int) -> set[Path]:
    rows = sorted(backups, key=lambda row: row.created_at, reverse=True)
    keep: set[Path] = set()

    if daily > 0:
        keep.update(row.path for row in rows[:daily])

    seen_weeks: set[tuple[int, int]] = set()
    for row in rows:
        iso = row.created_at.isocalendar()
        key = (iso.year, iso.week)
        if key in seen_weeks:
            continue
        if len(seen_weeks) >= max(0, weekly):
            break
        seen_weeks.add(key)
        keep.add(row.path)

    seen_months: set[tuple[int, int]] = set()
    for row in rows:
        key = (row.created_at.year, row.created_at.month)
        if key in seen_months:
            continue
        if len(seen_months) >= max(0, monthly):
            break
        seen_months.add(key)
        keep.add(row.path)

    return keep


def apply_retention(path: str, *, daily: int, weekly: int, monthly: int) -> list[str]:
    rows = [row for row in list_backups(path) if row.automatic]
    keep = retention_keep_set(rows, daily=daily, weekly=weekly, monthly=monthly)
    removed: list[str] = []
    for row in rows:
        if row.path not in keep:
            row.path.unlink(missing_ok=True)
            removed.append(row.name)
    return removed


def scheduled_backup_due(path: str, *, now: datetime, hour: int) -> bool:
    hour = min(23, max(0, int(hour)))
    scheduled = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if now < scheduled:
        return False
    for row in list_backups(path):
        if row.automatic and row.created_at.date() == now.date() and row.created_at >= scheduled:
            return False
    return True


def safe_backup_path(root: str, filename: str) -> Path:
    if filename != Path(filename).name:
        raise ValueError("Invalid backup filename")
    path = backup_dir(root) / filename
    if not path.is_file() or not filename.startswith("share-manager-") or path.suffix not in {".dump", ".sqlite"}:
        raise ValueError("Backup not found")
    return path


@dataclass(frozen=True)
class BackupPolicy:
    enabled: bool
    schedule_hour: int
    check_interval_minutes: int
    retention_daily: int
    retention_weekly: int
    retention_monthly: int


def get_backup_policy(db, defaults) -> BackupPolicy:
    row = db.get(BackupSettings, 1)
    if row is None:
        row = BackupSettings(
            id=1,
            enabled=True,
            schedule_hour=max(0, min(23, int(defaults.backup_schedule_hour))),
            check_interval_minutes=max(1, int(defaults.backup_check_interval_minutes)),
            retention_daily=max(1, int(defaults.backup_retention_daily)),
            retention_weekly=max(0, int(defaults.backup_retention_weekly)),
            retention_monthly=max(0, int(defaults.backup_retention_monthly)),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    return BackupPolicy(
        enabled=bool(row.enabled),
        schedule_hour=max(0, min(23, int(row.schedule_hour))),
        check_interval_minutes=max(1, int(row.check_interval_minutes)),
        retention_daily=max(1, int(row.retention_daily)),
        retention_weekly=max(0, int(row.retention_weekly)),
        retention_monthly=max(0, int(row.retention_monthly)),
    )
