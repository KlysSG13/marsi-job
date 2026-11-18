"""Дорожная карта «+X очков» — план на 3 месяца из зоны валентности.

Строится детерминированно: для каждой цели (вакансия / общий рост) движок
считает «обнуление какого фактора максимально поднимет скор» — карта
разворачивает это в помесячный план с действиями и прогнозом прироста.
"""
from typing import Any, Dict, List, Optional

from .engine import Engine
from .valence import valence_zone

# Библиотека действий по факторам (детерминированная)
FACTOR_ACTIONS = {
    "hard_skills": ("Подтвердить ключевые навыки тестом/пет-проектом и "
                    "проставить уровень честно", "онлайн-курс, GitHub, сертификация"),
    "experience_years": ("Наращивать релевантный опыт: open-source, "
                         "фриланс-проекты", "GitHub, freelance-платформы"),
    "domain_experience": ("Получить опыт в целевой отрасли через "
                          "пет-проект/стажировку", "open-source, хакатоны"),
    "education_level": ("Подтвердить/повысить формальный уровень образования",
                        "онлайн-курсы с сертификатом"),
    "languages": ("Поднять английский на один уровень (B1→B2)",
                  "регулярная практика, italki, тест EF SET"),
    "portfolio": ("Собрать 2–3 кейса с измеримым результатом",
                  "GitHub, Notion-портфолио, публикации"),
    "work_format": ("Добавить допустимые форматы (например, гибрид)",
                    "правка профиля в боте"),
    "schedule": ("Расширить допустимые графики", "правка профиля в боте"),
    "salary": ("Уточнить зарплатную вилку — сейчас часть вакансий отсекается",
               "анализ рынка, правка профиля"),
    "relocation": ("Рассмотреть готовность к релокации (если возможно)",
                   "анализ городов, правка профиля"),
    "location": ("Уточнить город и ЧП — влияет на окно удалённых вакансий",
                 "правка профиля"),
    "career_trajectory": ("Сфокусировать карьерную цель (deep/management/stability)",
                          "карьерный разбор, правка профиля"),
    "interests": ("Добавить теги интересов — синергия с продуктом компании",
                  "правка профиля"),
    "communication_style": ("Уточнить предпочитаемый режим коммуникации",
                            "правка профиля"),
    "sensory_sensitivity": ("Подобрать комфортную сенсорную среду",
                            "правка профиля, фильтрация вакансий"),
}


def build_roadmap(engine: Engine, profile: Dict[str, Any],
                  vacancies: List[Dict[str, Any]],
                  horizon_months: int = 3,
                  max_items: int = 4,
                  target_type: str = "overall",
                  vacancy: Optional[Dict[str, Any]] = None,
                  ) -> Dict[str, Any]:
    """Строит карту: {months: [{factor, action, source, delta}], forecast}"""
    if target_type == "vacancy" and vacancy:
        vz = valence_zone(engine, profile, vacancy, top_n=horizon_months * max_items)
        interventions = vz["interventions"]
        current_score = vz["score"]
    else:
        # общий рост: агрегируем по топ-N матчам
        scored = []
        for v in vacancies:
            from .engine import Engine  # noqa — уже импортирован
            if engine.prefilter_blocked(profile, v):
                continue
            res = engine.score(profile, v)
            if not res.blocked and res.band != "below":
                scored.append((res.score, v))
        scored.sort(key=lambda p: p[0], reverse=True)
        top = scored[:10]
        if top:
            current_score = sum(s for s, _ in top) / len(top)
            all_interventions = []
            for score, v in top:
                vz = valence_zone(engine, profile, v, top_n=3)
                all_interventions.extend(vz["interventions"])
            # суммируем приросты по факторам
            by_factor: Dict[str, float] = {}
            for iv in all_interventions:
                by_factor[iv["factor"]] = max(by_factor.get(iv["factor"], 0), iv["gain"])
            interventions = sorted(
                [{"factor": f, "gain": g, "action": FACTOR_ACTIONS.get(
                    f, ("", ""))[0], "match": 0} for f, g in by_factor.items()],
                key=lambda x: -x["gain"])[:horizon_months * max_items]
        else:
            interventions = []
            current_score = 0.0

    # раскладываем по месяцам (по max_items на месяц, в порядке убывания прироста)
    months: List[List[Dict[str, Any]]] = []
    idx = 0
    cumulative = current_score
    for m in range(horizon_months):
        items = []
        for _ in range(max_items):
            if idx >= len(interventions):
                break
            iv = interventions[idx]
            action_text, source = FACTOR_ACTIONS.get(
                iv["factor"], ("Улучшить фактор " + iv["factor"], "правка профиля"))
            items.append({
                "factor": iv["factor"],
                "action": action_text,
                "source": source,
                "delta": round(iv.get("gain", 0), 3),
            })
            cumulative += iv.get("gain", 0)
            idx += 1
        months.append(items)

    forecast = {
        "current": round(current_score, 3),
        "projected": round(min(1.0, cumulative), 3),
    }
    return {"months": months, "forecast": forecast,
            "target_type": target_type,
            "vacancy_id": vacancy.get("id") if vacancy else None}


def format_preview(roadmap: Dict[str, Any]) -> str:
    """Бесплатный предпросмотр: топ-1 пробел + Δ."""
    first = None
    for month in roadmap["months"]:
        if month:
            first = month[0]
            break
    if not first:
        return ("Дорожная карта: у вас нет значимых пробелов — профиль сбалансирован! "
                "Полный план всё равно поможет с приоритетами: /buy")
    return (f"🗺 <b>Дорожная карта: предпросмотр</b>\n\n"
            f"Топ-1 пробел: <b>{first['factor']}</b>\n"
            f"→ {first['action']}\n"
            f"Прогноз прироста: <b>+{first['delta'] * 100:.0f}%</b> к скору\n\n"
            f"Полная карта на {len(roadmap['months'])} мес: "
            f"{roadmap['forecast']['current'] * 100:.0f}% → "
            f"{roadmap['forecast']['projected'] * 100:.0f}% — "
            f"<b>349 ₽</b>, /buy")


def format_full(roadmap: Dict[str, Any]) -> str:
    """Полный отчёт дорожной карты."""
    fc = roadmap["forecast"]
    lines = [
        f"🗺 <b>Дорожная карта на {len(roadmap['months'])} месяца</b>",
        f"Прогноз скора: <b>{fc['current'] * 100:.0f}% → {fc['projected'] * 100:.0f}%</b>",
        "",
    ]
    for mi, items in enumerate(roadmap["months"], 1):
        if not items:
            continue
        lines.append(f"<b>Месяц {mi}:</b>")
        for item in items:
            lines.append(f"  • <b>{item['factor']}</b>: {item['action']}")
            lines.append(f"    Где: {item['source']} · +{item['delta'] * 100:.0f}%")
        lines.append("")
    lines.append("⚠ План опирается на текущие данные вакансий (ε) — "
                 "пересчёт через 30 дней бесплатен.")
    return "\n".join(lines)
