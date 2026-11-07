"""
Благомед — расчёт себестоимости
================================
Запуск: двойной клик по файлу.

ДВА РЕЖИМА ПОЛУЧЕНИЯ ЦЕН (выбирается автоматически):

  1. Режим 1С (основной): скрипт сам подключается к базе УТ 11.5 через
     COM-коннектор и забирает закупочные цены по артикулам каталога.
     Настройки подключения — в файле config.ini рядом со скриптом
     (создаётся автоматически при первом запуске, впиши туда сервер,
     базу, логин и пароль). PDF при этом не нужен.

  2. Режим PDF (резервный): если 1С недоступна или подключение не
     настроено — работает по-старому, из файла отчет.pdf в этой папке.

ВЕСА: в 1С корректных весов нет, поэтому веса хранятся локально в
weights.json. Файл пополняется автоматически из каждого распознанного
PDF. В режиме 1С веса берутся из weights.json. НЕ УДАЛЯЙ его.

Опционально (для подхвата новых позиций):
    Благомед_Себестоимость_ММ_ГГГГ.xlsx  ← свежий шаблон, скачанный из сервиса.
    Скрипт сам найдёт самый свежий шаблон, сверит его со своим каталогом
    и запомнит новые позиции (артикул WB ↔ артикул поставщика).

Генерирует два файла:
    себестоимость_контроль_ГГГГ_ММ.xlsx  — детальный расчёт для проверки
    Благомед_себестоимость_ГГГГ_ММ.xlsx  — файл для загрузки в сервис

Память каталога:
    wb_catalog.json — справочник «артикул WB ↔ артикул поставщика» с датами
    добавления. Создаётся автоматически при первом запуске. НЕ УДАЛЯЙ его —
    в нём накапливается история новых позиций.

Контроль изменений:
    cost_history.json — себестоимость по позициям на момент прошлого запуска.
    При резком изменении (порог COST_ALERT_PCT в CONFIG) строка подсвечивается
    красным в контрольном файле и пишется предупреждение в лог. НЕ УДАЛЯЙ.

Для режима 1С дополнительно нужно (один раз):
    pip install pywin32
    Разрядность Python должна совпадать с разрядностью платформы 1С
    (обычно 1С 32-битная → нужен 32-битный Python, см. лог при ошибке).

Лог пишется в blagomed_cost.log в той же папке.
Все параметры расчёта — в блоке CONFIG ниже.
"""

import sys, re, math, json, logging, traceback, configparser
from datetime import date, datetime
from pathlib import Path

# ════════════════════════════════════════════════════════════════════════════
# Папка скрипта — всё относительно неё
# ════════════════════════════════════════════════════════════════════════════

BASE_DIR     = Path(__file__).resolve().parent
LOG_FILE     = BASE_DIR / 'blagomed_cost.log'
CATALOG_FILE = BASE_DIR / 'wb_catalog.json'
HISTORY_FILE = BASE_DIR / 'cost_history.json'
WEIGHTS_FILE = BASE_DIR / 'weights.json'
CONFIG_FILE  = BASE_DIR / 'config.ini'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-7s  %(message)s',
    datefmt='%d.%m.%Y %H:%M:%S',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger('blagomed')

try:
    import pdfplumber
    from openpyxl import load_workbook, Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
except ImportError as e:
    log.critical('Не установлена библиотека: %s', e)
    log.critical('Выполни в командной строке: pip install pdfplumber openpyxl')
    input('\nНажми Enter для выхода...')
    sys.exit(1)


# ════════════════════════════════════════════════════════════════════════════
# CONFIG — редактируй только здесь
# ════════════════════════════════════════════════════════════════════════════

WEIGHT_RATE = 405   # руб / кг (стоимость доставки веса)
MARGIN      = 1.15  # коэффициент себестоимости (+15%)

# Порог «резкого изменения» себестоимости относительно прошлого запуска, %
# Если |изменение| >= порога — строка подсвечивается в контрольном файле
# и пишется предупреждение в лог. История хранится в cost_history.json.
COST_ALERT_PCT = 10

# Дополнительные расходы по артикулам поставщика
EXTRA = {
    '232957': (300,  'Стоимость наполнителя — абдуктор малая'),
    '232971': (332,  'Стоимость наполнителя — абдуктор большая'),
    '232956': (110,  'Стоимость наполнителя — подушка круглая'),
    '232916': (196,  'Стоимость наполнителя — подушка прямоуг. 40×46'),
    '232896': (176,  'Стоимость наполнителя — подушка прямоуг. 41×40'),
    '232899': (75,   'Стоимость наполнителя — подушка-кольцо Memory'),
    '232914': (75,   'Стоимость наполнителя — подушка-овал'),
}

# Фиксированная себестоимость для артикулов не из отчёта Гаврюшкина
# (другой поставщик или вне маркетплейс-выгрузки)
# Обновляй вручную при изменении цен по этим позициям
FIXED_COSTS = {
    '232924': 190,   # WB 142136014 | р.0   — другой поставщик
    '232983': 743,   # WB 188853174 | р.A   — другой поставщик
    '232991': 636,   # WB 304922949 | р.M   — другой поставщик
    '232943': 743,   # WB 151095160 | р.40-44
    '232923': 120,   # WB 141927831 | р.0
    '232948': 110,   # WB 158051690 | р.0
    '232951': 1100,  # WB 158144489 | р.0
    '232942': 110,   # WB 149597299 | р.0
    '232955': 449,   # WB 161054876 | р.0
    '232936': 170,   # WB 146776643 | р.0   — бренд Мега-Трейд
    '232965': 743,   # WB 174644152 | р.37-39
    '232934': 190,   # WB 146245042 | р.0
    '232897': 410,   # WB 920214761 | р.0
    '232937': 1520,  # WB 218634139 | р.0
    '232935': 110,   # WB 146410691 | р.0
    '333003': 652,   # WB 412735943 | р.0
    '232954': 921,   # WB 261053136 | р.0
    '232925': 1100,  # WB 142181214 | р.0
    '232800': 759,   # WB 434517901 | р.0
    '232962': 330,   # WB 181874478 | р.0
}


# ════════════════════════════════════════════════════════════════════════════
# Стартовый каталог WB (используется ТОЛЬКО при первом запуске,
# когда wb_catalog.json ещё не существует). Дальше скрипт живёт по JSON
# и сам пополняет его из свежих шаблонов сервиса.
# ════════════════════════════════════════════════════════════════════════════

