"""Парсер резюме МАРСИ-Job (этап 3).

По документу (Q&A 9) LLM используется исключительно как парсер: неструктурированный
текст → строгий JSON-профиль в схеме движка. Каждый ключ валидируется и
подтверждается пользователем в боте; ядро остаётся детерминированным.

Два провайдера:
  · api — любой OpenAI-совместимый /chat/completions (ключ в MARSIJOB_LLM_KEY);
  · heuristic — офлайн-эвристика по ключевым словам (fallback без ключа,
    гарантированно без «галлюцинаций»: извлекает только то, что буквально написано).
"""
import json
import re
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# Поля профиля, которые разрешено ставить парсеру (белый список схемы)
PARSABLE_FIELDS = {
    "hard_skills", "experience_years", "domain_experience", "education_level",
    "languages", "portfolio", "management_experience", "work_format",
    "schedule", "employment_type", "salary", "business_trips_tolerance",
    "relocation", "location", "work_permit", "commute_max", "interests",
    "career_trajectory",
}

SYSTEM_PROMPT = (
    "Ты — парсер резюме. Верни СТРОГО JSON без пояснений по схеме:\n"
    "{\n"
    '  "hard_skills": {"<навык>": {"level": 0.0-1.0, "verified": 0.0-1.0}},\n'
    '  "experience_years": float,\n'
    '  "domain_experience": ["it","fintech","ecommerce","gamedev","marketing","education",'
    '"analytics","healthcare","industry","logistics","services","bank"],\n'
    '  "education_level": "secondary|vocational|bachelor|master|phd",\n'
    '  "languages": {"english": {"level": 0.0-1.0, "verified": 0.3}},\n'
    '  "portfolio": 0.0-1.0,\n'
    '  "management_experience": {"has": bool, "team_size": int},\n'
    '  "work_format": ["remote","hybrid","office"],\n'
    '  "schedule": ["full_time","flexible","part_time","shift","night"],\n'
    '  "employment_type": ["tk","gph","self_employed","contractor"],\n'
    '  "salary": {"min": int, "max": int, "compromise": bool},\n'
    '  "business_trips_tolerance": 0.0-1.0,\n'
    '  "relocation": "no|yes_with_package|yes",\n'
    '  "location": {"city": str, "tz": int},\n'
    '  "work_permit": ["RU","EU","US","UK","Asia","CIS"],\n'
    '  "commute_max": int,\n'
    '  "interests": [str],\n'
    '  "career_trajectory": "deep|broad|management|stability"\n'
    "}\n"
    "Правила: уровень навыка из текста (junior≈0.4, middle≈0.6, senior≈0.8+); "
    "указывай только то, что есть в тексте; отсутствующее — опускай. "
    "Никаких домыслов."
)


class ParserError(Exception):
    pass


# ---------------------------------------------------------------------------
# Эвристический офлайн-парсер (fallback)
# ---------------------------------------------------------------------------

KNOWN_SKILLS = [
    "python", "sql", "javascript", "typescript", "react", "node.js", "java",
    "c#", "c++", "go", "php", "rust", "kotlin", "swift", "figma", "photoshop",
    "docker", "kubernetes", "git", "linux", "postgres", "mysql", "mongodb",
    "redis", "html", "css", "qa", "selenium", "api_testing", "manual_testing",
    "tableau", "power_bi", "excel", "1c", "bitrix", "wordpress", "content_writing",
    "smm", "seo", "docs", "analytics", "teaching", "b2b_sales", "autocad",
]
CITIES = [
    "москва", "санкт-петербург", "спб", "новосибирск", "екатеринбург",
    "казань", "нижний новгород", "челябинск", "самара", "омск", "ростов-на-дону",
    "уфа", "красноярск", "пермь", "воронеж", "волгоград", "краснодар", "сочи",
    "тюмень", "минск", "алматы", "ташкент", "тбилиси", "ереван", "варшава",
    "белград", "алма-ата",
]
SKILL_LEVEL_WORDS = [
    (0.9, ["эксперт", "expert", "продвинутый", "senior", "сеньор", "deep expert"]),
    (0.7, ["уверенный", "уверенно", "advanced", "strong"]),
    (0.5, ["средний", "middle", "мидл", "хороший"]),
    (0.35, ["базовый", "basic", "junior", "джун", "начинающий", "начальный"]),
]


def _level_from_occurrences(low: str, skill: str, window: int = 60) -> float:
    """Лучший (максимальный) уровень по всем упоминаниям навыка."""
    best = 0.0
    start = 0
    seen = 0
    while seen < 3:
        idx = low.find(skill, start)
        if idx < 0:
            break
        context = low[max(0, idx - window): idx + window]
        for level, words in SKILL_LEVEL_WORDS:
            if any(w in context for w in words):
                best = max(best, level)
                break
        start = idx + len(skill)
        seen += 1
    return best if best > 0 else 0.55  # упомянут без уровня — нейтральный


