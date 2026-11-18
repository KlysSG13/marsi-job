"""Тесты итеративного цикла: фидбек, индекс доверия, заморозка пар."""
import json
from pathlib import Path

import pytest

from marsijob.config import Config
from marsijob.engine import Engine
from marsijob.feedback import (
    apply_candidate_feedback, epsilon_penalty, should_freeze, trust_delta,
)
from marsijob.questionnaire import build_profile
from marsijob.storage import Storage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEED = PROJECT_ROOT / "data" / "seed_vacancies.json"

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


class TestFeedbackRules:
    def test_format_dislike_bumps(self):
        defaults = {"work_format": 0.85, "schedule": 0.80, "salary": 0.9}
        fb = apply_candidate_feedback(None, "dislike_format", defaults)
        assert fb["weights"]["work_format"] == pytest.approx(min(1.0, 0.85 * 1.2))
        assert fb["weights"]["schedule"] == pytest.approx(min(1.0, 0.80 * 1.2))
        assert "salary" not in fb["weights"]

    def test_cap_at_one(self):
        defaults = {"work_format": 0.95}
        fb = apply_candidate_feedback({"work_format": 0.95}, "dislike_format", defaults)
        assert fb["weights"]["work_format"] == 1.0

    def test_dislike_format_edits_profile(self):
        """Дислайк формата убирает значение вакансии из допустимых (бинарный фактор)."""
        defaults = {"work_format": 0.85, "schedule": 0.8}
        profile = {"work_format": ["remote", "hybrid", "office"],
                   "schedule": ["flexible", "full_time"]}
        vacancy = {"work_format": "office", "schedule": "full_time"}
        fb = apply_candidate_feedback(None, "dislike_format", defaults,
                                     vacancy=vacancy, profile=profile)
        assert fb["profile_edits"]["work_format"] == ["remote", "hybrid"]
        assert fb["profile_edits"]["schedule"] == ["flexible"]

    def test_no_edits_without_vacancy(self):
        fb = apply_candidate_feedback(None, "dislike_format", {"work_format": 0.85})
        assert fb["profile_edits"] == {}

    def test_trust_deltas(self):
        assert trust_delta("no_response") == -0.1
        assert trust_delta("interview_ok") == +0.1
        assert trust_delta("invite") == 0.0

    def test_epsilon_penalty(self):
        assert epsilon_penalty("no_response", 1) == 0.02
        assert epsilon_penalty("interview_ok", 5) == 0.0


class TestFreeze:
    def test_freeze_after_stable_iterations(self):
        frozen, iters = should_freeze(None, 0.5, 0)
        assert not frozen and iters == 1
        frozen, iters = should_freeze(0.5, 0.505, iters)
        assert not frozen and iters == 2
        frozen, iters = should_freeze(0.505, 0.51, iters)
        assert frozen and iters == 3

    def test_reset_on_big_change(self):
        frozen, iters = should_freeze(None, 0.5, 0)
        frozen, iters = should_freeze(0.5, 0.9, iters)
        assert not frozen and iters == 1  # большой сдвиг сбрасывает счётчик


