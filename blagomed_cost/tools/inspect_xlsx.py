"""Структура отчётов MPSPACE: листы, заголовки, первые строки.

Только чтение. Результат пишется в UTF-8 файл <имя>.inspect.txt,
чтобы кириллица не терялась в консоли Windows.

usage: python inspect_xlsx.py <файл.xlsx> [строка_заголовков=1]
"""

import glob
import sys
from pathlib import Path

from openpyxl import load_workbook


def main() -> int:
    args = list(sys.argv[1:])
    if not args:
        # режим самопоиска: ищем в input/ файлы по маскам
        args = []
        for pat in ("OZON*.xlsx", "*ВБ*.xlsx", "*ВБ*.xlsm"):
            for f in sorted(glob.glob(str(Path("input") / pat))):
                if f not in args:
                    args.append(f)
    if not args:
        print("ничего не найдено")
        return 2

    header_row_default = 3
    for path in args:
        header_row = header_row_default
        try:
            _inspect(path, header_row)
        except Exception as e:  # noqa: BLE001
            print(f"ОШИБКА {path}: {e}")


def _inspect(path: str, header_row: int) -> None:
    wb = load_workbook(path, read_only=True, data_only=True)
    out_path = Path(path).with_suffix(".inspect.txt")
    with out_path.open("w", encoding="utf-8") as f:
        f.write(f"Файл: {path}\n")
        f.write(f"Листы: {wb.sheetnames}\n")
        for sheet in wb.sheetnames:
            ws = wb[sheet]
            total = 0
            for idx, row in enumerate(ws.iter_rows(values_only=True), 1):
                if idx < header_row:
                    continue
                total += 1
                if idx == header_row:
                    f.write(f"Лист '{sheet}' | заголовки: {_clean(row)}\n")
                elif total <= 6:
                    f.write(f"  стр.{idx}: {_clean(row)}\n")
                elif total == 7:
                    f.write("  ...\n")
            f.write(f"-> строк данных (без шапки, с заголовком): {total}\n")
        f.write("done\n")
    wb.close()
    print(f"ok: {out_path}")


def _clean(row) -> list:
    return [(str(c).replace("\n", " ") if c is not None else None) for c in (row or ())]


if __name__ == "__main__":
    raise SystemExit(main())