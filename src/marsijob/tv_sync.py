"""Синк вакансий с «Работа России» (trudvsem.ru) — открытые данные, без ключей.

ГОСТ-портал государственной биржи труда: 489k+ вакансий, публичный JSON API
(opendata.trudvsem.ru/api/v1). Юридически чистый источник без регистрации.

Ограничения данных: навыки часто не заполнены, удалёнка не маркирована —
маппинг опирается на зарплату/график/регион/опыт/образование; среда —
нейтральные значения (как у hh).
"""
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

log = logging.getLogger("marsijob.tv")

TV_BASE = "https://opendata.trudvsem.ru/api/v1"
USER_AGENT = "MARSI-Job/1.0 (Telegram bot; https://t.me/MarsiJobBot)"

# фиды: свежие по регионам (коды областей) + точечные текстовые запросы
REGION_FEEDS: List[Dict[str, Any]] = [
    {"label": "moscow", "path": "region/7700000000000"},
    {"label": "spb", "path": "region/7800000000000"},
    {"label": "mosobl", "path": "region/5000000000000"},
    {"label": "sverdlovsk", "path": "region/6600000000000"},
    {"label": "nsk", "path": "region/5400000000000"},
    {"label": "tatarstan", "path": "region/1600000000000"},
    {"label": "krasnodar", "path": "region/0300000000000"},
    {"label": "tyumen", "path": "region/7200000000000"},
    {"label": "chelyabinsk", "path": "region/7500000000000"},
    {"label": "bashkiria", "path": "region/0200000000000"},
    {"label": "rostov", "path": "region/6000000000000"},
    {"label": "samara", "path": "region/6300000000000"},
    {"label": "nnovgorod", "path": "region/5200000000000"},
    {"label": "perm", "path": "region/5900000000000"},
    {"label": "voronezh", "path": "region/3600000000000"},
    {"label": "volgograd", "path": "region/4000000000000"},
]

TEXT_FEEDS: List[Dict[str, Any]] = [
    {"label": "it", "params": {"text": "программист"}},
    {"label": "eng", "params": {"text": "инженер"}},
    {"label": "mgr", "params": {"text": "менеджер"}},
    {"label": "acc", "params": {"text": "бухгалтер"}},
    {"label": "drv", "params": {"text": "водитель"}},
    {"label": "sales", "params": {"text": "продавец"}},
    {"label": "med", "params": {"text": "врач"}},
    {"label": "edu", "params": {"text": "учитель"}},
    {"label": "weld", "params": {"text": "сварщик"}},
    {"label": "adm", "params": {"text": "администратор"}},
    {"label": "law", "params": {"text": "юрист"}},
    {"label": "store", "params": {"text": "кладовщик"}},
]

PER_FEED = 100          # максимум API на страницу
PAGES_PER_FEED = 2      # 2 × 100 = 200 вакансий на фид
REQUEST_DELAY = 0.8     # пауза между HTTP-запросами (гос-API не любит чаще)
RETRY_ON_500 = 1        # один повтор при 5xx через паузу

SCHEDULE_MAP = [
    ("гибк", "flexible"), ("смен", "shift"), ("частичн", "part_time"),
    ("ночн", "night"), ("полный", "full_time"), ("ненорм", "full_time"),
]
EDU_MAP = [
    ("высш", "bachelor"), ("бакалавр", "bachelor"), ("магистр", "master"),
    ("средн...проф", "vocational"), ("среднее профессиональное", "vocational"),
    ("средн", "secondary"),
]
DOMAIN_MAP = [
    ("информацион", "it"), ("программ", "it"), ("здравоохран", "healthcare"),
    ("медиц", "healthcare"), ("образован", "education"), ("наук", "education"),
    ("финанс", "fintech"), ("банк", "bank"), ("страх", "fintech"),
    ("транспорт", "logistics"), ("логист", "logistics"), ("строит", "industry"),
    ("производств", "industry"), ("промышлен", "industry"),
    ("торгов", "ecommerce"), ("коммерц", "ecommerce"), ("туризм", "services"),
    ("культура", "services"), ("спорт", "services"), ("государств", "bank"),
    ("администр", "services"), ("безопасн", "services"),
]

CITY_TZ = {
    "москва": 3, "санкт-петербург": 3, "новосибирск": 7, "екатеринбург": 5,
    "нижний новгород": 3, "казань": 3, "челябинск": 5, "самара": 4, "омск": 6,
    "ростов-на-дону": 3, "уфа": 5, "красноярск": 7, "пермь": 5, "воронеж": 3,
    "волгоград": 3, "краснодар": 3, "саратов": 4, "тюмень": 5, "удмурт": 4,
    "башкортостан": 5, "свердловск": 5, "татарстан": 3, "чуваш": 3,
    "кемеров": 7, "иркутск": 8, "хабаровск": 10, "владивосток": 10,
    "калмык": 3, "крым": 3, "белгород": 3, "курск": 3, "липецк": 3,
    "тула": 3, "брянск": 3, "орел": 3, "рязань": 3, "владимир": 3,
    "иваново": 3, "кострома": 3, "смоленск": 3, "тверь": 3, "ярославль": 3,
    "псков": 3, "новгород": 3, "вологда": 3, "калуж": 3, "мурманск": 3,
    "архангельск": 3, "вологда": 3, "пермский": 5, "ставропол": 3,
    "дагестан": 3, "чечен": 3, "тюмен": 5, "ханты": 5, "ямал": 5,
}

from .hh_sync import NEUTRAL_ENV  # единые нейтральные значения среды


class TvError(Exception):
    pass


def _get(url: str, timeout: int = 30) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise TvError(f"trudvsem запрос не удался: {e}") from e


