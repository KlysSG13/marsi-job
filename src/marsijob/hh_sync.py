"""Автосинк вакансий с hh.ru (официальный открытый API, без токена).

Пайплайн: список по фидам (удалёнка + крупные города) → карточка деталей
(/vacancies/{id}, лимит ~3 запроса/с) → маппинг в схему движка МАРСИ →
upsert в БД (дедуп по коду HH-<id>) → чистка: активные старше 14 дней
закрываются, закрытые старше 60 дней удаляются.

Требования hh.ru соблюдены: User-Agent с названием приложения,
публичный read-only эндпоинт, в карточке бота — пометка «источник: hh.ru».
"""
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("marsijob.hh")

HH_BASE = "https://api.hh.ru"
USER_AGENT = "MARSI-Job/1.0 (Telegram bot; https://t.me/MarsiJobBot)"

# Фиды: удалёнка по России + крупнейшие города (id областей hh)
FEEDS: List[Dict[str, Any]] = [
    {"label": "remote-ru", "params": {"area": 113, "schedule": "remote"}},
    {"label": "msk", "params": {"area": 1}},
    {"label": "spb", "params": {"area": 2}},
    {"label": "ekb", "params": {"area": 3}},
    {"label": "nsk", "params": {"area": 4}},
    {"label": "kzn", "params": {"area": 88}},
]

MAX_PAGES_PER_FEED = 1        # 1 страница × 20 вакансий × 6 фидов ≈ 120/прогон
DETAIL_DELAY_SEC = 0.35       # бережём лимиты hh (~3 запроса/с)
STALE_DAYS = 14               # активные старше — закрываем
DELETE_AFTER_DAYS = 60        # закрытые старше — удаляем

EXPERIENCE_MAP = {
    "noExperience": 0, "between1And3": 1, "between3And6": 3, "moreThan6": 6,
}
SCHEDULE_MAP = {"fullDay": "full_time", "flexible": "flexible", "shift2": "shift"}
EMPLOYMENT_MAP = {"full": "tk", "project": "gph", "part": "gph", "probation": "tk"}

CITY_TZ = {
    "москва": 3, "санкт-петербург": 3, "новосибирск": 7, "екатеринбург": 5,
    "казань": 3, "нижний новгород": 3, "челябинск": 5, "самара": 4, "омск": 6,
    "ростов-на-дону": 3, "уфа": 5, "красноярск": 7, "пермь": 5, "воронеж": 3,
    "волгоград": 3, "краснодар": 3, "сочи": 3, "тюмень": 5, "минск": 3,
    "алматы": 6, "ташкент": 5, "тбилиси": 4, "ереван": 4, "владивосток": 10,
}

# нейтральные значения среды для внешних вакансий (данные неизвестны)
NEUTRAL_ENV = {
    "sensory_load": 0.5, "stress_level": 0.5, "multitask_intensity": 0.5,
    "micromanagement_degree": 0.5, "role_ambiguity": 0.5, "learning_intensity": 0.5,
    "stage_risk": 0.5, "mission_clarity": 0.4, "team_size": 8,
    "communication_mode": "mixed", "culture": "casual", "trajectory_offered": "broad",
    "motivation_offered": {"money": 0.6, "growth": 0.6, "stability": 0.5,
                           "mission": 0.4, "recognition": 0.4},
    "training_offered": False, "vacancy_criticality": 0.5,
}


class HhError(Exception):
    pass


def _get(url: str, timeout: int = 30, token: Optional[str] = None) -> Dict[str, Any]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise HhError(f"hh.ru запрос не удался ({url.split('?')[0]}): {e}") from e


