# -*- coding: utf-8 -*-
"""История расчётов (этап 6): runs + cost_snapshots + точность Decimal.

Отделяет сохранение истории от логики расчёта (app.core.calc) и записи Excel
(app.core.export). Гарантии:

- точность Decimal: денежные значения пишутся ДВАЖДЫ — в существующие REAL-поля
  (для совместимости/отчётов) и в текстовые колонки *_exact (str(Decimal)),
  добавленные миграцией 002. Чтение использует *_exact при наличии;
- идемпотентность: start_run с run_key 'done' запрещает дубли (DuplicateRun);
  run_key со статусом 'running' — повторная техническая попытка того же
  расчёта (вернётся существующий id, дубль не создаётся);
- исторические snapshots не изменяются после создания: запись обновляется
  только в момент вставки, finish_run не трогает cost_snapshots;
- транзакционность: вставка run+snapshots выполняется одним блоком `with con:`
  вызывающим кодом; при исключении sqlite откатывает всё целиком.

extras_json хранит детализацию доп. расходов со статьями и точными суммами,
а также фактические атрибуты цены 1С (регистр, вид цены, поставщик, дата).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

__all__ = [
    'RunError', 'DuplicateRun', 'RunAlreadyFinished', 'RunNotFound',
    'start_run', 'get_run_by_key', 'finish_run', 'fail_run',
    'insert_snapshot', 'insert_snapshots', 'snapshot_decimal_values',
    'run_asset_for_result', 'validate_run_snapshots', 'PPU_UNCONFIRMED',
]

# text-колонка точного Decimal -> REAL-колонка совместимости.
EXACT_PAIRS = (
    ('price_1c_exact', 'price_1c'),
    ('ppu_rate_exact', 'ppu_rate'),
    ('ppu_rub_exact', 'ppu_rub'),
    ('extras_rub_exact', 'extras_rub'),
    ('margin_pct_exact', 'margin_pct'),
)

# Отдельный статус позиции расчёта: себестоимость посчитана, но вес ППУ
# (ppu_weights) не утверждён на дату расчёта. Такие позиции НЕ фиксируются ни
# в cost_snapshots, ни в файлах «готово»: ни вес 0, ни рассчитанный на его
# основе cost не выдаются за проверенную запись (см. export._resolve_rows).
PPU_UNCONFIRMED = 'PPU_UNCONFIRMED'


class RunError(Exception):
    """Ошибка истории расчётов."""


class DuplicateRun(RunError):
    """Запуск с таким run_key уже завершён — повторный расчёт того же периода."""


class RunAlreadyFinished(RunError):
    """Попытка изменить статус уже завершённого (не running) run."""


class RunNotFound(RunError):
    """Run с указанным id не найден."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def get_run_by_key(con: sqlite3.Connection, run_key: str):
    return con.execute(
        'SELECT id, started_at, finished_at, status, source_1c, user,'
        '       wb_file, ozon_file, error'
        ' FROM runs WHERE run_key = ?', (run_key,)).fetchone()


