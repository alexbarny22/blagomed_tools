"""Прогон расчёта себестоимости с записью результатов в runs/run_items."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from app.calc import CalcError, calc_product


_CREATED_AT_FMT = "%Y-%m-%d %H:%M:%S"

# Явный список колонок run_items (16 = 14 прежних + prev_cost + jump)
_RUN_ITEM_COLUMNS = (
    "run_id", "marketplace", "row_num", "article_mp", "article_supplier",
    "product_id", "price", "price_source", "ppu_rub", "extras_rub",
    "margin_pct", "cost", "status", "message", "prev_cost", "jump",
)

_INSERT_RUN_ITEM_SQL = (
    "INSERT INTO run_items (" + ", ".join(_RUN_ITEM_COLUMNS) + ") "
    "VALUES (" + ", ".join(["?"] * len(_RUN_ITEM_COLUMNS)) + ")"
)


def _to_str(value: Any) -> str | None:
    """Приводит значение CalcResult к TEXT (None остаётся None)."""
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _insert_run_item(con, run_id, marketplace, row_num, article_mp, article_supplier,
                     product_id=None, price=None, price_source=None, ppu_rub=None,
                     extras_rub=None, margin_pct=None, cost=None, status="problem",
                     message="", prev_cost=None, jump=0):
    """Вставляет одну позицию прогона в run_items."""
    con.execute(_INSERT_RUN_ITEM_SQL, (
        run_id, marketplace, row_num, article_mp, article_supplier,
        product_id, price, price_source, ppu_rub, extras_rub,
        margin_pct, cost, status, message, prev_cost, jump,
    ))


def _prev_costs_map(con, run_id: int) -> dict[int, int]:
    """{product_id: cost} из самого свежего прогона с run_id < текущего.

    Один запрос на весь прогон (любой marketplace).
    """
    rows = con.execute(
        """
        SELECT ri.product_id AS product_id, ri.cost AS cost
        FROM run_items ri
        WHERE ri.status = 'ok'
          AND ri.cost IS NOT NULL
          AND ri.product_id IS NOT NULL
          AND ri.run_id = (
              SELECT MAX(ri2.run_id)
              FROM run_items ri2
              WHERE ri2.product_id = ri.product_id
                AND ri2.status = 'ok'
                AND ri2.run_id < ?
          )
        """,
        (run_id,),
    ).fetchall()
    return {row["product_id"]: row["cost"] for row in rows}


def run_calculation(con, calc_date, marketplaces, prices=None, price_errors=None):
    """Прогон расчёта. Возвращает id созданного прогона.

    Товары с is_active = 0 пропускаются полностью (без run_items и total).
    Для status='ok' пишутся prev_cost и jump (скачок >20% к прошлому прогону).
    """
    prices = prices if prices is not None else {}
    price_errors = price_errors if price_errors is not None else {}

    created_at = datetime.now().strftime(_CREATED_AT_FMT)
    total = 0
    ok_count = 0
    problem_count = 0

    try:
        cur = con.execute(
            "INSERT INTO runs (calc_date, created_at) VALUES (?, ?)",
            (calc_date, created_at),
        )
        run_id = int(cur.lastrowid)

        # Предыдущие себестоимости — одним запросом на весь прогон
        prev_costs = _prev_costs_map(con, run_id)

        # Состояние «поставляется» по всем товарам
        active = {
            row["id"]: row["is_active"]
            for row in con.execute("SELECT id, is_active FROM products").fetchall()
        }

        for marketplace, items in marketplaces.items():
            for item in items or []:
                row_num = item.get("row")
                article_mp = item.get("article_mp")
                article_supplier = item.get("article_supplier")

                product_row = None
                if article_supplier:
                    product_row = con.execute(
                        "SELECT id FROM products WHERE article_supplier = ?",
                        (article_supplier,),
                    ).fetchone()
                # Эталоны Ozon без артикула поставщика — мэтчинг по артикулу МП
                if product_row is None and article_mp:
                    mp_col = ('article_ozon' if str(marketplace).startswith('ozon')
                              else 'article_wb')
                    product_row = con.execute(
                        f"SELECT id FROM products WHERE {mp_col} = ?",
                        (article_mp,),
                    ).fetchone()

                if product_row is None:
                    _insert_run_item(
                        con, run_id, marketplace, row_num, article_mp, article_supplier,
                        status="problem",
                        message=f"Товар не найден в справочнике (article_supplier={article_supplier})",
                    )
                    total += 1
                    problem_count += 1
                    continue

                product_id = int(product_row["id"])

                # Не поставляется -> ни строки run_items, ни учёта в total
                if not active.get(product_id, 1):
                    continue

                error_message = price_errors.get(product_id)
                if error_message:
                    _insert_run_item(
                        con, run_id, marketplace, row_num, article_mp, article_supplier,
                        product_id=product_id, status="problem", message=str(error_message),
                    )
                    total += 1
                    problem_count += 1
                    continue

                try:
                    res = calc_product(
                        con, product_id=product_id,
                        price=prices.get(product_id),
                        calc_date=calc_date,
                    )
                except CalcError as exc:
                    _insert_run_item(
                        con, run_id, marketplace, row_num, article_mp, article_supplier,
                        product_id=product_id, status="problem", message=str(exc),
                    )
                    total += 1
                    problem_count += 1
                    continue

                cost = int(res.cost)
                prev_cost = prev_costs.get(product_id)
                jump = 0
                if prev_cost is not None and abs(cost - prev_cost) > 0.2 * prev_cost:
                    jump = 1

                _insert_run_item(
                    con, run_id, marketplace, row_num, article_mp, article_supplier,
                    product_id=product_id,
                    price=_to_str(res.price),
                    price_source=str(res.price_source),
                    ppu_rub=_to_str(res.ppu_rub),
                    extras_rub=_to_str(res.extras_rub),
                    margin_pct=_to_str(res.margin_pct),
                    cost=cost,
                    status="ok",
                    message="",
                    prev_cost=prev_cost,
                    jump=jump,
                )
                total += 1
                ok_count += 1

        con.execute(
            "UPDATE runs SET total = ?, ok = ?, problems = ? WHERE id = ?",
            (total, ok_count, problem_count, run_id),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise

    return run_id


def get_last_run(con: sqlite3.Connection) -> sqlite3.Row | None:
    """Возвращает последний прогон расчёта (по убыванию id) или None."""
    return con.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
    ).fetchone()


def get_run_costs(
    con: sqlite3.Connection, run_id: int, marketplace: str
) -> dict[str, int]:
    """Возвращает {article_supplier: cost} по успешным позициям прогона."""
    rows = con.execute(
        """
        SELECT article_supplier, cost
        FROM run_items
        WHERE run_id = ? AND marketplace = ? AND status = 'ok'
        """,
        (run_id, marketplace),
    ).fetchall()

    result: dict[str, int] = {}
    for row in rows:
        article_supplier = row["article_supplier"]
        if article_supplier is None:
            continue
        result[article_supplier] = int(row["cost"])
    return result


def get_run_problems(
    con: sqlite3.Connection, run_id: int
) -> list[sqlite3.Row]:
    """Возвращает проблемные позиции прогона, упорядоченные по marketplace и row_num."""
    return con.execute(
        """
        SELECT *
        FROM run_items
        WHERE run_id = ? AND status = 'problem'
        ORDER BY marketplace, row_num
        """,
        (run_id,),
    ).fetchall()


def get_price_history(con: sqlite3.Connection, product_id: int) -> list[sqlite3.Row]:
    """История (calc_date, price, cost, marketplace) по всем прогонам, новые сверху."""
    return con.execute(
        """
        SELECT r.calc_date    AS calc_date,
               ri.price       AS price,
               ri.cost        AS cost,
               ri.marketplace AS marketplace
        FROM run_items ri
        JOIN runs r ON r.id = ri.run_id
        WHERE ri.product_id = ?
        ORDER BY ri.run_id DESC, ri.id DESC
        """,
        (product_id,),
    ).fetchall()


def get_last_costs_map(con: sqlite3.Connection) -> dict[int, tuple]:
    """{product_id: (cost, prev_cost, jump)} по данным последнего прогона."""
    rows = con.execute(
        """
        SELECT ri.product_id AS product_id,
               ri.cost       AS cost,
               ri.prev_cost  AS prev_cost,
               ri.jump       AS jump
        FROM run_items ri
        WHERE ri.run_id = (SELECT MAX(id) FROM runs)
          AND ri.product_id IS NOT NULL
        ORDER BY ri.id
        """
    ).fetchall()

    result: dict[int, tuple] = {}
    for row in rows:
        result.setdefault(row["product_id"], (
            row["cost"],
            row["prev_cost"],
            row["jump"] if row["jump"] is not None else 0,
        ))
    return result


def get_no_price_count(con: sqlite3.Connection, run_id: int | None = None) -> int:
    """Число distinct товаров со status='problem'; по умолчанию — последний прогон."""
    if run_id is None:
        row = con.execute("SELECT MAX(id) FROM runs").fetchone()
        run_id = row[0] if row is not None else None
    if run_id is None:
        return 0
    row = con.execute(
        "SELECT COUNT(DISTINCT product_id) FROM run_items "
        "WHERE run_id = ? AND status = 'problem' AND product_id IS NOT NULL",
        (run_id,),
    ).fetchone()
    return int(row[0] or 0)
