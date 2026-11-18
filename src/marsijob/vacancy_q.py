"""Анкета вакансии для работодателя — зеркало факторной модели кандидата.

Собирает карточку вакансии в схеме движка (data/test_cases/*.json, сторона
вакансии). Повторяет Question/validate_answer из questionnaire.py.
"""
from typing import Any, Dict, List, Optional, Tuple

from .questionnaire import Question, validate_answer

VAC_QUESTIONS: List[Question] = [
    Question("title", "Название вакансии", "text", profile_field="title"),
    Question(
        "hard_skills", "Требуемые навыки: «название: уровень 0–100» через запятую.\n"
        "Критичные (must-have) отмечайте «!»: <code>python: 70!, docker: 60!</code>",
        "skills", profile_field="hard_skills",
        hint="Уровень 0–100. Без «!» навык — nice-to-have."),
    Question("experience_years_min", "Минимальный опыт (лет)?", "number",
             profile_field="experience_years_min"),
    Question(
        "domain", "Отрасль вашего продукта?",
        "choice_single", profile_field="domain",
        options=[("it", "IT"), ("fintech", "Финтех"), ("ecommerce", "E-commerce"),
                 ("gamedev", "Геймдев"), ("marketing", "Маркетинг"),
                 ("education", "Образование"), ("analytics", "Аналитика"),
                 ("healthcare", "Медицина"), ("industry", "Промышленность"),
                 ("logistics", "Логистика"), ("services", "Услуги"), ("bank", "Банк")]),
    Question(
        "education_level_min", "Требуется ли образование?",
        "choice_single", profile_field="education_level_min",
        options=[(None, "Не важно"), ("vocational", "Среднее специальное"),
                 ("bachelor", "Бакалавриат"), ("master", "Магистратура+")]),
    Question(
        "english_req", "Требуемый английский?",
        "choice_single", profile_field="languages.english.req",
        options=[(None, "Не требуется"), (0.5, "B1 — средний"),
                 (0.7, "B2 — уверенный"), (0.9, "C1 — продвинутый")]),
    Question(
        "portfolio_required", "Нужно ли портфолио?",
        "choice_single", profile_field="portfolio_required",
        options=[(0.0, "Нет"), (0.3, "Желательно"), (0.6, "Да, важно"), (0.8, "Обязательно, сильное")]),
    Question(
        "work_format", "Формат работы в вакансии?",
        "choice_single", profile_field="work_format",
        options=[("remote", "Удалённо"), ("hybrid", "Гибрид"), ("office", "Офис")]),
    Question(
        "schedule", "График?",
        "choice_single", profile_field="schedule",
        options=[("full_time", "Полный день"), ("flexible", "Гибкий"),
                 ("part_time", "Частичная занятость"), ("shift", "Сменный"), ("night", "Ночной")]),
    Question(
        "employment_type", "Тип оформления?",
        "choice_single", profile_field="employment_type",
        options=[("tk", "ТК РФ"), ("gph", "ГПХ"), ("self_employed", "Самозанятость"),
                 ("contractor", "ИП/Contractor")]),
    Question("salary_min", "Вилка зарплаты: минимум (₽/мес)?", "number",
             profile_field="salary.min"),
    Question("salary_max", "Вилка зарплаты: максимум (₽/мес)?", "number",
             profile_field="salary.max"),
    Question(
        "business_trips_required", "Сколько командировок предполагается?",
        "choice_single", profile_field="business_trips_required",
        options=[(0.0, "Нет"), (0.2, "Редко"), (0.5, "Иногда"), (0.8, "Часто")]),
    Question(
        "relocation_supported", "Предоставляете ли релокационный пакет?",
        "choice_single", profile_field="relocation_supported",
        options=[(False, "Нет"), (True, "Да")]),
    Question("city", "Город офиса (или «удалённо»)?", "text", profile_field="location.city",
             hint="Для удалённой работы напишите «удалённо»"),
    Question(
        "tz", "Часовой пояс команды (UTC)?", "choice_single", profile_field="location.tz",
        options=[(3, "UTC+3 (Москва)"), (5, "UTC+5"), (7, "UTC+7"), (10, "UTC+10"),
                 (2, "UTC+2"), (0, "UTC"), (1, "UTC+1"), (4, "UTC+4"), (6, "UTC+6"),
                 (8, "UTC+8"), (9, "UTC+9"), (11, "UTC+11"), (12, "UTC+12")]),
    Question(
        "work_permit_required", "Где сотрудник должен иметь право работать?",
        "choice_multi", profile_field="work_permit_required",
        options=[("RU", "Россия"), ("EU", "ЕС/Европа"), ("US", "США"),
                 ("UK", "Великобритания"), ("Asia", "Азия"), ("CIS", "СНГ")]),
    Question(
        "vacancy_criticality", "Насколько критичен простой этой ставки?",
        "choice_single", profile_field="vacancy_criticality",
        options=[(0.3, "Низкая"), (0.5, "Средняя"), (0.8, "Высокая: роль простаивает")]),
    Question(
        "sensory_load", "Сенсорная нагрузка среды (шум, open-space, созвоны)?",
        "choice_single", profile_field="sensory_load",
        options=[(0.1, "Тихо: удалёнка/кабинеты"), (0.3, "Скорее тихо"),
                 (0.5, "Средне: опенспейс местами"), (0.7, "Шумный опенспейс"),
                 (0.9, "Постоянный шум/созвоны")]),
    Question(
        "stress_level", "Уровень стресса роли?",
        "choice_single", profile_field="stress_level",
        options=[(0.2, "Низкий"), (0.4, "Умеренный"), (0.6, "Высокий: дедлайны"),
                 (0.8, "Очень высокий: горящие проекты")]),
    Question(
        "multitask_intensity", "Интенсивность переключений между задачами?",
        "choice_single", profile_field="multitask_intensity",
        options=[(0.2, "Одна задача"), (0.4, "Мало переключений"),
                 (0.6, "Средне"), (0.8, "Много параллельных")]),
    Question(
        "micromanagement_degree", "Сколько контроля за исполнением?",
        "choice_single", profile_field="micromanagement_degree",
        options=[(0.2, "Результат важен, процесс — нет"), (0.4, "Умеренный контроль"),
                 (0.6, "Регулярные чек-поинты"), (0.8, "Тесное сопровождение")]),
    Question(
        "role_ambiguity", "Насколько роль и задачи определены?",
        "choice_single", profile_field="role_ambiguity",
        options=[(0.2, "Всё описано"), (0.4, "В основном ясно"),
                 (0.6, "Есть неопределённость"), (0.8, "Роль строится на ходу")]),
    Question(
        "learning_intensity", "Сколько нового предстоит изучать?",
        "choice_single", profile_field="learning_intensity",
        options=[(0.2, "Мало: знакомый стек"), (0.4, "Средне"), (0.6, "Много нового"),
                 (0.9, "Постоянное обучение")]),
    Question(
        "stage_risk", "Стадия и риск компании?",
        "choice_single", profile_field="stage_risk",
        options=[(0.2, "Зрелая компания"), (0.4, "Растущая"),
                 (0.6, "Масштабирующийся стартап"), (0.8, "Ранняя стадия")]),
    Question(
        "team_size", "Размер команды (человек)?", "number", profile_field="team_size"),
    Question(
        "communication_mode", "Как в основном общаетесь?",
        "choice_single", profile_field="communication_mode",
        options=[("async", "Асинхронно: текст, таски"), ("sync", "Синхронно: созвоны"),
                 ("mixed", "Смешанно")]),
    Question(
        "culture", "Культура компании?",
        "choice_single", profile_field="culture",
        options=[("formal", "Формальная"), ("casual", "Неформальная")]),
    Question(
        "trajectory_offered", "Какая траектория роста на роли?",
        "choice_single", profile_field="trajectory_offered",
        options=[("deep", "Глубокая специализация"), ("broad", "Широкий профиль"),
                 ("management", "Менеджмент"), ("stability", "Стабильная роль")]),
    Question(
        "motivation_1", "Что вы даёте сотруднику в первую очередь?",
        "choice_single", profile_field="_mot1",
        options=[("money", "Деньги"), ("growth", "Развитие"), ("stability", "Стабильность"),
                 ("mission", "Миссия"), ("recognition", "Признание")]),
    Question(
        "motivation_2", "А во вторую?",
        "choice_single", profile_field="_mot2",
        options=[("money", "Деньги"), ("growth", "Развитие"), ("stability", "Стабильность"),
                 ("mission", "Миссия"), ("recognition", "Признание")]),
    Question(
        "training_offered", "Оплачиваете ли обучение?",
        "choice_single", profile_field="training_offered",
        options=[(False, "Нет"), (True, "Да")]),
    Question(
        "product_tags", "Теги продукта (для совпадения с интересами кандидатов)?",
        "choice_multi", profile_field="product_tags",
        options=[("data", "Данные"), ("games", "Игры"), ("automation", "Автоматизация"),
                 ("texts", "Тексты"), ("marketing", "Маркетинг"), ("education", "Образование"),
                 ("fintech", "Финансы"), ("distributed_systems", "Распределённые системы"),
                 ("research", "Исследования"), ("design", "Дизайн"), ("bank", "Банки"),
                 ("ecommerce", "E-commerce"), ("logistics", "Логистика"), ("services", "Услуги")]),
    Question("urgency_months", "За сколько месяцев нужно закрыть вакансию?", "number",
             profile_field="urgency_months"),
    Question("start_date_required_months", "Через сколько месяцев нужен выход?", "number",
             profile_field="start_date_required_months"),
]

