"""Аудит резюме МАРСИ-Job — детерминированное ядро (7 измерений, 0–100).

Каждое измерение — чистая функция по профилю кандидата и его топ-матчам.
LLM не участвует в вычислении скоров; опциональная секция «Формулировки»
генерируется отдельно и недоступна без ключа.
"""
import json
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Типы
# ---------------------------------------------------------------------------

DIMENSIONS = ("completeness", "verification", "portfolio", "experience",
              "positioning", "format", "languages")

DIMENSION_TITLES = {
    "completeness": "Полнота профиля",
    "verification": "Подтверждённость навыков",
    "portfolio": "Портфолио и публичные артефакты",
    "experience": "Опыт",
    "positioning": "Заголовок и позиционирование",
    "format": "Формат и контакты",
    "languages": "Языки",
}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


# ---------------------------------------------------------------------------
# Измерения (каждое → (score 0..1, статус, actions[1..3]))
# ---------------------------------------------------------------------------

def check_completeness(profile: Dict[str, Any]) -> Tuple[float, str, List[str]]:
    """% заполненных ключевых секций."""
    sections = {
        "hard_skills": bool(profile.get("hard_skills")),
        "experience_years": profile.get("experience_years") is not None,
        "work_format": bool(profile.get("work_format")),
        "schedule": bool(profile.get("schedule")),
        "salary": bool(profile.get("salary", {}).get("min")),
        "location": bool(profile.get("location", {}).get("city")),
        "education_level": bool(profile.get("education_level")),
        "languages": bool(profile.get("languages")),
        "portfolio": profile.get("portfolio") is not None,
        "career_trajectory": bool(profile.get("career_trajectory")),
    }
    filled = sum(1 for v in sections.values() if v)
    score = filled / len(sections)
    missing = [k for k, v in sections.items() if not v]
    actions = []
    if missing:
        actions.append("Дозаполнить: " + ", ".join(missing[:5]) + " — /fill")
    status = "ok" if score >= 0.8 else "attention" if score >= 0.5 else "critical"
    return score, status, actions or ["Профиль заполнен полностью"]


def check_verification(profile: Dict[str, Any]) -> Tuple[float, str, List[str]]:
    """Доля навыков с подтверждением (verified ≥ 0.5)."""
    skills = profile.get("hard_skills") or {}
    if not skills:
        return 0.0, "critical", ["Добавьте навыки в профиль"]
    verified = sum(1 for m in skills.values() if float(m.get("verified", 0)) >= 0.5)
    score = verified / len(skills)
    unverified = [name for name, m in skills.items() if float(m.get("verified", 0)) < 0.5]
    actions = []
    if unverified:
        actions.append("Подтвердите навыки тестом/ссылкой: " + ", ".join(unverified[:4]))
    status = "ok" if score >= 0.6 else "attention" if score >= 0.3 else "critical"
    return score, status, actions or ["Все ключевые навыки подтверждены"]


def check_portfolio(profile: Dict[str, Any]) -> Tuple[float, str, List[str]]:
    """Наличие GitHub/кейсов/ссылок."""
    portfolio = float(profile.get("portfolio", 0) or 0)
    score = _clamp01(portfolio + 0.1)  # небольшой буфер за наличие профиля
    actions = []
    if portfolio < 0.3:
        actions.append("Соберите 2–3 кейса с измеримым результатом на GitHub/Notion")
    elif portfolio < 0.6:
        actions.append("Усильте портфолио: добавьте описание решённой задачи и метрику")
    status = "ok" if portfolio >= 0.6 else "attention" if portfolio >= 0.3 else "critical"
    return score, status, actions or ["Портфолио сильное"]


def check_experience(profile: Dict[str, Any]) -> Tuple[float, str, List[str]]:
    """Длительность vs грейд; пробелы (упрощённо — по анкете)."""
    years = float(profile.get("experience_years", 0) or 0)
    trajectory = profile.get("career_trajectory", "")
    score = _clamp01(years / 6)  # 6+ лет = полный скор этого измерения
    actions = []
    if years < 1:
        actions.append("Для наращивания опыта: open-source, фриланс-проекты, pet-проекты")
    if trajectory == "management" and years < 3:
        actions.append("Менеджерская траектория при <3 лет опыта — усилите техническую базу")
    status = "ok" if years >= 3 else "attention" if years >= 1 else "critical"
    return score, status, actions or ["Опыт соответствует заявленному уровню"]


def check_positioning(profile: Dict[str, Any]) -> Tuple[float, str, List[str]]:
    """Специализация vs универсальность."""
    trajectory = profile.get("career_trajectory", "")
    domains = profile.get("domain_experience") or []
    interests = profile.get("interests") or []
    score = 0.5
    if trajectory in ("deep", "management"):
        score += 0.2
    if len(domains) >= 1 and len(domains) <= 3:
        score += 0.2
    if len(interests) >= 2:
        score += 0.1
    score = _clamp01(score)
    actions = []
    if trajectory == "broad":
        actions.append("Сузьте специализацию — универсальные резюме проигрывают профильным")
    if not domains:
        actions.append("Укажите отрасль(и) опыта — это помогает матчингу")
    status = "ok" if score >= 0.7 else "attention" if score >= 0.4 else "critical"
    return score, status, actions or ["Позиционирование чёткое"]