WB_CATALOG_SEED = [
    # (wb_art,      sup_art,   size,    brand)
    (897441529,   '232914',  '0',     ''),
    (386416243,   '232918',  '0',     ''),
    (142136014,   '232924',  '0',     ''),
    (174653471,   '232967',  '0',     ''),
    (188853174,   '232983',  'A',     ''),
    (215148140,   '232947',  '0',     ''),
    (181580686,   '232956',  '0',     ''),
    (304922949,   '232991',  'M',     ''),
    (190215429,   '232964',  '0',     ''),
    (151095160,   '232943',  '40-44', ''),
    (147022038,   '232938',  '0',     ''),
    (180058107,   '232969',  '0',     ''),
    (751483904,   '232916',  '0',     ''),
    (195801406,   '232979',  '0',     ''),
    (141927831,   '232923',  '0',     ''),
    (156281810,   '232949',  '0',     ''),
    (158051690,   '232948',  '0',     ''),
    (209899297,   '232971',  '0',     ''),
    (310907461,   '232995',  '0',     ''),
    (149716449,   '232939',  '0',     ''),
    (158144489,   '232951',  '0',     ''),
    (149597299,   '232942',  '0',     ''),
    (161054876,   '232955',  '0',     ''),
    (137949054,   '232922',  '0',     ''),
    (146776643,   '232936',  '0',     'Мега-Трейд'),
    (412671442,   '232899',  '0',     ''),
    (137948702,   '232921',  '0',     ''),
    (195728680,   '232982',  '0',     ''),
    (190210045,   '232946',  '0',     ''),
    (1030209137,  '232888',  '0',     ''),
    (1050631715,  '232892',  '0',     ''),
    (149716400,   '232940',  '0',     ''),
    (641663222,   '232898',  '0',     ''),
    (174644152,   '232965',  '37-39', ''),
    (146245042,   '232934',  '0',     ''),
    (178802754,   '232945',  '0',     ''),
    (920214761,   '232897',  '0',     ''),
    (1050631307,  '232891',  '0',     ''),
    (940202124,   '232889',  '0',     ''),
    (412714086,   '333001',  '0',     ''),
    (205317266,   '232981',  '0',     ''),
    (619308843,   '232987',  '0',     ''),
    (1030135177,  '232901',  '0',     ''),
    (143984568,   '232928',  '0',     ''),
    (241871293,   '232932',  '0',     ''),
    (218634139,   '232937',  '0',     ''),
    (434476127,   '232915',  '0',     ''),
    (146410691,   '232935',  '0',     ''),
    (189538738,   '232966',  '0',     ''),
    (412735943,   '333003',  '0',     ''),
    (920242277,   '232902',  '0',     ''),
    (261053136,   '232954',  '0',     ''),
    (311477092,   '232996',  '0',     ''),
    (144370436,   '232931',  '0',     ''),
    (142181214,   '232925',  '0',     ''),
    (550072996,   '232903',  '0',     ''),
    (846292156,   '232961',  '0',     ''),
    (196927709,   '232985',  '0',     ''),
    (343809919,   '232919',  '0',     ''),
    (174149845,   '232968',  '0',     ''),
    (188853475,   '232984',  'A',     ''),
    (434517901,   '232800',  '0',     ''),
    (412732643,   '333002',  '0',     ''),
    (181874478,   '232962',  '0',     ''),
    (343829613,   '232953',  '0',     ''),
    (897415114,   '232890',  '0',     ''),
    (670310019,   '232906',  '0',     ''),
    (169173498,   '232957',  '0',     ''),
    (718671485,   '232896',  '0',     ''),
    (144371029,   '232930',  '0',     ''),
    (301627008,   '232988',  '0',     ''),
]


# ════════════════════════════════════════════════════════════════════════════
# Каталог WB: загрузка / сохранение / синхронизация со свежим шаблоном сервиса
# ════════════════════════════════════════════════════════════════════════════

def load_catalog():
    """Загружает wb_catalog.json. Если его нет — создаёт из WB_CATALOG_SEED."""
    if CATALOG_FILE.exists():
        try:
            data = json.loads(CATALOG_FILE.read_text(encoding='utf-8'))
            items = data.get('items', [])
            log.info('Каталог WB: %s (%d позиций)', CATALOG_FILE.name, len(items))
            return items
        except Exception:
            log.error('Не удалось прочитать %s — файл повреждён?\n%s',
                      CATALOG_FILE.name, traceback.format_exc())
            log.error('Использую встроенный стартовый каталог. '
                      'Проверь/удали повреждённый JSON.')

    items = [{'wb': w, 'sup': s, 'size': sz, 'brand': b,
              'added': None, 'source': 'стартовый каталог'}
             for w, s, sz, b in WB_CATALOG_SEED]
    log.info('Файл %s не найден — создаю из встроенного каталога (%d позиций)',
             CATALOG_FILE.name, len(items))
    save_catalog(items)
    return items


def save_catalog(items):
    data = {
        'updated': datetime.now().strftime('%d.%m.%Y %H:%M:%S'),
        'count': len(items),
        'items': items,
    }
    CATALOG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                            encoding='utf-8')


def find_service_template():
    """Ищет в папке самый свежий шаблон, скачанный из сервиса.

    Шаблон отличаем по названию листа: 'Шаблон по себестоимости за ГГГГ-ММ'.
    Собственные выходные файлы скрипта (лист 'Себестоимость') не трогаем.
    """
    candidates = []
    for f in BASE_DIR.glob('*.xlsx'):
        if f.name.startswith('~') or f.name.startswith('себестоимость_контроль'):
            continue
        try:
            wb = load_workbook(f, read_only=True)
            title = wb.sheetnames[0] if wb.sheetnames else ''
            wb.close()
        except Exception:
            continue
        m = re.match(r'Шаблон по себестоимости за (\d{4})-(\d{2})', title)
        if m:
            period = (int(m.group(1)), int(m.group(2)))
            candidates.append((period, f))

    if not candidates:
        log.info('Шаблон сервиса в папке не найден — синхронизация каталога пропущена.')
        return None

    candidates.sort(key=lambda t: (t[0], t[1].stat().st_mtime))
    period, path = candidates[-1]
    log.info('Найден шаблон сервиса: %s (период %04d-%02d)',
             path.name, period[0], period[1])
    return path


def read_template_rows(path):
    """Читает строки шаблона сервиса: (артикул WB, арт. поставщика, размер, бренд)."""
    rows = []
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    for row in ws.iter_rows(min_row=3, max_col=4, values_only=True):
        wb_art, sup, size, brand = (list(row) + [None]*4)[:4]
        if wb_art is None:
            continue
        try:
            wb_art = int(wb_art)
        except (TypeError, ValueError):
            continue
        rows.append((wb_art,
                     str(sup).strip() if sup is not None else '',
                     str(size).strip() if size is not None else '0',
                     str(brand).strip() if brand is not None else ''))
    wb.close()
    return rows


