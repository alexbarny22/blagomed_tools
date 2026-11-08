# -*- coding: utf-8 -*-
"""Формирование итоговых XLSX WB/Ozon и оркестрация запуска (этап 6).

Принципы (см. docs/CONTEXT.md, раздел «ЭТАП 6»):

- исходные файлы MPSPACE неприкосновенны: SHA-256 каждого файла снимается до
  обработки и проверяется после; результат пишется в НОВЫЙ файл в out_dir;
- изменяется ТОЛЬКО целевая ячейка себестоимости (WB — колонка E, OZON — D);
  открытие оригинала без data_only=True (формулы и оформление сохраняются);
- расчёт выполняется исключительно app.core.calc.calc_product()/compute_cost();
- строгий режим (по умолчанию): если остаётся хотя бы одна неподтверждённая
  позиция — формируются ТОЛЬКО черновики и отчёт исключений; файлы «готово»
  не создаются. Для строк не-OK ячейка себестоимости НЕ перезаписывается
  (ноль/фиктивная цена не подставляются), а отчёт фиксирует, что прежнее
  значение в файле не проверено;
- товар, встречающийся на нескольких карточках, рассчитывается один раз на
  запуск (общий кэш по product_id);
- история: one run-запись на логический запуск (WB+OZON), cost_snapshots с
  точностью Decimal (text-колонки *_exact, миграция 002);
- транзакционность файлов и БД: run+snapshots фиксируются в БД одним
  транзакционным блоком до атомарной публикации файлов (os.replace); между
  коммитом БД и публикацией файлов возможен сбой — повторная попытка того же
  run_key обнаруживается по status='running' и безопасно завершается.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from openpyxl import load_workbook

from app.core import mapping, runs
from app.core.calc import calc_product
from app.core.onec import ItemStatus, OnecItem, OnecRequest

__all__ = [
    'ExportError', 'RowOutcome', 'RunReport',
    'run_exports', 'build_excel', 'verify_ouput_only_target',
    'write_exception_report', 'make_summary', 'sha256_file',
    'unique_output_path', 'HAS_READY_SUFFIX',
]

HAS_READY_SUFFIX = '_готово'
HAS_DRAFT_SUFFIX = '_черновик'

PRICE_STATUS_OK = {ItemStatus.OK}
EXCLUDE_STATUSES = {ItemStatus.AMBIGUOUS, ItemStatus.NOT_FOUND,
                    ItemStatus.NO_PRICE, ItemStatus.NEEDS_CONFIRM}


class ExportError(Exception):
    """Ошибка этапа экспорта (входные файлы, расчёт, публикация)."""


@dataclass(frozen=True)
class RowOutcome:
    """Статус одной строки отчёта после расчёта."""
    marketplace: str
    row_number: int
    mp_article: str | None
    supplier_article: str | None
    size: str | None
    product_id: int | None
    status: str                    # OK / NEEDS_CONFIRM / AMBIGUOUS / ...
    reason: str = ''
    previous_cost: object = None   # прежнее значение ячейки (не проверено)
    cost: int | None = None        # рассчитанная себестоимость (только OK)
    ppu_approved: bool = True
    notes: tuple[str, ...] = ()


@dataclass
class RunReport:
    """Результат одного запуска."""
    calc_date: str
    run_id: int | None
    status: str                    # 'ok' | 'draft' | 'error'
    wb_path: str | None
    ozon_path: str | None
    report_path: str | None
    rows: list[RowOutcome] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    hashes: dict = field(default_factory=dict)
    drafts: bool = False


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 16), b''):
            h.update(chunk)
    return h.hexdigest()


def _calc_date(value) -> str:
    if isinstance(value, date):
        return value.isoformat()
    s = str(value).strip()
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        raise ExportError(f'Некорректная дата расчёта: {value!r}') from None


def _load_catalog(con) -> dict[str, dict]:
    """sku_1c -> запись products (id, sku_1c, name_1c, guid_1c, manual_price)."""
    out = {}
    for r in con.execute(
            'SELECT id, sku_1c, name_1c, guid_1c, manual_price'
            ' FROM products WHERE active = 1'):
        if r['sku_1c']:
            out[str(r['sku_1c']).strip()] = dict(r)
    return out


def _ppu_approved(con, product_id: int, d: str) -> bool:
    row = con.execute(
        'SELECT 1 FROM ppu_weights WHERE product_id = ? AND valid_from <= ?'
        ' LIMIT 1', (product_id, d)).fetchone()
    return row is not None


# ── Excel: копия оригинала, запись только целевой колонки ───────────────────


def _target_sheet(wb, marketplace: str):
    prefix = mapping.MAPPING[marketplace]['sheet']['prefix']
    for title in wb.sheetnames:
        if (title == prefix if marketplace == 'ozon'
                else title.startswith(prefix)):
            return wb[title]
    raise ExportError(f'{marketplace}: лист «{prefix}…» не найден в файле')


def build_excel(src_path, marketplace: str, cost_col: int,
                updates: dict[int, int]):
    """Копия оригинала без data_only (формулы/оформление сохраняются) и
    запись ТОЛЬКО целевых ячеек себестоимости. Возвращает (workbook, changed)."""
    wb = load_workbook(src_path)          # data_only=False — формулы целы
    ws = _target_sheet(wb, marketplace)
    changed = []
    for row_no, value in updates.items():
        ws.cell(row=row_no, column=cost_col).value = int(value)
        changed.append((row_no, value))
    return wb, changed


def verify_ouput_only_target(src_path, dst_path, marketplace, cost_col,
                             updates: dict[int, int]) -> None:
    """Структурная сверка: значение может отличаться только в целевых ячейках."""
    src = load_workbook(src_path, data_only=False)
    try:
        dst = load_workbook(dst_path, data_only=False)
    except Exception:
        src.close()
        raise
    ws_src = _target_sheet(src, marketplace)
    ws_dst = _target_sheet(dst, marketplace)
    diff: list[str] = []
    try:
        if (ws_src.max_row != ws_dst.max_row
                or ws_src.max_column != ws_dst.max_column):
            raise ExportError(
                f'{marketplace}: размеры не совпали: src='
                f'{ws_src.max_row}x{ws_src.max_column}, dst='
                f'{ws_dst.max_row}x{ws_dst.max_column}')
        for r in range(1, ws_src.max_row + 1):
            for c in range(1, ws_src.max_column + 1):
                sv = ws_src.cell(row=r, column=c).value
                dv = ws_dst.cell(row=r, column=c).value
                if c == cost_col and r in updates:
                    continue            # ожидаемое изменение целевой ячейки
                if sv != dv:
                    diff.append(f'строка {r}, колонка {c} (col {c}): '
                                f'исходник {sv!r} vs результат {dv!r}')
        if diff:
            raise ExportError(
                f'{marketplace}: найдены несанкционированные изменения: '
                + '; '.join(diff[:8]))
    finally:
        src.close()
        dst.close()


# ── Сопоставление строк отчёта с товарами и статусы ─────────────────────────


def _map_item_status(item: OnecItem, calc_date: str) -> tuple[str, str]:
    """(status, reason) для строки с ценой из 1С — правила этапа 6.

    Цена допускается к расчёту только при достоверном статусе OK. Ошибка
    источника НЕ приравнивается к «нет цены»; прежние значения ячеек не
    считаются проверенными; отсутствующая цена нулём не подменяется.
    """
    if item.status not in PRICE_STATUS_OK:
        if item.status == ItemStatus.NEEDS_CONFIRM:
            return ('NEEDS_CONFIRM',
                    item.detail or 'сопоставление/источник требуют подтверждения')
        if item.status == ItemStatus.AMBIGUOUS:
            return 'AMBIGUOUS', item.detail or 'неоднозначный GUID/наименование'
        if item.status == ItemStatus.NOT_FOUND:
            return 'NOT_FOUND', 'товар не найден в 1С'
        if item.status == ItemStatus.NO_PRICE:
            return 'NO_PRICE', 'товар найден, закупочная цена отсутствует'
        return ('ERROR',
                f'источник цены: {item.status.value}: {item.detail or ""}'.rstrip())
    if item.price is None:
        return 'NO_PRICE', 'нет закупочной цены (None нулём не подменяется)'
    if item.price_date and calc_date < item.price_date:
        return ('NEEDS_CONFIRM',
                f'цена актуальна на {item.price_date}, что новее даты расчёта '
                f'{calc_date}; за историческую цену она не выдаётся')
    return 'OK', ''


def _price_meta(item: OnecItem | None) -> dict:
    if item is None:
        return {}
    return {
        'register': item.register,
        'price_type': item.price_type,
        'supplier': item.supplier,
        'price_date': item.price_date,
        'source_status': item.source_status,
    }


def _resolve_rows(*, con, wb_rows, oz_rows, calc_date: str,
                  fetch_prices, catalog: dict) -> tuple[list[RowOutcome], list]:
    """Сопоставляет строки с товарами, получает цены и строит RowOutcome.

    Товар рассчитывается ОДИН раз на запуск (кэш по product_id), поэтому
    карточки WB+OZON одного товара получают идентичную себестоимость.
    """
    outcomes: list[RowOutcome] = []
    by_sku = catalog
    matched: list[tuple] = []
    for row in (list(oz_rows) + list(wb_rows)):
        sku = row.supplier_article
        prod = by_sku.get(sku) if sku else None
        matched.append((row, prod))

    prod_by_sku = {pr['sku_1c']: pr for (_, pr) in matched if pr}
    need_price = [pr for pr in prod_by_sku.values()
                  if not pr['manual_price']]

    item_by_pid: dict[int, tuple[object | None, str]] = {}
    if need_price:
        if fetch_prices is None:
            for pr in need_price:
                item_by_pid[pr['id']] = (None,
                                         'цена не запрошена: источник 1С '
                                         'в этом запуске не подключался')
        else:
            requests = [OnecRequest(sku_1c=pr['sku_1c'],
                                    name_1c=pr['name_1c'],
                                    guid_1c=pr['guid_1c'])
                        for pr in need_price]
            items = list(fetch_prices(requests))
            if len(items) != len(requests):
                raise ExportError(
                    f'Источник цен вернул {len(items)} строк вместо '
                    f'{len(requests)} запрошенных')
            for pr, it in zip(need_price, items):
                item_by_pid[pr['id']] = (it, '')

        # 3) Расчёт/статус по каждому уникальному товару (кэш по product_id)
    cache: dict[int, dict] = {}
    for pr in prod_by_sku.values():
        pid = pr['id']
        if pid in cache:
            continue
        if pr['manual_price']:
            try:
                res = calc_product(con, product_id=pid, price=None,
                                   calc_date=calc_date)
            except Exception as e:
                cache[pid] = {'status': 'NO_PRICE',
                              'reason': f'ручная закупочная цена недоступна: {e}',
                              'result': None, 'item': None,
                              'price_meta': {}, 'ppu_approved': False,
                              'notes': ()}
                continue
            cache[pid] = {'status': 'OK', 'reason': '', 'result': res,
                          'item': None, 'price_meta': {},
                          'ppu_approved': _ppu_approved(con, pid, calc_date),
                          'notes': ()}
            continue
        item, why = item_by_pid.get(pid, (None, ''))
        if item is None:
            cache[pid] = {'status': 'NEEDS_CONFIRM', 'reason': why
                          or 'нет подтверждённой цены', 'result': None,
                          'item': None, 'price_meta': {},
                          'ppu_approved': False, 'notes': ()}
            continue
        status, reason = _map_item_status(item, calc_date)
        if status == 'OK':
            try:
                res = calc_product(con, product_id=pid,
                                   price=item.price, calc_date=calc_date)
            except Exception as e:
                cache[pid] = {'status': 'ERROR',
                              'reason': f'ошибка расчёта: {e}',
                              'result': None, 'item': item,
                              'price_meta': _price_meta(item),
                              'ppu_approved': False, 'notes': ()}
                continue
            ppu_ok = _ppu_approved(con, pid, calc_date)
            notes = () if ppu_ok else (
                'вес ППУ не утверждён (нет записи ppu_weights на дату) — '
                'нулевой вес не доказан',)
            cache[pid] = {'status': 'OK', 'reason': '', 'result': res,
                          'item': item, 'price_meta': _price_meta(item),
                          'ppu_approved': ppu_ok, 'notes': notes}
        else:
            cache[pid] = {'status': status, 'reason': reason,
                          'result': None, 'item': item,
                          'price_meta': _price_meta(item),
                          'ppu_approved': False, 'notes': ()}

    # 4) Строки отчёта (карточки одного товара наследуют общий статус)
    for row, prod in matched:
        if prod is None:
            outcomes.append(_outcome_for(
                row, None, 'NO_MATCH',
                'нет соответствия: артикул поставщика не найден в products'))
            continue
        pk = cache[prod['id']]
        oc = _outcome_for(row, prod['id'], pk['status'], pk['reason'],
                          cost=pk['result'].cost if pk['result'] else None,
                          ppu_approved=pk['ppu_approved'],
                          notes=pk['notes'])
        outcomes.append(oc)

    # Уникальные успешно рассчитанные товары (для cost_snapshots)
    ok_unique: list[dict] = []
    seen: set[int] = set()
    for row, prod in matched:
        if prod is None or prod['id'] in seen:
            continue
        seen.add(prod['id'])
        pk = cache[prod['id']]
        if pk['result'] is not None:
            ok_unique.append({'product_id': prod['id'],
                              'result': pk['result'],
                              'price_meta': pk['price_meta'],
                              'ppu_approved': pk['ppu_approved']})
    return outcomes, ok_unique


def _outcome_for(row, product_id, status: str, reason: str = '',
                 cost=None, ppu_approved: bool = True,
                 notes: tuple = ()) -> RowOutcome:
    return RowOutcome(
        marketplace=row.marketplace, row_number=row.row_number,
        mp_article=row.mp_article, supplier_article=row.supplier_article,
        size=row.size, product_id=product_id, status=status, reason=reason,
        previous_cost=row.current_cost, cost=cost, ppu_approved=ppu_approved,
        notes=tuple(notes))


# ── Сводки, отчёт исключений, публикация файлов ──────────────────────────────


def make_summary(rows: list[RowOutcome]) -> dict:
    s = {'wb_total': 0, 'ozon_total': 0, 'ok': 0, 'need_confirm': 0,
         'ambiguous': 0, 'not_found': 0, 'no_price': 0, 'no_match': 0,
         'errors': 0, 'ppu_unapproved': 0}
    for r in rows:
        if r.marketplace == 'wb':
            s['wb_total'] += 1
        else:
            s['ozon_total'] += 1
        st = r.status
        if st == 'OK':
            s['ok'] += 1
            if not r.ppu_approved:
                s['ppu_unapproved'] += 1
        elif st == 'NEEDS_CONFIRM':
            s['need_confirm'] += 1
        elif st == 'AMBIGUOUS':
            s['ambiguous'] += 1
        elif st == 'NOT_FOUND':
            s['not_found'] += 1
        elif st == 'NO_PRICE':
            s['no_price'] += 1
        elif st == 'NO_MATCH':
            s['no_match'] += 1
        else:
            s['errors'] += 1
    s['skipped'] = s['need_confirm'] + s['ambiguous'] + s['not_found'] \
        + s['no_price'] + s['no_match'] + s['errors']
    return s


def write_exception_report(report_path: Path, report: RunReport) -> Path:
    """Человекочитаемый отчёт: нерешённые строки + сводка."""
    mode_text = ('черновики (есть нерешённые позиции)' if report.drafts
                 else 'готовые файлы')
    lines = [
        'ОТЧЁТ ИСКЛЮЧЕНИЙ — себестоимость WB/OZON',
        f'Дата расчёта: {report.calc_date}',
        f'Режим: {mode_text}',
        f'WB исходный: {report.hashes.get("wb")}',
        f'OZON исходный: {report.hashes.get("ozon")}',
        '',
        'Проблемные строки (status <> OK):',
        'marketplace | row | mp_article | supplier_article | product_id | status | reason',
        '-' * 100,
    ]
    for r in report.rows:
        if r.status == 'OK':
            continue
        if r.previous_cost is not None:
            r_extra = (f' | прежнее значение в файле: {r.previous_cost}'
                       ' — НЕ пересчитано и НЕ проверено в этом запуске')
        else:
            r_extra = ''
        notes = ('; ' + '; '.join(r.notes)) if r.notes else ''
        lines.append(f'{r.marketplace} | {r.row_number} | {r.mp_article} | '
                     f'{r.supplier_article} | {r.product_id} | {r.status} | '
                     f'{r.reason}{r_extra}{notes}')
    lines += ['', 'Сводка:']
    sum_keys = [('wb_total', 'всего строк WB'), ('ozon_total', 'всего строк OZON'),
                ('ok', 'успешно рассчитано (OK)'),
                ('skipped', 'пропущено (не OK)'),
                ('need_confirm', 'требуют подтверждения (NEEDS_CONFIRM)'),
                ('ambiguous', 'неоднозначные соответствия (AMBIGUOUS)'),
                ('not_found', 'не найдено в 1С (NOT_FOUND)'),
                ('no_price', 'нет закупочной цены (NO_PRICE)'),
                ('no_match', 'нет товара в каталоге (NO_MATCH)'),
                ('errors', 'ошибки источника/расчёта (ERROR)'),
                ('ppu_unapproved', 'OK, но вес ППУ не утверждён')]
    for key, title in sum_keys:
        lines.append(f'  {title}: {report.summary.get(key, 0)}')
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return report_path


def unique_output_path(out_dir: Path, candidate: str, content_hash: str) -> Path:
    """Безопасное именование: не перезаписывает существующий результат.

    Если файл с тем же содержимым уже существует — возвращает его (идемпотентно);
    иначе — версия _v2, _v3…: имя формируется по правилу версионирования.
    """
    p = out_dir / candidate
    if not p.exists():
        return p
    if content_hash and sha256_file(p) == content_hash:
        return p
    stem, suffix = p.stem, p.suffix
    n = 2
    while True:
        q = out_dir / f'{stem}_v{n}{suffix}'
        if not q.exists():
            return q
        if content_hash and sha256_file(q) == content_hash:
            return q
        n += 1


def _run_key_signature(wb_path: Path, ozon_path: Path, calc_date: str,
                       user: str, salt: str) -> str:
    """Подпись запуска: входные файлы + дата + инициатор (+ явный salt).

    Одинаковые входные данные и параметры -> одинаковый ключ (идемпотентность;
    повторная техническая попытка одного run), отличия (salt/пользователь) —
    новый самостоятельный расчёт периода.
    """
    h = hashlib.sha1()
    for p in (wb_path, ozon_path):
        h.update(str(p).encode('utf-8', 'replace'))
        h.update(sha256_file(p).encode('ascii'))
    h.update(calc_date.encode('ascii'))
    h.update(user.encode('utf-8', 'replace'))
    h.update(str(salt).encode('utf-8', 'replace'))
    return h.hexdigest()[:20]


def _sorted_outcomes(rows: list[RowOutcome]) -> list[RowOutcome]:
    return sorted(rows,
                  key=lambda r: (0 if r.marketplace == 'wb' else 1,
                                 r.row_number))


def run_exports(*, con, calc_date, wb_path, ozon_path, out_dir,
                fetch_prices=None, source_1c: str = 'none', user: str = 'cli',
                mode: str = 'strict', salt: str = '', persist: bool = True,
                ) -> RunReport:
    """Основной запуск: парсинг → цены → расчёт → Excel → история → публикация.

    mode='strict': при любой не-OK строке (включая «OK без утверждённого веса
    ППУ») публикуются ТОЛЬКО черновики и отчёт исключений; готовые файлы не
    выпускаются. persist=False: записи в БД не производятся (dry-run).
    """
    d = _calc_date(calc_date)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    wb_path, ozon_path = Path(wb_path), Path(ozon_path)
    for p in (wb_path, ozon_path):
        if not p.exists():
            raise ExportError(f'Входной файл не найден: {p}')

    hashes = {'wb': sha256_file(wb_path), 'ozon': sha256_file(ozon_path)}

    wb_rows = mapping.parse(wb_path, 'wb')
    oz_rows = mapping.parse(ozon_path, 'ozon')

    catalog = _load_catalog(con) if con is not None else {}
    outcomes, ok_unique = _resolve_rows(
        con=con, wb_rows=wb_rows, oz_rows=oz_rows, calc_date=d,
        fetch_prices=fetch_prices, catalog=catalog)
    summary = make_summary(outcomes)
    drafts = bool(summary['skipped'] > 0 or summary['ppu_unapproved'] > 0) \
        if mode == 'strict' else False

    updates = {'wb': {}, 'ozon': {}}
    for oc in outcomes:
        if oc.status == 'OK' and oc.cost is not None:
            updates[oc.marketplace][oc.row_number] = oc.cost

    stamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')
    tmp_wb = out_dir / f'.tmp_wb_{stamp}.xlsx'
    tmp_oz = out_dir / f'.tmp_ozon_{stamp}.xlsx'
    wb_new, _ = build_excel(wb_path, 'wb',
                            mapping.MAPPING['wb']['cost_col'], updates['wb'])
    oz_new, _ = build_excel(ozon_path, 'ozon',
                            mapping.MAPPING['ozon']['cost_col'], updates['ozon'])
    try:
        wb_new.save(tmp_wb)
        oz_new.save(tmp_oz)
    finally:
        wb_new.close()
        oz_new.close()

    # Структурная сверка и неизменность оригиналов
    verify_ouput_only_target(wb_path, tmp_wb, 'wb',
                             mapping.MAPPING['wb']['cost_col'], updates['wb'])
    verify_ouput_only_target(ozon_path, tmp_oz, 'ozon',
                             mapping.MAPPING['ozon']['cost_col'], updates['ozon'])
    if (sha256_file(wb_path) != hashes['wb']
            or sha256_file(ozon_path) != hashes['ozon']):
        raise ExportError('SHA-256 исходных файлов изменился после обработки!')

    # История: одна транзакция run+snapshots
    run_id = None
    if persist and con is not None:
        key = _run_key_signature(wb_path, ozon_path, d, user, salt)
        existing = runs.get_run_by_key(con, key)
        if existing is not None and existing['status'] == 'done':
            # Идемпотентность: повторный запуск тех же входных данных — новых
            # записей не создаём и уже опубликованные файлы не плодим.
            report = RunReport(
                calc_date=d, run_id=existing['id'], status='ok',
                wb_path=existing['wb_file'], ozon_path=existing['ozon_file'],
                report_path=None, rows=_sorted_outcomes(outcomes),
                summary=summary, hashes=hashes,
                drafts=drafts)
            return _write_report_final(report, out_dir, d, stamp)
        with con:
            run_id = runs.start_run(con, run_key=key, user=user,
                                    source_1c=source_1c)
            exists = con.execute(
                'SELECT 1 FROM cost_snapshots WHERE run_id = ? LIMIT 1',
                (run_id,)).fetchone()
            if exists is None:
                for rec in ok_unique:
                    runs.insert_snapshot(
                        con, run_id, product_id=rec['product_id'],
                        result=rec['result'], price_meta=rec['price_meta'],
                        ppu_approved=rec['ppu_approved'])

    # Публикация: безопасное версионирование имён, атомарный os.replace
    suffix = HAS_DRAFT_SUFFIX if drafts else HAS_READY_SUFFIX
    period = d[:7]
    name_wb = f'WB_Себестоимость_{period}{suffix}.xlsx'
    name_oz = f'OZON_Себестоимость_{period}{suffix}.xlsx'
    try:
        final_wb = unique_output_path(out_dir, name_wb, sha256_file(tmp_wb))
        final_oz = unique_output_path(out_dir, name_oz, sha256_file(tmp_oz))
        os.replace(tmp_wb, final_wb)
        os.replace(tmp_oz, final_oz)
    except OSError as e:
        for t in (tmp_wb, tmp_oz):
            t.unlink(missing_ok=True)
        if run_id is not None and con is not None:
            with con:
                runs.fail_run(con, run_id, error=f'публикация: {e}')
        raise ExportError(f'Публикация файлов не удалась: {e}') from e

    if run_id is not None and con is not None:
        with con:
            runs.finish_run(con, run_id, status='done',
                            wb_file=str(final_wb), ozon_file=str(final_oz))

    report = RunReport(
        calc_date=d, run_id=run_id,
        status='draft' if drafts else 'ok',
        wb_path=str(final_wb), ozon_path=str(final_oz),
        report_path=None, rows=_sorted_outcomes(outcomes),
        summary=summary, hashes=hashes, drafts=drafts)
    return _write_report_final(report, out_dir, d, stamp)


def _write_report_final(report: RunReport, out_dir: Path, calc_date: str,
                        stamp: str) -> RunReport:
    """Пишет отчёт исключений и проставляет его путь в отчёт запуска."""
    report_path = out_dir / f'исключения_{calc_date[:7]}_{stamp}.txt'
    write_exception_report(report_path, report)
    report.report_path = str(report_path)
    return report