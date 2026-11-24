import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Any
import openpyxl
from openpyxl.utils import column_index_from_string


@dataclass(frozen=True)
class TemplateSpec:
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
    for sheet_name in wb.sheetnames:
        if sheet_name.startswith(spec.sheet_prefix):
            return wb[sheet_name]
    raise KeyError(f"Лист с префиксом '{spec.sheet_prefix}' не найден в книге")


def read_items(path: str, spec: TemplateSpec) -> List[Dict]:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = find_sheet(wb, spec)
    col_mp_idx = column_index_from_string(spec.col_mp)
    col_supplier_idx = column_index_from_string(spec.col_supplier)
    items = []
    for row_num in range(spec.data_start, ws.max_row + 1):
        mp_val = ws.cell(row=row_num, column=col_mp_idx).value
        supplier_val = ws.cell(row=row_num, column=col_supplier_idx).value
        if mp_val is not None:
            mp_val = str(mp_val).strip()
        if supplier_val is not None:
            supplier_val = str(supplier_val).strip()
        if (mp_val is None or mp_val == '') and (supplier_val is None or supplier_val == ''):
            continue
        if supplier_val is None or supplier_val == '':
            continue
        items.append({'row': row_num, 'article_mp': mp_val or '', 'article_supplier': supplier_val})
    wb.close()
    return items


def write_costs(src_path: str, dst_path: str, spec: TemplateSpec, costs: Dict[int, int]) -> int:
    wb = openpyxl.load_workbook(src_path)
    ws = find_sheet(wb, spec)
    col_cost_idx = column_index_from_string(spec.col_cost)
    written = 0
    for row_num, cost in costs.items():
        ws.cell(row=row_num, column=col_cost_idx, value=cost)
        written += 1
    wb.save(dst_path)
    wb.close()
    return written
