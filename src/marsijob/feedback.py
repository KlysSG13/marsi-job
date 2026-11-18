"""Итеративный цикл МАРСИ-Job (шаг i → i+1, SPEC §8).

Событие фидбека = «выполнение микро-метода + обратная связь» (МАРСИ-DN1, Q&A 5):
  · дислайк кандидата поднимает веса затронутых факторов его профиля (+20%, cap 1.0);
  · «отклик без ответа» снижает индекс доверия вакансии (−0.1) и растит её ε;
  · «успешный контакт» поднимает доверие (+0.1);
  · три пересчёта подряд без значимого изменения скора — пара замораживается
    (условие остановки, МАРСИ-DN1 «Проверка динамики»).
"""
from typing import Any, Dict, List, Optional, Tuple

# какие веса факторов поднимает событие (SPEC §8)
FACTOR_BUMPS: Dict[str, List[Tuple[str, float]]] = {
    "dislike_format": [("work_format", 1.2), ("schedule", 1.2)],
    "dislike_domain": [("domain_experience", 1.2), ("interests", 1.2)],
    "dislike_salary": [("salary", 1.15)],
    "skill_reject": [("hard_skills", 1.15)],
    "interview_ok": [],  # успешный контакт: доверие вакансии, веса не трогаем
    "no_response": [],   # доверие вакансии падает
    "invite": [],
}

TRUST_DELTAS: Dict[str, float] = {
    "no_response": -0.1,
    "interview_ok": +0.1,
}

FREEZE_ITERATIONS = 3     # 3 итерации без значимого изменения
FREEZE_SCORE_DELTA = 0.02  # что считать «без изменения»


def apply_candidate_feedback(
    personal_weights: Optional[Dict[str, float]],
    kind: str,
    factor_defaults: Dict[str, float],
    vacancy: Optional[Dict[str, Any]] = None,
    profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Шаг итерации i → i+1 для кандидата.

    Возвращает {"weights": …, "profile_edits": …}:
      · weights — персональные калибровки w0 (×1.2, cap 1.0);
      · profile_edits — детерминированные правки профиля для бинарных факторов
        (дислайк формата/графика убирает значение вакансии из допустимых —
        похожие вакансии больше не показываются).
    """
    weights = dict(personal_weights or {})
    for factor_id, mult in FACTOR_BUMPS.get(kind, []):
        base = weights.get(factor_id, factor_defaults.get(factor_id, 0.5))
        weights[factor_id] = min(1.0, base * mult)
    edits: Dict[str, Any] = {}
    if kind == "dislike_format" and vacancy and profile is not None:
        for field, vac_field in (("work_format", "work_format"), ("schedule", "schedule")):
            allowed = list(profile.get(field) or [])
            value = vacancy.get(vac_field)
            if value in allowed and len(allowed) > 1:
                allowed.remove(value)
                edits[field] = allowed
    return {"weights": weights, "profile_edits": edits}


def trust_delta(kind: str) -> float:
    return TRUST_DELTAS.get(kind, 0.0)


def epsilon_penalty(kind: str, count: int) -> float:
    """Каждое «отклик без ответа» добавляет вакансии шума (SPEC §8: +0.02)."""
    if kind != "no_response":
        return 0.0
    return min(0.1, 0.02 * count)


def should_freeze(prev_score: Optional[float], new_score: float,
                  iterations: int) -> Tuple[bool, int]:
    """Условие остановки цикла по паре.

    Возвращает (frozen, new_iterations): счётчик растёт, пока скор почти
    не меняется; при FREEZE_ITERATIONS стабильных пересчётов — заморозка.
    """
    if prev_score is not None and abs(new_score - prev_score) <= FREEZE_SCORE_DELTA:
        iterations += 1
    else:
        iterations = 1
    return iterations >= FREEZE_ITERATIONS, iterations
