"""Зона валентности МАРСИ-Job: поиск первопричины слабого матча.

По документу (p. 9–10): первопричина — узел, чьё «обнуление» (интервенция)
даёт максимальное снижение общего ущерба. Здесь: для каждого фактора с неполным
матчем пересчитывается скор при полном закрытии разрыва; фактор с максимальным
выигрышем — первопричина. При близких выигрышах (< 10%) приоритет у фактора
с меньшей ценой сближения r (детерминированный тай-брейк, p. 12 документа).
"""
from typing import Any, Dict, List, Optional, Sequence

from .engine import Engine

# Подсказки «что делать» по факторам (для /why).
ACTION_HINTS = {
    "hard_skills": "Подтвердите ключевые навыки: тест, пет-проект, ссылка на код — и проставьте честный уровень",
    "experience_years": "Добавьте релевантный опыт: проекты, фриланс, open-source считаются",
    "domain_experience": "Покажите переносимый опыт из смежной отрасли в описании кейсов",
    "education_level": "Укажите образование и курсы: для части вакансий это фильтр",
    "languages": "Подтвердите уровень языка сертификатом или укажите реальный уровень",
    "portfolio": "Соберите портфолио: 2–3 кейса с измеримым результатом",
    "management_experience": "Отразите управление людьми/проектами, даже неформальное",
    "work_format": "Расширьте допустимые форматы (например, добавьте гибрид) — так выйдет больше вакансий",
    "schedule": "Разрешите компромисс по графику или добавьте допустимые варианты",
    "employment_type": "Добавьте допустимые типы оформления (ТК, ГПХ, самозанятость)",
    "salary": "Скорректируйте вилку или разрешите торг: сейчас часть вакансий отсекается",
    "business_trips": "Уточните толерантность к командировкам",
    "relocation": "Если возможен переезд — отметьте готовность (с пакетом или без)",
    "location": "Укажите город и часовой пояс точно; для удалёнки это окно пересечения",
    "work_permit": "Отметьте все юрисдикции, где имеете право работать",
    "commute_max": "Пересмотрите допустимое время дороги до офиса",
    "stress_tolerance": "Ищите роли с меньшей нагрузкой: конфликт стресса — частая причина текучести",
    "multitask_preference": "Фильтруйте монозадачные роли — на мультизадачных вы теряете в матче",
    "autonomy_preference": "Изучите стиль управления команды до отклика",
    "team_size_preference": "Уточните комфортный размер команды",
    "communication_style": "Выбирайте компании с вашим режимом коммуникации (sync/async)",
    "motivation": "Взвесьте мотивацию честно: матч по деньгам/росту/стабильности зависит от этого",
    "risk_appetite": "Стадия компании должна соответствовать вашему аппетиту к риску",
    "sensory_sensitivity": "Ищите тихие среды/удалёнку — сенсорная перегрузка снижает скор сильнее всего",
    "structure_need": "Роли с высокой неопределённостью вам противопоказаны — фильтруйте их",
    "learning_appetite": "Роли с обучением усиливают ваш матч — отмечайте их приоритетом",
    "interests": "Добавьте теги интересов: пересечение с продуктом даёт бонус культуры",
    "culture_fit": "Формальные/неформальные компании различаются сильно — укажите предпочтение",
    "mission_importance": "Если миссия важна — ищите компании с ясной миссией",
    "career_trajectory": "Траектория роли должна совпадать с вашей целью (deep/management/стабильность)",
    "urgency": "Срочность влияет на спектр вакансий: учитывайте срок выхода",
    "start_date": "Уточните срок выхода: часть вакансий закрывается быстрее",
}


def valence_zone(
    engine: Engine,
    candidate: Dict[str, Any],
    vacancy: Dict[str, Any],
    top_n: int = 3,
) -> Dict[str, Any]:
    """Разбор одной пары: топ-N факторов-причин с выигрышем от интервенции."""
    base = engine.score(candidate, vacancy)
    interventions: List[Dict[str, Any]] = []
    for f in base.factors:
        if f.match is None or f.match >= 0.999 or f.w <= 0:
            continue
        improved = engine.score(candidate, vacancy, match_overrides={f.factor_id: 1.0})
        gain = improved.score - base.score
        interventions.append({
            "factor": f.factor_id,
            "name": f.name_ru,
            "gain": round(gain, 4),
            "match": round(f.match, 4),
            "r": round(f.r, 4),
            "action": ACTION_HINTS.get(f.factor_id, "Сблизьте значение фактора с рынком"),
        })
    interventions.sort(key=lambda x: (-x["gain"], x["r"]))
    # тай-брейк по документу: при разнице выигрышей < 10% первичен меньший r
    return {
        "vacancy_id": base.vacancy_id,
        "score": round(base.score, 4),
        "band": base.band,
        "blocked": base.blocked,
        "interventions": interventions[:top_n],
    }


def root_causes(
    engine: Engine,
    candidate: Dict[str, Any],
    vacancies: Sequence[Dict[str, Any]],
    top_n: int = 3,
) -> List[Dict[str, Any]]:
    """Зона валентности по потоку вакансий: усреднённый ущерб фактора.

    Отвечает на вопрос «почему меня в целом редко зовут»: фактор с максимальным
    средним ущербом (1−match)·W по всем незаблокированным вакансиям — первопричина.
    """
    losses: Dict[str, Dict[str, Any]] = {}
    count = 0
    for vac in vacancies:
        res = engine.score(candidate, vac)
        if res.blocked:
            continue
        count += 1
        for f in res.factors:
            if f.match is None:
                continue
            entry = losses.setdefault(f.factor_id, {"name": f.name_ru, "loss": 0.0})
            entry["loss"] += (1.0 - f.match) * f.w
    if count == 0:
        return []
    items = [(v["loss"] / count, k, v["name"]) for k, v in losses.items()]
    items.sort(key=lambda t: (-t[0],))
    return [
        {
            "factor": fid,
            "name": name,
            "avg_loss": round(loss, 4),
            "action": ACTION_HINTS.get(fid, "Сблизьте значение фактора с рынком"),
        }
        for loss, fid, name in items[:top_n]
    ]