def parse_resume_heuristic(text: str) -> Dict[str, Any]:
    """Извлекает из текста только буквально написанное. Без домыслов."""
    low = text.lower()
    profile: Dict[str, Any] = {}

    skills: Dict[str, Dict[str, float]] = {}
    for skill in KNOWN_SKILLS:
        idx = low.find(skill)
        if idx >= 0:
            context = low[max(0, idx - 40): idx + 60]
            verified = 0.7 if any(w in context for w in ("тест", "сертификат", "проект", "git", "портфолио")) else 0.3
            skills[skill] = {"level": _level_from_occurrences(low, skill), "verified": verified}
    if skills:
        profile["hard_skills"] = skills

    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:лет|года|год)\b[^.\n]{0,20}(?:опыт|стаж|работ)|опыт[^.\n]{0,20}?(\d+(?:[.,]\d+)?)\s*(?:лет|года|год)", low)
    if m:
        years = float((m.group(1) or m.group(2)).replace(",", "."))
        profile["experience_years"] = years

    for city in CITIES:
        if city in low:
            tz = {"москва": 3, "санкт-петербург": 3, "спб": 3}.get(city, None)
            profile["location"] = {"city": city.capitalize(), **({"tz": tz} if tz else {})}
            break

    if re.search(r"удалён|удален|remote", low):
        profile.setdefault("work_format", []).append("remote")
    if "гибрид" in low:
        profile.setdefault("work_format", []).append("hybrid")
    if re.search(r"\bофис", low):
        profile.setdefault("work_format", []).append("office")

    if re.search(r"частичн|part[- ]?time|подработк", low):
        profile.setdefault("schedule", []).append("part_time")
    if re.search(r"гибкий|flexible", low):
        profile.setdefault("schedule", []).append("flexible")

    m = re.search(r"(?:от|не менее)\s*(\d+)\s*(?:тыс|к\b|000)", low)
    if m:
        profile["salary"] = {"min": int(m.group(1)) * 1000 if int(m.group(1)) < 1000 else int(m.group(1))}
    m2 = re.search(r"до\s*(\d+)\s*(?:тыс|к\b|000)", low)
    if m2 and "salary" in profile:
        mx = int(m2.group(1)) * 1000 if int(m2.group(1)) < 1000 else int(m2.group(1))
        profile["salary"]["max"] = mx

    for word, tag in (("финтех", "fintech"), ("финанс", "fintech"), ("игр", "gamedev"),
                      ("образован", "education"),
                      ("маркетинг", "marketing"), ("медицин", "healthcare"),
                      ("логистик", "logistics"), ("банке|банк[аи]\\b", "bank"),
                      ("аналитик", "analytics")):
        if re.search(word, low):
            profile.setdefault("domain_experience", []).append(tag)

    for word, level in (("c1", 0.9), ("c2", 1.0), ("b2", 0.7), ("b1", 0.5), ("a2", 0.3)):
        if re.search(rf"английский[^.\n]{{0,15}}{word}", low):
            profile["languages"] = {"english": {"level": level, "verified": 0.3}}
            break

    if re.search(r"руководил|руководств|тимлид|менеджер команды|веду команду", low):
        m = re.search(r"(\d+)\s*(?:человек|людей|сотрудник)", low)
        profile["management_experience"] = {"has": True, "team_size": int(m.group(1)) if m else 3}

    return profile


# ---------------------------------------------------------------------------
# API-парсер (OpenAI-совместимый)
# ---------------------------------------------------------------------------


def parse_resume_api(
    text: str,
    api_key: str,
    base_url: str = "https://api.openai.com/v1",
    model: str = "gpt-4o-mini",
    timeout: int = 90,
) -> Dict[str, Any]:
    """Запрос к /chat/completions; ответ валидируется схемой профиля."""
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text[:12000]},
        ],
        "temperature": 0.0,
        "max_tokens": 4000,
        "response_format": {"type": "json_object"},
    }
    # Zhipu GLM (bigmodel.cn): отключаем reasoning, чтобы ответ был чистым JSON
    if "bigmodel" in base_url:
        payload["thinking"] = {"type": "disabled"}
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # сеть/HTTP/JSON — любая ошибка уходит в fallback
        raise ParserError(f"LLM-запрос не удался: {e}") from e
    try:
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise ParserError(f"Неожиданный формат ответа LLM: {e}") from e
    ok, errors = validate_parsed(parsed)
    if not ok:
        raise ParserError("Схема не прошла валидацию: " + "; ".join(errors))
    return parsed


# ---------------------------------------------------------------------------
# Валидация и слияние
# ---------------------------------------------------------------------------

ALLOWED_DOMAINS = {"it", "fintech", "ecommerce", "gamedev", "marketing", "education",
                   "analytics", "healthcare", "industry", "logistics", "services", "bank"}
