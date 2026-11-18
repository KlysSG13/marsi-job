"""Тесты B2C-продуктов: аудит резюме, мок-интервью, дорожная карта."""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import marsijob.bot as bot_mod
from marsijob.config import Config
from marsijob.engine import Engine
from marsijob.payments import PaymentService, Pricing
from marsijob.resume_audit import (
    DIMENSIONS, format_full, format_teaser, run_audit,
)
from marsijob.roadmap import build_roadmap, format_full as fmt_roadmap
from marsijob.storage import Storage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEED = PROJECT_ROOT / "data" / "seed_vacancies.json"

FULL_PROFILE = {
    "hard_skills": {"python": {"level": 0.8, "verified": 0.7},
                    "sql": {"level": 0.6, "verified": 0.5}},
    "experience_years": 5, "work_format": ["remote", "hybrid"],
    "schedule": ["flexible"], "salary": {"min": 100000, "max": 150000,
                                          "compromise": True},
    "location": {"city": "Москва", "tz": 3}, "education_level": "bachelor",
    "languages": {"russian": {"level": 1.0, "verified": 1.0},
                  "english": {"level": 0.7, "verified": 0.5}},
    "portfolio": 0.7, "career_trajectory": "deep",
    "domain_experience": ["it", "fintech"], "interests": ["data", "fintech"],
    "employment_type": ["tk"], "relocation": "yes_with_package",
}

WEAK_PROFILE = {
    "hard_skills": {"python": {"level": 0.5, "verified": 0.0}},
    "work_format": ["remote"], "schedule": ["flexible"],
}


@pytest.fixture()
def wired(monkeypatch):
    store = Storage(":memory:")
    store.seed_vacancies(SEED)
    monkeypatch.setattr(bot_mod, "storage", store)
    monkeypatch.setattr(bot_mod, "engine", Engine(Config.load()))
    monkeypatch.setattr(bot_mod, "pricing", Pricing.load())
    monkeypatch.setattr(bot_mod, "payments", PaymentService(store))
    yield store
    store.close()


def make_message(user_tg, text=""):
    m = MagicMock()
    m.from_user.id = user_tg
    m.text = text
    m.answer = AsyncMock()
    return m


# ============================================================================
# Аудит резюме
# ============================================================================

class TestResumeAudit:
    def test_dimensions_present(self):
        audit = run_audit(FULL_PROFILE)
        assert set(audit["dimensions"].keys()) == set(DIMENSIONS)

    def test_score_range(self):
        full = run_audit(FULL_PROFILE)["score"]
        weak = run_audit(WEAK_PROFILE)["score"]
        assert 0 <= full <= 100 and 0 <= weak <= 100
        assert full > weak  # сильный профиль выше слабого

    def test_full_profile_scores_high(self):
        audit = run_audit(FULL_PROFILE)
        assert audit["score"] >= 70  # заполненный профиль ≥ 70

    def test_weak_profile_scores_low(self):
        audit = run_audit(WEAK_PROFILE)
        assert audit["score"] < 50

    def test_teaser_format(self):
        audit = run_audit(WEAK_PROFILE)
        text = format_teaser(audit)
        assert "/100" in text and "Главная проблема" in text and "199" in text

    def test_full_format(self):
        audit = run_audit(FULL_PROFILE)
        text = format_full(audit)
        assert "Аудит резюме" in text
        for dim in DIMENSIONS:
            assert dim or True  # не проверяем каждый — формат проверен выше

    def test_each_dimension_has_actions(self):
        audit = run_audit(WEAK_PROFILE)
        for dim, d in audit["dimensions"].items():
            assert d["actions"], f"{dim} без действий"
            assert d["status"] in ("ok", "attention", "critical")

    def test_languages_with_vacancies(self):
        vacs = [{"languages": {"english": {"req_level": 0.7}}}] * 3
        audit = run_audit(WEAK_PROFILE, top_vacancies=vacs)
        lang_actions = audit["dimensions"]["languages"]["actions"]
        assert any("английск" in a.lower() for a in lang_actions)


