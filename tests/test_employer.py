"""Тесты стороны работодателя: анкета вакансии, модерация, поиск кандидатов."""
import json
from pathlib import Path

import pytest

from marsijob.config import Config
from marsijob.engine import Engine
from marsijob.explain import environment_compatibility, format_candidate_card
from marsijob.moderation import check_discrimination
from marsijob.questionnaire import build_profile
from marsijob.storage import Storage
from marsijob.vacancy_q import build_vacancy, parse_vacancy_skills

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEED = PROJECT_ROOT / "data" / "seed_vacancies.json"

# кандидат TC1
ANSWERS = {
    "hard_skills": {"python": {"level": 0.7, "verified": 0.7},
                    "git": {"level": 0.6, "verified": 0.3}},
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
    "interests": ["games", "data"], "culture_fit": "casual", "mission_importance": 0.4,
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


def _engine_score(vac):
    eng = Engine(Config.load())
    profile = build_profile(ANSWERS)
    return eng.score(profile, vac)


class TestVacancyQuestionnaire:
    def test_parse_vacancy_skills(self):
        skills = parse_vacancy_skills("python: 70!, docker: 60")
        assert skills["python"] == {"req_level": 0.7, "must": True}
        assert skills["docker"] == {"req_level": 0.6, "must": False}

    def test_build_vacancy_schema(self):
        answers = {
            "title": "Python-разработчик",
            "hard_skills": {"python": {"req_level": 0.7, "must": True}},
            "experience_years_min": 1, "domain": "it",
            "education_level_min": None, "english_req": None,
            "portfolio_required": 0.3, "work_format": "remote", "schedule": "flexible",
            "employment_type": "tk", "salary_min": 90000, "salary_max": 140000,
            "business_trips_required": 0.0, "relocation_supported": False,
            "city": "удалённо", "tz": 3, "work_permit_required": ["RU"],
            "vacancy_criticality": 0.5, "sensory_load": 0.2, "stress_level": 0.4,
            "multitask_intensity": 0.4, "micromanagement_degree": 0.3,
            "role_ambiguity": 0.4, "learning_intensity": 0.6, "stage_risk": 0.4,
            "team_size": 6, "communication_mode": "async", "culture": "casual",
            "trajectory_offered": "deep", "motivation_1": "growth",
            "motivation_2": "money", "training_offered": True,
            "product_tags": ["games"], "urgency_months": 2,
            "start_date_required_months": 1,
        }
        vac = build_vacancy(answers, "E1-1")
        assert vac["title"] == "Python-разработчик"
        assert vac["location"] == {"city": None, "tz": 3}
        assert vac["motivation_offered"]["growth"] == 0.9
        assert vac["motivation_offered"]["money"] == 0.7
        assert vac["hard_skills"]["python"]["must"] is True
        # схема совместима с движком
        result = _engine_score(vac)
        assert result is not None and result.score > 0



class TestModeration:
    def test_blocks_age(self):
        res = check_discrimination("Ищем менеджера до 35 лет")
        assert not res["ok"]

    def test_blocks_gender(self):
        res = check_discrimination("Требуется девушка для работы с клиентами")
        assert not res["ok"]

    def test_blocks_children(self):
        res = check_discrimination("Сотрудник без детей, полный день")
        assert not res["ok"]

    def test_clean_text_ok(self):
        res = check_discrimination("Ищем Python-разработчика, удалёнка, гибкий график")
        assert res["ok"] and not res["warnings"]

    def test_young_team_warning(self):
        res = check_discrimination("Молодая команда, динамичный продукт")
        assert res["ok"] and res["warnings"]


class TestEmployerSearch:
    def test_rank_and_card(self, store, engine):
        # работодатель + вакансия
        emp = store.upsert_user(200, "employer")
        vac = json.loads((SEED).read_text(encoding="utf-8"))[0]  # junior remote flexible
        vac["id"] = "E-TEST"
        store.insert_vacancy(emp, vac)
        # кандидат в базе
        cand = store.upsert_user(100, "candidate")
        profile = build_profile(ANSWERS)
        store.save_candidate_profile(cand, profile)

        candidates = store.list_candidate_profiles()
        assert len(candidates) == 1
        results = [(engine.score(c["profile"], vac), c) for c in candidates]
        top_result, top_cand = results[0]
        assert not top_result.blocked

        card = format_candidate_card(top_result, top_cand)
        assert "Матч" in card
        # чувствительные факторы не называются в карточке
        for word in ("сенсорн", "тревож", "мотивац", "СДВГ", "аутиз"):
            assert word.lower() not in card.lower()
        assert "Совместимость среды" in card  # агрегированный индекс есть

    def test_environment_compatibility(self, store, engine):
        profile = build_profile(ANSWERS)
        vac = json.loads(SEED.read_text(encoding="utf-8"))[0]
        res = engine.score(profile, vac)
        env = environment_compatibility(res)
        assert env is not None and 0.0 <= env <= 1.0

    def test_employer_vacancy_lifecycle(self, store):
        emp = store.upsert_user(300, "employer")
        vac = json.loads(SEED.read_text(encoding="utf-8"))[1]
        db_id = store.insert_vacancy(emp, vac)
        mine = store.list_employer_vacancies(emp)
        assert len(mine) == 1 and mine[0]["db_id"] == db_id
        # индекс доверия обновляется и клампится
        val = store.update_vacancy_trust(db_id, -0.1)
        assert val == pytest.approx(0.6)
        val = store.update_vacancy_trust(db_id, -10)
        assert val == 0.2
