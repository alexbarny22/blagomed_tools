# -*- coding: utf-8 -*-
"""Постоянные регрессионные тесты SQLite-миграций (аудит PR №10).

Покрытие:
- _split_statements: комментарии, кавычки, экранированные кавычки, `;` в строках;
- корректное разделение операторов (включая реальный migrations/001_init.sql);
- rollback при ошибке миграции и неизменность PRAGMA user_version;
- повторное применение миграций (идемпотентность);
- бэкап до изменения схемы;
- резервное копирование и восстановление в WAL-режиме (sqlite3.Connection.backup).
"""
import sqlite3
from pathlib import Path

import pytest

from app.core import db
from app.core.db import (
    _split_statements, apply_migrations, backup_db, connect, get_schema_version,
)

BASE = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = BASE / 'migrations'


# ── _split_statements: комментарии и кавычки ────────────────────────────

def test_split_ignores_semicolons_in_single_quote_strings():
    sql = "INSERT INTO t VALUES ('a;b'); INSERT INTO t VALUES ('ok');"
    stmts = _split_statements(sql)
    assert len(stmts) == 2
    assert "VALUES ('a;b')" in stmts[0]
    assert "VALUES ('ok')" in stmts[1]


def test_split_handles_escaped_quotes():
    sql = "INSERT INTO t VALUES ('it''s; fine');"
    stmts = _split_statements(sql)
    assert len(stmts) == 1
    assert "it''s; fine" in stmts[0]


def test_split_ignores_semicolons_in_double_quote_strings():
    sql = 'CREATE VIEW v AS SELECT s FROM t WHERE s = "a;b"; SELECT 1;'
    stmts = _split_statements(sql)
    assert len(stmts) == 2


def test_split_skips_line_and_block_comments():
    sql = (
        "-- first; comment\n"
        "CREATE TABLE a (x TEXT);\n"
        "/* block; comment ; still */\n"
        "INSERT INTO a VALUES ('s;');\n"
        " ;  -- trailing empty statement\n"
    )
    stmts = _split_statements(sql)
    assert stmts == [
        'CREATE TABLE a (x TEXT)',
        "INSERT INTO a VALUES ('s;')",
    ]


def test_split_real_001_init_sql_into_clean_statements():
    """Реальный файл миграции разбивается без потери операторов и комментариев."""
    text = (MIGRATIONS_DIR / '001_init.sql').read_text(encoding='utf-8')
    stmts = _split_statements(text)
    assert stmts
    assert all(s == s.strip() and s for s in stmts)
    assert all(not s.startswith('--') for s in stmts)
    assert all(not s.startswith('/*') for s in stmts)
    assert stmts[-1] == 'PRAGMA user_version = 1'


# ── apply_migrations: rollback, user_version, идемпотентность ───────────

INIT_001 = """\
CREATE TABLE t1 (a INTEGER);
INSERT INTO t1 VALUES (1);
PRAGMA user_version = 1;
"""

GOOD_002 = """\
CREATE TABLE t2 (a INTEGER);
INSERT INTO t2 VALUES (2);
PRAGMA user_version = 2;
"""

BAD_002 = """\
CREATE TABLE t2 (a INTEGER);
INSERT INTO missing_table (a) VALUES (1);
"""


def _write_migrations(dir_path: Path, name: str, sql: str) -> Path:
    (dir_path / name).write_text(sql, encoding='utf-8')
    return dir_path / name


def test_apply_migrations_is_idempotent(tmp_path, monkeypatch):
    mdir = tmp_path / 'migrations'
    mdir.mkdir()
    _write_migrations(mdir, '001_init.sql', INIT_001)
    db_path = tmp_path / 'cost.db'
    monkeypatch.setattr(db, 'BACKUP_DIR', tmp_path / 'backup')

    assert apply_migrations(db_path, migrations_dir=mdir) == 1
    assert apply_migrations(db_path, migrations_dir=mdir) == 0
    assert get_schema_version(db_path) == 1

    con = connect(db_path)
    try:
        assert [tuple(r) for r in con.execute('SELECT a FROM t1')] == [(1,)]
    finally:
        con.close()


def test_migration_failure_rolls_back_and_keeps_user_version(tmp_path, monkeypatch):
    mdir = tmp_path / 'migrations'
    mdir.mkdir()
    _write_migrations(mdir, '001_init.sql', INIT_001)
    db_path = tmp_path / 'cost.db'
    monkeypatch.setattr(db, 'BACKUP_DIR', tmp_path / 'backup')

    apply_migrations(db_path, migrations_dir=mdir)

    _write_migrations(mdir, '002_bad.sql', BAD_002)
    with pytest.raises(sqlite3.OperationalError):
        apply_migrations(db_path, migrations_dir=mdir)

    # Версия схемы и данные не изменились, неудачная таблица откачена
    assert get_schema_version(db_path) == 1
    con = connect(db_path)
    try:
        assert [tuple(r) for r in con.execute('SELECT a FROM t1')] == [(1,)]
        assert con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='t2'"
        ).fetchone()[0] == 0
    finally:
        con.close()


