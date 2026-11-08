# -*- coding: utf-8 -*-
"""Контролируемое подтверждение GUID 1С (этап 6, раздел «Запись GUID в SQLite»).

Никакой массовой записи GUID по совпадению артикула не производится.
Подтверждение относится к ТОЧНОЙ паре product_id + guid_1c и выполняется
только явно (apply-confirmed). Перед записью каждая пара проверяется:

- товар с product_id существует (products);
- GUID уникален — не занят другим товаром (products.guid_1c UNIQUE);
- отсутствует конфликт с текущей связью товара: если у товара уже
  установлен ДРУГОЙ guid_1c, операция отклоняется (автоматическая
  перезапись существующего GUID запрещена);
- показывается старое и новое значение;
- пишется запись audit_log;
- применяется в одной транзакции: при ошибке — полный откат (ни одна
  пара не записывается частично).

Режимы:
- dry_run: только диагностика кандидатов plan_guid_updates(), БД не меняется;
- apply-confirmed: применяются только пары со статусом 'ok'.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

__all__ = ['GuidPlan', 'plan_guid_updates', 'apply_guid_updates']


@dataclass(frozen=True)
class GuidPlan:
    """Вердикт одной пары товар->GUID (без изменения БД)."""
    product_id: int
    guid_1c: str
    old_guid_1c: str | None
    status: str          # 'ok' | 'unchanged' | 'guid_conflict' | 'product_missing' | 'guid_conflict_existing'
    reason: str = ''


def _norm_guid(guid) -> str | None:
    if guid is None:
        return None
    s = str(guid).strip()
    return s or None


def plan_guid_updates(con: sqlite3.Connection, pairs,
                      ) -> list[GuidPlan]:
    """Сухая проверка пар [(product_id, guid_1c), ...] без изменения БД."""
    plans: list[GuidPlan] = []
    for product_id, guid in pairs:
        guid = _norm_guid(guid)
        try:
            pid = int(product_id)
        except (TypeError, ValueError):
            plans.append(GuidPlan(product_id, str(guid), None,
                                  'product_missing', 'product_id не число'))
            continue
        if not guid:
            plans.append(GuidPlan(pid, '', None, 'guid_empty',
                                  'guid_1c пуст - запись невозможна'))
            continue
        prod = con.execute(
            'SELECT id, guid_1c, sku_1c FROM products WHERE id = ?',
            (pid,)).fetchone()
        if prod is None:
            plans.append(GuidPlan(pid, guid, None, 'product_missing',
                                  f'товар id={pid} не существует'))
            continue
        owner = con.execute(
            'SELECT id, sku_1c FROM products WHERE guid_1c = ? AND id != ?',
            (guid, pid)).fetchone()
        if owner is not None:
            plans.append(GuidPlan(
                pid, guid, prod['guid_1c'], 'guid_conflict',
                f'GUID уже занят товаром id={owner["id"]} '
                f'(sku={owner["sku_1c"]}) — привязка отклонена'))
            continue
        old = prod['guid_1c']
        if old and old.strip() and _norm_guid(old) == guid:
            plans.append(GuidPlan(pid, guid, old, 'unchanged',
                                  'GUID уже установлен'))
        elif old and old.strip():
            plans.append(GuidPlan(
                pid, guid, old, 'guid_conflict_existing',
                f'у товара уже установлен ДРУГОЙ GUID {old!r} — '
                'автоматическая перезапись запрещена'))
        else:
            plans.append(GuidPlan(pid, guid, old, 'ok', 'подтверждено'))
    return plans


def apply_guid_updates(con: sqlite3.Connection, pairs, *, user: str = 'cli',
                       ) -> tuple[list[str], int]:
    """Применяет только подтверждённые (status='ok') пары в одной транзакции.

    Возвращает (список сообщений-диагностики, число применённых пар).
    Любая ошибка откатывает всю транзакцию: частично применённых связей нет.
    """
    messages: list[str] = []
    applied = 0
    try:
        with con:
            for plan in plan_guid_updates(con, pairs):
                if plan.status != 'ok':
                    messages.append(
                        f'[SKIP] product_id={plan.product_id} guid={plan.guid_1c}: '
                        f'{plan.status}: {plan.reason}')
                    continue
                con.execute(
                    'UPDATE products SET guid_1c = ? WHERE id = ?',
                    (plan.guid_1c, plan.product_id))
                con.execute(
                    'INSERT INTO audit_log (user, entity, entity_id, old, new)'
                    ' VALUES (?, ?, ?, ?, ?)',
                    (user, 'products', plan.product_id,
                     json.dumps({'guid_1c': plan.old_guid_1c}, ensure_ascii=False),
                     json.dumps({'guid_1c': plan.guid_1c}, ensure_ascii=False)))
                applied += 1
                messages.append(
                    f'[OK] product_id={plan.product_id}: guid '
                    f'{plan.old_guid_1c!r} -> {plan.guid_1c!r}')
    except Exception as e:
        raise RuntimeError(
            f'Применение GUID отменено, транзакция откачена: {e}') from e
    return messages, applied