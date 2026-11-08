# -*- coding: utf-8 -*-
"""Тесты истории расчётов (этап 6): runs, cost_snapshots, idempotентность,
точность Decimal, откаты, безопасная публикация файлов и подтверждение GUID.

Сценарии 21–30 задания + контроль правил app.core.guid (сухая проверка,
транзакционное применение, отказ при конфликте GUID).
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.core import export as exmod
from app.core.calc import compute_cost
from app.core.db import connect, init_db
from app.core.export import ExportError, run_exports
from app.core.guid import apply_guid_updates, plan_guid_updates
from app.core.runs import (DuplicateRun, RunAlreadyFinished,
                           insert_snapshot, snapshot_decimal_values,
                           start_run)
from test_export import (DUMMY_OZ, make_fetcher, make_ozon, make_wb,
                         ok_item)


def _con(tmp_path, name='cost.db'):
    return init_db(tmp_path / name)


def _product(con, sku, manual=0):
    cur = con.execute('INSERT INTO products (sku_1c, manual_price, active)'
                      ' VALUES (?, ?, 1)', (sku, int(manual)))
    con.commit()
    return cur.lastrowid


def _settings(con):
    con.execute("INSERT INTO settings (key, value, valid_from)"
                " VALUES ('ppu_rub_per_kg', 405, '2026-07-01')")
    con.execute("INSERT INTO settings (key, value, valid_from)"
                " VALUES ('margin_pct', 15, '2026-07-01')")
    con.commit()


# ── 21. Создание run ─────────────────────────────────────────────────────────


def test_create_run(tmp_path):
    con = _con(tmp_path)
    run_id = start_run(con, run_key='k-1', user='owner', source_1c='com')
    con.commit()
    row = con.execute('SELECT * FROM runs WHERE run_key = ?',
                      ('k-1',)).fetchone()
    assert row is not None
    assert row['id'] == run_id
    assert row['status'] == 'running'
    assert row['source_1c'] == 'com'
    assert row['user'] == 'owner'


# ── 22/23. Создание snapshots + детализация расходов ─────────────────────────


def test_insert_snapshot_with_extras_detail(tmp_path):
    con = _con(tmp_path)
    pid = _product(con, 'S1')
    run_id = start_run(con, run_key='k-2')
    res = compute_cost(price=Decimal('1000.55'), weight_g=270, ppu_rate=405,
                       margin_pct=15,
                       extra_items=[('Поролон', 110), ('Упаковка', 7.5)])
    insert_snapshot(con, run_id, product_id=pid, result=res,
                    price_meta={'register': 'Reestr',
                                'price_type': 'Закупочная',
                                'supplier': 'ГАВРЮШКИН',
                                'price_date': '2026-11-01',
                                'source_status': 'exact'})
    con.commit()
    snap = con.execute('SELECT * FROM cost_snapshots').fetchone()
    assert snap['run_id'] == run_id and snap['product_id'] == pid
    assert snap['cost'] == res.cost
    assert snap['price_source'] == res.price_source
    assert Decimal(snap['price_1c_exact']) == Decimal('1000.55')
    assert Decimal(snap['ppu_rub_exact']) == Decimal('109.35')
    import json
    extra = json.loads(snap['extras_json'])
    kinds = [(e['kind'], Decimal(e['rub_exact'])) for e in extra['extras']]
    assert ('Поролон', Decimal('110')) in kinds
    assert ('Упаковка', Decimal('7.5')) in kinds
    assert extra['price']['register'] == 'Reestr'
    assert extra['price']['source_status'] == 'exact'


# ── 24. Неизменность истории ────────────────────────────────────────────────


def test_history_immutable(tmp_path):
    from app.core.runs import finish_run
    con = _con(tmp_path)
    pid = _product(con, 'S1')
    run_id = start_run(con, run_key='k-3')
    insert_snapshot(con, run_id, product_id=pid,
                    result=compute_cost(price=1000, margin_pct=15))
    con.commit()
    finish_run(con, run_id, status='done', wb_file='a.xlsx', ozon_file='b.xlsx')
    con.commit()
    snap = con.execute('SELECT cost, price_1c_exact FROM cost_snapshots'
                       ).fetchone()
    assert snap['cost'] == 1150
    assert snap['price_1c_exact'] == '1000'
    # повторное завершение запрещено
    with pytest.raises(RunAlreadyFinished):
        finish_run(con, run_id, status='done')
    # снимок не изменился
    snap2 = con.execute('SELECT cost FROM cost_snapshots').fetchone()
    assert snap2['cost'] == 1150


# ── 25. Повторный запуск без дублирования технической попытки ───────────────


def test_retry_same_run_key_no_duplicate(tmp_path):
    con = _con(tmp_path)
    first = start_run(con, run_key='k-same', user='cli', source_1c='com')
    second = start_run(con, run_key='k-same', user='cli', source_1c='com')
    con.commit()
    assert first == second
    assert con.execute('SELECT COUNT(*) c FROM runs').fetchone()['c'] == 1
    runs = con.execute('SELECT * FROM runs').fetchall()
    assert runs[0]['status'] == 'running'   # повторная попытка того же запуска


def test_done_run_duplicate_rejected(tmp_path):
    from app.core.runs import finish_run
    con = _con(tmp_path)
    run_id = start_run(con, run_key='k-done')
    finish_run(con, run_id, status='done')
    con.commit()
    with pytest.raises(DuplicateRun):
        start_run(con, run_key='k-done')


# ── 26. Откат при ошибке ─────────────────────────────────────────────────────


def test_rollback_on_error(tmp_path):
    from sqlite3 import IntegrityError
    con = _con(tmp_path)
    pid = _product(con, 'S1')
    with pytest.raises(IntegrityError):
        with con:
            start_run(con, run_key='k-rollback')
            insert_snapshot(con, 999, product_id=pid,  # несуществующий run (FK)
                            result=compute_cost(price=1000, margin_pct=15))
    assert con.execute('SELECT COUNT(*) c FROM runs').fetchone()['c'] == 0
    assert con.execute('SELECT COUNT(*) c FROM cost_snapshots'
                       ).fetchone()['c'] == 0


# ── 27/28. Ошибка записи файла: статус error, без частичного результата ──────


def test_file_write_error_marks_run_error(tmp_path, monkeypatch):
    con = _con(tmp_path)
    _settings(con)
    pid = _product(con, 'S1')
    con.execute('INSERT INTO ppu_weights (product_id, weight_g, valid_from)'
                ' VALUES (?, 270, ?)', (pid, '2026-07-01'))
    con.commit()
    p_wb = make_wb(tmp_path / 'wb.xlsx', [('B1', 'S1', '0', None, None)])
    p_oz = make_ozon(tmp_path / 'oz.xlsx', DUMMY_OZ)
    fetch = make_fetcher({'S1': ok_item('S1', '1000')})

    def boom(src, dst):
        raise OSError('disk full')

    monkeypatch.setattr(exmod.os, 'replace', boom)
    with pytest.raises(ExportError):
        run_exports(con=con, calc_date='2026-11-01', wb_path=p_wb,
                    ozon_path=p_oz, out_dir=tmp_path / 'out',
                    fetch_prices=fetch, source_1c='com', persist=True)
    run = con.execute('SELECT * FROM runs').fetchone()
    assert run['status'] == 'error'
    assert 'disk full' in (run['error'] or '')
    # частично готовых файлов нет
    assert list((tmp_path / 'out').glob('*_готово.xlsx')) == []
    assert list((tmp_path / 'out').glob('*_черновик.xlsx')) == []
    assert list((tmp_path / 'out').glob('.tmp_*')) == []


# ── 29. Восстановление после сбоя между БД и публикацией файлов ─────────────


def test_recovery_after_crash_before_publish(tmp_path):
    con = _con(tmp_path)
    _settings(con)
    pid = _product(con, 'S1')
    con.execute('INSERT INTO ppu_weights (product_id, weight_g, valid_from)'
                ' VALUES (?, 270, ?)', (pid, '2026-07-01'))
    con.commit()
    p_wb = make_wb(tmp_path / 'wb.xlsx', [('B1', 'S1', '0', None, None)])
    p_oz = make_ozon(tmp_path / 'oz.xlsx', DUMMY_OZ)
    fetch = make_fetcher({'S1': ok_item('S1', '1000')})

    # «сбой» после фиксации run+snapshots, до публикации файлов
    key = exmod._run_key_signature(p_wb, p_oz, '2026-11-01', 'cli', '')
    run_id = start_run(con, run_key=key, user='cli', source_1c='com')
    insert_snapshot(con, run_id, product_id=pid,
                    result=compute_cost(price=1000, margin_pct=15))
    con.commit()

    # повторная попытка того же запуска — безопасное восстановление
    report = run_exports(con=con, calc_date='2026-11-01', wb_path=p_wb,
                         ozon_path=p_oz, out_dir=tmp_path / 'out',
                         fetch_prices=fetch, source_1c='com', persist=True)
    assert report.run_id == run_id
    runs_rows = con.execute('SELECT status FROM runs').fetchall()
    assert len(runs_rows) == 1
    assert runs_rows[0]['status'] == 'done'
    assert report.wb_path and Path(report.wb_path).exists()
    assert report.ozon_path and Path(report.ozon_path).exists()
    assert 'черновик' in Path(report.wb_path).name     # есть исключения (NO_MATCH)
    assert con.execute('SELECT COUNT(*) c FROM cost_snapshots'
                       ).fetchone()['c'] == 1


# ── 30. Точное хранение Decimal (миграция 002) ──────────────────────────────


def test_decimal_exact_storage_and_legacy_fallback(tmp_path):
    con = _con(tmp_path)
    pid = _product(con, 'S1')
    run_id = start_run(con, run_key='k-dec')
    res = compute_cost(price=Decimal('123456789.123456789'), margin_pct=15)
    insert_snapshot(con, run_id, product_id=pid, result=res)
    # legacy-строка: без *_exact (импорт 001) — читается из REAL
    con.execute(
        "INSERT INTO cost_snapshots (run_id, product_id, price_1c,"
        " price_source, ppu_rate, margin_pct, cost, extras_json)"
        " VALUES (?, ?, 0.1, 'legacy', 405, 15, 0, '{}')",
        (run_id, pid))
    con.commit()
    rows = con.execute('SELECT * FROM cost_snapshots').fetchall()
    exact = snapshot_decimal_values(rows[0])
    assert exact['price_1c'] == Decimal('123456789.123456789')
    legacy = snapshot_decimal_values(rows[1])
    assert isinstance(legacy['price_1c'], Decimal)   # REAL-fallback (документирован)


# ── GUID: сухая проверка и контролируемое применение ────────────────────────


def test_guid_plan_dry_run(tmp_path):
    con = _con(tmp_path)
    p_a = _product(con, 'A')
    p_b = _product(con, 'B')
    con.execute('UPDATE products SET guid_1c = ? WHERE id = ?', ('G-A', p_a))
    con.commit()
    plans = plan_guid_updates(con, [(p_b, 'G-A'), (p_a, 'G-A'),
                                    (p_a, 'G-B'), (999, 'G-X')])
    by = {(p.product_id, p.guid_1c): p.status for p in plans}
    assert by[(p_b, 'G-A')] == 'guid_conflict'
    assert by[(p_a, 'G-A')] == 'unchanged'
    assert by[(p_a, 'G-B')] == 'guid_conflict_existing'
    assert by[(999, 'G-X')] == 'product_missing'
    # никаких изменений БД
    assert con.execute('SELECT guid_1c FROM products WHERE id = ?',
                       (p_b,)).fetchone()['guid_1c'] is None


def test_guid_apply_confirmed_writes_audit(tmp_path):
    con = _con(tmp_path)
    p_a = _product(con, 'A')
    messages, applied = apply_guid_updates(con, [(p_a, 'G-NEW')], user='owner')
    assert applied == 1
    assert con.execute('SELECT guid_1c FROM products WHERE id = ?',
                       (p_a,)).fetchone()['guid_1c'] == 'G-NEW'
    audit = con.execute("SELECT * FROM audit_log WHERE entity='products'"
                        ).fetchone()
    assert audit is not None
    assert audit['user'] == 'owner'
    assert audit['entity_id'] == p_a


def test_guid_apply_conflict_rolls_back(tmp_path):
    con = _con(tmp_path)
    p_a = _product(con, 'A')
    p_b = _product(con, 'B')
    # оба «ok» на плане, но products.guid_1c UNIQUE: второе обновление падает —
    # транзакция целиком откатывается, ни одна пара не сохранена
    with pytest.raises(RuntimeError):
        apply_guid_updates(con, [(p_a, 'G-X'), (p_b, 'G-X')], user='owner')
    assert con.execute('SELECT guid_1c FROM products WHERE id = ?',
                       (p_a,)).fetchone()['guid_1c'] is None
    assert con.execute('SELECT guid_1c FROM products WHERE id = ?',
                       (p_b,)).fetchone()['guid_1c'] is None
    assert con.execute('SELECT COUNT(*) c FROM audit_log').fetchone()['c'] == 0