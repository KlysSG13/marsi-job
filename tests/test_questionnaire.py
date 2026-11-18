"""Тесты анкеты: парсинг, валидация, пропуски, сборка профиля."""
import pytest

from marsijob.questionnaire import (
    QUESTIONS, Question, build_profile, next_question, parse_skills,
    progress, question_flow, skip_reason, validate_answer,
)


def q(key: str) -> Question:
    return next(q for q in QUESTIONS if q.key == key)


class TestParseSkills:
    def test_basic(self):
        skills = parse_skills("python: 70, sql: 50")
        assert skills["python"] == {"level": 0.7, "verified": 0.3}
        assert skills["sql"] == {"level": 0.5, "verified": 0.3}

    def test_verified_marker(self):
        skills = parse_skills("python: 70!")
        assert skills["python"]["verified"] == 0.7

    def test_bounds(self):
        skills = parse_skills("go: 300")
        assert skills["go"]["level"] == 1.0

    def test_garbage_tolerant(self):
        skills = parse_skills("python")
        assert skills["python"]["level"] == 0.5  # уровень не указан — нейтральный


class TestValidate:
    def test_choice_single_by_label(self):
        ok, value, err = validate_answer(q("education_level"), "Бакалавриат", {})
        assert ok and value == "bachelor"

    def test_choice_single_rejects_free_text(self):
        ok, _v, err = validate_answer(q("education_level"), "универ", {})
        assert not ok

    def test_choice_multi(self):
        ok, value, _e = validate_answer(q("work_format"), "Удалённо, Гибрид", {})
        assert ok and value == ["remote", "hybrid"]

    def test_choice_multi_none_collapses(self):
        ok, value, _e = validate_answer(q("neuro_profile"), "Нет, СДВГ", {})
        assert ok and value == ["none"]

    def test_number(self):
        ok, value, _e = validate_answer(q("experience_years"), "2,5", {})
        assert ok and value == 2.5

    def test_skills_input(self):
        ok, value, _e = validate_answer(q("hard_skills"), "python: 70!", {})
        assert ok and "python" in value


class TestFlow:
    def test_commute_skipped_for_remote_only(self):
        answers = {"work_format": ["remote"]}
        assert skip_reason(q("commute_max"), answers) == "no_office"

    def test_commute_kept_for_hybrid(self):
        answers = {"work_format": ["hybrid", "remote"]}
        assert skip_reason(q("commute_max"), answers) is None

    def test_sensitive_block_needs_consent(self):
        answers = {"sensitive_consent": False}
        assert skip_reason(q("stress_tolerance"), answers) == "no_consent"
        answers = {"sensitive_consent": True}
        assert skip_reason(q("stress_tolerance"), answers) is None

    def test_management_team_skipped(self):
        assert skip_reason(q("management_team"), {"management": False}) == "no_management"
        assert skip_reason(q("management_team"), {"management": True}) is None

    def test_flow_progresses(self):
        answers = {"hard_skills": {}, "experience_years": 2}
        nxt = next_question(answers)
        assert nxt.key == "domain_experience"
        done, total = progress(answers)
        assert done == 2 and total >= 25

    def test_flow_ends(self):
        answers = {qq.key: "x" for qq in QUESTIONS}
        # ответы-заглушки не проходят валидацию, но поток должен завершиться
        # т.к. все ключи заполнены
        assert next_question({qq.key: None for qq in QUESTIONS}) is None


def full_answers():
    return {
        "hard_skills": {"python": {"level": 0.7, "verified": 0.7},
                        "git": {"level": 0.6, "verified": 0.3}},
        "experience_years": 1.5,
        "domain_experience": ["it"],
        "education_level": "bachelor",
        "english_level": 0.5,
        "portfolio": 0.6,
        "management": False,
        "work_format": ["remote", "hybrid"],
        "schedule": ["flexible", "full_time"],
        "schedule_compromise": False,
        "employment_type": ["tk", "self_employed"],
        "salary_min": 80000,
        "salary_max": 130000,
        "salary_compromise": True,
        "business_trips": 0.2,
        "relocation": "no",
        "city": "Казань",
        "tz": 3,
        "work_permit": ["RU"],
        "commute_max": 40,
        "sensitive_consent": True,
        "stress_tolerance": 0.5,
        "multitask_preference": 0.4,
        "autonomy_preference": 0.7,
        "team_size": [2, 10],
        "communication_style": "async",
        "motivation_1": "growth",
        "motivation_2": "money",
        "risk_appetite": 0.5,
        "learning_appetite": 0.8,
        "sensory_sensitivity": 0.6,
        "structure_need": 0.6,
        "neuro_profile": ["adhd"],
        "family_constraints": ["none"],
        "interests": ["games", "data"],
        "culture_fit": "casual",
        "mission_importance": 0.4,
        "career_trajectory": "deep",
        "urgency_months": 2,
        "start_date_months": 1,
    }


class TestBuildProfile:
    def test_schema_matches_engine(self):
        profile = build_profile(full_answers())
        assert profile["hard_skills"]["python"]["level"] == 0.7
        assert profile["salary"] == {"min": 80000, "max": 130000, "compromise": True}
        assert profile["location"] == {"city": "Казань", "tz": 3}
        assert profile["languages"]["english"]["level"] == 0.5
        assert profile["languages"]["russian"]["level"] == 1.0
        assert profile["motivation"]["growth"] == 1.0
        assert profile["motivation"]["money"] == 0.7
        assert profile["motivation"]["stability"] == 0.3
        assert profile["neuro_profile"] == ["adhd"]
        assert profile["business_trips_tolerance"] == 0.2

    def test_no_consent_strips_sensitive(self):
        answers = full_answers()
        answers["sensitive_consent"] = False
        profile = build_profile(answers)
        for f in ("stress_tolerance", "motivation", "neuro_profile", "sensory_sensitivity"):
            assert f not in profile
