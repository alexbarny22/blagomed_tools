"""excel_io.py — Excel-ввод/вывод себестоимостей.

Содержит:
  * прежний статический API (TemplateSpec, TEMPLATES, find_sheet, read_items,
    write_costs) — обратная совместимость;
  * профили шаблонов из БД (Profile, read_items_profile, write_costs_profile);
  * безопасную загрузку книги (load_workbook_safe) с починкой битого
    xl/styles.xml (невалидный border style у файлов Ozon);
  * автодетект профиля по эталонному файлу (detect_profile).

Зависимости: openpyxl + stdlib. Python 3.10+.
"""

from __future__ import annotations

import os
import re
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterator, List, Optional, Tuple

import openpyxl
from openpyxl.utils import column_index_from_string, get_column_letter

__all__ = [
    'TemplateSpec', 'TEMPLATES', 'find_sheet', 'read_items', 'write_costs',
    'Profile', 'read_items_profile', 'write_costs_profile',
    'load_workbook_safe', 'detect_profile',
]


# ---------------------------------------------------------------------------
# 1. Статический API (обратная совместимость)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TemplateSpec:
    """Описание «вшитого» шаблона (WB / Ozon)."""
    marketplace: str
    sheet_prefix: str
    header_row: int
    data_start: int
    col_mp: str
    col_supplier: str
    col_cost: str


TEMPLATES = {
    'wb': TemplateSpec('wb', 'Себестоимость', 2, 3, 'A', 'B', 'E'),
    'ozon': TemplateSpec('ozon', 'Себестоимость', 3, 4, 'A', 'B', 'D'),
}


def find_sheet(wb: Any, spec: TemplateSpec) -> Any:
    """Ищет лист по префиксу из статического шаблона."""
    for sheet_name in wb.sheetnames:
        if sheet_name.startswith(spec.sheet_prefix):
            return wb[sheet_name]
    raise KeyError(f"Лист с префиксом '{spec.sheet_prefix}' не найден в книге")


# ---------------------------------------------------------------------------
# 2. Безопасная загрузка книги + починка битого styles.xml
# ---------------------------------------------------------------------------

# Допустимые значения атрибута style у элементов границ (OOXML / openpyxl).
VALID_BORDER_STYLES = frozenset({
    'hair', 'medium', 'dotted', 'mediumDashed', 'dashed', 'thin',
    'dashDot', 'dashDotDot', 'double', 'mediumDashDot', 'thick',
    'mediumDashDotDot', 'slantDashDot',
})

# Теги сторон границы: left / right / top / bottom / diagonal / vertical / horizontal
_BORDER_SIDE_TAG_RE = re.compile(
    r'<(left|right|top|bottom|diagonal|vertical|horizontal)\b[^>]*>'
)
# Атрибут style="..." (или style='...') внутри тега
_STYLE_ATTR_RE = re.compile(r'(\sstyle\s*=\s*)(["\'])(.*?)\2', re.DOTALL)


def _fix_styles_xml(xml_bytes: bytes) -> Tuple[bytes, int]:
    """Заменяет невалидные значения style у сторон границ на 'thin'.

    Возвращает (новый xml, число исправленных атрибутов).
    """
    text = xml_bytes.decode('utf-8', errors='replace')
    # Ozon пишет numFmtID вместо numFmtId — openpyxl такого атрибута не знает
    text = text.replace(' numFmtID=', ' numFmtId=')
    counter = 0

    def fix_attr(match: 're.Match') -> str:
        nonlocal counter
        value = match.group(3)
        if value in VALID_BORDER_STYLES:
            return match.group(0)
        counter += 1
        quote = match.group(2)
        return f'{match.group(1)}{quote}thin{quote}'

    def fix_tag(match: 're.Match') -> str:
        return _STYLE_ATTR_RE.sub(fix_attr, match.group(0))

    fixed = _BORDER_SIDE_TAG_RE.sub(fix_tag, text)
    return fixed.encode('utf-8'), counter


# Невалидные значения activePane у <pane> (Ozon пишет 'top' и т.п.)
_INVALID_ACTIVE_PANE_RE = re.compile(
    r'activePane="(?!topLeft|topRight|bottomLeft|bottomRight)[^"]*"'
)


def _fix_worksheet_xml(xml_bytes: bytes) -> bytes:
    """Чинит невалидный activePane в worksheet XML (Ozon: 'top' -> 'bottomLeft')."""
    text = xml_bytes.decode('utf-8', errors='replace')
    return _INVALID_ACTIVE_PANE_RE.sub('activePane="bottomLeft"', text).encode('utf-8')


