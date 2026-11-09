# -*- coding: utf-8 -*-
"""Загрузка и проверка XLSX: расширение, размер, ZIP-признак, реальное
открытие openpyxl и проверка формата MPSPACE. Оригинал не изменяется."""
from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime
from pathlib import Path
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from app.core.mapping import MappingFormatError, column_map, parse

ALLOWED_EXT = {'.xlsx'}
FORBIDDEN_NAME = set('<>:"/\\|?*')


class UploadError(ValueError):
    """Загрузка отклонена с понятным пользователю текстом."""


def safe_join(root: Path, *parts: str) -> Path:
    """Собирает путь и запрещает выход за пределы root (path traversal)."""
    root = Path(root).resolve()
    target = root.joinpath(*parts).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise UploadError('Недопустимый путь: выход за пределы рабочего '
                          'каталога запрещён') from None
    return target


def _sniff_marketplace(path: Path) -> str | None:
    for mp in ('wb', 'ozon'):
        try:
            column_map(path, mp)
            return mp
        except (MappingFormatError, InvalidFileException, BadZipFile,
                OSError, KeyError, ValueError):
            continue
    return None


def validate_xlsx(path: Path, marketplace: str | None = None) -> dict:
    """Проверяет файл и возвращает {marketplace, rows, sha256, size}."""
    path = Path(path)
    if path.suffix.lower() not in ALLOWED_EXT:
        raise UploadError('Разрешены только файлы .xlsx (не .xls/.csv и др.)')
    if not path.exists() or path.stat().st_size == 0:
        raise UploadError('Файл пустой')
    with open(path, 'rb') as f:
        if f.read(4) != b'PK\x03\x04':
            raise UploadError('Файл не является корректным XLSX (нет '
                              'ZIP-подписи); возможно, он повреждён или это '
                              'файл другого формата')
    mp = marketplace if marketplace in ('wb', 'ozon') else None
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
        wb.close()
    except (BadZipFile, InvalidFileException, OSError, KeyError,
            ValueError) as e:
        raise UploadError(f'Не удалось открыть XLSX: {e}') from None
    if mp is None:
        mp = _sniff_marketplace(path)
        if mp is None:
            raise UploadError(
                'Формат отчёта не распознан: ожидались листы MPSPACE '
                '«Себестоимость за …» (WB) или «Себестоимость» (Ozon).')
    try:
        column_map(path, mp)
        rows = parse(path, mp)
    except MappingFormatError as e:
        raise UploadError(f'Формат файла не соответствует MPSPACE: {e}') \
            from None
    except (BadZipFile, InvalidFileException, OSError, KeyError,
            ValueError) as e:
        raise UploadError(f'Повреждённый XLSX: {e}') from None
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return {'marketplace': mp, 'rows': len(rows), 'sha256': h.hexdigest(),
            'size': path.stat().st_size}


def save_upload(stream, filename: str, uploads_dir: Path,
                max_bytes: int, marketplace: str | None = None) -> dict:
    """Потоково сохраняет файл во временное имя, проверяет и публикует.

    Возвращает {path, marketplace, rows, sha256, size, original_name}.
    """
    uploads_dir = Path(uploads_dir)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    raw_name = (filename or '').replace('\\', '/')
    original = Path(raw_name).name
    bad = (not original or original in ('.', '..') or len(original) > 200
           or any(ch in FORBIDDEN_NAME for ch in original)
           or any(ord(ch) < 32 for ch in original))
    if bad:
        raise UploadError('Недопустимое имя файла')
    if Path(original).suffix.lower() not in ALLOWED_EXT:
        raise UploadError('Разрешены только файлы .xlsx')
    tmp = uploads_dir / f'.tmp_{uuid.uuid4().hex}.xlsx'
    size = 0
    try:
        with open(tmp, 'wb') as out:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise UploadError(
                        f'Файл больше допустимого размера '
                        f'({max_bytes // (1024 * 1024)} МБ)')
                out.write(chunk)
        info = validate_xlsx(tmp, marketplace)
        mp = info['marketplace']
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        final = safe_join(uploads_dir, f'{mp}_{stamp}_{uuid.uuid4().hex[:8]}'
                                      f'.xlsx')
        os.replace(tmp, final)
        info.update({'path': final, 'original_name': original})
        return info
    finally:
        tmp.unlink(missing_ok=True)