def sync_catalog(catalog, template_path):
    """Сверяет каталог со свежим шаблоном сервиса.

    Новые позиции — добавляет в каталог с датой и источником.
    Изменившиеся (размер/бренд/арт. поставщика) — обновляет.
    Позиции, пропавшие из шаблона, — НЕ удаляет, только предупреждает.
    Возвращает (catalog, список новых позиций).
    """
    rows = read_template_rows(template_path)
    if not rows:
        log.warning('Шаблон %s пустой или не распознан — каталог не менялся.',
                    template_path.name)
        return catalog, []

    by_wb = {it['wb']: it for it in catalog}
    today = date.today().strftime('%d.%m.%Y')
    new_items, changed = [], 0

    for wb_art, sup, size, brand in rows:
        it = by_wb.get(wb_art)
        if it is None:
            it = {'wb': wb_art, 'sup': sup, 'size': size, 'brand': brand,
                  'added': today, 'source': template_path.name}
            catalog.append(it)
            by_wb[wb_art] = it
            new_items.append(it)
            log.warning('НОВАЯ ПОЗИЦИЯ: WB %s → арт. поставщика %s (размер %s)%s',
                        wb_art, sup or '—', size,
                        f', бренд {brand}' if brand else '')
        elif (it['sup'], it['size'], it['brand']) != (sup, size, brand):
            log.info('ИЗМЕНЕНИЕ по WB %s: %s/%s/%s → %s/%s/%s',
                     wb_art, it['sup'], it['size'], it['brand'], sup, size, brand)
            it.update(sup=sup, size=size, brand=brand)
            changed += 1

    template_wb = {r[0] for r in rows}
    gone = [it for it in catalog if it['wb'] not in template_wb]
    for it in gone:
        log.warning('Позиции WB %s (арт. %s) нет в свежем шаблоне — '
                    'возможно, удалена из сервиса. В каталоге оставлена.',
                    it['wb'], it['sup'])

    if new_items or changed:
        save_catalog(catalog)
        log.info('Каталог обновлён: новых %d, изменено %d, всего %d позиций.',
                 len(new_items), changed, len(catalog))
    else:
        log.info('Новых позиций в шаблоне нет — каталог актуален (%d позиций).',
                 len(catalog))
    return catalog, new_items


# ════════════════════════════════════════════════════════════════════════════
# История себестоимости: для контроля резких изменений между запусками
# ════════════════════════════════════════════════════════════════════════════

def load_history():
    """Загружает cost_history.json: ключ 'артикул#номер_вхождения' →
    {'cost': ..., 'period': ..., 'updated': ...}."""
    if HISTORY_FILE.exists():
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding='utf-8'))
            log.info('История себестоимости: %s (%d записей)',
                     HISTORY_FILE.name, len(data))
            return data
        except Exception:
            log.error('Не удалось прочитать %s — начинаю историю заново.\n%s',
                      HISTORY_FILE.name, traceback.format_exc())
    else:
        log.info('Файл %s не найден — история начнётся с этого запуска.',
                 HISTORY_FILE.name)
    return {}


def save_history(history):
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2),
                            encoding='utf-8')


# ════════════════════════════════════════════════════════════════════════════
# Хранилище весов: weights.json (арт. поставщика → вес, г)
# В 1С корректных весов нет — источник истины: распознанные ранее PDF.
# ════════════════════════════════════════════════════════════════════════════

def load_weights():
    if WEIGHTS_FILE.exists():
        try:
            data = json.loads(WEIGHTS_FILE.read_text(encoding='utf-8'))
            log.info('Веса: %s (%d артикулов)', WEIGHTS_FILE.name, len(data))
            return data
        except Exception:
            log.error('Не удалось прочитать %s.\n%s',
                      WEIGHTS_FILE.name, traceback.format_exc())
    else:
        log.info('Файл %s не найден — будет создан из ближайшего PDF.',
                 WEIGHTS_FILE.name)
    return {}


def save_weights(weights):
    WEIGHTS_FILE.write_text(json.dumps(weights, ensure_ascii=False,
                                       indent=2, sort_keys=True),
                            encoding='utf-8')


def update_weights_from_items(weights, items):
    """Запоминает веса из распознанного PDF. Возвращает число обновлений."""
    changed = 0
    for art, name, price, weight in items:
        if weight and weights.get(art) != weight:
            weights[art] = weight
            changed += 1
    if changed:
        save_weights(weights)
        log.info('Веса обновлены из PDF: %d артикулов (всего в памяти %d).',
                 changed, len(weights))
    return changed


# ════════════════════════════════════════════════════════════════════════════
# Подключение к 1С (УТ 11.5) через COM-коннектор
# ════════════════════════════════════════════════════════════════════════════

CONFIG_TEMPLATE = """\
; Благомед — настройки подключения к 1С (УТ 11.5)
; Заполни значения и сохрани файл. Пароль хранится только здесь, локально.
; Чтобы отключить режим 1С и работать по PDF — поставь enabled = 0

[1C]
enabled  = 1
; имя сервера 1С (как в окне запуска 1С, поле "Кластер серверов")
server   = SERVER1C
; имя информационной базы на сервере (поле "Имя информационной базы")
base     = ut115
; пользователь 1С (лучше отдельный, с правами только на чтение)
user     = ЛОГИН
password = ПАРОЛЬ

[Цены]
; наименование вида цен поставщика с закупочными ценами
вид_цены = Закупочная
; часть наименования поставщика (фильтр цен, как в отчёте «Условия закупок»)
поставщик = ГАВРЮШКИН
"""


def load_config():
    """Читает config.ini. Если нет — создаёт шаблон и возвращает None."""
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(CONFIG_TEMPLATE, encoding='utf-8')
        log.warning('Создан файл настроек %s — впиши туда сервер, базу, '
                    'логин и пароль 1С. Пока работаю в режиме PDF.',
                    CONFIG_FILE.name)
        return None
    cfg = configparser.ConfigParser()
    try:
        cfg.read(CONFIG_FILE, encoding='utf-8')
    except Exception:
        log.error('Не удалось прочитать %s.\n%s',
                  CONFIG_FILE.name, traceback.format_exc())
        return None
    if not cfg.has_section('1C') or not cfg.getboolean('1C', 'enabled', fallback=False):
        log.info('Режим 1С выключен в %s — работаю по PDF.', CONFIG_FILE.name)
        return None
    c = {
        'server':   cfg.get('1C', 'server',   fallback='').strip(),
        'base':     cfg.get('1C', 'base',     fallback='').strip(),
        'user':     cfg.get('1C', 'user',     fallback='').strip(),
        'password': cfg.get('1C', 'password', fallback=''),
        'price_type': cfg.get('Цены', 'вид_цены', fallback='Закупочная').strip(),
        'supplier':   cfg.get('Цены', 'поставщик', fallback='ГАВРЮШКИН').strip(),
    }
    if (not c['server'] or not c['base'] or c['user'] in ('', 'ЛОГИН')
            or c['password'] in ('', 'ПАРОЛЬ')):
        log.warning('В %s не заполнены параметры подключения — режим PDF.',
                    CONFIG_FILE.name)
        return None
    return c