def _rewrite_styles_in_zip(src_path: str, dst_path: str) -> int:
    """Пересобирает xlsx: чинит styles.xml и worksheet XML. Остальное как есть."""
    fixed_total = 0
    with zipfile.ZipFile(os.fspath(src_path), 'r') as zin, \
            zipfile.ZipFile(os.fspath(dst_path), 'w') as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == 'xl/styles.xml':
                data, fixed_total = _fix_styles_xml(data)
            elif re.match(r'xl/worksheets/sheet\d+\.xml$', item.filename):
                data = _fix_worksheet_xml(data)
            # writestr(item, data) сохраняет имя, дату и тип сжатия исходной записи
            zout.writestr(item, data)
    return fixed_total


@contextmanager
def load_workbook_safe(path: str, data_only: bool = False) -> Iterator[Any]:
    """Контекстный менеджер над openpyxl.load_workbook.

    Если openpyxl падает с ValueError (битый styles.xml — невалидный border
    style у файлов Ozon), книга чинится во временном файле, грузится он,
    а по выходу из контекста временный файл удаляется.
    """
    tmp_path: Optional[str] = None
    wb = None
    try:
        try:
            wb = openpyxl.load_workbook(path, data_only=data_only)
        except ValueError as original_exc:
            if not zipfile.is_zipfile(os.fspath(path)):
                raise
            fd, tmp_path = tempfile.mkstemp(prefix='excel_io_fixed_', suffix='.xlsx')
            os.close(fd)
            try:
                _rewrite_styles_in_zip(path, tmp_path)
            except Exception as repair_exc:  # починить не удалось — летит исходная ошибка
                raise original_exc from repair_exc
            wb = openpyxl.load_workbook(tmp_path, data_only=data_only)
        yield wb
    finally:
        if wb is not None:
            try:
                wb.close()
            except Exception:
                pass
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 3. Вспомогательные утилиты
# ---------------------------------------------------------------------------

def _to_str(value: Any) -> str:
    """Приводит значение ячейки к строке без пробелов по краям."""
    if value is None:
        return ''
    return str(value).strip()


def _as_int(value: Any, default: int) -> int:
    """Мягкое приведение к int (в БД поле может прийти строкой)."""
    if value is None or value == '':
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def _find_sheet_by_name(wb: Any, name: str) -> Any:
    """Сначала точное совпадение имени листа, затем fallback по startswith."""
    name = (name or '').strip()
    if not name:
        raise KeyError('Пустое имя листа в профиле шаблона')
    if name in wb.sheetnames:
        return wb[name]
    for sheet_name in wb.sheetnames:
        if sheet_name.startswith(name):
            return wb[sheet_name]
    raise KeyError(f"Лист '{name}' (или с таким префиксом) не найден в книге")


def _find_sheet_by_prefix(wb: Any, prefix: str) -> Any:
    """Ищет лист по префиксу (часть имени до '{YYYY-MM}')."""
    candidates = [prefix, prefix.strip(), prefix.rstrip()]
    for candidate in candidates:
        if not candidate:
            continue
        for sheet_name in wb.sheetnames:
            if sheet_name.startswith(candidate):
                return wb[sheet_name]
    raise KeyError(f"Лист с префиксом '{prefix}' не найден в книге")


def _format_ym(calc_date: Any) -> str:
    """Возвращает 'YYYY-MM' из строки 'YYYY-MM-DD' / date / None (сегодня)."""
    if calc_date is None:
        return date.today().strftime('%Y-%m')
    if isinstance(calc_date, date):  # datetime — подкласс date
        return calc_date.strftime('%Y-%m')
    text = str(calc_date).strip()
    match = re.match(r'^(\d{4})-(\d{2})', text)
    if match:
        return f'{match.group(1)}-{match.group(2)}'
    raise ValueError(f"calc_date должен быть 'YYYY-MM-DD' или date, получено: {calc_date!r}")


def _looks_like_article(value: Any) -> bool:
    """Грубая эвристика: значение похоже на артикул/число, а не на описание."""
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    text = str(value).strip()
    if not text:
        return False
    low = text.lower()
    if low.startswith('нередактируемое') or low in ('-', '—', '–'):
        return False
    if re.search(r'\d', text):          # есть цифры — почти наверняка артикул
        return True
    if len(text) <= 40 and ' ' not in text:  # короткий токен без пробелов
        return True
    return False


