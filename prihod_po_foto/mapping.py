# -*- coding: utf-8 -*-
"""
Таблица сопоставлений «товар у поставщика → наш товар в 1С» — файл Сопоставления.xlsx в папке бота.

Бот сам дописывает строку, когда вы выбираете товар кнопкой. Таблицу можно заполнять и править в Excel:
по одной строке на каждое название (или код) у поставщика. Наш товар ищется по «Коду в 1С», а если код
пустой — по точному наименованию.
"""
import datetime as dt
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(HERE, "Сопоставления.xlsx")
PENDING = os.path.join(HERE, "mapping_pending.json")   # что не удалось записать, пока файл был открыт в Excel
OLD_JSON = os.path.join(HERE, "mapping.json")           # прежний служебный формат — переносится один раз
HEAD = ["Поставщик", "ИНН поставщика", "Название у поставщика", "Код у поставщика",
        "Наш товар в 1С", "Код в 1С", "Добавлено"]
WIDTH = [28, 15, 50, 18, 50, 14, 12]

_cache = {"mtime": None, "rows": []}


class Locked(Exception):
    """Файл открыт в Excel — Windows не даёт его перезаписать."""


def norm(s):
    s = str(s or "").lower().replace("ё", "е")
    s = re.sub(r"[\"'«»“”.,;:()\[\]/\\_\-–—№#*+]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _digits(s):
    return re.sub(r"\D", "", str(s or ""))


def _new_book(rows):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "Сопоставления"
    ws.append(HEAD)
    for c, w in zip(ws[1], WIDTH):
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="DDEBF7")
        ws.column_dimensions[c.column_letter].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{ws.cell(1, len(HEAD)).column_letter}1"
    for r in rows:
        ws.append([r.get(h, "") for h in HEAD])
    return wb


def _save(wb):
    tmp = FILE + ".tmp.xlsx"
    wb.save(tmp)
    try:
        os.replace(tmp, FILE)
    except PermissionError:
        os.remove(tmp)
        raise Locked("Файл «Сопоставления.xlsx» открыт в Excel — закройте его, и бот допишет строку.")


def _migrate_old():
    """Перенос из старого mapping.json (если был) в таблицу."""
    rows = []
    try:
        with open(OLD_JSON, encoding="utf-8") as f:
            old = json.load(f)
    except Exception:
        return rows
    for inn, items in old.items():
        for k, v in items.items():
            if k.startswith("n:"):
                rows.append({"ИНН поставщика": "" if inn == "без_инн" else inn, "Название у поставщика": k[2:],
                             "Наш товар в 1С": v.get("name", ""), "Добавлено": "перенесено"})
    return rows


def load():
    """Строки таблицы (список словарей). Перечитывается, только если файл изменился."""
    if not os.path.exists(FILE):
        _save(_new_book(_migrate_old()))
    mt = os.path.getmtime(FILE)
    if _cache["mtime"] == mt:
        return _cache["rows"]
    from openpyxl import load_workbook
    wb = load_workbook(FILE, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    head = [str(h or "").strip() for h in next(it, [])]
    idx = {h: head.index(h) for h in HEAD if h in head}
    rows = []
    for r in it:
        row = {h: ("" if i >= len(r) or r[i] is None else str(r[i]).strip()) for h, i in idx.items()}
        if row.get("Название у поставщика") or row.get("Код у поставщика"):
            rows.append(row)
    wb.close()
    _cache.update(mtime=mt, rows=rows)
    return rows


def lookup(inn, line):
    """Ищет строку таблицы для позиции накладной: сначала по коду поставщика, потом по названию."""
    _flush_pending()
    inn = _digits(inn)
    code, name = norm(line.get("article")), norm(line.get("name"))
    by_name = None
    for r in load():
        if _digits(r.get("ИНН поставщика")) not in ("", inn):
            continue
        if code and norm(r.get("Код у поставщика")) == code:
            return r
        if name and norm(r.get("Название у поставщика")) == name and by_name is None:
            by_name = r
    return by_name


def add(supplier, inn, line, our_name, our_code):
    """Запомнить связку. Если файл открыт в Excel — строка ждёт в очереди и запишется позже."""
    row = {"Поставщик": supplier or "", "ИНН поставщика": _digits(inn),
           "Название у поставщика": line.get("name", ""), "Код у поставщика": line.get("article", ""),
           "Наш товар в 1С": our_name, "Код в 1С": our_code or "",
           "Добавлено": dt.date.today().strftime("%d.%m.%Y")}
    pend = _read_pending()
    pend.append(row)
    _write_pending(pend)
    _flush_pending()


def remove(inn, line):
    """Удалить связку для позиции (когда сопоставили неверно)."""
    from openpyxl import load_workbook
    load()
    wb = load_workbook(FILE)
    ws = wb.worksheets[0]
    head = [str(c.value or "").strip() for c in ws[1]]
    ci = {h: head.index(h) + 1 for h in HEAD if h in head}
    inn, code, name = _digits(inn), norm(line.get("article")), norm(line.get("name"))
    for r in range(ws.max_row, 1, -1):
        v = lambda h: ws.cell(r, ci[h]).value if h in ci else ""   # noqa: E731
        if _digits(v("ИНН поставщика")) not in ("", inn):
            continue
        if (name and norm(v("Название у поставщика")) == name) or (code and norm(v("Код у поставщика")) == code):
            ws.delete_rows(r)
    _save(wb)
    _cache["mtime"] = None


def _read_pending():
    try:
        with open(PENDING, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _write_pending(rows):
    if rows:
        with open(PENDING, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
    elif os.path.exists(PENDING):
        os.remove(PENDING)


def _flush_pending():
    """Дописывает в таблицу всё из очереди; одинаковое название у того же поставщика заменяется."""
    pend = _read_pending()
    if not pend:
        return
    from openpyxl import load_workbook
    load()
    wb = load_workbook(FILE)
    ws = wb.worksheets[0]
    head = [str(c.value or "").strip() for c in ws[1]]
    ci = {h: head.index(h) + 1 for h in HEAD if h in head}
    for row in pend:
        inn, name = _digits(row["ИНН поставщика"]), norm(row["Название у поставщика"])
        for r in range(ws.max_row, 1, -1):
            if (_digits(ws.cell(r, ci["ИНН поставщика"]).value) == inn and
                    norm(ws.cell(r, ci["Название у поставщика"]).value) == name):
                ws.delete_rows(r)
        ws.append([row.get(h, "") for h in head])
    try:
        _save(wb)
    except Locked:
        return              # запишем при следующем обращении
    _write_pending([])
    _cache["mtime"] = None


def pending_count():
    return len(_read_pending())


def replace_file(data: bytes):
    """Заменить таблицу присланным файлом (после правки в Excel). Проверяет заголовки."""
    import io
    from openpyxl import load_workbook
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True)
    except Exception:
        raise ValueError("это не файл Excel (.xlsx) или он повреждён")
    head = [str(c or "").strip() for c in next(wb.worksheets[0].iter_rows(values_only=True), [])]
    wb.close()
    need = {"Название у поставщика", "Наш товар в 1С"}
    if not need <= set(head):
        raise ValueError("В файле нет нужных колонок: " + ", ".join(sorted(need - set(head))))
    tmp = FILE + ".tmp.xlsx"
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        os.replace(tmp, FILE)
    except PermissionError:
        os.remove(tmp)
        raise Locked("Файл «Сопоставления.xlsx» открыт в Excel на компьютере бота — закройте его и пришлите снова.")
    _cache["mtime"] = None
    return len(load())