def get_app_token(client_id: str, client_secret: str) -> str:
    """Токен приложения (client_credentials) — теперь нужен и для поиска вакансий
    (hh.ru закрыл анонимный доступ). Регистрация: https://dev.hh.ru"""
    import urllib.parse as _up
    data = _up.urlencode({"grant_type": "client_credentials",
                          "client_id": client_id,
                          "client_secret": client_secret}).encode()
    req = urllib.request.Request("https://hh.ru/oauth/token", data=data, method="POST",
                                  headers={"User-Agent": USER_AGENT,
                                           "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise HhError(f"не удалось получить токен hh: {e}") from e
    if "access_token" not in payload:
        raise HhError(f"hh не выдал токен: {payload}")
    return payload["access_token"]


def slug(name: str) -> str:
    s = re.sub(r"\s+", "_", name.strip().lower())[:40]
    return s.strip("._-/")


def map_salary(salary: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Зарплата hh → наша вилка (только RUR; без валюты/USD — пропускаем)."""
    if not salary or salary.get("currency") != "RUR":
        return None
    lo = salary.get("from")
    hi = salary.get("to")
    if lo is None and hi is None:
        return None
    lo = float(lo) if lo else round(float(hi) * 0.8)
    hi = float(hi) if hi else round(float(lo) * 1.2)
    return {"min": int(lo), "max": int(hi), "compromise": True}


def map_city(area: Optional[Dict[str, Any]]) -> Tuple[Optional[str], int]:
    name = (area or {}).get("name", "")
    low = name.strip().lower()
    tz = next((v for k, v in CITY_TZ.items() if k in low), 3)
    return (name or None), tz


def map_hh_vacancy(detail: Dict[str, Any]) -> Dict[str, Any]:
    """Карточка hh /vacancies/{id} → схема движка МАРСИ-Job."""
    schedule_id = ((detail.get("schedule") or {}).get("id")) or "fullDay"
    is_remote = schedule_id == "remote"
    city, tz = map_city(detail.get("area"))

    vacancy: Dict[str, Any] = {
        "id": f"HH-{detail['id']}",
        "title": detail.get("name", "Вакансия"),
        "hard_skills": {
            slug(s["name"]): {"req_level": 0.6, "must": False}
            for s in (detail.get("key_skills") or [])[:12]
        },
        "experience_years_min": EXPERIENCE_MAP.get(
            (detail.get("experience") or {}).get("id", ""), 0),
        "domain": "it" if detail.get("professional_roles") else "services",
        "education_level_min": None,
        "portfolio_required": 0.3,
        "work_format": "remote" if is_remote else "office",
        "schedule": "flexible" if is_remote else SCHEDULE_MAP.get(schedule_id, "full_time"),
        "employment_type": EMPLOYMENT_MAP.get(
            (detail.get("employment") or {}).get("id", ""), "tk"),
        "business_trips_required": 0.2,
        "relocation_supported": False,
        "location": {"city": None if is_remote else city, "tz": tz},
        "work_permit_required": ["RU"],
        "commute_minutes": None,
        "team_size": NEUTRAL_ENV["team_size"],
        "urgency_months": 2,
        "start_date_required_months": 1,
        "vacancy_clarity": 0.6,
        "trust_index": 0.55,          # внешний источник, не проверен работодателем
        "source": "hh",
        "external_url": detail.get("alternate_url"),
        "employer_name": (detail.get("employer") or {}).get("name"),
    }
    vacancy.update(NEUTRAL_ENV)
    vacancy["team_size"] = NEUTRAL_ENV["team_size"]
    salary = map_salary(detail.get("salary"))
    if salary:
        vacancy["salary"] = salary
    if (detail.get("employment") or {}).get("id") == "part":
        vacancy["schedule"] = "part_time"
    return vacancy


# ---------------------------------------------------------------------------
# Синхронизация
# ---------------------------------------------------------------------------


def fetch_feed(params: Dict[str, Any], page: int = 0,
               token: Optional[str] = None) -> List[Dict[str, Any]]:
    query = dict(params)
    query.update({"per_page": 20, "page": page, "order_by": "publication_time"})
    url = f"{HH_BASE}/vacancies?{urllib.parse.urlencode(query)}"
    data = _get(url, token=token)
    return data.get("items") or []


def fetch_detail(vacancy_id: str, token: Optional[str] = None) -> Dict[str, Any]:
    return _get(f"{HH_BASE}/vacancies/{vacancy_id}", token=token)


def sync_once(storage: Any, feeds: Optional[List[Dict[str, Any]]] = None,
              max_pages: int = MAX_PAGES_PER_FEED,
              fetch_details: bool = True,
              token: Optional[str] = None) -> Dict[str, int]:
    """Один прогон синка hh (требуется токен приложения)."""
    if not token:
        raise HhError("hh.ru требует авторизацию приложения: задайте HH_CLIENT_ID/HH_CLIENT_SECRET "
                      "(регистрация на https://dev.hh.ru)")
    stats = {"fetched": 0, "inserted": 0, "updated": 0, "closed": 0}
    employer_user_id = storage.ensure_external_employer("hh.ru")
    seen_codes: List[str] = []
    for feed in (feeds or FEEDS):
        for page in range(max_pages):
            try:
                items = fetch_feed(feed["params"], page, token=token)
            except HhError as e:
                log.warning("feed %s failed: %s", feed["label"], e)
                continue
            if not items:
                break
            for item in items:
                hh_id = str(item.get("id", ""))
                if not hh_id:
                    continue
                stats["fetched"] += 1
                seen_codes.append(f"HH-{hh_id}")
                try:
                    detail = fetch_detail(hh_id, token=token) if fetch_details else item
                    time.sleep(DETAIL_DELAY_SEC)
                except HhError as e:
                    log.warning("detail %s failed: %s", hh_id, e)
                    detail = item  # используем данные из списка
                mapped = map_hh_vacancy(detail if "key_skills" in detail else item)
                action = storage.upsert_external_vacancy(mapped, employer_user_id)
                stats[action] = stats.get(action, 0) + 1
    stats["closed"] = storage.prune_external_vacancies(
        stale_days=STALE_DAYS, delete_after_days=DELETE_AFTER_DAYS)
    log.info("hh sync: %s", stats)
    return stats