# значения по умолчанию для незаполненных атрибутов вакансии
VAC_DEFAULTS: Dict[str, Any] = {
    "education_level_min": None,
    "vacancy_clarity": 0.75,
    "mission_clarity": 0.5,
    "commute_minutes": None,
    "trust_index": 0.7,
}


def parse_vacancy_skills(raw: str) -> Dict[str, Dict[str, Any]]:
    """«python: 70!, docker: 60» → {python: {req_level: 0.7, must: true}, ...}."""
    result: Dict[str, Dict[str, Any]] = {}
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        must = False
        if chunk.endswith("!"):
            must = True
            chunk = chunk[:-1].strip()
        if ":" in chunk:
            name, _, lvl = chunk.partition(":")
        else:
            name, lvl = chunk, ""
        name = name.strip().lower()
        try:
            level = max(0.0, min(1.0, float(lvl.strip()) / 100.0))
        except ValueError:
            level = 0.5
        if name:
            result[name] = {"req_level": round(level, 3), "must": must}
    return result


def vac_next_question(answers: Dict[str, Any]) -> Optional[Question]:
    for q in VAC_QUESTIONS:
        if q.key in answers:
            continue
        return q
    return None


def vac_progress(answers: Dict[str, Any]) -> Tuple[int, int]:
    return len(answers), len(VAC_QUESTIONS)