# ---------------------------------------------------------------------------
# 4. Старые read/write на безопасной загрузке
# ---------------------------------------------------------------------------

def read_items(path: str, spec: TemplateSpec) -> List[Dict]:
    """Читает позиции по статическому шаблону (поведение прежнее)."""
    items: List[Dict] = []
    with load_workbook_safe(path, data_only=True) as wb:
        ws = find_sheet(wb, spec)
        col_mp_idx = column_index_from_string(spec.col_mp)
        col_supplier_idx = column_index_from_string(spec.col_supplier)
        for row_num in range(spec.data_start, (ws.max_row or 0) + 1):
            mp_val = _to_str(ws.cell(row=row_num, column=col_mp_idx).value)
            supplier_val = _to_str(ws.cell(row=row_num, column=col_supplier_idx).value)
            if not mp_val and not supplier_val:
                continue
            if not supplier_val:
                continue
            items.append({
                'row': row_num,
                'article_mp': mp_val,
                'article_supplier': supplier_val,
            })
    return items


def write_costs(src_path: str, dst_path: str, spec: TemplateSpec,
                costs: Dict[int, int]) -> int:
    """Пишет только колонку cost по статическому шаблону."""
    written = 0
    with load_workbook_safe(src_path) as wb:
        ws = find_sheet(wb, spec)
        col_cost_idx = column_index_from_string(spec.col_cost)
        for row_num, cost in costs.items():
            ws.cell(row=int(row_num), column=col_cost_idx, value=cost)
            written += 1
        wb.save(dst_path)
    return written


# ---------------------------------------------------------------------------
# 5. Профили шаблонов из БД (таблица template_profiles)
# ---------------------------------------------------------------------------

@dataclass
class Profile:
    """Профиль шаблона, загруженный из таблицы template_profiles."""
    marketplace: str
    name: str
    sheet: str
    header_row: int
    data_row: int
    col_article_mp: str
    col_article_supplier: Optional[str]
    col_cost_write: str

    @classmethod
    def from_row(cls, row: Any) -> 'Profile':
        """Собирает профиль из sqlite3.Row (или обычного dict)."""
        def val(key: str) -> Any:
            try:
                return row[key]
            except (KeyError, IndexError, TypeError):
                return None

        header_row = _as_int(val('header_row'), 1)
        data_row = _as_int(val('data_row'), header_row + 1)
        supplier = val('col_article_supplier')
        supplier = str(supplier).strip().upper() if supplier not in (None, '') else None

        return cls(
            marketplace=_to_str(val('marketplace')),
            name=_to_str(val('name')),
            sheet=_to_str(val('sheet')),
            header_row=header_row,
            data_row=data_row,
            col_article_mp=_to_str(val('col_article_mp')).upper(),
            col_article_supplier=supplier,
            col_cost_write=_to_str(val('col_cost_write')).upper(),
        )


def _read_items_profile_openpyxl(path: str, profile: Profile) -> List[Dict]:
    """Читает позиции по профилю из БД (openpyxl-движок).

    Лист: сначала точное имя profile.sheet, затем fallback по startswith.
    Если col_article_supplier = None — строки без артикула поставщика не
    отбрасываются (достаточно article_mp).
    """
    items: List[Dict] = []
    with load_workbook_safe(path, data_only=True) as wb:
        ws = _find_sheet_by_name(wb, profile.sheet)
        col_mp_idx = column_index_from_string(profile.col_article_mp)
        col_supplier_idx = (column_index_from_string(profile.col_article_supplier)
                            if profile.col_article_supplier else None)

        for row_num in range(profile.data_row, (ws.max_row or 0) + 1):
            mp_val = _to_str(ws.cell(row=row_num, column=col_mp_idx).value)

            if col_supplier_idx is None:
                if not mp_val:
                    continue
                items.append({
                    'row': row_num,
                    'article_mp': mp_val,
                    'article_supplier': None,
                })
                continue

            supplier_val = _to_str(ws.cell(row=row_num, column=col_supplier_idx).value)
            if not mp_val and not supplier_val:
                continue
            if not supplier_val:
                continue
            items.append({
                'row': row_num,
                'article_mp': mp_val,
                'article_supplier': supplier_val,
            })
    return items