def check_format(profile: Dict[str, Any]) -> Tuple[float, str, List[str]]:
    """Структура, зарплатные ожидания, тип оформления."""
    score = 0.5
    actions = []
    salary = profile.get("salary") or {}
    if salary.get("min"):
        score += 0.2
    else:
        actions.append("Укажите зарплатную вилку — вакансии без неё матчатся хуже")
    if profile.get("employment_type"):
        score += 0.15
    else:
        actions.append("Уточните допустимый тип оформления")
    if profile.get("relocation"):
        score += 0.15
    else:
        actions.append("Отметьте готовность/неготовность к релокации")
    score = _clamp01(score)
    status = "ok" if score >= 0.8 else "attention" if score >= 0.5 else "critical"
    return score, actions and status or "ok", actions or ["Формат в порядке"]


def check_languages(profile: Dict[str, Any],
                    top_vacancies: Optional[List[Dict]] = None) -> Tuple[float, str, List[str]]:
    """Соответствие уровня языков требованиям топ-матчей."""
    langs = profile.get("languages") or {}
    score = 0.3
    actions = []
    if langs.get("russian"):
        score += 0.3
    eng = langs.get("english") or {}
    eng_level = float(eng.get("level", 0) or 0)
    if eng_level >= 0.7:
        score += 0.4
    elif eng_level >= 0.5:
        score += 0.25
        actions.append("Английский B1→B2 откроет ~30% больше IT-вакансий")
    else:
        actions.append("Английский B1 — минимум для большинства IT-вакансий с удалёнкой")
    # сверка с топ-вакансиями: сколько требуют английский
    if top_vacancies:
        need_eng = sum(1 for v in top_vacancies
                       if (v.get("languages") or {}).get("english"))
        if need_eng >= 2 and eng_level < 0.5:
            actions.append(f"⚠ {need_eng} из ваших топ-матчей требуют английский")
    score = _clamp01(score)
    status = "ok" if score >= 0.7 else "attention" if score >= 0.4 else "critical"
    return score, status, actions or ["Языковой уровень достаточен"]


# ---------------------------------------------------------------------------
# Итоговый аудит
# ---------------------------------------------------------------------------

CHECKS = {
    "completeness": check_completeness,
    "verification": check_verification,
    "portfolio": check_portfolio,
    "experience": check_experience,
    "positioning": check_positioning,
    "format": check_format,
    "languages": check_languages,
}


def run_audit(profile: Dict[str, Any],
              top_vacancies: Optional[List[Dict]] = None,
              weights: Optional[Dict[str, float]] = None,
              ) -> Dict[str, Any]:
    """Полный аудит → {score: 0..100, dimensions: {name: {score, status, actions}}}"""
    weights = weights or {d: 1 / len(DIMENSIONS) for d in DIMENSIONS}
    dimensions: Dict[str, Dict[str, Any]] = {}
    total = 0.0
    for dim in DIMENSIONS:
        fn = CHECKS[dim]
        if dim == "languages":
            sc, st, acts = fn(profile, top_vacancies)
        else:
            sc, st, acts = fn(profile)
        dimensions[dim] = {"title": DIMENSION_TITLES[dim],
                           "score": round(sc, 3), "status": st, "actions": acts}
        total += sc * weights.get(dim, 0)
    score_100 = round(total * 100)
    return {"score": score_100, "dimensions": dimensions}


def format_teaser(audit: Dict[str, Any]) -> str:
    """Бесплатный мини-аудит: скор + топ-1 проблема."""
    worst = min(audit["dimensions"].values(), key=lambda d: d["score"])
    lines = [
        f"📋 <b>Аудит резюме: {audit['score']}/100</b>",
        "",
        f"⚠ Главная проблема: <b>{worst['title']}</b> ({worst['status']}).",
        f"→ {worst['actions'][0]}",
        "",
        "Полный отчёт: разбор по 7 измерениям с правками и прогнозом прироста — "
        "<b>199 ₽</b>, /buy",
    ]
    return "\n".join(lines)


def format_full(audit: Dict[str, Any]) -> str:
    """Полный отчёт аудита."""
    status_icons = {"ok": "✅", "attention": "⚠️", "critical": "❌"}
    lines = [
        f"📋 <b>Аудит резюме: {audit['score']}/100</b>",
        "",
    ]
    for dim in DIMENSIONS:
        d = audit["dimensions"][dim]
        icon = status_icons.get(d["status"], "•")
        lines.append(f"{icon} <b>{d['title']}</b> — {int(d['score'] * 100)}%")
        for a in d["actions"][:2]:
            lines.append(f"   → {a}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM-секция «Формулировки» (опционально)
# ---------------------------------------------------------------------------

LLM_SYSTEM = (
    "Ты — карьерный консультант. Проанализируй профиль кандидата и верни "
    "СТРОГО JSON: {\"remarks\": [{\"text\": \"...\"}]} — 3–5 конкретных "
    "замечаний по формулировкам резюме (штампы, достижения без цифр, "
    "структура). Каждое — 1–2 предложения, без воды."
)


def llm_remarks(profile: Dict[str, Any], api_key: str,
                base_url: str = "https://open.bigmodel.cn/api/paas/v4",
                model: str = "glm-4.6", timeout: int = 60) -> Optional[List[str]]:
    """3–5 замечаний по тексту (None при сбое/нет ключа)."""
    if not api_key:
        return None
    summary = json.dumps({k: profile.get(k) for k in
                          ("hard_skills", "experience_years", "domain_experience",
                           "career_trajectory", "education_level")},
                         ensure_ascii=False, default=str)[:2000]
    payload = {"model": model,
               "messages": [{"role": "system", "content": LLM_SYSTEM},
                            {"role": "user", "content": summary}],
               "temperature": 0.3, "max_tokens": 500,
               "response_format": {"type": "json_object"}}
    if "bigmodel" in base_url:
        payload["thinking"] = {"type": "disabled"}
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return [r["text"] for r in parsed.get("remarks", [])][:5]
    except Exception:
        return None