def build_vacancy(answers: Dict[str, Any], vacancy_id: str) -> Dict[str, Any]:
    """Собирает карточку вакансии в схеме движка."""
    vacancy: Dict[str, Any] = dict(VAC_DEFAULTS)
    vacancy["id"] = vacancy_id
    for q in VAC_QUESTIONS:
        if q.key not in answers:
            continue
        value = answers[q.key]
        if q.profile_field == "hard_skills":
            vacancy["hard_skills"] = value
        elif q.profile_field == "languages.english.req":
            if value is not None:
                vacancy.setdefault("languages", {"russian": {"req_level": 0.9, "must": True}})
                vacancy["languages"]["english"] = {"req_level": float(value), "must": True}
        elif q.profile_field in ("_mot1", "_mot2"):
            continue
        elif q.profile_field == "location.city":
            city = str(value).strip()
            if city.lower() in ("удалённо", "удаленно", "remote", "-"):
                vacancy["location"] = {"city": None, "tz": answers.get("tz", 3)}
            else:
                vacancy["location"] = {"city": city, "tz": answers.get("tz", 3)}
        elif q.profile_field == "location.tz":
            continue  # вложено в location выше
        else:
            vacancy[q.profile_field] = value
    # motivation_offered: топ-1 → 0.9, топ-2 → 0.7, остальные 0.4
    m1, m2 = answers.get("motivation_1"), answers.get("motivation_2")
    keys = ["money", "growth", "stability", "mission", "recognition"]
    vacancy["motivation_offered"] = {
        k: (0.9 if k == m1 else 0.7 if k == m2 and m2 != m1 else 0.4) for k in keys
    }
    return vacancy
