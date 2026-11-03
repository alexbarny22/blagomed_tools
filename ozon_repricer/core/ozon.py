"""Клиент Ozon Seller API: чтение цен, названий и запись цен."""
import os
import time
import random
import requests

BASE_URL = "https://api-seller.ozon.ru"


class OzonError(Exception):
    pass


class OzonClient:
    def __init__(self, client_id: str, api_key: str, timeout: int = 60):
        self.client_id = (client_id or "").strip()
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self.session = requests.Session()

    def _post(self, path: str, body: dict) -> dict:
        if not self.client_id or not self.api_key:
            raise OzonError("Не заданы Client-Id и Api-Key (страница «Настройки»).")
        headers = {
            "Client-Id": self.client_id,
            "Api-Key": self.api_key,
            "Content-Type": "application/json",
        }
        last_err = None
        for attempt in range(5):
            try:
                r = self.session.post(BASE_URL + path, json=body, headers=headers, timeout=self.timeout)
            except requests.RequestException as e:
                last_err = f"Нет связи с Ozon: {e}"
                time.sleep(2 + attempt * 3)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last_err = f"Ozon ответил {r.status_code}: {r.text[:300]}"
                time.sleep(2 + attempt * 3)
                continue
            if r.status_code in (401, 403):
                raise OzonError("Ozon отклонил ключи (проверьте Client-Id и Api-Key, у ключа должны быть права на цены).")
            if r.status_code != 200:
                raise OzonError(f"Ozon ответил {r.status_code}: {r.text[:500]}")
            return r.json()
        raise OzonError(last_err or "Ozon не отвечает")

    # --- чтение ---
    def get_all_prices(self, visibility: str = "ALL") -> list:
        """Все товары с ценами и индексами цен (/v5/product/info/prices, постранично)."""
        items, cursor = [], ""
        for _ in range(200):
            data = self._post("/v5/product/info/prices", {
                "cursor": cursor,
                "filter": {"visibility": visibility},
                "limit": 1000,
            })
            batch = data.get("items") or []
            items.extend(batch)
            cursor = data.get("cursor") or ""
            if not batch or not cursor:
                break
        return items

    def get_product_info(self, product_ids: list) -> list:
        """Названия и SKU (/v3/product/info/list), до 1000 за запрос."""
        out = []
        for i in range(0, len(product_ids), 1000):
            chunk = [str(x) for x in product_ids[i:i + 1000]]
            data = self._post("/v3/product/info/list", {"product_id": chunk})
            out.extend(data.get("items") or [])
        return out

    # --- запись ---
    def import_prices(self, prices: list) -> list:
        """Обновление цен (/v1/product/import/prices), до 1000 за запрос."""
        result = []
        for i in range(0, len(prices), 1000):
            data = self._post("/v1/product/import/prices", {"prices": prices[i:i + 1000]})
            result.extend(data.get("result") or [])
        return result


class MockOzonClient:
    """Имитация Ozon для проверки без настоящих ключей (переменная OZON_MOCK=1)."""

    _state = None

    def __init__(self, *a, **k):
        if MockOzonClient._state is None:
            rnd = random.Random(7)
            names = ["Трость опорная регулируемая", "Ходунки шагающие складные", "Бандаж коленный",
                     "Корсет поясничный", "Костыли подмышечные", "Ортопедическая подушка",
                     "Стул-туалет складной", "Противопролежневый матрас", "Тонометр плечевой",
                     "Ролллятор 4-колёсный", "Бандаж на голеностоп", "Кресло-коляска лёгкая"]
            st = {}
            for i, n in enumerate(names):
                pid = 900000 + i
                price = rnd.choice([890, 1290, 1990, 2490, 3490, 5990, 8990, 12990])
                st[pid] = {
                    "product_id": pid, "offer_id": f"BM-{100 + i}", "sku": 1500000000 + i, "name": n,
                    "price": float(price), "old_price": float(round(price * 1.4)), "min_price": 0.0,
                    "promo": rnd.choice([1.0, 1.0, 0.9, 0.85, 0.8]),
                }
            MockOzonClient._state = st

    def _item(self, s):
        seller = round(s["price"] * s["promo"])
        comp = round(seller * random.uniform(0.9, 1.15))
        idx = round(seller / comp, 2) if seller <= comp else round(2 - comp / seller, 2)
        return {
            "offer_id": s["offer_id"], "product_id": s["product_id"],
            "price": {"price": s["price"], "old_price": s["old_price"], "marketing_seller_price": seller,
                      "min_price": s["min_price"], "currency_code": "RUB", "auto_action_enabled": False},
            "price_indexes": {
                "color_index": random.choice(["GREEN", "YELLOW", "RED"]),
                "ozon_index_data": {"min_price": comp, "min_price_currency": "RUB", "price_index_value": idx},
                "external_index_data": {"min_price": 0, "price_index_value": 0},
                "self_marketplaces_index_data": {"min_price": 0, "price_index_value": 0},
            },
            "marketing_actions": {"actions": ([{"title": "Скидка продавца", "date_from": "2026-10-01T00:00:00Z",
                                                "date_to": "2026-11-30T00:00:00Z"}] if s["promo"] < 1 else [])},
        }

    def get_all_prices(self, visibility="ALL"):
        for s in MockOzonClient._state.values():
            if random.random() < 0.15:
                s["promo"] = random.choice([1.0, 0.9, 0.8, 0.7])
        return [self._item(s) for s in MockOzonClient._state.values()]

    def get_product_info(self, product_ids):
        st = MockOzonClient._state
        return [{"id": p, "sku": st[p]["sku"], "name": st[p]["name"], "offer_id": st[p]["offer_id"]}
                for p in map(int, product_ids) if p in st]

    def import_prices(self, prices):
        res = []
        for p in prices:
            s = MockOzonClient._state.get(int(p["product_id"]))
            if not s:
                res.append({"product_id": p["product_id"], "updated": False,
                            "errors": [{"code": "NOT_FOUND", "message": "нет товара"}]})
                continue
            s["price"] = float(p["price"])
            s["old_price"] = float(p.get("old_price", s["old_price"]))
            if "min_price" in p:
                s["min_price"] = float(p["min_price"])
            res.append({"product_id": p["product_id"], "offer_id": s["offer_id"], "updated": True, "errors": []})
        return res


def make_client(client_id, api_key):
    if os.environ.get("OZON_MOCK") == "1":
        return MockOzonClient()
    return OzonClient(client_id, api_key)
