"""Печать первых строк xlsx без ограничений по шапке.

usage: python peek_xlsx.py <файл.xlsx>   (можно маску: input/B*.xlsx)
"""
# -*- coding: utf-8 -*-
import glob
import sys
from pathlib import Path

from openpyxl import load_workbook


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python peek_xlsx.py <файл.xlsx или маска>")
        return 2
    for raw in glob.glob(sys.argv[1]):
        path = raw
        if not path.lower().endswith((".xlsx", ".xlsm", ".xltx", ".xltm")):
            continue
        _peek(path)
    return 0


def _peek(path: str) -> None:
    wb = load_workbook(path, read_only=True, data_only=True)
    out_path = str(Path(path).with_suffix(".peek.txt"))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"Файл: {path}\nЛисты: {wb.sheetnames}\n")
        for sheet in wb.sheetnames:
            ws = wb[sheet]
            f.write(f"--- Лист '{sheet}'\n")
            n = 0
            for row in ws.iter_rows(values_only=True):
                n += 1
                if n > 8:
                    f.write("...\n")
                    break
                f.write(f"стр.{n}: {[c for c in row]}\n")
        f.write("done\n")
    wb.close()
    print("ok:", out_path)


if __name__ == "__main__":
    raise SystemExit(main())