QUERY_PRICES = """
ВЫБРАТЬ
    Ном.Артикул КАК Артикул,
    Ном.Наименование КАК Наименование,
    Цены.Цена КАК Цена,
    Цены.Период КАК Период
ИЗ
    РегистрСведений.ЦеныНоменклатуры.СрезПоследних(
        , ВидЦены.Наименование = &ВидЦены) КАК Цены
    ВНУТРЕННЕЕ СОЕДИНЕНИЕ Справочник.Номенклатура КАК Ном
        ПО Цены.Номенклатура = Ном.Ссылка
ГДЕ
    Ном.Артикул В(&Артикулы)
    И НЕ Ном.ПометкаУдаления
"""

QUERY_SUPPLIER_PRICES = """
ВЫБРАТЬ
    Ном.Артикул КАК Артикул,
    Ном.Наименование КАК Наименование,
    Цены.Цена КАК Цена,
    Цены.Период КАК Период
ИЗ
    РегистрСведений.ЦеныНоменклатурыПоставщиков.СрезПоследних(
        {SREZ_ARGS}) КАК Цены
    ВНУТРЕННЕЕ СОЕДИНЕНИЕ Справочник.Номенклатура КАК Ном
        ПО Цены.Номенклатура = Ном.Ссылка
ГДЕ
    Ном.Артикул В(&Артикулы)
    И НЕ Ном.ПометкаУдаления
УПОРЯДОЧИТЬ ПО
    Наименование
"""

QUERY_SUPPLIER_STRUCTURE = """
ВЫБРАТЬ ПЕРВЫЕ 1 *
ИЗ РегистрСведений.ЦеныНоменклатурыПоставщиков
"""


QUERY_DIAG_NOM = """
ВЫБРАТЬ
    Ном.Артикул КАК Артикул,
    Ном.Наименование КАК Наименование,
    Ном.ПометкаУдаления КАК Пометка
ИЗ
    Справочник.Номенклатура КАК Ном
ГДЕ
    Ном.Артикул В(&Артикулы)
"""

QUERY_DIAG_PRICES = """
ВЫБРАТЬ
    Ном.Артикул КАК Артикул,
    Цены.ВидЦены.Наименование КАК ВидЦены,
    Цены.Цена КАК Цена
ИЗ
    РегистрСведений.ЦеныНоменклатуры.СрезПоследних() КАК Цены
    ВНУТРЕННЕЕ СОЕДИНЕНИЕ Справочник.Номенклатура КАК Ном
        ПО Цены.Номенклатура = Ном.Ссылка
ГДЕ
    Ном.Артикул В(&Артикулы)
"""


QUERY_LAST_PURCHASE = """
ВЫБРАТЬ ПЕРВЫЕ 500
    Ном.Артикул КАК Артикул,
    Ном.Наименование КАК Наименование,
    Товары.Цена КАК Цена,
    Товары.Ссылка.Дата КАК Дата,
    Товары.Ссылка.Номер КАК Номер
ИЗ
    Документ.{DOC}.Товары КАК Товары
    ВНУТРЕННЕЕ СОЕДИНЕНИЕ Справочник.Номенклатура КАК Ном
        ПО Товары.Номенклатура = Ном.Ссылка
ГДЕ
    Ном.Артикул В(&Артикулы)
    И Товары.Ссылка.Проведен
УПОРЯДОЧИТЬ ПО
    Дата УБЫВ
"""