class TestIterationPipeline:
    def test_dislike_format_excludes_office(self, store, engine):
        """После дислайка «не мой формат» офисные вакансии блокируются."""
        cand = store.upsert_user(100, "candidate")
        profile = build_profile(ANSWERS)
        profile["work_format"] = ["remote", "hybrid", "office"]
        store.save_candidate_profile(cand, profile)

        vacancies = store.list_active_vacancies()
        office = [v for v in vacancies if v["work_format"] == "office"]
        if not office:
            pytest.skip("нет офисных вакансий в seed")
        office = office[0]

        assert not engine.score(profile, office).blocked  # до фидбека — допустимо
        defaults = {fid: f["w0"] for fid, f in engine.cfg.factors.items()}
        fb = apply_candidate_feedback(None, "dislike_format", defaults,
                                      vacancy=office, profile=profile)
        profile.update(fb["profile_edits"])
        result = engine.score(profile, office)
        assert result.blocked, "офис должен уйти в блокировку BL-002 после дислайка"

    def test_dislike_domain_changes_scores(self, store, engine):
        """Дислайк сферы меняет скоринг: вакансии вне сферы теряют больше."""
        cand = store.upsert_user(100, "candidate")
        profile = build_profile(ANSWERS)
        store.save_candidate_profile(cand, profile)
        vacancies = store.list_active_vacancies()
        other_domain = [v for v in vacancies if v.get("domain") not in profile["domain_experience"]]
        if not other_domain:
            pytest.skip("нет вакансий чужой сферы")
        vac = other_domain[0]
        defaults = {fid: f["w0"] for fid, f in engine.cfg.factors.items()}
        fb = apply_candidate_feedback(None, "dislike_domain", defaults)
        before = engine.score(profile, vac).score
        after = engine.score(profile, vac, weight_overrides=fb["weights"]).score
        assert after < before, "чужая сфера должна опуститься после дислайка"

    def test_no_response_lowers_score(self, store, engine):
        """Серия «отклик без ответа» снижает скор вакансии (trust + ε)."""
        cand = store.upsert_user(100, "candidate")
        profile = build_profile(ANSWERS)
        store.save_candidate_profile(cand, profile)
        vacancies = store.list_active_vacancies()
        vac = dict(vacancies[0])
        vac["trust_index"] = 0.7
        before = engine.score(profile, vac).score
        vac["trust_index"] = 0.4     # −0.1 трижды
        vac["epsilon_extra"] = 0.06  # +0.02 трижды
        after = engine.score(profile, vac).score
        assert after < before

    def test_match_freeze_flow(self, store, engine):
        """Три пересчёта без изменений — пара замораживается и выпадает из /match."""
        cand = store.upsert_user(100, "candidate")
        profile = build_profile(ANSWERS)
        store.save_candidate_profile(cand, profile)
        vac = store.list_active_vacancies()[0]

        res = engine.score(profile, vac)
        out1 = store.save_match(cand, vac["db_id"], res.breakdown())
        assert not out1["frozen"]
        out2 = store.save_match(cand, vac["db_id"], res.breakdown())
        assert not out2["frozen"]
        out3 = store.save_match(cand, vac["db_id"], res.breakdown())
        assert out3["frozen"]
        assert store.is_match_frozen(cand, vac["db_id"])

        # большой сдвиг скора размораживает (счётчик сбрасывается)
        changed = res.breakdown()
        changed["score"] = res.score + 0.1
        out4 = store.save_match(cand, vac["db_id"], changed)
        assert not out4["frozen"]

    def test_full_cycle(self, store, engine):
        """Сценарий: /match → /dislike → /match с новыми весами."""
        cand = store.upsert_user(100, "candidate")
        profile = build_profile(ANSWERS)
        store.save_candidate_profile(cand, profile)

        vacancies = store.list_active_vacancies()
        weights_before = sorted(
            [(r.vacancy_id, r.score) for r in
             (engine.score(profile, v) for v in vacancies)],
            key=lambda t: -t[1],
        )
        from marsijob.feedback import apply_candidate_feedback
        defaults = {fid: f["w0"] for fid, f in engine.cfg.factors.items()}
        store.save_candidate_weights(
            cand, apply_candidate_feedback(None, "dislike_domain", defaults)["weights"]
        )
        overrides = store.get_candidate_weights(cand)
        weights_after = sorted(
            [(r.vacancy_id, r.score) for r in
             (engine.score(profile, v, weight_overrides=overrides) for v in vacancies)],
            key=lambda t: -t[1],
        )
        assert weights_before != weights_after, "фидбек должен менять ранжирование"
        assert all(s >= 0 for _v, s in weights_after)
