from datetime import datetime, timedelta
from pathlib import Path

from app.services.backups import BackupInfo, backup_filename, retention_keep_set, scheduled_backup_due


def row(name: str, when: datetime, automatic: bool = True) -> BackupInfo:
    return BackupInfo(name=name, path=Path('/tmp') / name, created_at=when, size=123, automatic=automatic)


def test_backup_filename_marks_automatic_and_manual():
    now = datetime(2026, 9, 8, 3, 0, 0)
    assert backup_filename(now, automatic=True) == 'share-manager-auto-20260908-030000.dump'
    assert backup_filename(now, automatic=False) == 'share-manager-manual-20260908-030000.dump'


def test_schedule_due_after_hour_when_no_automatic_backup(tmp_path):
    now = datetime(2026, 9, 8, 10, 0, 0)
    assert scheduled_backup_due(str(tmp_path), now=now, hour=3) is True


def test_schedule_not_due_before_hour(tmp_path):
    now = datetime(2026, 9, 8, 2, 59, 0)
    assert scheduled_backup_due(str(tmp_path), now=now, hour=3) is False


def test_schedule_not_due_twice_same_day(tmp_path):
    existing = tmp_path / 'share-manager-auto-20260908-030500.dump'
    existing.write_bytes(b'x')
    ts = datetime(2026, 9, 8, 3, 5).timestamp()
    existing.touch()
    import os
    os.utime(existing, (ts, ts))
    assert scheduled_backup_due(str(tmp_path), now=datetime(2026, 9, 8, 12, 0), hour=3) is False


def test_retention_keeps_daily_weekly_and_monthly_union():
    base = datetime(2026, 9, 8, 3, 0)
    rows = [row(f'b{i}.dump', base - timedelta(days=i)) for i in range(70)]
    keep = retention_keep_set(rows, daily=7, weekly=4, monthly=3)
    # The seven newest must always survive.
    assert {r.path for r in rows[:7]}.issubset(keep)
    # Retention should preserve older representative points as well.
    assert any(p not in {r.path for r in rows[:7]} for p in keep)
    months = {(r.created_at.year, r.created_at.month) for r in rows if r.path in keep}
    assert len(months) >= 3