def fetch_prices_from_1c(cfg, sup_arts, weights):
    """Подключается к УТ 11.5 через V83.COMConnector и возвращает items
    в формате [(арт, наименование, цена, вес), ...] или None при неудаче.

    Сначала ищет цены в регистре ЦеныНоменклатуры (вид цены из config.ini),
    если там пусто — в ЦеныНоменклатурыПоставщиков.
    Веса подставляются из weights.json.
    """
    try:
        import win32com.client
        import pythoncom
    except ImportError:
        log.warning('Не установлен pywin32 — режим 1С недоступен. '
                    'Выполни: pip install pywin32. Перехожу на PDF.')
        return None

    conn_str = (f'Srvr="{cfg["server"]}";Ref="{cfg["base"]}";'
                f'Usr="{cfg["user"]}";Pwd="{cfg["password"]}";')
    log.info('Подключаюсь к 1С: сервер=%s, база=%s, пользователь=%s ...',
             cfg['server'], cfg['base'], cfg['user'])

    pythoncom.CoInitialize()
    try:
        import time
        t0 = time.monotonic()
        try:
            connector = win32com.client.Dispatch('V83.COMConnector')
        except Exception:
            log.error('COM-коннектор V83.COMConnector не зарегистрирован.')
            log.error('Варианты решения:')
            log.error('  1) Разрядность Python и платформы 1С должны совпадать '
                      '(32-битная 1С → 32-битный Python).')
            log.error('  2) Зарегистрируй коннектор (cmd от администратора):')
            log.error('     regsvr32 "C:\\Program Files (x86)\\1cv8\\<версия>\\bin\\comcntr.dll"')
            log.error('     (для 64-битной 1С — без "(x86)")')
            return None
        log.info('  [тайминг] создание COM-коннектора: %.1f сек', time.monotonic() - t0)

        t0 = time.monotonic()
        try:
            v8 = connector.Connect(conn_str)
        except Exception as e:
            log.error('Не удалось подключиться к базе 1С: %s', e)
            log.error('Проверь сервер/базу/логин/пароль в %s и что у '
                      'пользователя есть право внешнего соединения (COM).',
                      CONFIG_FILE.name)
            return None
        dt = time.monotonic() - t0
        log.info('Подключение к 1С установлено.')
        log.info('  [тайминг] Connect к базе: %.1f сек%s', dt,
                 '  ← ДОЛГО: проверь DNS (впиши IP в config.ini), '
                 'поиск лицензии, антивирус' if dt > 30 else '')

        # Массив артикулов для параметра запроса
        arr = v8.NewObject('Array')
        for a in sup_arts:
            arr.Add(a)

        def run_query(text, params=None):
            q = v8.NewObject('Query')
            q.Text = text
            q.SetParameter('Артикулы', arr)
            for k, val in (params or {}).items():
                q.SetParameter(k, val)
            sel = q.Execute().Select()
            rows = []
            while sel.Next():
                art   = str(sel.Артикул).strip()
                name  = str(sel.Наименование).strip()
                price = float(sel.Цена or 0)
                try:
                    period = sel.Период
                except Exception:
                    period = None
                if art and price > 0:
                    rows.append((art, name, price, period))
            return rows

        def dedupe_latest(rows):
            """Схлопывает дубли одной номенклатуры (артикул+наименование),
            оставляя запись с самым поздним Периодом — как в отчёте
            «Условия закупок». Разные номенклатуры с одним артикулом
            (напр. жилет/пояс 232890) НЕ схлопываются."""
            best, order = {}, []
            for art, name, price, period in rows:
                key = (art, name)
                if key not in best:
                    best[key] = (price, period)
                    order.append(key)
                else:
                    old_price, old_period = best[key]
                    try:
                        newer = (old_period is None) or (period is not None
                                                         and period > old_period)
                    except Exception:
                        newer = False
                    if newer:
                        if abs(old_price - price) > 0.01:
                            log.info('  Дубль %s «%s»: %.2f → беру более '
                                     'позднюю %.2f', art, name[:40],
                                     old_price, price)
                        best[key] = (price, period)
                    elif abs(old_price - price) > 0.01:
                        log.info('  Дубль %s «%s»: %.2f (старая) отброшена, '
                                 'оставлена %.2f', art, name[:40],
                                 price, old_price)
            return [(a, n, best[(a, n)][0]) for a, n in order]

        def com_err_text(exc):
            """Достаёт человеческий текст ошибки 1С из COM-исключения."""
            try:
                info = exc.excepinfo
                if info and info[2]:
                    return str(info[2]).strip()
            except Exception:
                pass
            return str(exc)

        # 1) ГЛАВНЫЙ источник — «Цены номенклатуры поставщиков»
        #    (это и есть отчёт «Условия закупок»: вид цены поставщика «Закупочная»)
        #    Имена измерений отличаются между релизами УТ — определяем по факту.
        t0 = time.monotonic()
        rows = []

        cols = []
        try:
            qs = v8.NewObject('Query')
            qs.Text = QUERY_SUPPLIER_STRUCTURE
            res = qs.Execute()
            kolonki = res.Колонки
            for i in range(kolonki.Количество()):
                cols.append(str(kolonki.Получить(i).Имя))
            log.info('Структура регистра цен поставщиков: %s', ', '.join(cols))
        except Exception as e:
            log.warning('Регистр «Цены номенклатуры поставщиков» недоступен: %s',
                        com_err_text(e)[:200])

        if cols:
            vid_dim = next((c for c in cols if 'видцен' in c.lower()), None)
            sup_dim = next((c for c in cols if 'поставщик' in c.lower()
                            and 'видцен' not in c.lower()), None)
            if not vid_dim:
                log.warning('В регистре нет измерения вида цены (колонки: %s) — '
                            'беру все строки без фильтра по виду.', ', '.join(cols))

            # Варианты условий среза от точного к общему
            attempts = []
            if vid_dim and sup_dim:
                attempts.append((
                    f'{vid_dim} + {sup_dim}',
                    f', {vid_dim}.Наименование = &ВидЦены '
                    f'И {sup_dim}.Наименование ПОДОБНО &Поставщик',
                    {'ВидЦены': cfg['price_type'],
                     'Поставщик': f'%{cfg["supplier"]}%'}))
            if vid_dim:
                attempts.append((
                    f'{vid_dim} + владелец-поставщик',
                    f', {vid_dim}.Наименование = &ВидЦены '
                    f'И {vid_dim}.Владелец.Наименование ПОДОБНО &Поставщик',
                    {'ВидЦены': cfg['price_type'],
                     'Поставщик': f'%{cfg["supplier"]}%'}))
                attempts.append((
                    f'только {vid_dim}',
                    f', {vid_dim}.Наименование = &ВидЦены',
                    {'ВидЦены': cfg['price_type']}))
            attempts.append(('срез без условий', '', {}))

            for label, srez_args, params in attempts:
                try:
                    rows = run_query(
                        QUERY_SUPPLIER_PRICES.replace('{SREZ_ARGS}', srez_args),
                        params)
                except Exception as e:
                    log.info('Вариант «%s» не подошёл: %s',
                             label, com_err_text(e)[:200])
                    continue
                if rows:
                    log.info('Цены поставщиков (%s): %d строк', label, len(rows))
                    break
                log.info('Вариант «%s» выполнился, но вернул 0 строк '
                         '(проверь «вид_цены»/«поставщик» в %s).',
                         label, CONFIG_FILE.name)

        # Дубли одной номенклатуры схлопываем по последнему периоду;
        # разные номенклатуры с одним артикулом (жилет/пояс 232890) остаются.
        # Порядок по наименованию, как в отчёте «Условия закупок».
        if rows:
            before = len(rows)
            rows = dedupe_latest(rows)
            if before != len(rows):
                log.info('Схлопнуто дублей записей регистра: %d (строк '
                         'осталось: %d)', before - len(rows), len(rows))
        items, no_weight = [], []
        seen = set()
        for art, name, price in rows:
            seen.add(art)
            weight = weights.get(art)
            if not weight:
                no_weight.append(art)
            items.append((art, name, price, weight))

        # 2) Добор из собственного регистра «Цены номенклатуры»
        missing = sorted(set(sup_arts) - seen)
        if missing:
            try:
                rows2 = dedupe_latest(
                    run_query(QUERY_PRICES, {'ВидЦены': cfg['price_type']}))
                added = 0
                for art, name, price in rows2:
                    if art in seen:
                        continue
                    seen.add(art)
                    items.append((art, name, price, weights.get(art)))
                    added += 1
                if added:
                    log.info('Добор из регистра «Цены номенклатуры» (СВЕРЬ эти '
                             'цены, регистр может быть устаревшим): %s',
                             ', '.join(a for a, *_ in items[-added:]))
            except Exception:
                log.warning('Регистр «Цены номенклатуры» недоступен.')
        log.info('  [тайминг] запросы цен: %.1f сек', time.monotonic() - t0)

        if not items:
            log.error('1С не вернула ни одной цены по %d артикулам каталога. '
                      'Проверь вид цены в %s. Перехожу на PDF.',
                      len(sup_arts), CONFIG_FILE.name)
            return None

        if no_weight:
            log.info('Без веса (вес не участвует в расчёте): %d поз.',
                     len(no_weight))

        # ДИАГНОСТИКА: артикулы каталога, по которым 1С не вернула цену
        missing = sorted(set(sup_arts) - seen)
        if missing:
            log.warning('В регистрах цен нет цены по %d артикулам: %s',
                        len(missing), ', '.join(missing))
            log.info('Ищу цены последнего поступления по этим артикулам...')
            try:
                m_arr = v8.NewObject('Array')
                for a in missing:
                    m_arr.Add(a)

                purchase_rows = []
                for doc_name in ('ПриобретениеТоваровУслуг',
                                 'ПоступлениеТоваровУслуг'):
                    try:
                        qp = v8.NewObject('Query')
                        qp.Text = QUERY_LAST_PURCHASE.replace('{DOC}', doc_name)
                        qp.SetParameter('Артикулы', m_arr)
                        selp = qp.Execute().Select()
                        while selp.Next():
                            purchase_rows.append((
                                str(selp.Артикул).strip(),
                                str(selp.Наименование).strip(),
                                float(selp.Цена or 0),
                                str(selp.Дата), str(selp.Номер)))
                        break  # имя документа подошло
                    except Exception:
                        continue  # пробуем следующее имя

                # Первая строка по артикулу = самое свежее поступление
                for art, name, price, dt, num in purchase_rows:
                    if art in seen or price <= 0:
                        continue
                    seen.add(art)
                    items.append((art, name, price, weights.get(art)))
                    log.info('  Арт %s «%s» — цена последнего поступления: '
                             '%.2f (док. №%s от %s)', art, name[:45], price,
                             num.strip(), dt[:10])
                missing = sorted(set(sup_arts) - seen)
            except Exception:
                log.error('Поиск по поступлениям не удался:\n%s',
                          traceback.format_exc())

        if missing:
            log.warning('Цена не нашлась нигде (ни регистры, ни поступления): %s',
                        ', '.join(missing))
            try:
                m_arr = v8.NewObject('Array')
                for a in missing:
                    m_arr.Add(a)

                qd = v8.NewObject('Query')
                qd.Text = QUERY_DIAG_NOM
                qd.SetParameter('Артикулы', m_arr)
                sel = qd.Execute().Select()
                found_cards = {}
                while sel.Next():
                    found_cards[str(sel.Артикул).strip()] = (
                        str(sel.Наименование).strip(), bool(sel.Пометка))

                no_card = [a for a in missing if a not in found_cards]
                if no_card:
                    log.warning('  ДИАГНОЗ: нет карточки номенклатуры с таким '
                                'артикулом: %s', ', '.join(no_card))
                    log.warning('    → проверь поле «Артикул» в карточках 1С '
                                '(пробелы, другой код).')
                for a, (nm, mark) in sorted(found_cards.items()):
                    if mark:
                        log.warning('  ДИАГНОЗ: %s «%s» — карточка ПОМЕЧЕНА НА '
                                    'УДАЛЕНИЕ.', a, nm)

                qd2 = v8.NewObject('Query')
                qd2.Text = QUERY_DIAG_PRICES
                qd2.SetParameter('Артикулы', m_arr)
                sel2 = qd2.Execute().Select()
                price_types = {}
                while sel2.Next():
                    price_types.setdefault(str(sel2.Артикул).strip(), []).append(
                        f'{sel2.ВидЦены}={float(sel2.Цена or 0):g}')
                for a in sorted(set(found_cards) - set(no_card)):
                    if not found_cards[a][1]:  # не помеченные
                        pts = price_types.get(a)
                        if pts:
                            log.warning('  ДИАГНОЗ: %s «%s» — есть цены, но под '
                                        'другими видами: %s', a,
                                        found_cards[a][0], '; '.join(pts))
                        else:
                            log.warning('  ДИАГНОЗ: %s «%s» — карточка есть, но '
                                        'в регистре «Цены номенклатуры» нет НИ '
                                        'ОДНОЙ цены.', a, found_cards[a][0])
                if price_types:
                    log.warning('  → если цены под другим видом — поменяй '
                                '«вид_цены» в %s или установи закупочные цены '
                                'в 1С по этим позициям.', CONFIG_FILE.name)
            except Exception:
                log.error('Диагностика не удалась:\n%s', traceback.format_exc())

        return items
    finally:
        pythoncom.CoUninitialize()


