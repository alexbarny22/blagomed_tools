"""Клиент Ozon Seller API: чтение цен, названий и запись цен."""
import os
import time
import random
from datetime import datetime, timedelta
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

    def get_stocks(self, visibility: str = "ALL") -> list:
        """Остатки всех товаров (/v4/product/info/stocks, постранично)."""
        items, cursor = [], ""
        for _ in range(200):
            data = self._post("/v4/product/info/stocks", {
                "filter": {"visibility": visibility},
                "limit": 1000, "cursor": cursor,
            })
            batch = data.get("items") or []
            items.extend(batch)
            cursor = data.get("cursor") or ""
            if not batch or not cursor:
                break
        return items

    # --- запись ---
    def import_prices(self, prices: list) -> list:
        """Обновление цен (/v1/product/import/prices), до 1000 за запрос."""
        result = []
        for i in range(0, len(prices), 1000):
            data = self._post("/v1/product/import/prices", {"prices": prices[i:i + 1000]})
            result.extend(data.get("result") or [])
        return result

    # --- акции ---
    def _get(self, path: str) -> dict:
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
                r = self.session.get(BASE_URL + path, headers=headers, timeout=self.timeout)
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

    def get_actions(self) -> list:
        """Акции, к которым могут присоединиться продавцы (/v1/actions, GET)."""
        data = self._get("/v1/actions")
        return data.get("result") or []

    def get_action_products(self, action_id) -> list:
        """Товары, участвующие в акции (/v1/actions/products)."""
        data = self._post("/v1/actions/products",
                          {"action_id": action_id, "limit": 1000, "offset": 0})
        return (data.get("result") or {}).get("products") or []

    def get_action_candidates(self, action_id) -> list:
        """Товары-кандидаты для входа в акцию (/v1/actions/candidates)."""
        data = self._post("/v1/actions/candidates",
                          {"action_id": action_id, "limit": 1000, "offset": 0})
        return (data.get("result") or {}).get("products") or []

    def activate_action(self, action_id, products: list):
        """Вход товаров в акцию (/v1/actions/products/activate).
        products: [{product_id, action_price}]. Возвращает (product_ids, rejected)."""
        data = self._post("/v1/actions/products/activate",
                          {"action_id": action_id, "products": products})
        res = data.get("result") or {}
        return res.get("product_ids") or [], res.get("rejected") or []

    def deactivate_action(self, action_id, product_ids: list):
        """Выход товаров из акции (/v1/actions/products/deactivate).
        Возвращает (product_ids, rejected)."""
        data = self._post("/v1/actions/products/deactivate",
                          {"action_id": action_id, "product_ids": [int(x) for x in product_ids]})
        res = data.get("result") or {}
        return res.get("product_ids") or [], res.get("rejected") or []


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
                    "stock_fbo": rnd.choice([0, 0, 2, 5, 9, 14]),
                    "stock_fbs": rnd.choice([0, 1, 3, 6, 11]),
                }
            MockOzonClient._state = st
        if getattr(MockOzonClient, "_actions", None) is None:
            MockOzonClient._actions = self._build_actions()

    @staticmethod
    def _build_actions(action_id=None):
        """Детерминированная имитация акций Ozon (3 акции, завершившаяся — при «отключённой»)."""
        rnd = random.Random(11)
        st = MockOzonClient._state

        def iso_dt(dt):
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        def days(delta):
            now = datetime.utcnow()
            return iso_dt(now), iso_dt(now + timedelta(days=delta)), iso_dt(now - timedelta(days=abs(delta)))

        _, end14, start5 = days(14)
        _, end21, start7 = days(21)
        _, end60, start90 = days(60)
        acts = {
            63301: {"id": 63301, "title": "Скидка продавца: медтехника",
                    "date_start": start5, "date_end": end14, "status": "STARTED",
                    "participants": {}, "candidates": {}},
            63302: {"id": 63302, "title": "Умная скидка Лабиринта",
                    "date_start": start7, "date_end": end21, "status": "NOT_STARTED",
                    "participants": {}, "candidates": {}},
            63303: {"id": 63303, "title": "Распродажа лета",
                    "date_start": start90, "date_end": end60, "status": "ENDED",
                    "participants": {}, "candidates": {}},
        }
        for pid, s in st.items():
            promo = s.get("promo", 1.0)
            price = float(s["price"])
            if promo < 1:  # уже в «Скидке продавца»
                acts[63301]["participants"][pid] = {"price": price, "action_price": round(price * 0.85),
                                                    "max_action_price": round(price * 0.9)}
            else:          # кандидаты для «Скидки продавца»
                acts[63301]["candidates"][pid] = {"price": price, "action_price": round(price * 0.85),
                                                  "max_action_price": round(price * 0.97)}
            # «Умная скидка»: все — кандидаты
            acts[63302]["candidates"][pid] = {"price": price, "action_price": round(price * 0.8),
                                              "max_action_price": round(price * 0.95)}
            # изредка «случайная» цена акции (проверка, что берём предложение Ozon)
            if rnd.random() < 0.2:
                crosses = round(price * rnd.choice([0.5, 0.6, 0.7]))
                acts[63302]["candidates"][pid]["action_price"] = max(crosses, 1)
        return acts

    def _action_meta(self, a):
        return {"id": a["id"], "title": a["title"], "date_start": a["date_start"],
                "date_end": a["date_end"], "status": a["status"],
                "is_participating": bool(a["participants"])}

    def _get_action(self, action_id):
        a = MockOzonClient._actions.get(action_id)
        if a is None:
            try:
                a = MockOzonClient._actions.get(int(action_id or 0))
            except (TypeError, ValueError):
                a = None
        return a

    def _prod_rows(self, d):
        st = MockOzonClient._state
        out = []
        for pid, rec in (d or {}).items():
            s = st.get(pid, {})
            out.append({"id": pid, "price": rec.get("price", s.get("price", 0)),
                        "action_price": rec.get("action_price", 0),
                        "max_action_price": rec.get("max_action_price", 0)})
        return out

    def get_actions(self):
        return [self._action_meta(a) for a in MockOzonClient._actions.values()]

    def get_action_products(self, action_id):
        a = self._get_action(action_id)
        return self._prod_rows(a["participants"]) if a else []

    def get_action_candidates(self, action_id):
        a = self._get_action(action_id)
        return self._prod_rows(a["candidates"]) if a else []

    def activate_action(self, action_id, products):
        a = self._get_action(action_id)
        ok, rej = [], []
        for p in products or []:
            pid = int(p.get("product_id") or 0)
            if not a:
                rej.append({"product_id": pid, "reason": "акция не найдена"})
                continue
            s = MockOzonClient._state.get(pid)
            if not s or pid not in a["candidates"]:
                rej.append({"product_id": pid, "reason": "товар не подходит для этой акции"})
                continue
            ap = int(round(float(p.get("action_price") or s["price"])))
            a["participants"][pid] = {"price": s["price"], "action_price": ap}
            a["candidates"].pop(pid, None)
            ok.append(pid)
        return ok, rej

    def deactivate_action(self, action_id, product_ids):
        a = self._get_action(action_id)
        ok, rej = [], []
        for pid in product_ids or []:
            pid = int(pid)
            if a and pid in a["participants"]:
                del a["participants"][pid]
                ok.append(pid)
            else:
                rej.append({"product_id": pid, "reason": "товар не участвует в акции"})
        return ok, rej

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
        def img(p):
            hue = (p * 47) % 360
            svg = (f"<svg xmlns='http://www.w3.org/2000/svg' width='200' height='200'><rect width='200' height='200' "
                   f"fill='hsl({hue},45%,85%)'/><circle cx='100' cy='90' r='45' fill='hsl({hue},45%,55%)'/>"
                   f"<rect x='50' y='140' width='100' height='22' rx='6' fill='hsl({hue},45%,55%)'/></svg>")
            import urllib.parse
            return "data:image/svg+xml," + urllib.parse.quote(svg)
        return [{"id": p, "sku": st[p]["sku"], "name": st[p]["name"], "offer_id": st[p]["offer_id"],
                 "primary_image": [img(p)], "images": [img(p)]}
                for p in map(int, product_ids) if p in st]

    def get_stocks(self, visibility="ALL"):
        """Остатки FBO/FBS для мока; FBP не используется (показывается «—»)."""
        res = []
        for s in MockOzonClient._state.values():
            st = []
            if s.get("stock_fbo"):
                st.append({"type": "fbo", "present": s["stock_fbo"], "reserved": 0, "sku": s["sku"],
                           "shipment_type": "SHIPMENT_TYPE_GENERAL"})
            if s.get("stock_fbs"):
                st.append({"type": "fbs", "present": s["stock_fbs"], "reserved": 0, "sku": s["sku"],
                           "shipment_type": "SHIPMENT_TYPE_GENERAL"})
            res.append({"product_id": s["product_id"], "offer_id": s["offer_id"], "stocks": st})
        return res

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