def _write_costs_profile_openpyxl(src_path: str, dst_path: str, profile: Profile,
                                  costs: Dict[int, int], calc_date: Any = None) -> int:
    """Пишет только колонку cost по профилю из БД (openpyxl-движок).

    Если в profile.sheet есть '{YYYY-MM}', лист ищется по префиксу до '{',
    переименовывается в 'Себестоимость за 2026-11' (по calc_date) и
    сохраняется под новым именем. Иначе — лист по имени (fallback startswith).
    """
    written = 0
    with load_workbook_safe(src_path) as wb:
        template_name = profile.sheet or ''

        if '{YYYY-MM}' in template_name:
            ym = _format_ym(calc_date)
            prefix = template_name.split('{YYYY-MM}', 1)[0]
            ws = _find_sheet_by_prefix(wb, prefix)
            new_title = template_name.replace('{YYYY-MM}', ym)
            ws.title = new_title  # переименовываем найденный лист перед сохранением
        else:
            ws = _find_sheet_by_name(wb, template_name)

        col_cost_idx = column_index_from_string(profile.col_cost_write)
        for row_num, cost in costs.items():
            ws.cell(row=int(row_num), column=col_cost_idx, value=cost)
            written += 1
        wb.save(dst_path)
    return written


# ---------------------------------------------------------------------------
# 6. Автодетект профиля по эталонному файлу
# ---------------------------------------------------------------------------

def _pick_sheet_for_detect(wb, warnings):
    """Выбрать лист для автодетекта профиля."""
    worksheets = list(wb.worksheets)

    if not worksheets:
        warnings.append("В книге нет ни одного листа")
        return None

    # 'Инструкция' пропускаем, только если есть другие листы
    candidates = [
        ws for ws in worksheets
        if _to_str(ws.title).strip().lower() != "инструкция"
    ]
    if not candidates:
        candidates = worksheets  # остался только 'Инструкция' — используем его

    # Предпочитаем лист, в имени которого есть 'себестоим'
    for ws in candidates:
        if "себестоим" in _to_str(ws.title).lower():
            return ws

    return candidates[0]


def detect_profile(path):
    """Автодетект профиля эталонного файла.

    Возвращает словарь с именем листа, номерами строк заголовков/данных
    и буквами нужных колонок. Ненайденные значения — None.
    """
    result = {
        "sheet": None,
        "header_row": None,
        "data_row": None,
        "col_article_mp": None,
        "col_article_supplier": None,
        "col_cost_write": None,
        "warnings": [],
    }
    warnings = result["warnings"]

    with load_workbook_safe(path, data_only=True) as wb:
        ws = _pick_sheet_for_detect(wb, warnings)
        if ws is None:
            return result

        result["sheet"] = ws.title

        max_row = ws.max_row or 0
        max_col = ws.max_column or 0
        if max_row < 1 or max_col < 1:
            warnings.append(f"Лист '{ws.title}' пуст — заголовки не найдены")
            return result

        # --- поиск строки заголовков в первых 6 строках ---
        header_row = None
        for r in range(1, min(6, max_row) + 1):
            for c in range(1, max_col + 1):
                text = _to_str(ws.cell(row=r, column=c).value).strip().lower()
                if text.startswith("артикул"):
                    header_row = r
                    break
            if header_row is not None:
                break

        if header_row is None:
            warnings.append(
                f"В первых 6 строках листа '{ws.title}' не найден заголовок 'Артикул'"
            )
            return result

        result["header_row"] = header_row

        # --- определение колонок в строке заголовков ---
        col_article_mp = None
        col_article_supplier = None
        col_cost_write = None
        cost_from_new = False  # найден ли приоритетный заголовок 'Новая себестоимость'

        for c in range(1, max_col + 1):
            raw = _to_str(ws.cell(row=header_row, column=c).value).strip()
            if not raw:
                continue
            text = raw.lower()
            letter = get_column_letter(c)

            # Артикул поставщика проверяем раньше основного артикула
            if "поставщик" in text:
                if col_article_supplier is None:
                    col_article_supplier = letter
                continue

            # Основной артикул маркетплейса
            if (text == "артикул" or text.startswith("артикул")) and col_article_mp is None:
                col_article_mp = letter

            # Себестоимость: 'Новая себестоимость' имеет приоритет
            if "новая себестоимость" in text:
                if not cost_from_new:
                    col_cost_write = letter
                    cost_from_new = True
            elif text == "себестоимость" and col_cost_write is None and not cost_from_new:
                col_cost_write = letter

        result["col_article_mp"] = col_article_mp
        result["col_article_supplier"] = col_article_supplier
        result["col_cost_write"] = col_cost_write

        if col_article_mp is None:
            warnings.append(
                f"В строке заголовков {header_row} не найдена колонка 'Артикул'"
            )
        if col_cost_write is None:
            warnings.append(
                f"В строке заголовков {header_row} не найдена колонка себестоимости "
                "('Себестоимость' / 'Новая себестоимость')"
            )

        # --- поиск первой строки данных (максимум 5 пропусков) ---
        data_row = header_row + 1
        if col_article_mp is not None:
            mp_col_idx = column_index_from_string(col_article_mp)
            skipped = 0
            while (
                data_row <= max_row
                and skipped < 5
                and not _looks_like_article(
                    ws.cell(row=data_row, column=mp_col_idx).value
                )
            ):
                data_row += 1
                skipped += 1

        result["data_row"] = data_row if data_row <= max_row else None

    return result