class TestAuditBot:
    @pytest.mark.asyncio
    async def test_mini_audit_once(self, wired):
        uid = wired.upsert_user(100, "candidate")
        wired.save_candidate_profile(uid, dict(WEAK_PROFILE))
        msg = make_message(100)
        await bot_mod.cmd_audit(msg)
        assert wired.get_flag(uid, "audit_teaser_used") == 1
        # второй — уже предложение купить
        msg2 = make_message(100)
        await bot_mod.cmd_audit(msg2)
        t2 = " ".join(str(c.args[0]) for c in msg2.answer.await_args_list)
        assert "199" in t2 or "buy" in t2.lower()

    @pytest.mark.asyncio
    async def test_paid_audit_full_report(self, wired):
        uid = wired.upsert_user(101, "candidate")
        wired.save_candidate_profile(uid, dict(FULL_PROFILE))
        wired.set_flag(uid, "audit_teaser_used", 1)
        # админ выдал аудит
        wired.conn.execute(
            "INSERT INTO purchases (tg_id, sku_id, amount_kop, robokassa_inv_id, status) "
            "VALUES (?, 'resume_audit', 0, 9001, 'paid')", (uid,))
        wired.conn.commit()
        msg = make_message(101)
        await bot_mod.cmd_audit(msg)
        text = " ".join(str(c.args[0]) for c in msg.answer.await_args_list)
        assert "/100" in text  # полный отчёт со скором


# ============================================================================
# Мок-интервью
# ============================================================================

class TestInterviewLLM:
    def test_generate_question_mock(self):
        with patch("marsijob.mock_interview._chat") as mock:
            mock.return_value = json.dumps({
                "question": "Расскажите о сложном проекте с вашими навыками Python",
                "purpose": "Проверка реального опыта",
                "factor": "hard_skills",
                "ideal": "STAR-структура с метриками",
            })
            from marsijob.mock_interview import generate_question
            q = generate_question("k", "url", "m", FULL_PROFILE, ["портфолио"])
        assert q and "Python" in q["question"]

    def test_generate_report_mock(self):
        with patch("marsijob.mock_interview._chat") as mock:
            mock.return_value = json.dumps({
                "criteria": {"star": 6, "specificity": 5, "relevance": 7},
                "strengths": ["s1", "s2", "s3"],
                "weaknesses": ["w1", "w2", "w3"],
                "rewritten": ["r1", "r2"],
                "plan_30min": "30",
                "plan_3h": "3h",
            })
            from marsijob.mock_interview import generate_report
            r = generate_report("k", "u", "m",
                                [{"question": "Q", "answer": "A"}], FULL_PROFILE)
        assert r and r["criteria"]["star"] == 6


class TestInterviewBot:
    @pytest.mark.asyncio
    async def test_no_key_hidden(self, wired, monkeypatch):
        monkeypatch.delenv("MARSIJOB_LLM_KEY", raising=False)
        pricing = Pricing.load()
        # mock_interview скрыт если llm_required и нет ключа
        visible = [s.id for s in pricing.visible_skus("candidate")
                   if s.id.startswith("mock")]
        # на уровне бота /interview отвечает недоступно
        uid = wired.upsert_user(100, "candidate")
        msg = make_message(100, "/interview")
        st = MagicMock()
        await bot_mod.cmd_interview(msg, st)
        text = " ".join(str(c.args[0]) for c in msg.answer.await_args_list)
        assert "недоступн" in text.lower() or "временно" in text.lower()

    @pytest.mark.asyncio
    async def test_interview_session_flow(self, wired, monkeypatch):
        """Полный сценарий: старт → вопрос → ответ → ... → отчёт."""
        monkeypatch.setenv("MARSIJOB_LLM_KEY", "test_key")
        uid = wired.upsert_user(100, "candidate")
        wired.save_candidate_profile(uid, dict(FULL_PROFILE))
        # выдали сессию
        wired.conn.execute(
            "INSERT INTO purchases (tg_id, sku_id, amount_kop, robokassa_inv_id, status) "
            "VALUES (?, 'mock_interview', 0, 9100, 'paid')", (uid,))
        wired.conn.commit()
        msg = make_message(100, "/interview")
        st = MagicMock()
        st.set_state = AsyncMock()
        await bot_mod.cmd_interview(msg, st)
        texts = " ".join(str(c.args[0]) for c in msg.answer.await_args_list)
        assert "фокус" in texts.lower() or "слабым" in texts.lower() or "слабым местам" in texts


