"""Быстрый старт (волшебник) и сила профиля МАРСИ-Job.

Философия прогрессивного профилирования: 8 быстрых вопросов → /match уже
работает; остальное дозаполняется опционально (/fill, голос, интервью).
"""
from typing import Any, Dict, List, Optional, Tuple

from .questionnaire import QUESTIONS, Question

# Порядок быстрого старта (ключи вопросов анкеты)
QUICK_KEYS = [
    "hard_skills",        # навыки
    "experience_years",   # опыт
    "work_format",        # формат (мульти)
    "schedule",           # график (мульти)
    "salary_min",         # зарплата от
    "salary_max",         # зарплата до
    "city",               # город
    "tz",                 # часовой пояс
]

# Веса для «силы профиля»: критичные поля 60%, расширенные 25%, блок среды 15%
CRITICAL_KEYS = {
    "hard_skills": 8, "experience_years": 8, "work_format": 8, "schedule": 8,
    "salary_min": 5, "salary_max": 5, "city": 5, "tz": 4, "work_permit": 9,
}
EXTENDED_KEYS = {
    "employment_type": 3, "relocation": 3, "commute_max": 2, "education_level": 2,
    "english_level": 3, "portfolio": 2, "domain_experience": 2, "interests": 2,
    "culture_fit": 2, "mission_importance": 2, "career_trajectory": 2,
    "urgency_months": 1, "start_date_months": 1, "salary_compromise": 1,
    "schedule_compromise": 1, "business_trips": 1, "management": 1,
}
SENSITIVE_KEYS = {
    "stress_tolerance": 2, "multitask_preference": 2, "autonomy_preference": 2,
    "team_size": 2, "communication_style": 2, "motivation_1": 3, "risk_appetite": 2,
    "learning_appetite": 2, "sensory_sensitivity": 2, "structure_need": 2,
    "neuro_profile": 1, "family_constraints": 1,
}


def q_by_key(key: str) -> Question:
    return next(q for q in QUESTIONS if q.key == key)


def quick_questions() -> List[Question]:
    return [q_by_key(k) for k in QUICK_KEYS]


# Понятные названия незаполненных полей для подсказок
_MISSING_LABELS = {
    "hard_skills": "навыки", "experience_years": "опыт", "work_format": "формат работы",
    "schedule": "график", "salary_min": "зарплатная вилка", "salary_max": "зарплатная вилка",
    "city": "город", "tz": "часовой пояс", "work_permit": "право работы",
}


def profile_strength(profile: Dict[str, Any]) -> Tuple[int, List[str]]:
    """Сила профиля 0..100 и список незаполненных критичных полей (для подсказок)."""
    def filled(key: str) -> bool:
        field = q_by_key(key).profile_field
        node: Any = profile
        for part in field.split("."):
            if not isinstance(node, dict):
                return False
            node = node.get(part)
        if node is None:
            return False
        if isinstance(node, (list, dict, str)) and len(node) == 0:
            return False
        if isinstance(node, list) and node == ["none"]:
            return True  # явное «нет» — это тоже данные
        return True

    total = sum(CRITICAL_KEYS.values()) + sum(EXTENDED_KEYS.values()) + sum(SENSITIVE_KEYS.values())
    got = 0
    missing_critical: List[str] = []
    for key, w in CRITICAL_KEYS.items():
        if filled(key):
            got += w
        else:
            _MISSING_LABELS.setdefault(key, q_by_key(key).key)
            missing_critical.append(_MISSING_LABELS[key])
    got += sum(w for key, w in EXTENDED_KEYS.items() if filled(key))
    got += sum(w for key, w in SENSITIVE_KEYS.items() if filled(key))
    percent = round(100 * got / max(1, total))
    return percent, missing_critical


def next_quick(answers: Dict[str, Any]) -> Optional[Question]:
    for key in QUICK_KEYS:
        if key not in answers:
            return q_by_key(key)
    return None


def reset_answers_for_fill(answers: Dict[str, Any]) -> Dict[str, Any]:
    """Готовит снимок ответов к /fill: пропущенные вопросы (None) задаются
    снова, отказ от блока среды тоже можно пересмотреть."""
    answers = {k: v for k, v in answers.items() if v is not None}
    if answers.get("sensitive_consent") is False:
        answers.pop("sensitive_consent")
    return answers


def strength_report(profile: Dict[str, Any]) -> str:
    percent, missing = profile_strength(profile)
    bar_filled = percent // 10
    bar = "▰" * bar_filled + "▱" * (10 - bar_filled)
    lines = [f"💪 <b>Сила профиля: {percent}%</b>\n<code>{bar}</code>"]
    if percent >= 85:
        lines.append("Профиль отличный — матчинг работает на полную.")
    elif percent >= 55:
        lines.append("Уже хорошо: вакансии подбираются, но можно точнее.")
    else:
        lines.append("База для подбора есть, но стоит дозаполнить профиль.")
    if missing:
        lines.append("Не хватает: " + ", ".join(missing[:4]))
    lines.append("\n➕ Дозаполнить: /fill · 🎤 Голосом: /voice · 💬 Интервью: /talk")
    return "\n".join(lines)
