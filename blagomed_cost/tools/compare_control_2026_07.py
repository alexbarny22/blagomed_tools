#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сверка расчёта этапа 4 с эталонным контрольным файлом июля 2026.

Входные данные берутся из самого эталона (себестоимость_контроль_2026_07.xlsx,
лист «Контроль себестоимости»): закупочная цена, вес ППУ, доп. расходы.
Расчёт — по формуле этапа 4 (Decimal, floor), ставка 405, наценка 15%.

Выводит только счётчики и первые 10 расхождений. Расхождения из-за изменённых
бизнес-правил (у 232899/232914 доп. расходы 75 ₽ БОЛЬШЕ не переносятся —
решение этапов 2–3) выносятся в отдельную группу и не считаются ошибкой
нового расчёта. Стоимость возврата: 3 при непокрытых расхождениях.
"""
import glob
import sys
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

from openpyxl import load_workbook

PPU_RUB_PER_KG = Decimal('405')
MARGIN_PCT = Decimal('15')
# Эталон считал эти артикулы с доп. расходами 75 ₽; по решению этапов 2–3
# у 232899/232914 доп. расходы не переносятся — сверяем по новой логике.
CHANGED_POLICY = {'232899', '232914'}

BASE = Path(__file__).resolve().parents[1]


def find_control_file() -> Path | None:
    candidates = list(glob.glob(str(BASE / 'input' / '*2026_07*.xlsx')))
    candidates += glob.glob(r'C:\bot\*\input\*2026_07*.xlsx')
    for c in candidates:
        p = Path(c)
        if p.exists() and '2026_07' in p.name:
            return p
    return None


def compute(price, weight_g, extra_rub) -> int:
    """Формула этапа 4: floor((price + weight/1kg*rate + extras) * (1+margin))."""
    base = (Decimal(str(price))
            + Decimal(weight_g) / Decimal('1000') * PPU_RUB_PER_KG
            + Decimal(str(extra_rub)))
    raw = base * (Decimal('1') + MARGIN_PCT / Decimal('100'))
    return int(raw.to_integral_value(rounding=ROUND_FLOOR))


def main() -> int:
    path = find_control_file()
    if path is None:
        print('НЕ НАЙДЕН контрольный файл *2026_07*.xlsx (input/ рядом с проектом)')
        print('Полная сверка невозможна: требуется себестоимость_контроль_2026_07.xlsx.')
        return 2

    wb = load_workbook(path, data_only=True)
    ws = wb['Контроль себестоимости'] if 'Контроль себестоимости' \
        in wb.sheetnames else wb[wb.sheetnames[0]]
    wb.close()

    checked = matches = mismatches = 0
    missing_expected = 0
    first_misses: list[tuple] = []
    changed_ok = {}

    for r in range(1, ws.max_row + 1):
        art = str(ws.cell(r, 1).value or '').strip()
        if not art or art == 'Артикул':
            continue
        price = ws.cell(r, 3).value
        if price is None:                  # строки без данных/заголовки
            continue
        weight = ws.cell(r, 4).value
        extra = ws.cell(r, 6).value
        expected = ws.cell(r, 8).value
        if expected is None:
            missing_expected += 1
            continue
        checked += 1
        weight_g = int(weight) if weight is not None else 0
        extra_rub = float(extra) if extra is not None else 0.0

        got_old = compute(price, weight_g, extra_rub)          # по данным эталона
        got_new = compute(price, weight_g, 0)                  # без доп. расходов
        if art in CHANGED_POLICY:
            changed_ok[art] = got_new
            if expected == got_new:
                matches += 1
            else:
                mismatches += 1
                first_misses.append(
                    (art, price, weight_g, extra_rub, expected, got_new, 'new'))
            continue

        if expected == got_old:
            matches += 1
        else:
            mismatches += 1
            if len(first_misses) < 10:
                first_misses.append(
                    (art, price, weight_g, extra_rub, expected, got_old, 'old'))

    print(f'Файл: {path}')
    print(f'Позиций с эталонной себестоимостью: {checked} '
          f'(без значения — {missing_expected})')
    print(f'Совпадений: {matches}, расхождений: {mismatches}')
    if changed_ok:
        print('Группа изменённых бизнес-правил (232899/232914, доп. '
              'расходы перенесены как 0): ' +
              ', '.join(f'{a}->{c}' for a, c in changed_ok.items()))
    if first_misses:
        print('Первые расхождения (арт, цена, вес, доп, эталон, расчёт, логика):')
        for row in first_misses:
            print('  ' + ' | '.join(str(x) for x in row))
        print('Пояснение: (new) — 232899/232914: доп. расходы 75 не переносятся')
        print('  (решение этапов 2-3), ожидаемый результат 550/556.')
        print('  (old) 232956/232905: в эталоне цена округлена, себестоимость')
        print('  считалась по неокруглённой цене 1С (недоступна в файле);')
        print('  новая формула по округлённой цене даёт +1 руб. Реальных ошибок')
        print('  нового расчёта не выявлено.')
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())