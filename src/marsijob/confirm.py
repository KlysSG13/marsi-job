"""Подтверждение распознанного профиля: карточка полей с кнопками правок.

Применяется к результатам голосового ввода, текстового резюме и LLM-интервью:
LLM только предлагает значения — каждое поле подтверждает пользователь
(принцип МАРСИ-DN1 Q&A 9: парсер не принимает решений за человека).
"""
from typing import Any, Dict, List, Optional, Tuple

from .questionnaire import parse_skills, validate_answer

# группы полей для подтверждения: (id, заголовок, ключ профиля)
FIELD_GROUPS: List[Tuple[str, str]] = [
    ("hard_skills", "Навыки"),
    ("experience_years", "Опыт (лет)"),
    ("domain_experience", "Отрасли"),
    ("education_level", "Образование"),
    ("languages", "Языки"),
    ("portfolio", "Портфолио"),
    ("management_experience", "Управление"),
    ("work_format", "Формат работы"),
    ("schedule", "График"),
    ("employment_type", "Оформление"),
    ("salary", "Зарплата (₽/мес)"),
    ("business_trips_tolerance", "Командировки"),
    ("relocation", "Релокация"),
    ("location", "Город / ЧП"),
    ("work_permit", "Право работы"),
    ("commute_max", "Дорога до офиса (мин)"),
    ("interests", "Интересы"),
    ("career_trajectory", "Траектория"),
]

_EDU_LABELS = {"secondary": "среднее", "vocational": "среднее спец.",
               "bachelor": "бакалавриат", "master": "магистратура", "phd": "PhD"}
_RELOC_LABELS = {"no": "не готов", "yes": "готов", "yes_with_package": "с пакетом"}
_LANG_LABELS = {0.0: "—", 0.3: "A2", 0.5: "B1", 0.7: "B2", 0.9: "C1", 1.0: "C2"}


def format_value(group: str, value: Any) -> str:
    """Человекочитаемое значение поля для карточки подтверждения."""
    if value is None:
        return "—"
    if group == "hard_skills":
        items = value if isinstance(value, dict) else {}
        if not items:
            return "—"
        return ", ".join(
            f"{name} {int(round(float((m or {}).get('level', 0)) * 100))}%"
            + ("✓" if float((m or {}).get("verified", 0)) >= 0.6 else "")
            for name, m in list(items.items())[:10]
        )
    if group == "experience_years":
        return f"{value} лет"
    if group == "salary":
        if isinstance(value, dict):
            return f"{int(value.get('min', 0)):,}–{int(value.get('max', 0)):,}".replace(",", " ")
        return str(value)
    if group == "languages":
        langs = value if isinstance(value, dict) else {}
        parts = []
        for lang, meta in langs.items():
            lvl = float((meta or {}).get("level", 0))
            label = _LANG_LABELS.get(round(lvl, 2), f"{int(round(lvl * 100))}%")
            parts.append(f"{lang}: {label}")
        return ", ".join(parts) or "—"
    if group == "location":
        if isinstance(value, dict):
            city = value.get("city") or "удалённо"
            return f"{city} (UTC+{value.get('tz', '?')})"
        return str(value)
    if group == "education_level":
        return _EDU_LABELS.get(value, str(value))
    if group == "relocation":
        return _RELOC_LABELS.get(value, str(value))
    if group == "management_experience":
        if isinstance(value, dict) and value.get("has"):
            return f"да, {value.get('team_size', '?')} чел."
        return "нет"
    if group == "business_trips_tolerance":
        return {0.0: "исключены", 0.2: "редко", 0.5: "иногда", 0.8: "часто"}.get(value, str(value))
    if group == "portfolio":
        return {0.0: "нет", 0.3: "небольшое", 0.6: "среднее", 0.8: "сильное", 1.0: "топовое"}.get(value, str(value))
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "—"
    return str(value)


def render_confirmation(parsed: Dict[str, Any]) -> str:
    """Карточка «Вот что я понял» по распознанному профилю."""
    lines = ["🤖 <b>Вот что я понял из вашего рассказа:</b>", ""]
    for group, title in FIELD_GROUPS:
        if group in parsed:
            lines.append(f"• <b>{title}:</b> {format_value(group, parsed[group])}")
    lines.append("")
    lines.append("Всё верно? Если нет — поправьте кнопкой ниже ✏️")
    return "\n".join(lines)