# =============================================================================
# 7. ЧИСТЫЙ XML-ДВИЖОК (zipfile + xml.etree.ElementTree)
# -----------------------------------------------------------------------------
# Читает/пишет .xlsx напрямую по XML, минуя openpyxl. Нужен для «битых»
# эталонов Ozon (styles.xml, activePane в worksheet XML), на которых openpyxl
# падает. При записи пересобирается только XML целевого листа (+
# xl/workbook.xml при переименовании), остальные части zip копируются как есть.
# =============================================================================

import logging
import posixpath
import shutil
from datetime import datetime
from xml.etree import ElementTree as ET

_LOG = logging.getLogger(__name__)

NS_M   = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
NS_R   = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
NS_MC  = 'http://schemas.openxmlformats.org/markup-compatibility/2006'
NS_X14 = 'http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac'

# Регистрируем префиксы ДО сериализации, иначе ET выдаст ns0:/ns1:
ET.register_namespace('', NS_M)
ET.register_namespace('r', NS_R)
ET.register_namespace('mc', NS_MC)
ET.register_namespace('x14ac', NS_X14)

_DECL_RE = re.compile(rb'^\s*<\?xml[^>]*\?>')
_ROOT_TAG_RE = re.compile(rb'<(?![?!])[^>]*>')
_XMLNS_PREFIX_RE = re.compile(rb'xmlns:([A-Za-z_][\w.\-]*)="([^"]*)"')
_MONTH_RE = re.compile(r'^\d{4}-\d{2}$')


def _m(tag: str) -> str:
    """Тег в основном пространстве имён SpreadsheetML."""
    return '{%s}%s' % (NS_M, tag)


class _MemZip(dict):
    """Мини-подмена ZipFile: только .read(name), данные уже в памяти."""

    def read(self, name):
        return self[name]


# ---------------------------------------------------------------------------
# 7.1 Разбор структуры пакета
# ---------------------------------------------------------------------------

def _resolve_part(target: str) -> str:
    """'worksheets/sheet1.xml' -> 'xl/worksheets/sheet1.xml'."""
    t = (target or '').replace('\\', '/').strip()
    if not t:
        return ''
    if t.startswith('/'):
        return t.lstrip('/')
    if not t.startswith('xl/'):
        t = 'xl/' + t
    return posixpath.normpath(t)


def _xml_sheets_map(z) -> dict:
    """{имя_листа: 'xl/worksheets/sheetN.xml'} из workbook.xml + его rels."""
    try:
        wb_root = ET.fromstring(z.read('xl/workbook.xml'))
    except KeyError:
        raise ValueError('xlsx: отсутствует xl/workbook.xml')

    rels = {}
    try:
        rels_root = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
    except KeyError:
        rels_root = None
    if rels_root is not None:
        for rel in rels_root:
            if (rel.get('TargetMode') or '').lower() == 'external':
                continue
            rid = rel.get('Id')
            target = rel.get('Target')
            if rid and target:
                rels[rid] = _resolve_part(target)

    sheets_el = wb_root.find(_m('sheets'))
    if sheets_el is None:
        raise ValueError('xlsx: в workbook.xml нет <sheets>')

    out = {}
    for sh in sheets_el.findall(_m('sheet')):
        name = sh.get('name')
        rid = sh.get('{%s}id' % NS_R)
        if not name or not rid:
            continue
        part = rels.get(rid)
        if part:
            out[name] = part
    return out


def _xml_shared_strings(z) -> list:
    """Список строк из xl/sharedStrings.xml (rich-text склеивается); может не быть."""
    try:
        raw = z.read('xl/sharedStrings.xml')
    except KeyError:
        return []
    root = ET.fromstring(raw)
    out = []
    for si in root.findall(_m('si')):
        out.append(''.join((t.text or '') for t in si.iter(_m('t'))))
    return out