ALLOWED_FORMATS = {"remote", "hybrid", "office"}
ALLOWED_SCHEDULES = {"full_time", "flexible", "part_time", "shift", "night"}
ALLOWED_EDU = {"secondary", "vocational", "bachelor", "master", "phd"}
ALLOWED_RELOC = {"no", "yes_with_package", "yes"}
ALLOWED_PERMIT = {"RU", "EU", "US", "UK", "Asia", "CIS"}
ALLOWED_TRAJECTORY = {"deep", "broad", "management", "stability"}


def _num01(x: Any) -> bool:
    return isinstance(x, (int, float)) and 0.0 <= float(x) <= 1.0


def _coerce_money(v: Any) -> Optional[float]:
    """'150000', '150к', '150 000 ₽' → float."""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace(" ", "").replace("\u00a0", "").replace("₽", "").replace("р.", "")
        s = s.replace("руб", "").replace(",", ".")
        mult = 1.0
        if s.endswith("к"):
            mult = 1000.0
            s = s[:-1]
        try:
            return float(s) * mult
        except ValueError:
            return None
    return None


def validate_parsed(parsed: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Строгая проверка: только известные поля и допустимые значения.

    Зарплата: достаточно одного из min/max — второе достраивается в merge;
    строки-числа приводятся к числам на месте.
    """
    errors: List[str] = []
    if not isinstance(parsed, dict):
        return False, ["корень не объект"]
    for key in parsed:
        if key not in PARSABLE_FIELDS:
            errors.append(f"лишнее поле {key}")
    if "hard_skills" in parsed:
        skills = parsed["hard_skills"]
        if not isinstance(skills, dict):
            errors.append("hard_skills не словарь")
        else:
            for name, meta in skills.items():
                if not isinstance(meta, dict) or not _num01(meta.get("level")) or not _num01(meta.get("verified")):
                    errors.append(f"навык {name}: level/verified должны быть 0..1")
    if "experience_years" in parsed and not isinstance(parsed["experience_years"], (int, float)):
        errors.append("experience_years не число")
    if "domain_experience" in parsed and not set(parsed["domain_experience"]) <= ALLOWED_DOMAINS:
        errors.append("domain_experience вне словаря")
    if "education_level" in parsed and parsed["education_level"] not in ALLOWED_EDU:
        errors.append("education_level вне словаря")
    if "work_format" in parsed and not set(parsed["work_format"]) <= ALLOWED_FORMATS:
        errors.append("work_format вне словаря")
    if "schedule" in parsed and not set(parsed["schedule"]) <= ALLOWED_SCHEDULES:
        errors.append("schedule вне словаря")
    if "employment_type" in parsed:
        if not set(parsed["employment_type"]) <= {"tk", "gph", "self_employed", "contractor"}:
            errors.append("employment_type вне словаря")
    if "relocation" in parsed and parsed["relocation"] not in ALLOWED_RELOC:
        errors.append("relocation вне словаря")
    if "work_permit" in parsed and not set(parsed["work_permit"]) <= ALLOWED_PERMIT:
        errors.append("work_permit вне словаря")
    if "career_trajectory" in parsed and parsed["career_trajectory"] not in ALLOWED_TRAJECTORY:
        errors.append("career_trajectory вне словаря")
    if "salary" in parsed:
        sal = parsed["salary"]
        if not isinstance(sal, dict):
            errors.append("salary не объект")
        else:
            lo = _coerce_money(sal.get("min"))
            hi = _coerce_money(sal.get("max"))
            if lo is None and hi is None:
                errors.append("salary без числовых min/max")
            else:
                if lo is None:
                    lo = round(hi * 0.8)
                if hi is None or hi < lo:
                    hi = round(lo * 1.2)
                parsed["salary"] = {"min": int(lo), "max": int(hi),
                                    "compromise": bool(sal.get("compromise", False))}
    return (not errors), errors


def merge_profile(parsed: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    """Накладывает распознанные поля на текущий профиль (парсер не трогает остальное)."""
    merged = json.loads(json.dumps(current))  # глубокая копия
    ok, _errors = validate_parsed(parsed)
    if not ok:
        return merged
    for key, value in parsed.items():
        if key == "languages" and isinstance(value, dict):
            merged.setdefault("languages", {"russian": {"level": 1.0, "verified": 1.0}})
            merged["languages"].update(value)
        else:
            merged[key] = value
    return merged


def parse_resume(text: str, api_key: Optional[str] = None,
                 base_url: str = "https://api.openai.com/v1",
                 model: str = "gpt-4o-mini") -> Dict[str, Any]:
    """Единая точка: LLM при наличии ключа, иначе детерминированная эвристика."""
    if api_key:
        return parse_resume_api(text, api_key, base_url, model)
    return parse_resume_heuristic(text)
