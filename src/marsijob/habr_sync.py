"""Синк вакансий с Хабр Карьеры через публичный RSS (легальная синдикация).

Фид: https://career.habr.com/vacancies/rss[?q=запрос] — 50 свежих вакансий,
в описании — компания, зарплатная вилка, график, навыки #тегами.
IT-сегмент: качество выше, объём меньше trudvsem.

Особенность: город в RSS не отдаётся — для офисных вакансий ставится
city_unknown (движок не оценивает/не блокирует локацию), удалёнка
детектится по фразе «Можно удалённо».
"""
import logging
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

import xml.etree.ElementTree as ET  # noqa: F401 (для тестов)

from .hh_sync import NEUTRAL_ENV, slug

log = logging.getLogger("marsijob.habr")

RSS_URL = "https://career.habr.com/vacancies/rss"
USER_AGENT = "MARSI-Job/1.0 (Telegram bot; https://t.me/MarsiJobBot)"
REQUEST_DELAY = 0.4

QUERIES = [
    "", "python", "frontend", "backend", "разработчик", "тестировщик",
    "дизайнер", "аналитик", "devops", "администратор",
]

SENIORITY = {"intern": 0, "стажёр": 0, "junior": 0, "джун": 0,
             "middle": 2, "мидл": 2, "senior": 5, "сеньор": 5,
             "lead": 6, "лид": 6}


class HabrError(Exception):
    pass


def fetch_rss(query: str = "", timeout: int = 30) -> str:
    url = RSS_URL + (f"?q={urllib.parse.quote(query)}" if query else "")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        raise HabrError(f"habr rss не получен: {e}") from e


def _salary_from(text: str) -> Optional[Dict[str, int]]:
    m = re.search(r"от\s*([\d\s\u00a0]+)(?:\s*до\s*([\d\s\u00a0]+))?\s*₽", text)
    if m:
        lo = int(m.group(1).replace(" ", "").replace("\u00a0", ""))
        hi = int(m.group(2).replace(" ", "").replace("\u00a0", "")) if m.group(2) else round(lo * 1.2)
        return {"min": lo, "max": hi, "compromise": True}
    m = re.search(r"до\s*([\d\s\u00a0]+)\s*₽", text)
    if m:
        hi = int(m.group(1).replace(" ", "").replace("\u00a0", ""))
        return {"min": round(hi * 0.8), "max": hi, "compromise": True}
    return None


def _schedule_from(text: str, remote: bool) -> str:
    low = text.lower()
    if "частичн" in low:
        return "part_time"
    if "гибк" in low:
        return "flexible"
    if "сменн" in low:
        return "shift"
    if "полный" in low:
        return "full_time"
    return "flexible" if remote else "full_time"


def map_habr_item(item: ET.Element) -> Dict[str, Any]:
    title_raw = (item.findtext("title") or "").strip()
    description = (item.findtext("description") or "").strip()
    guid = (item.findtext("guid") or "").strip() or title_raw[:16]
    link = (item.findtext("link") or "").strip()
    company = (item.findtext("author") or "").strip()

    # «Требуется «Разработчик 1С» (от 160 000 до 180 000 ₽)» → роль
    m = re.search(r"[«\"](.+?)[»\"]", title_raw)
    role = m.group(1).strip() if m else title_raw

    low_desc = description.lower()
    remote = ("удалённ" in low_desc) or ("удаленн" in low_desc)

    # навыки: #теги из описания; сеньорити-теги → опыт
    tags = re.findall(r"#([\wа-яА-ЯёЁ./+-]+)", description)
    skills: Dict[str, Dict[str, Any]] = {}
    experience = 0
    for tag in tags[:14]:
        key = slug(tag)
        if not key:
            continue
        seniority = next((v for k, v in SENIORITY.items()
                          if k in tag.lower()), None)
        if seniority is not None:
            experience = max(experience, seniority)
            continue  # сеньорити — не навык
        skills[key] = {"req_level": 0.6, "must": False}

    vacancy: Dict[str, Any] = {
        "id": f"HB-{re.sub(r'[^0-9]', '', guid) or abs(hash(guid)) % 10**8}",
        "title": role,
        "hard_skills": skills,
        "experience_years_min": experience,
        "domain": "it",
        "education_level_min": None,
        "portfolio_required": 0.3,
        "work_format": "remote" if remote else "office",
        "schedule": _schedule_from(description, remote),
        "employment_type": "tk",
        "business_trips_required": 0.2,
        "relocation_supported": False,
        "location": {"city": None, "tz": 3},
        "city_unknown": not remote,   # город в RSS не отдаётся
        "work_permit_required": ["RU"],
        "commute_minutes": None,
        "team_size": NEUTRAL_ENV["team_size"],
        "urgency_months": 2,
        "start_date_required_months": 1,
        "vacancy_clarity": 0.7,
        "trust_index": 0.6,
        "source": "habr",
        "external_url": link,
        "employer_name": company,
    }
    vacancy.update(NEUTRAL_ENV)
    vacancy["team_size"] = NEUTRAL_ENV["team_size"]
    salary = _salary_from(title_raw + " " + description)
    if salary:
        vacancy["salary"] = salary
    return vacancy


def sync_habr(storage: Any, queries: Optional[List[str]] = None) -> Dict[str, int]:
    """Прогон синка Хабр Карьеры. Возвращает {fetched, inserted, updated}."""
    stats = {"fetched": 0, "inserted": 0, "updated": 0}
    employer_user_id = storage.ensure_external_employer("Хабр Карьера")
    seen: set = set()
    for i, query in enumerate(queries if queries is not None else QUERIES):
        try:
            xml_text = fetch_rss(query)
            time.sleep(REQUEST_DELAY)
            root = ET.fromstring(xml_text)
        except (HabrError, ET.ParseError) as e:
            log.warning("habr feed %r failed: %s", query, e)
            continue
        for item in root.iter("item"):
            try:
                mapped = map_habr_item(item)
            except Exception as e:
                log.warning("habr map failed: %s", e)
                continue
            if mapped["id"] in seen:
                continue
            seen.add(mapped["id"])
            stats["fetched"] += 1
            action = storage.upsert_external_vacancy(mapped, employer_user_id)
            stats[action] = stats.get(action, 0) + 1
    log.info("habr sync: %s", stats)
    return stats