# ════════════════════════════════════════════════════════════════════════════
# Поиск PDF в папке скрипта
# ════════════════════════════════════════════════════════════════════════════

def find_pdf(quiet=False):
    pdfs = [f for f in BASE_DIR.glob('*.pdf') if not f.name.startswith('~')]
    if not pdfs:
        if quiet:
            log.info('PDF в папке нет — веса берутся только из weights.json.')
        else:
            log.critical('PDF-отчёт не найден в папке: %s', BASE_DIR)
            log.critical('Положи выгрузку из 1С (файл .pdf) в эту же папку.')
        return None
    if len(pdfs) > 1:
        log.warning('Найдено несколько PDF: %s — берём первый по алфавиту.',
                    [f.name for f in pdfs])
    pdf_path = sorted(pdfs)[0]
    log.info('PDF-отчёт: %s', pdf_path.name)
    return pdf_path


# ════════════════════════════════════════════════════════════════════════════
# Парсинг PDF
# ════════════════════════════════════════════════════════════════════════════

def parse_pdf(path):
    items = []
    lines = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            lines.extend(page.extract_text().splitlines())

    pat = re.compile(
        r'^(.+?)\s+(\d{5,6})\s+Закупочная\s+([\d\s]+,\d{2})\s+RUB(?:\s+([\d,]+))?'
    )
    for line in lines:
        m = pat.match(line.strip())
        if not m:
            continue
        name   = m.group(1).strip()
        art    = m.group(2)
        price  = float(m.group(3).replace(' ', '').replace(',', '.'))
        w_str  = m.group(4)
        weight = float(w_str.replace(',', '.')) if w_str else None
        items.append((art, name, price, weight))

    return items


# ════════════════════════════════════════════════════════════════════════════
# Расчёт
# ════════════════════════════════════════════════════════════════════════════

def calc(price, weight, extra_rub):
    weight_cost = round(weight / 1000 * WEIGHT_RATE, 2) if weight else 0
    cost_int    = math.floor((price + weight_cost + extra_rub) * MARGIN)
    return weight_cost, cost_int


# ════════════════════════════════════════════════════════════════════════════
# Файл 1: детальный контрольный
# ════════════════════════════════════════════════════════════════════════════

