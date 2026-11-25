"""Получение закупочных цен из 1С для прогона расчёта."""

from __future__ import annotations

import configparser
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable

from app import onec

_CONFIG_NAME = "config.ini"
_MODE_DEFAULT = "com"


def load_params(root: str | Path) -> tuple[onec.Params, str] | None:
    """Прочитать параметры 1С из config.ini в корне приложения.

    Возвращает (params, mode) либо None, если файла нет. Ошибки разбора
    конфига (OnecError, configparser.Error) не перехватываются — решение
    о показе пользователю принимает вызывающий код.
    """
    path = Path(root) / _CONFIG_NAME
    if not path.is_file():
        return None

    params = onec.Params.from_file(path)

    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    mode = _MODE_DEFAULT
    if parser.has_section("1C"):
        raw_mode = parser.get("1C", "mode", fallback=_MODE_DEFAULT)
        mode = (raw_mode or "").strip().lower() or _MODE_DEFAULT

    return params, mode


def _default_source_factory(mode: str) -> Callable[[onec.Params], Any]:
    """Фабрика источника 1С по режиму подключения."""

    def factory(params: onec.Params) -> Any:
        if mode == "com":
            return onec.ComSource(params)
        return onec.OdataSource(params)

    return factory


def fetch_prices(
    products: Iterable[dict[str, Any]],
    params: onec.Params,
    mode: str,
    source_factory: Callable[[onec.Params], Any] | None = None,
) -> tuple[dict[int, Decimal], dict[int, str], dict[int, str]]:
    """Получить закупочные цены и наименования из 1С.

    products: отобранные товары (dict с ключами id, article_supplier, name,
        onec_guid) — как правило external-товары с manual_price=0.
    Возвращает (prices, errors, names): prices — {product_id: Decimal},
    errors — {product_id: текст проблемы}, names — {product_id: name_1c}
    для всех товаров с непустым наименованием 1С (независимо от статуса
    цены). Исключения 1С наружу не утекают: любая ошибка источника
    превращается в сообщения проблем по всем товарам.
    """
    products = list(products)
    if not products:
        return {}, {}, {}

    factory = source_factory or _default_source_factory(mode)

    requests = [
        onec.OnecRequest(
            sku_1c=p["article_supplier"],
            name_1c=(p.get("name") or None),
            guid_1c=(p.get("onec_guid") or None),
        )
        for p in products
    ]

    try:
        items = onec.fetch_from_source(factory(params), requests)
    except Exception as exc:  # 1С недоступна — не выпускаем исключение наружу
        message = f"1С недоступна: {exc}"
        return {}, {int(p["id"]): message for p in products}, {}

    prices: dict[int, Decimal] = {}
    errors: dict[int, str] = {}
    names: dict[int, str] = {}
    for product, item in zip(products, items):
        pid = int(product["id"])
        if item.name_1c:
            names[pid] = item.name_1c
        if item.status == onec.ItemStatus.OK and item.price is not None:
            prices[pid] = Decimal(str(item.price))
        else:
            errors[pid] = f"1С: {item.status.value} — {item.detail or 'без деталей'}"

    return prices, errors, names
