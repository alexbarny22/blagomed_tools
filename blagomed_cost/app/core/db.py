#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Слой доступа к SQLite: миграции по PRAGMA user_version, бэкап перед изменением схемы.

Перед любой миграцией делает копию файла БД в backup/ с датой (правило CLINE_PROMPT).
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

BACKUP_DIR = Path(__file__).resolve().parents[2] / 'backup'
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / 'migrations'


def apply_migrations(db_path: Path) -> int:
    """Применяет не применённые миграции migrations/NNN_*.sql.

    - Каждый NNN_*.sql выполняется в отдельной транзакции: BEGIN/COMMIT,
      при ошибке ROLLBACK и повторный выброс исключения.
    - Версия схемы = PRAGMA user_version, берётся из номера файла.
    - Перед изменением существующей базы делается копия в backup/ с датой.
    Возвращает число применённых миграций.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    applied = get_schema_version(db_path) if db_path.exists() else 0
    pending = sorted(p for p in MIGRATIONS_DIR.glob('[0-9][0-9][0-9]_*.sql')
                     if int(p.name[:3]) > applied)
    if not pending:
        return 0

    if db_path.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        shutil.copy2(db_path, BACKUP_DIR / f'{db_path.stem}_{stamp}.db')

    con = connect(db_path)
    con.isolation_level = None  # ручное управление транзакциями в этом потоке
    try:
        for mig in pending:
            stmts = _split_statements(mig.read_text(encoding='utf-8'))
            con.execute('BEGIN')
            try:
                for st in stmts:
                    con.execute(st)
                con.execute(f'PRAGMA user_version = {int(mig.name[:3])}')
                con.execute('COMMIT')
            except BaseException:
                con.execute('ROLLBACK')
                raise
    finally:
        con.close()
    return len(pending)


def _split_statements(sql: str) -> list[str]:
    """Разбивает SQL-скрипт на отдельные операторы.

    Учитывает строки ('...', '...", в т.ч. экранированные `''`/`""`) и комментарии
    (-- и /* */): ';' внутри них текст не режет. Комментарии при разбиении выбрасываются.
    """
    stmts: list[str] = []
    cur: list[str] = []
    i, n = 0, len(sql)
    quote: str | None = None
    while i < n:
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ''
        if quote is not None:
            cur.append(c)
            if c == quote:
                if nxt == quote:           # экранированная кавычка ('' или "")
                    cur.append(nxt)
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if c == '-' and nxt == '-':        # однострочный комментарий
            j = sql.find('\n', i)
            i = n if j == -1 else j
            continue
        if c == '/' and nxt == '*':        # блочный комментарий
            j = sql.find('*/', i + 2)
            i = n if j == -1 else j + 2
            continue
        if c in ("'", '"'):
            quote = c
            cur.append(c)
            i += 1
            continue
        if c == ';':
            st = ''.join(cur).strip()
            if st:
                stmts.append(st)
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    st = ''.join(cur).strip()
    if st:
        stmts.append(st)
    return stmts


def get_schema_version(db_path: Path) -> int:
    """Текущая версия схемы (PRAGMA user_version); 0 — база не инициализирована."""
    con = connect(db_path)
    try:
        return con.execute('PRAGMA user_version').fetchone()[0]
    finally:
        con.close()


def connect(db_path: Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys = ON')
    con.execute('PRAGMA busy_timeout = 5000')
    return con


def init_db(db_path: Path | str) -> sqlite3.Connection:
    """Инициализирует/приводит схему к последней версии и возвращает открытое соединение."""
    apply_migrations(Path(db_path))
    return connect(db_path)