def _xml_find_sheet_part(sheets: dict, wanted: str) -> str:
    """Часть листа по имени: точное совпадение, иначе первый startswith."""
    wanted = (wanted or '').strip()
    if wanted in sheets:
        return sheets[wanted]
    cands = [n for n in sheets if wanted and n.startswith(wanted)]
    if cands:
        return sheets[cands[0]]
    raise ValueError('xlsx: лист %r не найден; есть: %s'
                     % (wanted, ', '.join(sheets)))


# ---------------------------------------------------------------------------
# 7.2 Значения ячеек
# ---------------------------------------------------------------------------

def _cell_col_letters(ref: str) -> str:
    """'BD5' -> 'BD'."""
    m = re.match(r'^([A-Za-z]+)', ref or '')
    return m.group(1).upper() if m else ''


def _col_to_index(letters: str) -> int:
    """'A' -> 1, 'BD' -> 56."""
    if not letters:
        raise ValueError('пустая ссылка на колонку')
    idx = 0
    for ch in letters.upper():
        if not ('A' <= ch <= 'Z'):
            raise ValueError('некорректная колонка: %r' % letters)
        idx = idx * 26 + (ord(ch) - 64)
    return idx


def _num_to_str(raw: str) -> str:
    """'12.0' -> '12', '0.5' -> '0.5', нечисло -> как есть."""
    s = (raw or '').strip()
    if not s:
        return ''
    try:
        f = float(s)
    except ValueError:
        return s
    if f.is_integer() and abs(f) < 1e15:
        return str(int(f))
    return s


def _cell_text(c, shared) -> str:
    """Текст/значение ячейки <c> с учётом t=s / inlineStr / чисел."""
    t = c.get('t')
    v_el = c.find(_m('v'))
    if t == 'inlineStr':
        is_el = c.find(_m('is'))
        if is_el is None:
            return ''
        return ''.join((x.text or '') for x in is_el.iter(_m('t')))
    if v_el is None or v_el.text is None:
        return ''
    raw = v_el.text
    if t == 's':
        try:
            i = int(raw)
        except ValueError:
            return ''
        return shared[i] if 0 <= i < len(shared) else ''
    if t in ('str', 'e'):
        return raw
    if t == 'b':
        return '1' if raw.strip().lower() in ('1', 'true') else '0'
    return _num_to_str(raw)


def _iter_rows(sheet_root):
    """Генератор (row_num, <row>) по <sheetData>; строки без r пропускаются."""
    sheet_data = sheet_root.find(_m('sheetData'))
    if sheet_data is None:
        return
    for row in sheet_data:
        if row.tag != _m('row'):
            continue
        r = (row.get('r') or '').strip()
        if not r.isdigit():
            continue
        yield int(r), row


# ---------------------------------------------------------------------------
# 7.3 Чтение эталонов
# ---------------------------------------------------------------------------

def read_items_xml(path, profile) -> list:
    """Чтение эталонов чистым XML-движком (fallback для read_items_profile)."""
    col_mp = getattr(profile, 'col_article_mp', None)
    if not col_mp:
        raise ValueError('profile.col_article_mp не задан')
    col_mp = str(col_mp).strip().upper()

    col_sup = getattr(profile, 'col_article_supplier', None)
    col_sup = str(col_sup).strip().upper() if col_sup else None

    first_row = getattr(profile, 'data_row', 1) or 1
    try:
        first_row = int(first_row)
    except (TypeError, ValueError):
        first_row = 1

    with zipfile.ZipFile(path, 'r') as z:
        sheets = _xml_sheets_map(z)
        part = _xml_find_sheet_part(sheets, getattr(profile, 'sheet', '') or '')
        shared = _xml_shared_strings(z)
        sheet_root = ET.fromstring(z.read(part))

    items = []
    for rnum, row in _iter_rows(sheet_root):
        if rnum < first_row:
            continue
        vals = {}
        for c in row:
            if c.tag != _m('c'):
                continue
            col = _cell_col_letters(c.get('r') or '')
            if col:
                vals[col] = _cell_text(c, shared)

        mp = (vals.get(col_mp) or '').strip()
        if not mp:                                   # пустой артикул МП — пропуск
            continue
        if col_sup is not None:
            sup = (vals.get(col_sup) or '').strip()
            if not sup:                              # колонка задана -> фильтр
                continue
        else:
            sup = None                               # колонки нет -> не фильтруем
        items.append({'row': rnum, 'article_mp': mp, 'article_supplier': sup})

    items.sort(key=lambda x: x['row'])
    return items