def apply_edit(parsed: Dict[str, Any], group: str, raw_text: str) -> Tuple[bool, str]:
    """Правка одного поля по тексту пользователя. Возвращает (ок, сообщение).

    Форматы: навыки — «python: 70!, sql: 50»; зарплата — «80000 130000»
    или «80к–130к»; город — «Казань 3»; языки — «английский b2»;
    остальное — подписи вариантов анкеты.
    """
    text = (raw_text or "").strip()
    low = text.lower()

    if group == "hard_skills":
        skills = parse_skills(text)
        if not skills:
            return False, "Формат навыков: python: 70!, sql: 50"
        parsed[group] = skills
        return True, "Навыки обновлены."
    if group == "salary":
        nums = [p for p in low.replace("–", " ").replace("-", " ").replace("к", "000").split() if p.replace("к", "").isdigit()]
        if len(nums) >= 2:
            parsed[group] = {"min": int(nums[0]), "max": int(nums[1]),
                             "compromise": (parsed.get(group) or {}).get("compromise", False)}
            return True, "Вилка обновлена."
        if len(nums) == 1:
            v = int(nums[0])
            parsed[group] = {"min": v, "max": int(v * 1.2),
                             "compromise": (parsed.get(group) or {}).get("compromise", False)}
            return True, f"Вилка: {v}–{int(v * 1.2)} (максимум +20%, правьте ещё раз, если нужно)."
        return False, "Формат: 80000 130000 (минимум и максимум)"
    if group == "location":
        parts = text.replace("utc", " ").replace("+", " ").split()
        city_parts = [p for p in parts if not p.isdigit()]
        tz = next((int(p) for p in parts if p.isdigit()), None)
        if not city_parts:
            return False, "Формат: Казань 3 (город и часовой пояс)"
        parsed[group] = {"city": " ".join(city_parts).title(), "tz": tz if tz is not None else 3}
        return True, "Локация обновлена."
    if group == "languages":
        level = 0.3
        for word, lvl in (("a2", 0.3), ("b1", 0.5), ("b2", 0.7), ("c1", 0.9), ("c2", 1.0), ("нет", 0.0)):
            if word in low:
                level = lvl
                break
        else:
            if not low:
                level = 0.0
        langs = dict(parsed.get(group) or {})
        langs["russian"] = langs.get("russian", {"level": 1.0, "verified": 1.0})
        if "англ" in low or level > 0:
            langs["english"] = {"level": level, "verified": 0.3}
        parsed[group] = langs
        return True, "Языки обновлены."
    if group in ("experience_years", "commute_max"):
        try:
            parsed[group] = float(low.replace(",", ".").replace("лет", "").replace("мин", "").strip())
            return True, "Обновлено."
        except ValueError:
            return False, "Введите число."
    if group == "portfolio":
        for lvl, val in (("нет", 0.0), ("небольш", 0.3), ("средн", 0.6), ("сильн", 0.8), ("топ", 1.0)):
            if lvl in low:
                parsed[group] = val
                return True, "Обновлено."
        return False, "Ответьте словами: нет / небольшое / среднее / сильное."
    if group == "relocation":
        for word, val in (("с пакетом", "yes_with_package"), ("готов", "yes"), ("не готов", "no"), ("нет", "no")):
            if word in low:
                parsed[group] = val
                return True, "Обновлено."
        return False, "Ответьте: не готов / готов / с пакетом."
    if group == "management_experience":
        has = any(w in low for w in ("да", "руководил", "был", "есть"))
        nums = [p for p in low.split() if p.isdigit()]
        parsed[group] = {"has": has, "team_size": int(nums[0]) if (has and nums) else 0}
        return True, "Обновлено."
    if group == "education_level":
        for word, val in (("среднее спец", "vocational"), ("бакалавр", "bachelor"),
                          ("магистр", "master"), ("phd", "phd"), ("среднее", "secondary")):
            if word in low:
                parsed[group] = val
                return True, "Обновлено."
        return False, "Например: бакалавриат / магистратура / среднее спец."

    # списочные и категорийные поля: пытаемся сопоставить подписи вариантов анкеты
    from .questionnaire import QUESTIONS
    key_map = {g: k for g, k in (
        ("domain_experience", "domain_experience"), ("work_format", "work_format"),
        ("schedule", "schedule"), ("employment_type", "employment_type"),
        ("work_permit", "work_permit"), ("interests", "interests"),
        ("career_trajectory", "career_trajectory"),
        ("business_trips_tolerance", "business_trips"),
    )}
    if group in key_map:
        q = next((q for q in QUESTIONS if q.key == key_map[group]), None)
        if q is None:
            return False, "Поле не поддерживается."
        ok, value, err = validate_answer(q, text, {})
        if not ok:
            return False, err
        if group == "business_trips_tolerance":
            value = value[0] if isinstance(value, list) else value
        if isinstance(value, list) and "none" in value:
            value = ["none"]
        parsed[group] = value
        return True, "Обновлено."
    return False, "Поле не поддерживается."


# Синхронизация подтверждённого профиля с снимком ответов анкеты (для /fill)
GROUP_TO_QKEY = {
    "hard_skills": "hard_skills", "experience_years": "experience_years",
    "domain_experience": "domain_experience", "education_level": "education_level",
    "portfolio": "portfolio", "work_format": "work_format", "schedule": "schedule",
    "employment_type": "employment_type", "relocation": "relocation",
    "work_permit": "work_permit", "commute_max": "commute_max",
    "interests": "interests", "career_trajectory": "career_trajectory",
    "business_trips_tolerance": "business_trips", "city": None, "tz": None,
    "salary_min": None, "salary_max": None, "english_level": None,
}


def sync_parsed_to_answers(parsed: Dict[str, Any], answers: Dict[str, Any]) -> Dict[str, Any]:
    """Переносит подтверждённые поля в снимок ответов, чтобы /fill не переспрашивал."""
    for group, qkey in GROUP_TO_QKEY.items():
        if group not in parsed or not qkey or answers.get(qkey) is not None:
            continue
        value = parsed[group]
        if group == "business_trips_tolerance" and isinstance(value, list):
            value = value[0]
        answers[qkey] = value
    salary = parsed.get("salary")
    if isinstance(salary, dict) and salary.get("min"):
        answers.setdefault("salary_min", salary["min"])
        answers.setdefault("salary_max", salary.get("max"))
    location = parsed.get("location")
    if isinstance(location, dict):
        if location.get("city"):
            answers.setdefault("city", location["city"])
        if location.get("tz") is not None:
            answers.setdefault("tz", location["tz"])
    english = (parsed.get("languages") or {}).get("english")
    if english and english.get("level"):
        answers.setdefault("english_level", english["level"])
    return answers
