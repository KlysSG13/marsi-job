"""Тесты свежести вакансий: пороги 7/30/45, авто-архив, fallback, дедуп, cooldown."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import marsijob.bot as bot_mod
from marsijob.config import Config
from marsijob.engine import Engine
from marsijob.payments import PaymentService, Pricing
from marsijob.storage import Storage

PROFILE = {
    "hard_skills": {"python": {"level": 0.7, "verified": 0.5}},
    "experience_years": 3, "work_format": ["remote", "hybrid", "office"],
    "schedule": ["full_time", "flexible"], "schedule_compromise": False,
    "employment_type": ["tk"], "salary": {"min": 50000, "max": 150000,
                                          "compromise": True},
    "business_trips_tolerance": 0.3, "relocation": "no",
    "location": {"city": "Москва", "tz": 3}, "work_permit": ["RU"],
    "languages": {"russian": {"level": 1.0, "verified": 1.0}},
    "management_experience": {"has": False, "team_size": 0},
    "education_level": "bachelor", "domain_experience": ["it"],
    "portfolio": 0.5, "interests": [], "culture_fit": "casual",
    "mission_importance": 0.3, "career_trajectory": "deep",
    "urgency_months": 2, "start_date_months": 1,
}

FRESHNESS = {"fresh_days": 7, "stale_days": 30, "auto_archive_days": 45,
             "min_fallback_results": 3, "reactivate_cooldown_days": 30}


@pytest.fixture()
def wired(monkeypatch):
    store = Storage(":memory:")
    monkeypatch.setattr(bot_mod, "storage", store)
    monkeypatch.setattr(bot_mod, "engine", Engine(Config.load()))
    monkeypatch.setattr(bot_mod, "pricing", Pricing.load())
    monkeypatch.setattr(bot_mod, "payments", PaymentService(store))
    yield store
    store.close()


def seed_vacancy(store, emp_id, vid, days_old):
    db_id = store.insert_vacancy(emp_id, {
        "id": vid, "title": f"Вакансия {vid}",
        "hard_skills": {"python": {"req_level": 0.6, "must": False}},
        "experience_years_min": 1, "domain": "it", "education_level_min": None,
        "portfolio_required": 0.2, "work_format": "remote", "schedule": "flexible",
        "employment_type": "tk", "salary": {"min": 60000, "max": 120000},
        "business_trips_required": 0.0, "relocation_supported": False,
        "location": {"city": None, "tz": 3}, "work_permit_required": ["RU"],
        "commute_minutes": None, "sensory_load": 0.3, "role_ambiguity": 0.4,
        "stress_level": 0.4, "multitask_intensity": 0.4,
        "micromanagement_degree": 0.3, "learning_intensity": 0.5,
        "stage_risk": 0.4, "mission_clarity": 0.5, "team_size": 6,
        "communication_mode": "async", "culture": "casual",
        "trajectory_offered": "deep",
        "motivation_offered": {"money": 0.6, "growth": 0.7, "stability": 0.5,
                               "mission": 0.4, "recognition": 0.4},
        "training_offered": False, "vacancy_criticality": 0.5,
        "vacancy_clarity": 0.8, "product_tags": ["it"],
        "urgency_months": 2, "start_date_required_months": 1,
    })
    store.conn.execute(
        "UPDATE vacancies SET updated_at = datetime('now', ?), "
        "created_at = datetime('now', ?) WHERE id = ?",
        (f"-{days_old} days", f"-{days_old} days", db_id))
    store.conn.commit()
    return db_id


def make_message(user_tg):
    m = MagicMock()
    m.from_user.id = user_tg
    m.answer = AsyncMock()
    return m


class TestFreshnessThresholds:
    def test_age_days(self, wired):
        emp = wired.upsert_user(100, "employer")
        db1 = seed_vacancy(wired, emp, "V1", 5)
        db2 = seed_vacancy(wired, emp, "V2", 40)
        v1 = [v for v in wired.list_active_vacancies() if v["id"] == "V1"][0]
        v2 = [v for v in wired.list_active_vacancies() if v["id"] == "V2"][0]
        assert wired.vacancy_age_days(v1) <= 7
        assert 30 < wired.vacancy_age_days(v2) <= 45

    def test_auto_archive_over_45(self, wired):
        emp = wired.upsert_user(100, "employer")
        seed_vacancy(wired, emp, "OLD", 50)
        seed_vacancy(wired, emp, "NEW", 5)
        archived = wired.archive_stale_vacancies(45)
        assert archived == 1
        active_ids = [v["id"] for v in wired.list_active_vacancies()]
        assert "NEW" in active_ids and "OLD" not in active_ids

    def test_freshness_stats_buckets(self, wired):
        emp = wired.upsert_user(100, "employer")
        seed_vacancy(wired, emp, "F1", 3)   # 0-7
        seed_vacancy(wired, emp, "F2", 15)  # 8-30
        seed_vacancy(wired, emp, "F3", 40)  # 31-45
        stats = wired.freshness_stats()
        f = stats["freshness"]
        assert f["0-7"] >= 1 and f["8-30"] >= 1 and f["31-45"] >= 1


class TestReactivation:
    def test_reactivate_ok_then_cooldown(self, wired):
        emp = wired.upsert_user(100, "employer")
        db = seed_vacancy(wired, emp, "R1", 50)
        wired.archive_stale_vacancies(45)
        ok, why = wired.reactivate_vacancy(db, 30)
        assert ok
        # снова в архив и повторная попытка — cooldown
        wired.archive_stale_vacancies(45)
        ok2, why2 = wired.reactivate_vacancy(db, 30)
        assert not ok2 and why2 == "cooldown"

    def test_paid_reactivate_no_cooldown(self, wired):
        emp = wired.upsert_user(100, "employer")
        wired.grant_subscription(emp, "pro", 30)
        db = seed_vacancy(wired, emp, "R2", 50)
        wired.archive_stale_vacancies(45)
        ok1, _ = wired.reactivate_vacancy(db, 30, is_paid=True)
        assert ok1
        # снова состарили и заархивировали — Pro реактивирует без cooldown
        wired.conn.execute(
            "UPDATE vacancies SET updated_at = datetime('now', '-50 days') WHERE id = ?",
            (db,))
        wired.conn.commit()
        wired.archive_stale_vacancies(45)
        ok2, why2 = wired.reactivate_vacancy(db, 30, is_paid=True)
        assert ok2 and why2 == "ok"  # бесплатным тут был бы cooldown

    def test_touch_resets_age(self, wired):
        emp = wired.upsert_user(100, "employer")
        db = seed_vacancy(wired, emp, "T1", 40)
        wired.touch_vacancy(db)
        v = [x for x in wired.list_active_vacancies() if x["db_id"] == db][0]
        assert wired.vacancy_age_days(v) <= 1


class TestDedup:
    def test_dedup_imported(self, wired):
        emp = wired.ensure_external_employer("trudvsem.ru")
        base = {"source": "trudvsem", "employer_name": "ООО Ромашка",
                "location": {"city": "Москва", "tz": 3},
                "title": "Врач-терапевт", "work_format": "office",
                "schedule": "full_time", "salary": {"min": 50000, "max": 80000},
                "hard_skills": {}, "experience_years_min": 3, "domain": "healthcare",
                "education_level_min": None, "portfolio_required": 0,
                "employment_type": "tk", "business_trips_required": 0,
                "relocation_supported": False, "work_permit_required": ["RU"],
                "commute_minutes": None, "vacancy_clarity": 0.6,
                "trust_index": 0.6, "urgency_months": 2,
                "start_date_required_months": 1}
        v1 = dict(base, id="TV-aaa")
        v2 = dict(base, id="TV-bbb")  # дубль (та же тройка)
        wired.upsert_external_vacancy(v1, emp)
        wired.upsert_external_vacancy(v2, emp)
        deduped = wired.dedup_imported_vacancies()
        assert deduped >= 1
        active = [v["id"] for v in wired.list_active_vacancies()
                  if str(v.get("id", "")).startswith("TV-")]
        assert len(active) == 1  # осталась последняя


class TestMatchFreshnessFilter:
    @pytest.mark.asyncio
    async def test_stale_hidden_without_fallback(self, wired):
        uid = wired.upsert_user(300, "candidate")
        wired.save_candidate_profile(uid, dict(PROFILE))
        emp = wired.upsert_user(100, "employer")
        # 3 свежие + 2 старые — старые не показываются
        for i, days in enumerate((1, 2, 3, 40, 50)):
            seed_vacancy(wired, emp, f"S{i}", days)
        msg = make_message(300)
        await bot_mod.do_match(msg, user_id=300)
        text = " ".join(str(c.args[0]) for c in msg.answer.await_args_list)
        assert "S0" in text or "Вакансия S" in text
        assert "без обновления" not in text  # старые не попали

    @pytest.mark.asyncio
    async def test_fallback_when_less_than_three(self, wired):
        uid = wired.upsert_user(301, "candidate")
        wired.save_candidate_profile(uid, dict(PROFILE))
        emp = wired.upsert_user(100, "employer")
        # только 1 свежая и 2 старые: fallback покажет старые с пометкой
        seed_vacancy(wired, emp, "F0", 1)
        seed_vacancy(wired, emp, "F1", 35)
        seed_vacancy(wired, emp, "F2", 42)
        msg = make_message(301)
        await bot_mod.do_match(msg, user_id=301)
        text = " ".join(str(c.args[0]) for c in msg.answer.await_args_list)
        # старые подтянулись fallback-ом
        assert "F1" in text or "F2" in text or "без обновления" in text


class TestVacancyCard:
    def test_badge_and_updated_line(self):
        from marsijob.explain import freshness_badge, updated_line
        assert freshness_badge({"_age_days": 2}) == " 🆕"
        assert freshness_badge({"_age_days": 15}) == ""
        assert freshness_badge({"_age_days": 35}) == " ⚠ 35 дн. без обновления"
        assert freshness_badge({}) == ""
        assert updated_line({"_age_days": 5}) == "Обновлено: 5 дн. назад"
        assert updated_line({}) == ""


class TestMyVacancies:
    @pytest.mark.asyncio
    async def test_list_with_status(self, wired):
        emp = wired.upsert_user(100, "employer")
        seed_vacancy(wired, emp, "MV1", 3)
        seed_vacancy(wired, emp, "MV2", 50)
        wired.archive_stale_vacancies(45)
        msg = make_message(100)
        await bot_mod.cmd_myvacancies(msg)
        text = msg.answer.await_args.args[0]
        assert "активна" in text and "архив" in text
        assert "обновлено" in text.lower() or "Обновлено" in text
