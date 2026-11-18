"""Мок-интервью с ИИ — модуль генерации вопросов и отчёта.

Сессия управляется ботом (FSM); здесь — чистые функции для LLM:
сводка профиля → вопросы по строгой схеме; ответы → отчёт.
Транскрипты НЕ сохраняются — только в памяти сессии.
"""
import json
import urllib.request
from typing import Any, Dict, List, Optional

QUESTION_SYSTEM = (
    "Ты — опытный интервьюер. На основе профиля кандидата и его слабых мест "
    "сгенерируй вопрос для собеседования. Верни СТРОГО JSON:\n"
    '{"question": "...", "purpose": "...", "factor": "...", '
    '"ideal": "краткое описание идеального ответа"}\n'
    "Вопрос — открытый, 1–2 предложения, на русском, требует конкретики. "
    "Не задавай вопросов о поле, возрасте, здоровье, семье."
)

REPORT_SYSTEM = (
    "Ты — карьерный коуч. Проанализируй ответы кандидата на вопросы "
    "собеседования и верни СТРОГО JSON:\n"
    '{"criteria": {"star": 0-10, "specificity": 0-10, "relevance": 0-10}, '
    '"strengths": ["...", "...", "..."], "weaknesses": ["...", "...", "..."], '
    '"rewritten": ["переписанный пример ответа 1", "переписанный пример ответа 2"], '
    '"plan_30min": "...", "plan_3h": "..."}\n'
    "Все тексты на русском, конкретно, без воды."
)


def _chat(system: str, user: str, api_key: str, base_url: str, model: str,
          max_tokens: int, timeout: int = 90) -> Optional[str]:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user[:8000]}],
        "temperature": 0.4, "max_tokens": max_tokens,
    }
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
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def profile_summary(profile: Dict[str, Any]) -> str:
    """Компактная сводка профиля для промпта."""
    skills = list((profile.get("hard_skills") or {}).keys())[:6]
    return (f"Навыки: {', '.join(skills) or '—'}. "
            f"Опыт: {profile.get('experience_years', '?')} лет. "
            f"Отрасль: {', '.join(profile.get('domain_experience', [])[:2]) or '—'}. "
            f"Траектория: {profile.get('career_trajectory', '?')}.")


def generate_question(api_key: str, base_url: str, model: str,
                      profile: Dict[str, Any],
                      weak_points: List[str],
                      vacancy_reqs: Optional[str] = None,
                      asked_so_far: Optional[List[str]] = None,
                      max_tokens: int = 200) -> Optional[Dict[str, str]]:
    """Следующий вопрос интервью → {question, purpose, factor, ideal} | None."""
    context = profile_summary(profile)
    if weak_points:
        context += f"\nСлабые места кандидата: {', '.join(weak_points[:3])}."
    if vacancy_reqs:
        context += f"\nТребования вакансии: {vacancy_reqs[:500]}"
    if asked_so_far:
        context += ("\nУже заданные вопросы (не повторяй):\n"
                    + "\n".join(f"- {q[:80]}" for q in asked_so_far[-5:]))
    raw = _chat(QUESTION_SYSTEM, context, api_key, base_url, model, max_tokens)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if "question" in parsed:
            return parsed
    except json.JSONDecodeError:
        pass
    return None


def generate_report(api_key: str, base_url: str, model: str,
                    qa_pairs: List[Dict[str, str]],
                    profile: Dict[str, Any],
                    max_tokens: int = 800) -> Optional[Dict[str, Any]]:
    """Финальный отчёт по ответам → {criteria, strengths, ...} | None."""
    transcript = "\n".join(
        f"Вопрос: {q['question']}\nОтвет: {q.get('answer', '')}"
        for q in qa_pairs)
    context = (f"Профиль: {profile_summary(profile)}\n\n"
               f"Переписка интервью:\n{transcript}")
    raw = _chat(REPORT_SYSTEM, context, api_key, base_url, model, max_tokens)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if "criteria" in parsed:
            return parsed
    except json.JSONDecodeError:
        pass
    return None


def format_report(report: Dict[str, Any]) -> str:
    """Человекочитаемый отчёт."""
    c = report.get("criteria", {})
    lines = ["📋 <b>Отчёт по мок-интервью</b>", ""]
    lines.append(f"⭐ STAR-структура: {c.get('star', '?')}/10")
    lines.append(f"🎯 Конкретика: {c.get('specificity', '?')}/10")
    lines.append(f"🔗 Релевантность: {c.get('relevance', '?')}/10")
    lines.append("")
    strengths = report.get("strengths", [])
    if strengths:
        lines.append("<b>Сильные стороны:</b>")
        for s in strengths[:3]:
            lines.append(f"  ✅ {s}")
    weaknesses = report.get("weaknesses", [])
    if weaknesses:
        lines.append("<b>Что улучшить:</b>")
        for w in weaknesses[:3]:
            lines.append(f"  ⚠ {w}")
    rewritten = report.get("rewritten", [])
    if rewritten:
        lines.append("\n<b>Переписанные примеры ответов:</b>")
        for i, r in enumerate(rewritten[:2], 1):
            lines.append(f"  {i}. {r}")
    if report.get("plan_30min"):
        lines.append(f"\n⏱ <b>Подготовка за 30 мин:</b> {report['plan_30min']}")
    if report.get("plan_3h"):
        lines.append(f"📚 <b>Подготовка за 3 часа:</b> {report['plan_3h']}")
    return "\n".join(lines)
