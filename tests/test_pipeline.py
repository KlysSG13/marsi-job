"""Интеграционный конвейер: анкета → профиль → хранилище → движок → выдача."""
import json
from pathlib import Path

import pytest

from marsijob.config import Config
from marsijob.engine import Engine
from marsijob.explain import format_match_card, format_root_causes
from marsijob.questionnaire import build_profile
from marsijob.storage import Storage
from marsijob.valence import root_causes, valence_zone

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEED = PROJECT_ROOT / "data" / "seed_vacancies.json"

# ответы анкеты — профиль, эквивалентный кандидату TC1
ANSWERS = {
    "hard_skills": {"python": {"level": 0.7, "verified": 0.7},
                    "git": {"level": 0.6, "verified": 0.3},
                    "sql": {"level": 0.5, "verified": 0.3}},
    "experience_years": 1.5, "domain_experience": ["it"],
    "education_level": "bachelor", "english_level": 0.5, "portfolio": 0.6,
    "management": False,
    "work_format": ["remote", "hybrid"], "schedule": ["flexible", "full_time"],
    "schedule_compromise": False, "employment_type": ["tk", "self_employed"],
    "salary_min": 80000, "salary_max": 130000, "salary_compromise": True,
    "business_trips": 0.2, "relocation": "no", "city": "Казань", "tz": 3,
    "work_permit": ["RU"], "commute_max": 40,
    "sensitive_consent": True,
    "stress_tolerance": 0.5, "multitask_preference": 0.4,
    "autonomy_preference": 0.7, "team_size": [2, 10],
    "communication_style": "async", "motivation_1": "growth", "motivation_2": "money",
    "risk_appetite": 0.5, "learning_appetite": 0.8,
    "sensory_sensitivity": 0.6, "structure_need": 0.6,
    "neuro_profile": ["adhd"], "family_constraints": ["none"],
    "interests": ["games", "data", "automation"],
    "culture_fit": "casual", "mission_importance": 0.4,
    "career_trajectory": "deep", "urgency_months": 2, "start_date_months": 1,
}


@pytest.fixture()
def store() -> Storage:
    s = Storage(":memory:")
    s.seed_vacancies(SEED)
    yield s
    s.close()


@pytest.fixture(scope="module")
def engine() -> Engine:
    return Engine(Config.load())


def test_seed_loaded(store):
    vacancies = store.list_active_vacancies()
    assert len(vacancies) >= 10
    assert all("id" in v and "title" in v for v in vacancies)


def test_storage_roundtrip(store):
    user_id = store.upsert_user(100500, "candidate")
    profile = build_profile(ANSWERS)
    store.save_candidate_profile(user_id, profile)
    loaded = store.get_candidate_profile(user_id)
    assert loaded["location"]["city"] == "Казань"
    assert loaded["hard_skills"]["python"]["level"] == 0.7


def test_full_match_pipeline(store, engine):
    """Сценарий /match: профиль из анкеты × seed-вакансии."""
    user_id = store.upsert_user(100500, "candidate")
    profile = build_profile(ANSWERS)
    store.save_candidate_profile(user_id, profile)

    vacancies = store.list_active_vacancies()
    results = [engine.score(profile, v) for v in vacancies]
    for res, v in zip(results, vacancies):
        store.save_match(user_id, v["db_id"], res.breakdown())

    rows = store.list_matches(user_id)
    assert len(rows) == len(vacancies)

    shown = sorted(
        [r for r in results if not r.blocked and r.band != "below"],
        key=lambda r: r.score, reverse=True,
    )
    assert shown, "должны быть показываемые вакансии"
    top = shown[0]
    assert top.score > 0.5, f"топ-скор слишком низок: {top.score}"

    by_id = {v["id"]: v for v in vacancies}
    vac = by_id[top.vacancy_id]
    interventions = valence_zone(engine, profile, vac, top_n=2)["interventions"]
    card = format_match_card(top, vac, interventions)
    assert "матч" in card.lower() or "%" in card
    assert len(card) > 80  # карточка содержательная

    blocked = [r for r in results if r.blocked]
    for r in blocked:
        card = format_match_card(r, by_id[r.vacancy_id])
        assert "невозможна" in card.lower()

    causes = root_causes(engine, profile, vacancies, top_n=3)
    text = format_root_causes(causes)
    assert "валентности" in text


def test_remote_junior_top_is_friendly(store, engine):
    """Топ-1 для джуна с СДВГ — удалённая/гибкая вакансия, а не open-space."""
    profile = build_profile(ANSWERS)
    vacancies = store.list_active_vacancies()
    results = sorted(
        (engine.score(profile, v) for v in vacancies),
        key=lambda r: r.score, reverse=True,
    )
    top = results[0]
    by_id = {v["id"]: v for v in vacancies}
    vac = by_id[top.vacancy_id]
    assert vac["schedule"] in ("flexible", "part_time") or vac["sensory_load"] <= 0.4