# ---------------------------------------------------------------------------
# 7.4 Запись себестоимости (хирургическая правка XML)
# ---------------------------------------------------------------------------

def _profile_cost_col(profile) -> str:
    """Колонка записи себестоимости: в профиле БД это col_cost_write."""
    for attr in ('col_cost_write', 'col_cost', 'cost_col', 'col_price'):
        v = getattr(profile, attr, None)
        if v:
            col = str(v).strip().upper()
            if re.match(r'^[A-Z]+$', col):
                return col
            raise ValueError('profile.%s=%r — не буквы колонки' % (attr, v))
    raise ValueError('профиль: не задана колонка себестоимости (col_cost_write)')


def _month_key(calc_date) -> str:
    """'YYYY-MM' из строки 'YYYY-MM-DD', date/datetime или None(=сегодня)."""
    if calc_date is None:
        return date.today().strftime('%Y-%m')
    if isinstance(calc_date, str):
        s = calc_date.strip()
        if _MONTH_RE.match(s):
            return s
        if len(s) >= 7 and s[:4].isdigit() and s[5:7].isdigit():
            return s[:7]
        raise ValueError('calc_date=%r не похож на дату' % calc_date)
    if isinstance(calc_date, (datetime, date)):
        return calc_date.strftime('%Y-%m')
    raise TypeError('calc_date: ожидалась str/date/datetime/None, получено %r'
                    % type(calc_date))


def _dump_xml(root, original: bytes = None) -> bytes:
    """Сериализация с сохранением XML-декларации и объявлений xmlns корня."""
    body = ET.tostring(root, encoding='UTF-8', xml_declaration=False)
    if original:
        m = _DECL_RE.match(original)
        decl = m.group(0).strip() if m else b"<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        body = _restore_root_ns_decls(original, body)
    else:
        decl = b"<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
    return decl + b'\r\n' + body


def _restore_root_ns_decls(orig: bytes, out: bytes) -> bytes:
    """Возвращает в корневой тег xmlns:prefix, которые ElementTree потерял
    (могут упоминаться только в значениях, напр. mc:Ignorable="x14ac")."""
    m_out = _ROOT_TAG_RE.search(out)
    m_orig = _ROOT_TAG_RE.search(orig)
    if not m_out or not m_orig:
        return out
    root_out = m_out.group(0)
    extra = []
    for pm in _XMLNS_PREFIX_RE.finditer(m_orig.group(0)):
        if b'xmlns:' + pm.group(1) + b'=' not in root_out:
            extra.append(b' ' + pm.group(0))
    if not extra:
        return out
    if root_out.endswith(b'/>'):
        new_root = root_out[:-2] + b''.join(extra) + b'/>'
    else:
        new_root = root_out[:-1] + b''.join(extra) + b'>'
    return out[:m_out.start()] + new_root + out[m_out.end():]


def _insert_ordered(parent, new_el, key_func, new_key):
    """Вставка дочернего элемента с сохранением порядка по key_func."""
    idx = len(parent)
    for i, ch in enumerate(list(parent)):
        try:
            k = key_func(ch)
        except Exception:
            k = None
        if k is not None and k > new_key:
            idx = i
            break
    parent.insert(idx, new_el)


def _row_key(el):
    r = el.get('r')
    return int(r) if r and r.isdigit() else None


def _cell_key(el):
    col = _cell_col_letters(el.get('r') or '')
    return _col_to_index(col) if col else None


def _set_cell_value(row, col_letters: str, row_num: int, value) -> None:
    """Найти/создать <c r="COL+N">, снять t, очистить детей, положить <v>."""
    ref = '%s%d' % (col_letters, row_num)
    target_idx = _col_to_index(col_letters)

    cell = None
    for c in list(row):
        if c.tag != _m('c'):
            continue
        if _cell_col_letters(c.get('r') or '') == col_letters:
            cell = c
            break

    if cell is None:
        cell = ET.Element(_m('c'), {'r': ref})
        _insert_ordered(row, cell, _cell_key, target_idx)

    # t удаляем (значение теперь числовое), стиль s не трогаем
    if 't' in cell.attrib:
        del cell.attrib['t']
    for ch in list(cell):
        cell.remove(ch)

    if isinstance(value, float) and value.is_integer():
        value = int(value)
    v = ET.SubElement(cell, _m('v'))
    v.text = str(int(value)) if isinstance(value, int) else str(value)


