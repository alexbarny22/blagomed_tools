# -*- coding: utf-8 -*-
"""
Работа с 1С:Управление торговлей 11.5 через COM-соединитель V83.COMConnector.

Главная идея: состав реквизитов "Заказа клиента" не задаётся в коде.
Берётся существующий заказ-образец, из метаданных вычитывается список
реквизитов шапки и они копируются в новый документ. Подменяются только
дата и табличная часть "Товары".
"""

import gc
import json
import os
import pythoncom
import win32com.client

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, "nomenclature.json")

# Стандартные реквизиты, которые копировать с образца нельзя
SKIP_ATTRS = {"Номер", "Дата", "Проведен", "ПометкаУдаления", "Ссылка"}


class OneCError(Exception):
    pass


class OneC:
    """Контекстный менеджер COM-соединения с 1С."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.conn = None

    def __enter__(self):
        pythoncom.CoInitialize()
        try:
            connector = win32com.client.Dispatch("V83.COMConnector")
        except Exception as e:
            pythoncom.CoUninitialize()
            raise OneCError(
                "Не удалось создать V83.COMConnector. Скорее всего не "
                "зарегистрирована comcntr.dll или не совпадает разрядность "
                f"Python и платформы 1С. Исходная ошибка: {e}"
            )
        s = self.cfg["1c"]
        cs = (
            f'Srvr="{s["srvr"]}";Ref="{s["ref"]}";'
            f'Usr="{s["usr"]}";Pwd="{s["pwd"]}";'
        )
        try:
            self.conn = connector.Connect(cs)
        except Exception as e:
            pythoncom.CoUninitialize()
            raise OneCError(f"1С отказала в подключении: {e}")
        return self

    def __exit__(self, *exc):
        self.conn = None
        gc.collect()
        pythoncom.CoUninitialize()
        return False

    # ---------- вспомогательное ----------

    def query(self, text, **params):
        """Выполняет запрос и возвращает выборку 1С."""
        q = self.conn.NewObject("Запрос")
        q.Текст = text
        for k, v in params.items():
            q.УстановитьПараметр(k, v)
        return q.Выполнить().Выбрать()

    def guid(self, ref):
        """Ссылка 1С -> строковый GUID (для хранения в JSON)."""
        return str(self.conn.XMLСтрока(ref))

    def ref_from_guid(self, guid, full_type="СправочникСсылка.Номенклатура"):
        """Строковый GUID -> ссылка 1С."""
        return self.conn.XMLЗначение(self.conn.Тип(full_type), guid)

    # ---------- номенклатура ----------

    def dump_nomenclature(self):
        """Выгружает справочник номенклатуры в локальный JSON-кеш."""
        sel = self.query(
            """
            ВЫБРАТЬ
                Н.Ссылка КАК Ссылка,
                Н.Наименование КАК Наименование,
                Н.Артикул КАК Артикул
            ИЗ Справочник.Номенклатура КАК Н
            ГДЕ НЕ Н.ПометкаУдаления
              И НЕ Н.ЭтоГруппа
            """
        )
        items = []
        while sel.Следующий():
            items.append(
                {
                    "guid": self.guid(sel.Ссылка),
                    "name": str(sel.Наименование),
                    "art": str(sel.Артикул or ""),
                }
            )
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        return items

    # ---------- партнёр и образец ----------

    def find_partner(self, name):
        sel = self.query(
            """
            ВЫБРАТЬ ПЕРВЫЕ 1 П.Ссылка КАК Ссылка
            ИЗ Справочник.Партнеры КАК П
            ГДЕ П.Наименование = &Имя И НЕ П.ПометкаУдаления
            """,
            Имя=name,
        )
        if not sel.Следующий():
            raise OneCError(f'Партнёр "{name}" не найден в справочнике "Партнёры".')
        return sel.Ссылка

    def find_template(self, partner_ref, number=""):
        """Ищет заказ-образец: по номеру или последний заказ этого партнёра."""
        if number:
            sel = self.query(
                """
                ВЫБРАТЬ ПЕРВЫЕ 1 З.Ссылка КАК Ссылка
                ИЗ Документ.ЗаказКлиента КАК З
                ГДЕ З.Номер = &Номер
                """,
                Номер=number,
            )
        else:
            sel = self.query(
                """
                ВЫБРАТЬ ПЕРВЫЕ 1 З.Ссылка КАК Ссылка
                ИЗ Документ.ЗаказКлиента КАК З
                ГДЕ З.Партнер = &Партнер И НЕ З.ПометкаУдаления
                УПОРЯДОЧИТЬ ПО З.Дата УБЫВ
                """,
                Партнер=partner_ref,
            )
        if not sel.Следующий():
            raise OneCError(
                "Не найден заказ-образец. Создайте в 1С руками один заказ "
                "на этого клиента — он станет шаблоном шапки."
            )
        return sel.Ссылка.ПолучитьОбъект()

    def _doc_meta(self):
        md = self.conn.Метаданные.Документы.Найти("ЗаказКлиента")
        if md is None:
            raise OneCError("В конфигурации нет документа ЗаказКлиента.")
        return md

    def header_attribute_names(self):
        """Список имён реквизитов шапки документа из метаданных."""
        attrs = self._doc_meta().Реквизиты
        names = []
        for i in range(attrs.Количество()):
            name = str(attrs.Получить(i).Имя)
            if name not in SKIP_ATTRS:
                names.append(name)
        return names

    def goods_column_names(self):
        """Имена реквизитов табличной части Товары из метаданных."""
        ts = self._doc_meta().ТабличныеЧасти.Найти("Товары")
        if ts is None:
            raise OneCError("У документа ЗаказКлиента нет табличной части Товары.")
        attrs = ts.Реквизиты
        return [str(attrs.Получить(i).Имя) for i in range(attrs.Количество())]

    # ---------- создание заказа ----------

    def create_order(self, partner_name, lines, template_number="", comment=""):
        """
        partner_name    — наименование партнёра в справочнике "Партнёры"
        lines           — список словарей {"guid": str, "qty": float}
        template_number — номер заказа-образца, пусто = последний заказ партнёра
        Возвращает (номер, дата) созданного документа.
        """
        partner = self.find_partner(partner_name)
        tmpl = self.find_template(partner, template_number)

        doc = self.conn.Документы.ЗаказКлиента.СоздатьДокумент()

        # шапка — копируем всё, что есть в метаданных
        for name in self.header_attribute_names():
            try:
                setattr(doc, name, getattr(tmpl, name))
            except Exception:
                pass  # реквизиты только для чтения и прочая экзотика

        doc.Дата = self.conn.ТекущаяДата()
        try:
            doc.Партнер = partner
        except Exception:
            pass

        # строка-образец из таблицы товаров образца: даёт ставку НДС,
        # склад, вариант обеспечения и прочие умолчания
        row_tmpl = tmpl.Товары.Получить(0) if tmpl.Товары.Количество() > 0 else None
        col_names = self.goods_column_names()

        doc.Товары.Очистить()
        for ln in lines:
            row = doc.Товары.Добавить()
            if row_tmpl is not None:
                for cname in col_names:
                    try:
                        setattr(row, cname, getattr(row_tmpl, cname))
                    except Exception:
                        pass
            nom = self.ref_from_guid(ln["guid"])
            row.Номенклатура = nom
            row.Количество = float(ln["qty"])
            # единица измерения — родная для номенклатуры
            try:
                row.ЕдиницаИзмерения = nom.ЕдиницаИзмерения
            except Exception:
                pass
            # цены оставляем нулевыми: менеджер заполнит в 1С кнопкой "Заполнить цены"
            for fld in ("Цена", "Сумма", "СуммаНДС", "СуммаСНДС", "СуммаРучнойСкидки"):
                try:
                    setattr(row, fld, 0)
                except Exception:
                    pass
            row.Количество = float(ln["qty"])

        if comment:
            try:
                doc.Комментарий = (str(doc.Комментарий or "") + "\n" + comment).strip()
            except Exception:
                pass

        # пишем НЕпроведённым
        doc.Записать(self.conn.РежимЗаписиДокумента.Запись)
        return str(doc.Номер), str(doc.Дата)


def load_cache():
    if not os.path.exists(CACHE_FILE):
        return []
    with open(CACHE_FILE, encoding="utf-8") as f:
        return json.load(f)
