"""Прогон расчёта себестоимости с записью результатов в runs/run_items."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from app.calc import CalcError, calc_product


_CREATED_AT_FMT = "%Y-%m-%d %H:%M:%S"


def _insert_run_item(
    con: sqlite3.Connection,
    *,
    run_id: int,
    marketplace: str,
    row_num: int | None,
    article_mp: str | None,
    article_supplier: str | None,
    product_id: int | None,
    price: str | None,
    price_source: str | None,
    ppu_rub: str | None,
    extras_rub: str | None,
    margin_pct: str | None,
    cost: int | None,
    status: str,
    message: str,
) -> None:
    """Вставляет одну позицию прогона в run_items."""
    con.execute(
        """
        INSERT INTO run_items (
            run_id, marketplace, row_num, article_mp, article_supplier,
            product_id, price, price_source, ppu_rub, extras_rub,
            margin_pct, cost, status, message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            marketplace,
            row_num,
            article_mp,
            article_supplier,
            product_id,
            price,
            price_source,
            ppu_rub,
            extras_rub,
            margin_pct,
            cost,
            status,
            message,
        ),
    )


def run_calculation(
    con: sqlite3.Connection,
    *,
    calc_date: str,
    marketplaces: dict[str, list[dict[str, Any]]],
    prices: dict[int, Any] | None = None,
    price_errors: dict[int, str] | None = None,
) -> int:
    """Выполняет прогон расчёта по маркетплейсам и сохраняет результаты.

    Для каждой позиции ищет товар по article_supplier и вызывает
    calc_product. prices — {product_id: закупочная цена из 1С} для товаров
    с manual_price=0; если цены нет, передаётся None и calc_product сам
    бросает PriceRequired. price_errors — {product_id: текст проблемы}:
    для таких товаров calc_product не вызывается, позиция сразу уходит
    в 'problem' с этим сообщением. Возвращает id созданного прогона.
    """
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

        for marketplace, items in marketplaces.items():
            for item in items or []:
                row_num = item.get("row")
                article_mp = item.get("article_mp")
                article_supplier = item.get("article_supplier")

                product_id: int | None = None
                price: str | None = None
                price_source: str | None = None
                ppu_rub: str | None = None
                extras_rub: str | None = None
                margin_pct: str | None = None
                cost: int | None = None
                message = ""

                if article_supplier is None:
                    product_row = None
                else:
                    product_row = con.execute(
                        "SELECT id FROM products WHERE article_supplier = ?",
                        (article_supplier,),
                    ).fetchone()

                if product_row is None:
                    status = "problem"
                    problem_count += 1
                    message = (
                        "Товар не найден в справочнике "
                        f"(article_supplier={article_supplier})"
                    )
                else:
                    product_id = int(product_row["id"])
                    error_message = (price_errors or {}).get(product_id)
                    if error_message:
                        status = "problem"
                        problem_count += 1
                        message = error_message
                    else:
                        try:
                            res = calc_product(
                                con, product_id=product_id,
                                price=(prices or {}).get(product_id),
                                calc_date=calc_date,
                            )
                        except CalcError as exc:
                            status = "problem"
                            problem_count += 1
                            message = str(exc)
                        else:
                            status = "ok"
                            ok_count += 1
                            price = str(res.price)
                            price_source = str(res.price_source)
                            ppu_rub = str(res.ppu_rub)
                            extras_rub = str(res.extras_rub)
                            margin_pct = str(res.margin_pct)
                            cost = res.cost

                _insert_run_item(
                    con,
                    run_id=run_id,
                    marketplace=marketplace,
                    row_num=row_num,
                    article_mp=article_mp,
                    article_supplier=article_supplier,
                    product_id=product_id,
                    price=price,
                    price_source=price_source,
                    ppu_rub=ppu_rub,
                    extras_rub=extras_rub,
                    margin_pct=margin_pct,
                    cost=cost,
                    status=status,
                    message=message,
                )
                total += 1

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