def test_apply_migrations_allows_recovery_after_fix(tmp_path, monkeypatch):
    """После исправления битой миграции тот же файл применяется, версия растёт."""
    mdir = tmp_path / 'migrations'
    mdir.mkdir()
    _write_migrations(mdir, '001_init.sql', INIT_001)
    db_path = tmp_path / 'cost.db'
    monkeypatch.setattr(db, 'BACKUP_DIR', tmp_path / 'backup')

    apply_migrations(db_path, migrations_dir=mdir)
    _write_migrations(mdir, '002_bad.sql', BAD_002)
    with pytest.raises(sqlite3.OperationalError):
        apply_migrations(db_path, migrations_dir=mdir)

    _write_migrations(mdir, '002_bad.sql', GOOD_002)   # тот же файл исправлен
    assert apply_migrations(db_path, migrations_dir=mdir) == 1
    assert get_schema_version(db_path) == 2
    con = connect(db_path)
    try:
        assert [tuple(r) for r in con.execute('SELECT a FROM t2')] == [(2,)]
    finally:
        con.close()


# ── Бэкап: до изменения схемы, WAL, восстановление ──────────────────────

def test_backup_is_taken_before_schema_change(tmp_path, monkeypatch):
    mdir = tmp_path / 'migrations'
    mdir.mkdir()
    _write_migrations(mdir, '001_init.sql', INIT_001)
    db_path = tmp_path / 'cost.db'
    backup_dir = tmp_path / 'backup'
    monkeypatch.setattr(db, 'BACKUP_DIR', backup_dir)

    apply_migrations(db_path, migrations_dir=mdir)

    _write_migrations(mdir, '002_create.sql', GOOD_002)
    apply_migrations(db_path, migrations_dir=mdir)

    # Бэкап снят ДО второй миграции: версия 1, таблицы t2 ещё нет
    backups = sorted(backup_dir.glob('*.db'))
    assert backups
    con = sqlite3.connect(str(backups[-1]))
    try:
        assert con.execute('PRAGMA user_version').fetchone()[0] == 1
        assert con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='t2'"
        ).fetchone()[0] == 0
        assert con.execute('SELECT a FROM t1').fetchall() == [(1,)]
    finally:
        con.close()


def test_backup_and_restore_in_wal_mode(tmp_path, monkeypatch):
    """Бэкап в WAL-режиме: данные активного WAL включены, основная БД
    не изменяется, копия восстанавливается на тестовую БД."""
    backup_dir = tmp_path / 'backup'
    monkeypatch.setattr(db, 'BACKUP_DIR', backup_dir)

    src = tmp_path / 'src.db'
    con = sqlite3.connect(str(src))
    con.isolation_level = None
    try:
        assert con.execute('PRAGMA journal_mode = WAL').fetchone()[0] == 'wal'
        con.execute('CREATE TABLE t (a TEXT, b INTEGER)')
        con.execute("INSERT INTO t VALUES ('one', 1)")

        # WAL остаётся активным (соединение открыто, чекпойнта нет) в момент бэкапа
        backup1 = backup_db(src)
        assert backup1.parent == backup_dir
        assert backup1.exists()

        # Основная БД не заменена и не испорчена; вторая копия — после новой записи
        con.execute("INSERT INTO t VALUES ('two; data', 2)")
        backup2 = backup_db(src)
        assert backup2 != backup1
        assert con.execute('SELECT COUNT(*) FROM t').fetchone()[0] == 2
        assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    finally:
        con.close()

    for backup, expected in (
            (backup1, [('one', 1)]),
            (backup2, [('one', 1), ('two; data', 2)])):
        # integrity_check и восстановление (restore) на тестовую БД
        con = sqlite3.connect(str(backup))
        try:
            assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        finally:
            con.close()

        restored = tmp_path / f'restored_{backup.name}.db'
        s, d = sqlite3.connect(str(backup)), sqlite3.connect(str(restored))
        try:
            s.backup(d)
        finally:
            s.close()
            d.close()
        rc = sqlite3.connect(str(restored))
        try:
            assert rc.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert list(rc.execute('SELECT a, b FROM t ORDER BY b')) == expected
        finally:
            rc.close()