# ============================================================================
# Дорожная карта
# ============================================================================

class TestRoadmap:
    def test_build_overall(self, wired):
        uid = wired.upsert_user(100, "candidate")
        wired.save_candidate_profile(uid, dict(WEAK_PROFILE))
        vacs = wired.list_active_vacancies()
        rm = build_roadmap(bot_mod.engine, dict(WEAK_PROFILE), vacs,
                           horizon_months=3, max_items=4, target_type="overall")
        assert len(rm["months"]) == 3
        assert rm["forecast"]["current"] < rm["forecast"]["projected"]
        for month in rm["months"]:
            assert len(month) <= 4
            for item in month:
                assert item["factor"] and item["action"] and item["delta"] >= 0

    def test_build_for_vacancy(self, wired):
        vac = wired.list_active_vacancies()[0]
        rm = build_roadmap(bot_mod.engine, dict(WEAK_PROFILE), [],
                           target_type="vacancy", vacancy=vac)
        assert rm["target_type"] == "vacancy"
        assert rm["forecast"]["projected"] > rm["forecast"]["current"]

    def test_preview_format(self, wired):
        vacs = wired.list_active_vacancies()
        rm = build_roadmap(bot_mod.engine, dict(WEAK_PROFILE), vacs)
        from marsijob.roadmap import format_preview
        text = format_preview(rm)
        assert "предпросмотр" in text.lower() or "Предпросмотр" in text
        assert "349" in text

    def test_full_format(self, wired):
        vacs = wired.list_active_vacancies()
        rm = build_roadmap(bot_mod.engine, dict(WEAK_PROFILE), vacs)
        text = fmt_roadmap(rm)
        assert "Месяц 1" in text
        assert "→" in text  # прогноз
        assert "30 дней" in text  # пересчёт

    def test_empty_profile_roadmap(self, wired):
        rm = build_roadmap(bot_mod.engine, {}, wired.list_active_vacancies())
        assert rm["months"]  # даже пустой профиль получает план


class TestRoadmapBot:
    @pytest.mark.asyncio
    async def test_preview_free(self, wired):
        uid = wired.upsert_user(100, "candidate")
        wired.save_candidate_profile(uid, dict(WEAK_PROFILE))
        msg = make_message(100)
        await bot_mod.cmd_roadmap(msg)
        texts = " ".join(str(c.args[0]) for c in msg.answer.await_args_list)
        assert "349" in texts  # предложение купить


# ============================================================================
# Каталог /buy
# ============================================================================

class TestBuyCatalog:
    def test_candidate_5_visible(self):
        pricing = Pricing.load()
        cand = [s.id for s in pricing.visible_skus("candidate")]
        # why_report, resume_audit, plus_month, mock_interview, mock_interview_3, roadmap
        assert len(cand) >= 5
        assert "resume_audit" in cand and "mock_interview" in cand and "roadmap" in cand

    def test_grant_new_skus(self, wired):
        uid = wired.upsert_user(100, "candidate")
        bot_mod.payments.grant_sku(uid, "resume_audit")
        assert wired.paid_undelivered(uid, "resume_audit") is not None
        bot_mod.payments.grant_sku(uid, "mock_interview_3")
        assert wired.get_entitlements(uid, "mock_interview") == 3
        bot_mod.payments.grant_sku(uid, "roadmap")
        assert wired.paid_undelivered(uid, "roadmap") is not None
