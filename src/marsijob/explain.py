"""Форматирование объяснений матчинга для пользователя (чистые функции)."""
import html
from typing import Any, Dict, List, Optional

BAND_TITLES = {
    "strong": "💪 Сильный матч",
    "decent": "👍 Достойный матч",
    "conditional": "🤝 Условный матч (есть что сближать)",
    "below": "👇 Слабый матч",
}

BLOCK_TITLES = {
    "BL-001": "не хватает must-have навыка",
    "BL-002": "формат работы не подходит",
    "BL-003": "график не подходит",
    "BL-004": "зарплатные вилки не пересекаются",
    "BL-005": "нет права работать в этой юрисдикции",
    "BL-006": "часовые пояса/город несовместимы без релокации",
    "BL-007": "язык ниже требуемого уровня",
    "BL-008": "абсолютное сенсорное противопоказание",
}


def _fmt_pct(x: float) -> str:
    return f"{round(x * 100)}%"


def format_match_card(
    result: Any,
    vacancy: Dict[str, Any],
    interventions: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Карточка матча: скор, вклад факторов, Φ-эффекты, что сблизить."""
    lines: List[str] = []
    title = html.escape(str(vacancy.get("title", vacancy.get("id", "вакансия"))))
    lines.append(f"🎯 <b>{title}</b>")
    if vacancy.get("source"):
        url = vacancy.get("external_url")
        labels = {"hh": "hh.ru", "trudvsem": "Работа России", "habr": "Хабр Карьера"}
        label = labels.get(str(vacancy.get("source")), str(vacancy.get("source")))
        link = f" · <a href=\"{html.escape(str(url))}\">открыть</a>" if url else ""
        lines.append(f"<i>Источник: {label}{link}</i>")
        if vacancy.get("employer_name"):
            lines.append(f"<i>Работодатель: {html.escape(str(vacancy['employer_name']))}</i>")
    if result.blocked:
        lines.append("⛔ <b>Пара невозможна</b>")
        for reason in result.block_reasons:
            code = reason.split(":")[0]
            lines.append(f"   • {BLOCK_TITLES.get(code, code)} ({reason})")
        return "\n".join(lines)

    band = BAND_TITLES.get(result.band, result.band)
    lines.append(f"{band} · <b>{_fmt_pct(result.score)}</b>"
                 + freshness_badge(vacancy))
    upd = updated_line(vacancy)
    if upd:
        lines.append(f"<i>{upd}</i>")
    lines.append("")

    top = result.top_factors(4)
    if top:
        lines.append("<b>Что сходится:</b>")
        for f in top:
            lines.append(f"   + {_fmt_pct(f.contribution / max(result.sums['sum_w'], 1e-9))} — {f.name_ru}")
    worst = [f for f in result.worst_factors(3) if f.match is not None and f.match < 0.7]
    if worst:
        lines.append("<b>Что мешает:</b>")
        for f in worst:
            lines.append(f"   − {f.name_ru}: совпадение {_fmt_pct(f.match)}" +
                         (f" (+ресурсы: {_fmt_pct(f.r)})" if f.r > 0 else ""))

    pos = [p for p in result.phi_effects if p.value > 0][:3]
    neg = [p for p in result.phi_effects if p.value < 0][:2]
    if pos:
        lines.append("<b>Синергии (Φ+):</b>")
        for p in pos:
            lines.append(f"   ✦ {p.comment}")
    if neg:
        lines.append("<b>Конфликты (Φ−):</b>")
        for p in neg:
            lines.append(f"   ✖ {p.comment}")

    if interventions:
        lines.append("<b>Как поднять скор:</b>")
        for iv in interventions[:2]:
            lines.append(f"   → {iv['action']} (+{_fmt_pct(iv['gain'])})")
    return "\n".join(lines)


def format_match_list(results: List[Any], vacancies: Dict[str, Dict[str, Any]],
                      limit: int = 5, show_blocked_count: bool = True) -> List[str]:
    """Список карточек для /match."""
    cards = []
    shown = sorted(
        [r for r in results if not r.blocked and r.band != "below"],
        key=lambda r: r.score, reverse=True,
    )[:limit]
    for r in shown:
        cards.append(r.vacancy_id)
    return cards


def freshness_badge(vacancy: Dict[str, Any]) -> str:
    """Метка свежести: 🆕 <7 дней; ⚠ N дней без обновления (>30)."""
    age = vacancy.get("_age_days")
    if age is None:
        return ""
    if age < 7:
        return " 🆕"
    if age > 30:
        return f" ⚠ {age} дн. без обновления"
    return ""


def updated_line(vacancy: Dict[str, Any]) -> str:
    age = vacancy.get("_age_days")
    if age is None:
        return ""
    return f"Обновлено: {age} дн. назад"


def format_match_short(result: Any, vacancy: Dict[str, Any]) -> str:
    """Сокращённая карточка матча (бесплатный тариф): скор + топ-2–3 фактора."""
    title = html.escape(str(vacancy.get("title", vacancy.get("id", "вакансия"))))
    lines = [f"🎯 <b>{title}</b> · <b>{_fmt_pct(result.score)}</b>"
             + freshness_badge(vacancy)]
    upd = updated_line(vacancy)
    if upd:
        lines.append(f"<i>{upd}</i>")
    if result.blocked:
        lines.append("⛔ несовместимость")
        return "\n".join(lines)
    top = result.top_factors(3)
    if top:
        lines.append("Сходство: " + ", ".join(f.name_ru.lower() for f in top))
    if vacancy.get("source"):
        labels = {"hh": "hh.ru", "trudvsem": "Работа России", "habr": "Хабр Карьера"}
        lines.append("<i>Источник: " + labels.get(str(vacancy.get("source")), "внешний") + "</i>")
    sal = vacancy.get("salary")
    if sal:
        lines.append(f"{int(sal.get('min', 0)):,}–{int(sal.get('max', 0)):,} ₽".replace(",", " "))
    return "\n".join(lines)


def format_candidate_short(result: Any, candidate: Dict[str, Any]) -> str:
    """Карточка кандидата без контактов (бесплатный тариф работодателя)."""
    profile = candidate.get("profile", candidate)
    skills = sorted((profile.get("hard_skills") or {}).keys())[:4]
    years = profile.get("experience_years", "—")
    city = html.escape(str((profile.get("location") or {}).get("city", "—")))
    line = (f"👤 Кандидат #{candidate.get('user_id', '?')} · {years} лет · {city} · "
            f"<b>{_fmt_pct(result.score)}</b>")
    if result.blocked:
        line += " · ⛔"
    else:
        line += "\n   Навыки: " + (html.escape(", ".join(skills)) if skills else "—")
    return line


def format_root_causes(causes: List[Dict[str, Any]]) -> str:
    lines = ["🧭 <b>Зона валентности: почему матчи слабее, чем могли бы быть</b>", ""]
    if not causes:
        return ("Все вакансии сейчас заблокированы фильтрами — смягчьте условия "
                "(формат, зарплата, география) или дождитесь новых вакансий.")
    for i, c in enumerate(causes, 1):
        lines.append(f"{i}. <b>{c['name']}</b> — средняя потеря {_fmt_pct(c['avg_loss'])}")
        lines.append(f"   → {c['action']}")
    lines.append("")
    lines.append("Приоритет №1 даёт максимальный прирост скора при исправлении "
                 "(принцип интервенции МАРСИ).")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Карточка кандидата для работодателя (этичный режим)
# ---------------------------------------------------------------------------

# факторы, которые работодатель НЕ видит по-отдельности (особая категория ПДн)
SENSITIVE_FACTOR_IDS = {
    "stress_tolerance", "multitask_preference", "autonomy_preference",
    "team_size_preference", "communication_style", "motivation", "risk_appetite",
    "sensory_sensitivity", "structure_need", "learning_appetite",
}


def environment_compatibility(result: Any) -> Optional[float]:
    """Агрегированная совместимость среды (0..1) по чувствительным факторам.

    Работодатель видит только этот индекс, без деталей профиля (SPEC §6).
    """
    scored = [f for f in result.factors if f.match is not None and f.factor_id in SENSITIVE_FACTOR_IDS]
    if not scored:
        return None
    total_w = sum(f.w for f in scored)
    if total_w <= 0:
        return None
    return sum(f.match * f.w for f in scored) / total_w


def format_candidate_card(result: Any, candidate: Dict[str, Any]) -> str:
    """Карточка кандидата для работодателя: без чувствительных деталей."""
    profile = candidate.get("profile", candidate)
    skills = ", ".join(sorted((profile.get("hard_skills") or {}).keys())[:6]) or "—"
    years = profile.get("experience_years", "—")
    fmt = ", ".join(profile.get("work_format", [])) or "—"
    city = html.escape(str((profile.get("location") or {}).get("city", "—")))

    lines = [
        f"👤 Кандидат #{candidate.get('user_id', '?')} · опыт {years} лет · {city}",
        f"   Навыки: {html.escape(skills)}",
        f"   Формат: {html.escape(str(fmt))}",
    ]
    if result.blocked:
        lines.append("   ⛔ Пара невозможна: " + "; ".join(result.block_reasons[:2]))
        return "\n".join(lines)

    lines.append(f"🎯 Матч: <b>{_fmt_pct(result.score)}</b> · {BAND_TITLES.get(result.band, '')}")
    top = [f for f in result.top_factors(4) if f.factor_id not in SENSITIVE_FACTOR_IDS]
    if top:
        lines.append("   Сильные стороны совпадения: " + html.escape(
            ", ".join(f.name_ru for f in top)))
    env = environment_compatibility(result)
    if env is not None:
        lines.append(f"   🛡 Совместимость среды: {_fmt_pct(env)} "
                     "(агрегированный индекс, детали кандидата скрыты)")
    worst = [f for f in result.worst_factors(2)
             if f.match is not None and f.match < 0.6 and f.factor_id not in SENSITIVE_FACTOR_IDS]
    if worst:
        lines.append("   Зоны сближения: " + html.escape(
            ", ".join(f"{f.name_ru} ({_fmt_pct(f.match)})" for f in worst)))
    lines.append(f"   → Пригласить: /invite {candidate.get('user_id', '?')}")
    return "\n".join(lines)
