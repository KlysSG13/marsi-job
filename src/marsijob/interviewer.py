"""LLM-интервью МАРСИ-Job: живой диалог вместо анкеты (альтернативный режим).

ИИ-интервьюер ведёт беседу (6–10 обменов), собирая факторы профиля; по итогам
диалог запускается через обычный LLM-парсер → строгий JSON → подтверждение
пользователем (confirm.py). Ядро скоринга остаётся детерминированным.
"""
import json
import urllib.request
from typing import Any, Dict, List, Optional

INTERVIEWER_SYSTEM = (
    "Ты — дружелюбный карьерный интервьюер бота МАРСи-Job. Задача: за 6–8 вопросов "
    "понять кандидата и собрать данные для подбора работы.\n"
    "Правила:\n"
    "— задавай РОВНО ОДИН вопрос за раз, коротко (1–2 предложения), живым тоном, без списков;\n"
    "— сначала узнай: чем занимается и ключевые навыки с уровнем; затем опыт в годах; "
    "желаемый формат (удалёнка/гибрид/офис) и график; зарплатную вилку в рублях; город и "
    "часовой пояс; готовность к переезду; уровень английского; чего хочет по карьере "
    "(специализация/широта/менеджмент/стабильность); что важно в работе (деньги/рост/"
    "стабильность/миссия — выбрать главное).\n"
    "— адаптируйся: если человек уже назвал что-то — не переспрашивай;\n"
    "— ЗАПРЕЩЕНО спрашивать о здоровье, диагнозах, нейроособенностях, семье, детях, "
    "поле, возрасте — эти блоки заполняются отдельно и добровольно;\n"
    "— не используй HTML-разметку и markdown;\n"
    "— когда всё собрал (или после 10 обменов) — ответь одним словом: ГОТОВО\n"
)

FINISH_WORD = "ГОТОВО"

PARSE_FROM_DIALOG_SYSTEM = (
    "Ты — парсер карьерного интервью. Ниже диалог интервьюера и кандидата. "
    "Извлеки из него факты профиля кандидата и верни СТРОГО JSON по схеме:\n"
    "{\n"
    '  "hard_skills": {"<навык>": {"level": 0.0-1.0, "verified": 0.3}},\n'
    '  "experience_years": float, "domain_experience": ["it","fintech",...],\n'
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
    '  "work_permit": ["RU"],\n'
    '  "commute_max": int, "interests": [str],\n'
    '  "career_trajectory": "deep|broad|management|stability"\n'
    "}\n"
    "Указывай только то, что кандидат сказал явно. Уровень навыка оцени по его словам "
    "(junior≈0.4, middle≈0.6, senior≈0.8). Никаких домыслов и пояснений — только JSON."
)


class InterviewerError(Exception):
    pass


def _chat(messages: List[Dict[str, str]], api_key: str,
          base_url: str, model: str, timeout: int = 120) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": 800,
        "response_format": {"type": "json_object"} if False else None,
    }
    # интервьюеру JSON-режим не нужен (обычный текст); GLM: отключаем reasoning
    if "bigmodel" in base_url:
        payload["thinking"] = {"type": "disabled"}
    payload = {k: v for k, v in payload.items() if v is not None}
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise InterviewerError(f"LLM-запрос не удался: {e}") from e
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError) as e:
        raise InterviewerError(f"Неожиданный ответ LLM: {e}") from e


def next_question(messages: List[Dict[str, str]], api_key: str,
                  base_url: str, model: str) -> str:
    """Следующая реплика интервьюера; ГОТОВО — диалог завершён."""
    reply = _chat([{"role": "system", "content": INTERVIEWER_SYSTEM}] + messages,
                  api_key, base_url, model)
    return reply


def is_finished(reply: str) -> bool:
    return FINISH_WORD in reply.upper() or "Готово" in reply


def parse_dialog(dialog_text: str, api_key: str,
                 base_url: str, model: str) -> Dict[str, Any]:
    """Диалог → профиль (строгий JSON, та же валидация, что у парсера резюме)."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": PARSE_FROM_DIALOG_SYSTEM},
            {"role": "user", "content": dialog_text[:16000]},
        ],
        "temperature": 0.0,
        "max_tokens": 4000,
        "response_format": {"type": "json_object"},
    }
    if "bigmodel" in base_url:
        payload["thinking"] = {"type": "disabled"}
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise InterviewerError(f"LLM-запрос не удался: {e}") from e
    try:
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise InterviewerError(f"Не удалось разобрать ответ: {e}") from e
    from .parser_llm import validate_parsed
    ok, errors = validate_parsed(parsed)
    if not ok:
        raise InterviewerError("Схема не прошла валидацию: " + "; ".join(errors))
    return parsed