def start_run(con: sqlite3.Connection, *, run_key: str, user: str = 'cli',
              source_1c: str = 'com', wb_file: str | None = None,
              ozon_file: str | None = None) -> int:
    """Создаёт run (status='running') или возвращает id технической попытки.

    - run_key существует и status='done'   -> DuplicateRun (не дублируем).
    - run_key существует и status='running' или 'error' — это повторная
      техническая попытка завершить тот же запуск: возвращаем существующий id,
      дубль не создаём. Содержимое снимков при этом проверяется вызовом
      validate_run_snapshots (export.run_exports): entry-данные (цены 1С, вес
      ППУ, доп. расходы) фактически входят в run_key, поэтому совпадение ключа
      гарантирует, что повторная попытка продолжает ТОТ ЖЕ расчёт.
    """
    existing = get_run_by_key(con, run_key)
    if existing is not None:
        if existing['status'] == 'done':
            raise DuplicateRun(
                f'Запуск с run_key {run_key} уже завершён (id={existing["id"]}). '
                'Для нового самостоятельного расчёта того же периода используйте '
                'другой run_key (поле salt), чтобы не дублировать статистику.')
        return existing['id']
    cur = con.execute(
        'INSERT INTO runs (started_at, status, source_1c, user, wb_file,'
        ' ozon_file, run_key) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (_utcnow(), 'running', source_1c, user, wb_file, ozon_file, run_key))
    return int(cur.lastrowid)
def finish_run(con: sqlite3.Connection, run_id: int, *, status: str = 'done',
               error: str | None = None, wb_file: str | None = None,
               ozon_file: str | None = None) -> None:
    """Завершает run (status='done'|'error'). Не изменяет уже завершённые."""
    if status not in ('done', 'error'):
        raise RunError(f'Недопустимый статус завершения: {status!r}')
    row = con.execute('SELECT status FROM runs WHERE id = ?', (run_id,)).fetchone()
    if row is None:
        raise RunNotFound(f'run id={run_id} не найден')
    if row['status'] != 'running':
        raise RunAlreadyFinished(
            f'run id={run_id} уже завершён (status={row["status"]}) — '
            'историю не перезаписываем')
    con.execute(
        'UPDATE runs SET finished_at = ?, status = ?, error = ?,'
        ' wb_file = ?, ozon_file = ? WHERE id = ?',
        (_utcnow(), status, error, wb_file, ozon_file, run_id))


def fail_run(con: sqlite3.Connection, run_id: int, error: str | None = None,
             **kwargs) -> None:
    """Помечает run ошибкой (status='error'), историю не перезаписывает."""
    finish_run(con, run_id, status='error', error=error, **kwargs)
def _empty_str_to_none(v):
    if isinstance(v, str) and not v.strip():
        return None
    return v


def insert_snapshot(con: sqlite3.Connection, run_id: int, *,
                    product_id: int, result, price_meta: dict | None = None,
                    ppu_approved: bool = False) -> None:
    """Одна запись cost_snapshots по рассчитанной позиции."""
    price_meta = dict(price_meta or {})
    real, exact = {}, {}
    pairs = {
        'price_1c': result.price,
        'ppu_rate': result.ppu_rate,
        'ppu_rub': result.ppu_rub,
        'extras_rub': result.extras_rub,
        'margin_pct': result.margin_pct,
    }
    for col, value in pairs.items():
        d = Decimal(str(value))
        real[col] = float(d)
        exact[col + '_exact'] = str(d)

    extras_json = json.dumps({
        'extras': [{'kind': e.kind, 'rub': float(e.rub),
                    'rub_exact': str(e.rub)} for e in result.extras],
        'price': {
            'register': _empty_str_to_none(price_meta.get('register')),
            'price_type': _empty_str_to_none(price_meta.get('price_type')),
            'supplier': _empty_str_to_none(price_meta.get('supplier')),
            'price_date': _empty_str_to_none(price_meta.get('price_date')),
            'source_status': _empty_str_to_none(price_meta.get('source_status')),
        },
        'ppu_approved': bool(ppu_approved),
    }, ensure_ascii=False)

    con.execute(
        'INSERT INTO cost_snapshots (run_id, product_id, price_1c,'
        ' price_1c_exact, price_source, ppu_weight_g, ppu_rate, ppu_rate_exact,'
        ' ppu_rub, ppu_rub_exact, extras_rub, extras_rub_exact, extras_json,'
        ' margin_pct, margin_pct_exact, cost)'
        ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (run_id, int(product_id), real['price_1c'], exact['price_1c_exact'],
         result.price_source, int(result.ppu_weight_g),
         real['ppu_rate'], exact['ppu_rate_exact'],
         real['ppu_rub'], exact['ppu_rub_exact'],
         real['extras_rub'], exact['extras_rub_exact'], extras_json,
         real['margin_pct'], exact['margin_pct_exact'], int(result.cost)))


def insert_snapshots(con: sqlite3.Connection, run_id: int,
                     results: Iterable, *, price_meta: dict | None = None,
                     ppu_approved: bool = False) -> int:
    """Вставляет снимки по результатам; возвращает число записей."""
    n = 0
    for res in results:
        insert_snapshot(con, run_id, product_id=res.product_id, result=res,
                        price_meta=price_meta, ppu_approved=ppu_approved)
        n += 1
    return n


def run_asset_for_result(result) -> dict:
    """«Актив» снимка по результату расчёта — эталон validate_run_snapshots.

    Все значения приводятся к канонической форме готовой строки cost_snapshots
    (см. insert_snapshot): price/extras_rub — str(Decimal(...)), cost — INTEGER.
    """
    return {
        'price': str(Decimal(str(result.price))),
        'ppu_weight_g': int(result.ppu_weight_g),
        'extras_rub': str(Decimal(str(result.extras_rub))),
        'cost': int(result.cost),
    }


def validate_run_snapshots(con: sqlite3.Connection, run_id: int,
                           expected: dict[int, dict]) -> list[int]:
    """Проверяет полноту snapshots повторной попытки (восстановление после сбоя).

    `expected` — {product_id: asset} эталонных снимков текущего расчёта (см.
    run_asset_for_result). Возвращает product_id, для которых снимок отсутствует
    целиком (их нужно дозаписать — случай «сбой до коммита снимков»).

    Если снимок есть, но его содержимое (цена 1С, вес ППУ, сумма доп. расходов,
    себестоимость) не совпадает с эталоном ИЛИ run содержит снимки позиций,
    которых нет в эталоне, — поднимается RunError: повторная попытка не является
    техническим продолжением того же самого расчёта (входные данные изменились),
    и дописывать «поверх» снимков нельзя.
    """
    rows = con.execute(
        'SELECT id, product_id, price_1c_exact, price_1c, ppu_weight_g,'
        '       extras_rub_exact, extras_rub, cost'
        ' FROM cost_snapshots WHERE run_id = ? ORDER BY id', (run_id,)).fetchall()
    stored: dict[int, dict] = {}
    for r in rows:
        price_raw = (r['price_1c_exact'] if r['price_1c_exact'] is not None
                     else r['price_1c'])
        extras_raw = (r['extras_rub_exact'] if r['extras_rub_exact'] is not None
                      else r['extras_rub'])
        stored[int(r['product_id'])] = {
            'price': str(Decimal(str(price_raw))),
            'ppu_weight_g': int(r['ppu_weight_g']) if r['ppu_weight_g'] is not None else 0,
            'extras_rub': str(Decimal(str(extras_raw))),
            'cost': int(r['cost']),
        }
    extra = sorted(set(stored) - set(expected))
    if extra:
        raise RunError(
            f'run {run_id}: снимки позиций {extra} отсутствуют в текущем расчёте. '
            'Повторная попытка не продолжает тот же run_key (вход изменился) — '
            'запустите расчёт заново (при желании с новым salt).')
    mismatch = [pid for pid in expected
                if pid in stored and stored[pid] != expected[pid]]
    if mismatch:
        bad = ', '.join(f'{pid}: сохранено {stored[pid]} != рассчитано '
                        f'{expected[pid]}' for pid in mismatch)
        raise RunError(
            f'run {run_id}: содержимое снимков позиций {mismatch} не совпадает '
            f'с текущим расчётом ({bad}). Цены 1С / вес ППУ / доп. расходы '
            'изменились — повторная попытка не является тем же расчётом.')
    return [pid for pid in expected if pid not in stored]


def snapshot_decimal_values(row) -> dict[str, Decimal]:
    """Точные Decimal из *_exact; для legacy-строк (NULL) — REAL-значение."""
    d = {}
    for exact_col, real_col in EXACT_PAIRS:
        raw = row[exact_col] if exact_col in row.keys() else None
        if raw is None or str(raw).strip() == '':
            raw = row[real_col] if real_col in row.keys() else None
        try:
            d[real_col] = Decimal(str(raw))
        except Exception:
            d[real_col] = Decimal('0')
    return d