#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сверка этапа 4 с эталонным контрольным файлом июля 2026 — БЕЗ дублирования
формулы.

Данные читаются только из эталона (себестоимость_контроль_2026_07.xlsx,
лист «Контроль себестоимости»): закупочная цена, вес ППУ, доп. расходы
(наполнитель), себестоимость. Эталон не изменяется.

Расчёт выполняет ЕДИНСТВЕННАЯ формула продукта app.core.calc.compute_cost()
(Decimal, ROUND_FLOOR); собственная копия формулы отсутствует.

Результаты разбиваются на группы:
  1) историческое совпадение: расчёт по данным эталона == числу эталона;
  2) 232899/232914 — изменённые бизнес-правила (этапы 2–3): доп. расход 75 ₽
     больше не переносится. Печатаются ОБА значения — старая логика (с доп.
     75, обязана совпасть с эталоном) и новая логика (без доп., обязана дать
     550/556). Расхождения этой группы объяснены правилами и ошибкой не
     считаются;
  3) НЕВЫЯСНЕННЫЕ расхождения: совпадения нет ни в одной из групп. Причина не
     объявляется доказанной без исходных данных (неокруглённая цена 1С есть
     только у поставщика). По 232956/232905 правдоподобно влияние округления
     исходной цены, но это НЕ ДОКАЗАНО.

Exit code: 0 — невыясненных расхождений нет; 1 — есть невыясненные; 2 — эталон
не найден.
"""
import glob
import sys
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from openpyxl import load_workbook

from app.core.calc import compute_cost

PPU_RUB_PER_KG = Decimal('405')
MARGIN_PCT = Decimal('15')
# Доп. расход в эталоне и ожидаемый результат НОВОЙ логики (без переноса 75).
POLICY_CHANGED = {
    '232899': {'extra': Decimal('75'), 'new_expected': 550},
    '232914': {'extra': Decimal('75'), 'new_expected': 556},
}


def find_control_file() -> Path | None:
    candidates = list(glob.glob(str(BASE / 'input' / '*2026_07*.xlsx')))
    candidates += glob.glob(r'C:\bot\*\input\*2026_07*.xlsx')
    for c in candidates:
        p = Path(c)
        if p.exists() and '2026_07' in p.name:
            return p
    return None


def run_calc(price, weight_g, extra_rub, label):
    """Единственный путь расчёта — compute_cost() продукта."""
    extra_items = [(label, extra_rub)] if extra_rub is not None else None
    return compute_cost(
        price=price, weight_g=weight_g, ppu_rate=PPU_RUB_PER_KG,
        margin_pct=MARGIN_PCT, extra_items=extra_items)

def price_interval_for(expected, weight_g, extra_rub) -> str:
    """Интервал исходной (неокруглённой) цены, дающий эталонное значение.

    Используется только для ХАРАКТЕРИЗАЦИИ гипотезы округления: показывает,
    какая неокруглённая цена 1С могла бы дать эталон (проверить её нельзя —
    в файле данные только округлённые).
    """
    w = (Decimal(weight_g) / Decimal('1000')) * PPU_RUB_PER_KG
    lo = Decimal(expected) / (Decimal('1') + MARGIN_PCT / Decimal('100')) - w \
        - (extra_rub or Decimal('0'))
    hi = (Decimal(expected) + Decimal('1')) / \
        (Decimal('1') + MARGIN_PCT / Decimal('100')) - w - (extra_rub or Decimal('0'))
    return f'[{lo.to_integral_value(rounding=ROUND_FLOOR)};' \
        f' {hi.quantize(Decimal("0.01"))})'


def main() -> int:
    path = find_control_file()
    if path is None:
        print('НЕ НАЙДЕН контрольный файл *2026_07*.xlsx (input/ рядом с проектом)')
        return 2

    wb = load_workbook(path, data_only=True)
    ws = wb['Контроль себестоимости'] if 'Контроль себестоимости' \
        in wb.sheetnames else wb[wb.sheetnames[0]]
    wb.close()

    groups = {'historic': 0, 'policy': [], 'unexplained': []}

    for r in range(1, ws.max_row + 1):
        art = str(ws.cell(r, 1).value or '').strip()
        if not art or art == 'Артикул':
            continue
        price = ws.cell(r, 3).value
        if price is None:                # строки без данных/заголовки
            continue
        weight_v = ws.cell(r, 4).value
        extra_v = ws.cell(r, 6).value
        label = str(ws.cell(r, 7).value or 'доп.расход').strip()[:30]
        expected = ws.cell(r, 8).value
        if expected is None:
            continue

        weight_g = int(weight_v) if weight_v is not None else 0
        extra_rub = Decimal(str(extra_v)) if extra_v is not None else None
        expected = int(expected)

        r_old = run_calc(price, weight_g, extra_rub, label)
        if art in POLICY_CHANGED:
            # новая логика: доп. расход 75 БОЛЬШЕ не переносится
            r_new = run_calc(price, weight_g, None, label)
            ok_old = r_old.cost == expected
            ok_new = r_new.cost == POLICY_CHANGED[art]['new_expected']
            groups['policy'].append(
                (art, expected, r_old.cost, r_new.cost, ok_old, ok_new))
            if not (ok_old and ok_new):
                groups['unexplained'].append(
                    (art, price, weight_g, extra_rub, expected, r_new.cost,
                     f'старая логика не повторила эталон '
                     f'({r_old.cost} != {expected})'))
            continue

        if r_old.cost == expected:
            groups['historic'] += 1
        else:
            groups['unexplained'].append(
                (art, price, weight_g, extra_rub, expected, r_old.cost, ''))

    print(f'Файл: {path}')
    print(f'Группа 1 — совпадения с историческим эталоном: {groups["historic"]}')
    for art, exp, old, new, ok_old, ok_new in groups['policy']:
        print(f'Группа 2 — {art}: эталон={exp}, старая логика={old} '
              f'({"совпала" if ok_old else "НЕ СОВПАЛА"}), новая логика={new} '
              f'({POLICY_CHANGED[art]["new_expected"]} '
              f'{"ok" if ok_new else "ОШИБКА!"})')
    if groups['unexplained']:
        print(f'Группа 3 — НЕВЫЯСНЕННЫЕ расхождения: '
              f'{len(groups["unexplained"])}')
        for art, price, w, extra, expected, got, note in groups['unexplained']:
            print(f'  {art}: расчёт={got}, эталон={expected}, Δ={got - expected:+d} '
                  f'(цена={price}, вес={w}, доп.={extra})')
            if not note:
                print(f'    гипотеза «эталон считал по неокруглённой цене 1С '
                      f'из интервала {price_interval_for(expected, w, extra)}» — '
                      f'исходная цена 1С в файле отсутствует, ДОКАЗАТЬ НЕЛЬЗЯ.')
            else:
                print('   ', note)

    print()
    print('Контрольные артикулы (итог):')
    for art, cfg in POLICY_CHANGED.items():
        res = next((n for a, e, o, n, _, _ in groups['policy'] if a == art), '—')
        print(f'  {art}: новая логика = {res} ₽ (требование {cfg["new_expected"]} ₽)')

    unexplained_n = len(groups['unexplained'])
    print()
    if unexplained_n:
        print(f'ИТОГ: {unexplained_n} НЕВЫЯСНЕННЫХ расхождений — причина не '
              f'доказана, сверка НЕ пройдена. exit code 1.')
        return 1
    print('ИТОГ: невыясненных расхождений нет — сверка пройдена. exit code 0.')
    return 0


if __name__ == '__main__':
    sys.exit(main())