def write_costs_xml(src_path, dst_path, profile, costs: dict,
                    calc_date=None) -> int:
    """Пишет costs {row_num: cost} прямо в XML листа. Возвращает число ячеек."""
    # 1) читаем весь zip в память в исходном порядке
    with zipfile.ZipFile(src_path, 'r') as zin:
        order = zin.infolist()
        parts = _MemZip()
        for info in order:
            parts[info.filename] = zin.read(info.filename)

    # 2) какой лист, и не надо ли переименовать
    wanted = (getattr(profile, 'sheet', '') or '').strip()
    sheets = _xml_sheets_map(parts)
    old_name = None
    new_name = None

    if '{YYYY-MM}' in wanted:
        prefix = wanted.split('{YYYY-MM}', 1)[0]
        month = _month_key(calc_date)
        cands = [n for n in sheets if n.startswith(prefix)]
        if not cands:
            raise ValueError('xlsx: нет листа с префиксом %r; есть: %s'
                             % (prefix, ', '.join(sheets)))
        old_name = cands[0]
        # меняем в имени листа именно YYYY-MM (суффикс/скобки сохраняем)
        m = re.match(r'^(.*?)(\d{4}-\d{2})(.*)$', old_name)
        if m:
            new_name = m.group(1) + month + m.group(3)
        else:
            new_name = wanted.replace('{YYYY-MM}', month)
        sheet_part = sheets[old_name]
    else:
        sheet_part = _xml_find_sheet_part(sheets, wanted)

    written = 0

    # 3) при необходимости — переименование в xl/workbook.xml
    if new_name and new_name != old_name:
        wb_part = 'xl/workbook.xml'
        wb_raw = parts.get(wb_part)
        if wb_raw is None:
            raise ValueError('xlsx: отсутствует xl/workbook.xml')
        wb_root = ET.fromstring(wb_raw)
        renamed = False
        for sh in wb_root.iter(_m('sheet')):
            if sh.get('name') == old_name:
                sh.set('name', new_name)
                renamed = True
                break
        if not renamed:
            raise ValueError('xlsx: не нашёл <sheet name=%r> в workbook.xml' % old_name)
        parts[wb_part] = _dump_xml(wb_root, wb_raw)

    # 4) правка XML листа
    if costs:
        raw = parts[sheet_part]
        root = ET.fromstring(raw)
        sheet_data = root.find(_m('sheetData'))
        if sheet_data is None:
            raise ValueError('xlsx: в %s нет <sheetData>' % sheet_part)

        rows = {}
        for row in list(sheet_data):
            if row.tag != _m('row'):
                continue
            k = _row_key(row)
            if k is not None:
                rows[k] = row

        cost_col = _profile_cost_col(profile)

        for rnum in sorted(costs):
            cost = costs[rnum]
            if cost is None:
                continue
            try:
                rnum = int(rnum)
            except (TypeError, ValueError):
                raise ValueError('costs: ключ %r не номер строки' % (rnum,))

            row = rows.get(rnum)
            if row is None:
                row = ET.Element(_m('row'), {'r': str(rnum)})
                _insert_ordered(sheet_data, row, _row_key, rnum)
                rows[rnum] = row

            _set_cell_value(row, cost_col, rnum, cost)
            written += 1

        parts[sheet_part] = _dump_xml(root, raw)

    # 5) сборка dst в исходном порядке записей
    tmp = None
    if dst_path == src_path:
        tmp = dst_path + '.tmp_xlsx'
        out_path = tmp
    else:
        out_path = dst_path

    with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zout:
        for info in order:
            data = parts[info.filename]
            zout.writestr(info, data)  # ZipInfo сохраняет имя/дату/тип сжатия

    if tmp:
        shutil.move(tmp, dst_path)

    return written


# ---------------------------------------------------------------------------
# 7.5 Публичные функции с фолбэком openpyxl -> чистый XML
# ---------------------------------------------------------------------------

def read_items_profile(path: str, profile: Profile) -> List[Dict]:
    """Читает позиции по профилю: openpyxl, при любой ошибке — чистый XML."""
    try:
        return _read_items_profile_openpyxl(path, profile)
    except Exception:
        return read_items_xml(path, profile)


def write_costs_profile(src_path: str, dst_path: str, profile: Profile,
                        costs: Dict[int, int], calc_date: Any = None) -> int:
    """Пишет себестоимость по профилю: openpyxl, при любой ошибке — чистый XML."""
    try:
        return _write_costs_profile_openpyxl(src_path, dst_path, profile,
                                             costs, calc_date=calc_date)
    except Exception:
        return write_costs_xml(src_path, dst_path, profile, costs,
                               calc_date=calc_date)