def _map_schedule(raw: Optional[str]) -> str:
    low = (raw or "").lower()
    for key, val in SCHEDULE_MAP:
        if key in low:
            return val
    return "full_time"


def _map_education(raw: Optional[str]) -> Optional[str]:
    low = (raw or "").lower()
    for key, val in EDU_MAP:
        if key in low:
            return val
    return None


def _map_domain(category: Optional[str], title: str) -> str:
    low = f"{category or ''} {title}".lower()
    for key, val in DOMAIN_MAP:
        if key in low:
            return val
    return "services"


def _safe_experience(raw: Any) -> int:
    """Опыт бывает числом или строкой «от 0»/«от 1 года»."""
    if raw is None:
        return 0
    if isinstance(raw, (int, float)):
        return int(raw)
    m = re.search(r"\d+", str(raw))
    return int(m.group(0)) if m else 0


def map_tv_vacancy(item: Dict[str, Any]) -> Dict[str, Any]:
    """Карточка trudvsem → схема движка МАРСИ-Job."""
    v = item.get("vacancy") or item
    vid = str(v.get("id", ""))[:8] or "x"
    region = ((v.get("region") or {}).get("name")) or ""
    address = ""
    addrs = ((v.get("addresses") or {}).get("address")) or []
    if isinstance(addrs, list) and addrs:
        address = str((addrs[0] or {}).get("location", ""))

    city_match = re.search(r"(?:г|город)\s+([А-ЯЁа-яё\- ]+?)[,;]", address)
    city = (city_match.group(1).strip() if city_match else region).title()
    tz = 3
    low_region = region.lower()
    for key, offset in CITY_TZ.items():
        if key in low_region:
            tz = offset
            break

    salary_min = v.get("salary_min")
    salary_max = v.get("salary_max")

    vacancy: Dict[str, Any] = {
        "id": f"TV-{vid}",
        "title": v.get("job-name") or "Вакансия",
        "hard_skills": {
            re.sub(r"\s+", "_", str(s).strip().lower())[:40]: {"req_level": 0.6, "must": False}
            for s in (v.get("skills") or [])[:12] if str(s).strip()
        },
        "experience_years_min": _safe_experience((v.get("requirement") or {}).get("experience")),
        "domain": _map_domain((v.get("category") or {}).get("specialisation"),
                              v.get("job-name") or ""),
        "education_level_min": _map_education((v.get("requirement") or {}).get("education")),
        "portfolio_required": 0.0,
        "work_format": "office",
        "schedule": _map_schedule(v.get("schedule")),
        "employment_type": "tk",
        "business_trips_required": 0.2,
        "relocation_supported": False,
        "location": {"city": city or None, "tz": tz},
        "work_permit_required": ["RU"],
        "commute_minutes": None,
        "team_size": NEUTRAL_ENV["team_size"],
        "urgency_months": 2,
        "start_date_required_months": 1,
        "vacancy_clarity": 0.6,
        "trust_index": 0.6,
        "source": "trudvsem",
        "external_url": v.get("vac_url"),
        "employer_name": (v.get("company") or {}).get("name"),
    }
    vacancy.update(NEUTRAL_ENV)
    vacancy["team_size"] = NEUTRAL_ENV["team_size"]
    if salary_min or salary_max:
        lo = float(salary_min) if salary_min else round(float(salary_max) * 0.8)
        hi = float(salary_max) if salary_max else round(float(salary_min) * 1.2)
        vacancy["salary"] = {"min": int(lo), "max": int(hi), "compromise": True}
    return vacancy


def fetch_page(params: Dict[str, Any], limit: int = PER_FEED,
               offset: int = 0, path: str = "") -> List[Dict[str, Any]]:
    query = dict(params)
    query.update({"limit": limit, "offset": offset})
    base = f"{TV_BASE}/vacancies"
    if path:
        base = f"{TV_BASE}/vacancies/{path}"
    url = f"{base}?{urllib.parse.urlencode(query)}"
    try:
        data = _get(url)
    except TvError:
        if RETRY_ON_500 < 1:
            raise
        time.sleep(3)  # гос-API иногда отдаёт 5xx под нагрузкой — повторяем
        data = _get(url)
    time.sleep(REQUEST_DELAY)
    results = data.get("results") or {}
    items = results.get("vacancies") or []
    return items if isinstance(items, list) else [items]


def sync_trudvsem(storage: Any,
                  region_feeds: Optional[List[Dict[str, Any]]] = None,
                  text_feeds: Optional[List[Dict[str, Any]]] = None,
                  pages_per_feed: int = PAGES_PER_FEED) -> Dict[str, int]:
    """Прогон синка trudvsem (регионы + текстовые запросы, с пагинацией)."""
    stats = {"fetched": 0, "inserted": 0, "updated": 0}
    employer_user_id = storage.ensure_external_employer("trudvsem.ru")

    def run_feed(fetch_call) -> None:
        for page in range(pages_per_feed):
            try:
                items = fetch_call(page)
            except TvError as e:
                log.warning("tv feed failed: %s", e)
                return
            for item in items:
                try:
                    mapped = map_tv_vacancy(item)
                except Exception as e:
                    log.warning("tv map failed: %s", e)
                    continue
                if not mapped["title"]:
                    continue
                stats["fetched"] += 1
                action = storage.upsert_external_vacancy(mapped, employer_user_id)
                stats[action] = stats.get(action, 0) + 1

    for feed in (region_feeds if region_feeds is not None else REGION_FEEDS):
        run_feed(lambda p, f=feed: fetch_page({}, offset=p * PER_FEED, path=f["path"]))
    for feed in (text_feeds if text_feeds is not None else TEXT_FEEDS):
        run_feed(lambda p, f=feed: fetch_page(f["params"], offset=p * PER_FEED))
    log.info("trudvsem sync: %s", stats)
    return stats