def make_control_file(items, out_path, history):
    """Контрольный файл. Сравнивает себестоимость с прошлым запуском
    (cost_history.json) и подсвечивает резкие изменения (>= COST_ALERT_PCT %).
    Возвращает обновлённую историю."""
    wb = Workbook()
    ws = wb.active
    ws.title = 'Контроль себестоимости'

    thin  = Side(style='thin', color='BFBFBF')
    brd   = Border(left=thin, right=thin, top=thin, bottom=thin)
    hfont = Font(name='Arial', bold=True, color='FFFFFF', size=10)
    fills = {
        'blue':   PatternFill('solid', start_color='1F4E79'),
        'green':  PatternFill('solid', start_color='375623'),
        'purple': PatternFill('solid', start_color='5B2C8D'),
        'orange': PatternFill('solid', start_color='843C0C'),
        'red':    PatternFill('solid', start_color='9C0006'),
        'alt':    PatternFill('solid', start_color='EBF3FB'),
        'g_cell': PatternFill('solid', start_color='E2EFDA'),
        'p_cell': PatternFill('solid', start_color='EAD7F7'),
        'o_cell': PatternFill('solid', start_color='FCE4D6'),
        'alert':  PatternFill('solid', start_color='FFC7CE'),  # резкое изменение
    }
    alert_font = Font(name='Arial', size=10, bold=True, color='9C0006')

    headers = [
        ('Артикул',                                           'blue'),
        ('Наименование',                                      'blue'),
        ('Цена закупочная, руб.',                             'blue'),
        ('Вес, г',                                            'blue'),
        (f'Стоимость веса\n({WEIGHT_RATE} руб./кг)',          'green'),
        ('Доп. расходы, руб.',                                'purple'),
        ('Причина доп. расходов',                             'purple'),
        (f'Себестоимость, руб.\n(закуп.+вес+доп.)×{MARGIN}', 'orange'),
        ('Пред. себестоимость,\nруб.',                        'red'),
        (f'Изменение\n(алерт от ±{COST_ALERT_PCT}%)',         'red'),
    ]
    for col, (h, color) in enumerate(headers, 1):
        c = ws.cell(row=1, column=col, value=h)
        c.font = hfont
        c.fill = fills[color]
        c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        c.border = brd
    ws.row_dimensions[1].height = 36

    period = date.today().strftime('%Y_%m')
    today  = date.today().strftime('%d.%m.%Y')
    new_history = {}
    occurrence  = {}
    alerts = []

    for i, (art, name, price, weight) in enumerate(items, 2):
        extra_rub, reason = EXTRA.get(art, (0, ''))
        weight_cost, cost = calc(price, weight, extra_rub)

        # Сравнение с прошлым запуском
        occurrence[art] = occurrence.get(art, 0) + 1
        key  = f'{art}#{occurrence[art]}'
        prev = history.get(key)
        prev_cost = prev['cost'] if prev else None
        delta_pct = None
        is_alert  = False
        if prev_cost:
            delta_pct = (cost - prev_cost) / prev_cost * 100
            is_alert  = abs(delta_pct) >= COST_ALERT_PCT
            if is_alert:
                alerts.append((art, name, prev_cost, cost, delta_pct,
                               prev.get('period', '?')))
        new_history[key] = {'cost': cost, 'period': period, 'updated': today,
                            'name': name}

        row_fill = fills['alt'] if i % 2 == 0 else None
        delta_str = (f'{delta_pct:+.1f}%' if delta_pct is not None
                     else ('новая' if prev_cost is None else ''))
        vals = [art, name, price,
                weight or None,
                weight_cost if weight else None,
                extra_rub or None,
                reason or None,
                cost,
                prev_cost,
                delta_str or None]
        for col, val in enumerate(vals, 1):
            c = ws.cell(row=i, column=col, value=val)
            c.font = Font(name='Arial', size=10)
            c.border = brd
            c.alignment = Alignment(vertical='center')
            if col == 5 and weight:       c.fill = fills['g_cell']
            elif col in (6,7) and extra_rub: c.fill = fills['p_cell']
            elif col == 8:                c.fill = fills['o_cell']
            elif row_fill:                c.fill = row_fill
        if is_alert:
            for col in (8, 9, 10):
                ws.cell(row=i, column=col).fill = fills['alert']
                ws.cell(row=i, column=col).font = alert_font
        for col in (1, 3, 4, 5, 6, 8, 9, 10):
            ws.cell(row=i, column=col).alignment = Alignment(horizontal='center', vertical='center')
        for col in (3, 5, 6, 8, 9):
            ws.cell(row=i, column=col).number_format = '#,##0.00'

    ws.column_dimensions['A'].width = 12
    ws.column_dimensions['B'].width = 54
    ws.column_dimensions['C'].width = 20
    ws.column_dimensions['D'].width = 9
    ws.column_dimensions['E'].width = 20
    ws.column_dimensions['F'].width = 18
    ws.column_dimensions['G'].width = 40
    ws.column_dimensions['H'].width = 24
    ws.column_dimensions['I'].width = 20
    ws.column_dimensions['J'].width = 16
    ws.freeze_panes = 'A2'

    if alerts:
        log.warning('РЕЗКИЕ ИЗМЕНЕНИЯ СЕБЕСТОИМОСТИ (порог ±%d%%): %d поз.',
                    COST_ALERT_PCT, len(alerts))
        for art, name, old, new, d, old_period in alerts:
            log.warning('  Арт %s | %s: %s → %s руб. (%+.1f%%, пред. период %s)',
                        art, name[:40], old, new, d, old_period)

    # Лист «Параметры»
    ws2 = wb.create_sheet('Параметры')
    ws2['A1'] = 'Параметр'; ws2['B1'] = 'Значение'; ws2['C1'] = 'Примечание'
    for c in ('A1','B1','C1'): ws2[c].font = Font(bold=True)
    ws2.append(['Ставка веса (руб./кг)', WEIGHT_RATE, 'Стоимость доставки 1 кг'])
    ws2.append(['Коэффициент наценки', MARGIN, f'+{round((MARGIN-1)*100)}% к базе'])
    ws2.append(['Порог алерта изменения, %', COST_ALERT_PCT,
                'Подсветка при резком изменении себестоимости'])
    ws2.append([])
    ws2.append(['Доп. расходы по артикулам (стоимость наполнителя):','',''])
    ws2.append(['Артикул','Сумма, руб.','Причина'])
    for art, (amt, rsn) in sorted(EXTRA.items()):
        ws2.append([art, amt, rsn])
    for col in ('A','B','C'):
        ws2.column_dimensions[col].width = 40 if col == 'C' else 20

    wb.save(out_path)
    log.info('Контрольный файл сохранён: %s', out_path.name)
    return new_history


# ════════════════════════════════════════════════════════════════════════════
# Файл 2: файл для сервиса (генерируется из каталога WB + расчёта)
# ════════════════════════════════════════════════════════════════════════════

