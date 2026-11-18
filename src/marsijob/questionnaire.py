"""Детерминированная анкета кандидата МАРСИ-Job (путь без LLM, Q&A 9 документа).

Модуль чистой логики: определяет вопросы, валидирует ответы и собирает профиль
в схеме data/test_cases/*.json (профиль кандидата). Telegram-слой только
показывает вопрос и передаёт ответ сюда.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Определение вопросов
# ---------------------------------------------------------------------------


@dataclass
class Question:
    key: str                    # ключ состояния анкеты
    text: str                   # текст вопроса
    kind: str                   # choice_single | choice_multi | number | number_range | skills | text
    options: List[Tuple[Any, str]] = field(default_factory=list)  # (значение, подпись)
    profile_field: str = ""     # куда пишем в профиле
    sensitive: bool = False     # блок E/F/G (только по согласию)
    hint: str = ""
    skip_if: Optional[str] = None  # условие пропуска (см. SKIP_RULES)


LEVELS = [
    (0.0, "Нет"), (0.3, "Базовый"), (0.5, "Средний"),
    (0.7, "Уверенный"), (0.9, "Продвинутый"), (1.0, "Максимальный"),
]

QUESTIONS: List[Question] = [
    Question(
        "hard_skills", "Перечислите ключевые навыки в формате «название: уровень 0–100» "
        "через запятую.\nНапример: <code>python: 70, sql: 50, figma: 80</code>",
        "skills", profile_field="hard_skills",
        hint="Уровень 0–100 переводится в шкалу 0–1. Можно добавить «!» для подтверждённых "
             "тестом/проектом навыков: <code>python: 70!</code>"),
    Question(
        "experience_years", "Сколько лет релевантного опыта у вас есть?",
        "number", profile_field="experience_years", hint="Можно дробное, например 2.5"),
    Question(
        "domain_experience", "В каких отраслях работали? (можно несколько)",
        "choice_multi", profile_field="domain_experience",
        options=[("it", "IT"), ("fintech", "Финтех"), ("ecommerce", "E-commerce"),
                 ("gamedev", "Геймдев"), ("marketing", "Маркетинг"),
                 ("education", "Образование"), ("analytics", "Аналитика"),
                 ("healthcare", "Медицина"), ("industry", "Промышленность"),
                 ("logistics", "Логистика"), ("services", "Услуги"), ("bank", "Банки")]),
    Question(
        "education_level", "Ваше образование?",
        "choice_single", profile_field="education_level",
        options=[("secondary", "Среднее"), ("vocational", "Среднее специальное"),
                 ("bachelor", "Бакалавриат"), ("master", "Магистратура"), ("phd", "Аспирантура/PhD")]),
    Question(
        "english_level", "Уровень английского?",
        "choice_single", profile_field="languages.english",
        options=[(0.0, "Нет"), (0.3, "A2 — базовый"), (0.5, "B1 — средний"),
                 (0.7, "B2 — уверенный"), (0.9, "C1 — продвинутый"), (1.0, "C2/Native")]),
    Question(
        "portfolio", "Есть ли публичное портфолио (GitHub, кейсы, публикации)?",
        "choice_single", profile_field="portfolio",
        options=[(0.0, "Нет"), (0.3, "Небольшое"), (0.6, "Среднее, 3+ работ"),
                 (0.8, "Сильное, известные работы"), (1.0, "Публично известное")]),
    Question(
        "management", "Управляли ли людьми или проектами?",
        "choice_single", profile_field="management_experience.has",
        options=[(False, "Нет"), (True, "Да")]),
    Question(
        "management_team", "Сколько человек было в команде?",
        "number", profile_field="management_experience.team_size",
        skip_if="no_management"),
    Question(
        "work_format", "Какие форматы работы вам подходят? (можно несколько)",
        "choice_multi", profile_field="work_format",
        options=[("remote", "Удалённо"), ("hybrid", "Гибрид"), ("office", "Офис")]),
    Question(
        "schedule", "Какие графики вам подходят? (можно несколько)",
        "choice_multi", profile_field="schedule",
        options=[("full_time", "Полный день"), ("flexible", "Гибкий"),
                 ("part_time", "Частичная занятость"), ("shift", "Сменный"), ("night", "Ночной")]),
    Question(
        "schedule_compromise", "Готовы рассмотреть другие графики, если вакансия сильно подходит?",
        "choice_single", profile_field="schedule_compromise",
        options=[(False, "Нет"), (True, "Да, как компромисс")]),
    Question(
        "employment_type", "Какое оформление для вас приемлемо? (можно несколько)",
        "choice_multi", profile_field="employment_type",
        options=[("tk", "ТК РФ"), ("gph", "ГПХ"), ("self_employed", "Самозанятость"),
                 ("contractor", "ИП/Contractor (междунар.)")]),
    Question(
        "salary_min", "Минимальная желаемая зарплата (₽/мес, до вычета)?",
        "number", profile_field="salary.min"),
    Question(
        "salary_max", "Зарплата, при которой вы точно согласны (₽/мес)?",
        "number", profile_field="salary.max"),
    Question(
        "salary_compromise", "Готовы обсуждать зарплату ниже вилки, если вакансия очень подходит?",
        "choice_single", profile_field="salary.compromise",
        options=[(False, "Нет"), (True, "Да")]),
    Question(
        "business_trips", "Как относитесь к командировкам?",
        "choice_single", profile_field="business_trips_tolerance",
        options=[(0.0, "Исключены"), (0.2, "Редко, 1–2 раза в год"),
                 (0.5, "Иногда, раз в квартал"), (0.8, "Не проблема, часто")]),
    Question(
        "relocation", "Готовы ли к переезду?",
        "choice_single", profile_field="relocation",
        options=[("no", "Нет"), ("yes_with_package", "Да, с релокационным пакетом"),
                 ("yes", "Да, даже без пакета")]),
    Question(
        "city", "Ваш город?", "text", profile_field="location.city"),
    Question(
        "tz", "Ваш часовой пояс (UTC)?", "choice_single", profile_field="location.tz",
        options=[(2, "UTC+2 (Калининград)"), (3, "UTC+3 (Москва)"),
                 (5, "UTC+5 (Екатеринбург)"), (7, "UTC+7 (Новосибирск)"),
                 (10, "UTC+10 (Владивосток)"), (0, "Другой/UTC"), (1, "UTC+1"), (4, "UTC+4"),
                 (6, "UTC+6"), (8, "UTC+8"), (9, "UTC+9"), (11, "UTC+11"), (12, "UTC+12")]),
    Question(
        "work_permit", "Где у вас есть право работать? (можно несколько)",
        "choice_multi", profile_field="work_permit",
        options=[("RU", "Россия"), ("EU", "ЕС/Европа"), ("US", "США"),
                 ("UK", "Великобритания"), ("Asia", "Азия"), ("CIS", "СНГ")]),
    Question(
        "commute_max", "Максимальное время дороги до офиса (минут в одну сторону)?",
        "number", profile_field="commute_max", skip_if="no_office"),
    # ---- Блок среды (чувствительные, по согласию) -------------------------
    Question(
        "sensitive_consent",
        "Дальше — блок о рабочей среде (стресс, сенсорика, нейропрофиль). Он опционален, "
        "но повышает точность подбора комфортной среды.\nДанные видны только алгоритму; "
        "работодателю — лишь агрегированный индекс совместимости, без деталей.",
        "choice_single", profile_field="_sensitive_consent",
        options=[(False, "Пропустить блок"), (True, "Заполнить")], sensitive=True),
    Question(
        "stress_tolerance", "Насколько спокойно вы переносите стресс и горящие дедлайны?",
        "choice_single", profile_field="stress_tolerance", sensitive=True,
        options=[(0.2, "Очень тяжело"), (0.4, "Скорее тяжело"), (0.6, "Нормально"),
                 (0.8, "Хорошо, энергизирует"), (1.0, "Спокойно переношу любой")]),
    Question(
        "multitask_preference", "Вам комфортнее переключаться между задачами или копать одну?",
        "choice_single", profile_field="multitask_preference", sensitive=True,
        options=[(0.2, "Только одна задача"), (0.4, "Скорее одна"), (0.6, "Несколько — ок"),
                 (0.8, "Люблю переключаться"), (1.0, "Скучно без многозадачности")]),
    Question(
        "autonomy_preference", "Сколько самостоятельности вам нужно в работе?",
        "choice_single", profile_field="autonomy_preference", sensitive=True,
        options=[(0.2, "Желательны чёткие инструкции"), (0.4, "Умеренная"),
                 (0.6, "Сам решаю, как делать"), (0.8, "Высокая, свои правила"),
                 (1.0, "Полная автономия")]),
    Question(
        "team_size", "Команда какого размера вам комфортна?",
        "choice_single", profile_field="team_size_preference", sensitive=True,
        options=[([1, 3], "1–3 человека"), ([3, 8], "3–8"), ([8, 20], "8–20"),
                 ([20, 100], "20+"), ([1, 100], "Любая")]),
    Question(
        "communication_style", "Как вам удобнее общаться?",
        "choice_single", profile_field="communication_style", sensitive=True,
        options=[("sync", "Созвоны и звонки"), ("async", "Текст, мессенджеры"),
                 ("mixed", "Комбинированный")]),
    Question(
        "motivation_1", "Что для вас важнее всего в работе? (главное)",
        "choice_single", profile_field="_motivation_1", sensitive=True,
        options=[("money", "Деньги"), ("growth", "Развитие и рост"),
                 ("stability", "Стабильность"), ("mission", "Миссия и смысл"),
                 ("recognition", "Признание")]),
    Question(
        "motivation_2", "А на втором месте?",
        "choice_single", profile_field="_motivation_2", sensitive=True,
        options=[("money", "Деньги"), ("growth", "Развитие и рост"),
                 ("stability", "Стабильность"), ("mission", "Миссия и смысл"),
                 ("recognition", "Признание")]),
    Question(
        "risk_appetite", "Насколько вам комфортен риск (стартап vs корпорация)?",
        "choice_single", profile_field="risk_appetite", sensitive=True,
        options=[(0.2, "Только стабильные"), (0.4, "Скорее стабильность"),
                 (0.6, "Средне"), (0.8, "Люблю стартапы"), (1.0, "Чем рискованнее, тем интереснее")]),
    Question(
        "learning_appetite", "Сколько нового вы хотите изучать на работе?",
        "choice_single", profile_field="learning_appetite", sensitive=True,
        options=[(0.2, "Минимум, устойчивый стек"), (0.4, "Иногда новое"),
                 (0.6, "Регулярно учусь"), (0.8, "Хочу много учиться"),
                 (1.0, "Постоянный поток нового")]),
    Question(
        "sensory_sensitivity", "Чувствительны ли вы к шуму, свету, open-space?",
        "choice_single", profile_field="sensory_sensitivity", sensitive=True,
        options=[(0.1, "Нет, шум не мешает"), (0.3, "Слегка"), (0.5, "Средне"),
                 (0.7, "Сильно"), (0.9, "Критично, нужна тишина")]),
    Question(
        "structure_need", "Нужны ли вам чёткие процессы и постановка задач?",
        "choice_single", profile_field="structure_need", sensitive=True,
        options=[(0.2, "Нет, сам структурирую"), (0.4, "Скорее нет"), (0.6, "Средне"),
                 (0.8, "Нужны рамки"), (1.0, "Обязательны чёткие инструкции")]),
    Question(
        "neuro_profile", "Есть ли особенности, которые важно учитывать в среде? "
        "(необязательно, влияет только на подбор условий)",
        "choice_multi", profile_field="neuro_profile", sensitive=True,
        options=[("none", "Нет"), ("adhd", "СДВГ"), ("autism", "Аутистический спектр"),
                 ("dyslexia", "Дислексия")]),
    Question(
        "family_constraints", "Есть ли семейные обстоятельства, влияющие на график?",
        "choice_multi", profile_field="family_constraints", sensitive=True,
        options=[("none", "Нет"), ("small_children", "Маленькие дети"),
                 ("dependents", "Иждивенцы/уход")]),
    # ---- Ценности и цели ----------------------------------------------------
    Question(
        "interests", "Что вам интересно? (влияет на совпадение с продуктом компании)",
        "choice_multi", profile_field="interests",
        options=[("data", "Данные"), ("games", "Игры"), ("automation", "Автоматизация"),
                 ("texts", "Тексты"), ("marketing", "Маркетинг"), ("education", "Образование"),
                 ("fintech", "Финансы"), ("distributed_systems", "Распределённые системы"),
                 ("research", "Исследования"), ("patterns", "Паттерны и структуры"),
                 ("family", "Семейные ценности"), ("design", "Дизайн")]),
    Question(
        "culture_fit", "Какая культура компании вам ближе?",
        "choice_single", profile_field="culture_fit",
        options=[("formal", "Формальная: процессы, иерархия"),
                 ("casual", "Неформальная: флет, лёгкое общение")]),
    Question(
        "mission_importance", "Насколько важна миссия продукта?",
        "choice_single", profile_field="mission_importance",
        options=[(0.2, "Не важна"), (0.5, "Приятно, но не решает"),
                 (0.8, "Важно"), (1.0, "Критично важно")]),
    Question(
        "career_trajectory", "Куда вы движетесь профессионально?",
        "choice_single", profile_field="career_trajectory",
        options=[("deep", "Глубокая специализация"), ("broad", "Широкий профиль"),
                 ("management", "Менеджмент"), ("stability", "Стабильность и комфорт")]),
    Question(
        "urgency_months", "Как быстро вам нужна работа? (месяцев на поиск)",
        "number", profile_field="urgency_months"),
    Question(
        "start_date_months", "Через сколько месяцев вы готовы выйти? (0 — сразу)",
        "number", profile_field="start_date_months"),
]

# ---------------------------------------------------------------------------
# Пропуски вопросов
# ---------------------------------------------------------------------------


def skip_reason(q: Question, answers: Dict[str, Any]) -> Optional[str]:
    if q.skip_if == "no_management":
        if not answers.get("management"):
            return "no_management"
    if q.skip_if == "no_office":
        formats = answers.get("work_format") or []
        if not formats or all(f not in formats for f in ("office", "hybrid")):
            return "no_office"
    if q.sensitive and q.key != "sensitive_consent":
        if not answers.get("sensitive_consent"):
            return "no_consent"
    return None


def question_flow(answers: Dict[str, Any]) -> List[Question]:
    """Полный список вопросов с учётом уже данных ответов (для навигации)."""
    flow = []
    for q in QUESTIONS:
        if q.key in answers:
            continue
        if skip_reason(q, answers) is not None:
            continue
        flow.append(q)
    return flow


def next_question(answers: Dict[str, Any]) -> Optional[Question]:
    flow = question_flow(answers)
    return flow[0] if flow else None


def progress(answers: Dict[str, Any]) -> Tuple[int, int]:
    flow_all = question_flow({})
    remaining = len(question_flow(answers))
    return len(flow_all) - remaining, len(flow_all)


# ---------------------------------------------------------------------------
# Валидация и сборка профиля
# ---------------------------------------------------------------------------


def _set_nested(profile: Dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = profile
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value


def apply_answer(profile: Dict[str, Any], q: Question, value: Any) -> Dict[str, Any]:
    """Применяет ОДИН ответ анкеты к профилю на месте (без пересборки).

    Используется волшебником быстрого старта и /fill: профиль — единый
    источник истины, ничего не теряется при дозаполнении.
    """
    if value is None:  # «пропустить» — не трогаем профиль
        return profile
    if q.profile_field == "languages.english":
        if float(value) > 0:
            profile.setdefault("languages", {"russian": {"level": 1.0, "verified": 1.0}})
            profile["languages"]["english"] = {"level": float(value), "verified": 0.3}
    elif q.profile_field == "_motivation_1":
        m2 = profile.get("_mot2_ans")
        keys = ["money", "growth", "stability", "mission", "recognition"]
        profile["motivation"] = {
            k: (1.0 if k == value else 0.7 if k == m2 and m2 != value else 0.3) for k in keys
        }
        profile["_mot1_ans"] = value
    elif q.profile_field == "_motivation_2":
        m1 = profile.get("_mot1_ans")
        keys = ["money", "growth", "stability", "mission", "recognition"]
        profile["motivation"] = {
            k: (1.0 if k == m1 else 0.7 if k == value and value != m1 else 0.3) for k in keys
        }
        profile["_mot2_ans"] = value
    elif q.profile_field == "_sensitive_consent":
        profile["_sensitive_consent"] = bool(value)
    else:
        _set_nested(profile, q.profile_field, value)
    return profile


def parse_skills(raw: str) -> Dict[str, Dict[str, float]]:
    """«python: 70!, sql: 50» → {python: {level: 0.7, verified: 0.7}}.

    «!» — подтверждённый навык (verified 0.7), иначе 0.3 (самооценка).
    """
    result: Dict[str, Dict[str, float]] = {}
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        verified = 0.3
        if chunk.endswith("!"):
            verified = 0.7
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
            result[name] = {"level": round(level, 3), "verified": verified}
    return result


def validate_answer(q: Question, raw: str, answers: Dict[str, Any]) -> Tuple[bool, Any, str]:
    """Возвращает (ок, значение, сообщение об ошибке)."""
    text = (raw or "").strip()
    if q.kind == "choice_single":
        for value, _label in q.options:
            if _label == text:
                return True, value, ""
        return False, None, "Пожалуйста, выберите вариант из списка."
    if q.kind == "choice_multi":
        chosen: List[Any] = []
        parts = [p.strip() for p in text.split(",") if p.strip()]
        labels = {label: value for value, label in q.options}
        for p in parts:
            if p not in labels:
                return False, None, f"«{p}» нет в списке. Выберите через запятую из предложенных."
            chosen.append(labels[p])
        if not chosen:
            return False, None, "Выберите хотя бы один вариант."
        if "none" in chosen and len(chosen) > 1:
            chosen = ["none"]
        return True, chosen, ""
    if q.kind == "number":
        try:
            num = float(text.replace(",", ".").replace(" ", ""))
        except ValueError:
            return False, None, "Введите число."
        if num < 0:
            return False, None, "Число должно быть неотрицательным."
        return True, num, ""
    if q.kind == "skills":
        skills = parse_skills(text)
        if not skills:
            return False, None, "Не удалось разобрать навыки. Формат: python: 70, sql: 50"
        return True, skills, ""
    if q.kind == "text":
        if len(text) < 2:
            return False, None, "Введите текст (минимум 2 символа)."
        return True, text, ""
    return False, None, "Неизвестный тип вопроса."


def build_profile(answers: Dict[str, Any]) -> Dict[str, Any]:
    """Собирает профиль кандидата в схеме движка из ответов анкеты."""
    profile: Dict[str, Any] = {
        "languages": {"russian": {"level": 1.0, "verified": 1.0}},
        "management_experience": {"has": False, "team_size": 0},
        "location": {},
        "salary": {},
        "neuro_profile": ["none"],
        "family_constraints": ["none"],
    }
    for q in QUESTIONS:
        if q.key not in answers:
            continue
        value = answers[q.key]
        if value is None:  # пропущенный вопрос
            continue
        if q.profile_field == "languages.english":
            if value and float(value) > 0:
                profile["languages"]["english"] = {"level": float(value), "verified": 0.3}
        elif q.profile_field == "_motivation_1" or q.profile_field == "_motivation_2":
            continue  # обрабатываются ниже
        elif q.profile_field == "_sensitive_consent":
            profile["_sensitive_consent"] = bool(value)
        else:
            _set_nested(profile, q.profile_field, value)
    # мотивация: главный приоритет 1.0, второй 0.7, остальные 0.3
    m1 = answers.get("motivation_1")
    m2 = answers.get("motivation_2")
    keys = ["money", "growth", "stability", "mission", "recognition"]
    profile["motivation"] = {
        k: (1.0 if k == m1 else 0.7 if k == m2 and m2 != m1 else 0.3) for k in keys
    }
    # согласие не дано — чувствительные поля не попадают в профиль
    if not profile.get("_sensitive_consent"):
        for f in ("stress_tolerance", "multitask_preference", "autonomy_preference",
                  "team_size_preference", "communication_style", "motivation",
                  "risk_appetite", "sensory_sensitivity", "structure_need",
                  "learning_appetite", "neuro_profile", "family_constraints"):
            profile.pop(f, None)
            answers.pop(f, None)
    return profile