def make_service_file(items, catalog, out_path, new_wb_arts=()):
    # Словарь наших себестоимостей: арт.поставщика -> [cost, ...]
    our = {}
    for art, name, price, weight in items:
        extra_rub, _ = EXTRA.get(art, (0, ''))
        _, cost = calc(price, weight, extra_rub)
        our.setdefault(art, []).append(cost)

    wb = Workbook()
    ws = wb.active
    ws.title = 'Себестоимость'

    period = date.today().strftime('%Y-%m')

    # Строка 1: заголовок (копируем стиль оригинала)
    ws['A1'] = f'Благомед_себестоимость_{period}'
    ws['A1'].font = Font(bold=True)
    ws['C1'] = 'ВНИМАНИЕ: Все цены указываются без копеек'
    ws['C1'].font = Font(bold=True, color='FF0000')
    ws['E1'] = 'Редактируемыми являются ячейки зеленых столбцов'
    ws['E1'].font = Font(bold=True)

    # Строка 2: шапка таблицы
    blue_fill   = PatternFill('solid', start_color='6495ED')
    green_fill  = PatternFill('solid', start_color='00FF00')
    yellow_fill = PatternFill('solid', start_color='FFFF00')
    new_fill    = PatternFill('solid', start_color='BDD7EE')  # новые позиции
    hfont = Font(bold=True)
    col_headers = ['Артикул WB', 'Артикул поставщика', 'Размер', 'Бренд',
                   'Себестоимость', 'Доп. расходы']
    for col, h in enumerate(col_headers, 1):
        c = ws.cell(row=2, column=col, value=h)
        c.font = hfont
        c.fill = green_fill if col >= 5 else blue_fill
        c.alignment = Alignment(horizontal='center', vertical='center')

    # Данные из каталога
    updated = 0
    skipped = []
    occurrence = {}
    new_wb_arts = set(new_wb_arts)

    for row_idx, it in enumerate(catalog, 3):
        wb_art, sup_art = it['wb'], it['sup']
        occurrence[sup_art] = occurrence.get(sup_art, 0) + 1
        idx = occurrence[sup_art] - 1

        if sup_art in our:
            costs = our[sup_art]
            cost  = costs[idx] if idx < len(costs) else costs[-1]
            updated += 1
        elif sup_art in FIXED_COSTS:
            cost = FIXED_COSTS[sup_art]
            log.info('  Арт %s — фиксированная себестоимость: %s', sup_art, cost)
        else:
            cost = 0
            skipped.append(sup_art)
            log.warning('  Арт %s не найден ни в данных (1С/PDF), ни в FIXED_COSTS — себестоимость = 0', sup_art)

        ws.cell(row=row_idx, column=1, value=wb_art)
        ws.cell(row=row_idx, column=2, value=sup_art)
        ws.cell(row=row_idx, column=3, value=it['size'])
        ws.cell(row=row_idx, column=4, value=it['brand'])
        c_cost = ws.cell(row=row_idx, column=5, value=cost)
        ws.cell(row=row_idx, column=6, value=0)

        # Голубой — позиция добавлена в каталог при этом запуске
        if wb_art in new_wb_arts:
            ws.cell(row=row_idx, column=1).fill = new_fill
            ws.cell(row=row_idx, column=2).fill = new_fill
        # Жёлтый — не нашли ни в PDF, ни в FIXED_COSTS
        if sup_art in skipped:
            c_cost.fill = yellow_fill

    ws.column_dimensions['A'].width = 15
    ws.column_dimensions['B'].width = 23
    ws.column_dimensions['C'].width = 11
    ws.column_dimensions['D'].width = 10
    ws.column_dimensions['E'].width = 18
    ws.column_dimensions['F'].width = 17

    wb.save(out_path)
    log.info('Файл для сервиса сохранён: %s', out_path.name)
    fixed = len(catalog) - updated - len(skipped)
    log.info('  Из данных: %d | Фиксированные: %d | Не найдено (0): %d | Всего: %d',
             updated, fixed, len(skipped), len(catalog))
    if skipped:
        log.warning('  Нет нигде (себестоимость = 0): %s', ', '.join(skipped))
        log.warning('  → если это новые позиции другого поставщика, добавь их в FIXED_COSTS;')
        log.warning('  → если они от Гаврюшкина, проверь, что в 1С есть закупочная цена (или позиция есть в отчет.pdf).')
    return updated, skipped


# ════════════════════════════════════════════════════════════════════════════
# Точка входа
# ════════════════════════════════════════════════════════════════════════════

def main():
    period = date.today().strftime('%Y_%m')

    log.info('=' * 60)
    log.info('ЗАПУСК  %s', datetime.now().strftime('%d.%m.%Y %H:%M:%S'))
    log.info('Рабочая папка: %s', BASE_DIR)
    log.info('Параметры: ставка веса=%s руб./кг, наценка=×%s, доп.расходов=%d арт.',
             WEIGHT_RATE, MARGIN, len(EXTRA))

    # 1. Каталог WB: загружаем память и синхронизируем со свежим шаблоном сервиса
    catalog = load_catalog()
    new_items = []
    template = find_service_template()
    if template:
        catalog, new_items = sync_catalog(catalog, template)

    # 2. Источник цен: 1С (основной) или PDF (резервный)
    weights  = load_weights()
    pdf_path = find_pdf(quiet=True)

    # Веса собираем из PDF всегда, когда он есть — независимо от источника цен
    pdf_items = None
    if pdf_path:
        try:
            pdf_items = parse_pdf(pdf_path)
            if pdf_items:
                update_weights_from_items(weights, pdf_items)
        except Exception:
            log.error('Не удалось разобрать PDF для весов:\n%s',
                      traceback.format_exc())

    items  = None
    source = None

    cfg = load_config()
    if cfg:
        sup_arts = sorted({it['sup'] for it in catalog} - set(FIXED_COSTS))
        items = fetch_prices_from_1c(cfg, sup_arts, weights)
        if items:
            source = '1С'

    if items is None:
        if pdf_items is None:
            if not pdf_path:
                log.critical('Ни 1С, ни PDF недоступны — данных для расчёта нет.')
                log.critical('Положи отчет.pdf в папку или настрой config.ini.')
            else:
                log.critical('Позиции из PDF не распознаны. Проверь формат PDF.')
            input('\nНажми Enter для выхода...')
            sys.exit(1)
        items = pdf_items
        source = 'PDF'
        log.info('Распознано из PDF: %d позиций', len(items))
    else:
        log.info('Получено из 1С: %d позиций', len(items))

    log.info('Источник цен: %s', source)

    # Подсказка: артикулы из источника, для которых нет артикула WB в каталоге
    known_sup = {it['sup'] for it in catalog}
    orphans = sorted({art for art, *_ in items if art not in known_sup})
    if orphans:
        log.warning('В данных (%s) есть артикулы без артикула WB в каталоге: %s',
                    source, ', '.join(orphans))
        log.warning('  → скачай свежий шаблон из сервиса в эту папку и перезапусти — '
                    'скрипт подхватит сопоставление автоматически.')

    # 3. Генерация файлов
    control_path = BASE_DIR / f'себестоимость_контроль_{period}.xlsx'
    service_path = BASE_DIR / f'Благомед_себестоимость_{period}.xlsx'

    history = load_history()
    new_history = make_control_file(items, control_path, history)
    history.update(new_history)   # объединяем: старые записи не теряются
    save_history(history)
    updated, skipped = make_service_file(
        items, catalog, service_path,
        new_wb_arts=[it['wb'] for it in new_items])

    log.info('ИТОГ: заполнено %d / не найдено %d / всего в каталоге %d',
             updated, len(skipped), len(catalog))
    if new_items:
        log.info('НОВЫЕ ПОЗИЦИИ за этот запуск (%d):', len(new_items))
        for it in new_items:
            log.info('  WB %s → арт. %s (добавлена %s, источник: %s)',
                     it['wb'], it['sup'], it['added'], it['source'])
    log.info('Лог: %s', LOG_FILE)
    log.info('=' * 60)

    print('\nГотово! Нажми Enter для выхода...')
    input()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        log.critical('КРИТИЧЕСКАЯ ОШИБКА:\n%s', traceback.format_exc())
        input('\nНажми Enter для выхода...')
        sys.exit(1)
