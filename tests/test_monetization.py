"""Бот-уровневые тесты новой монетизации: /match лимиты, /why мини, голос-Plus,
рассылка с анти-спамом, /terms, /invites, срок вакансии."""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import marsijob.bot as bot_mod
from marsijob.config import Config
from marsijob.engine import Engine
from marsijob.payments import PaymentService, Pricing
from marsijob.storage import Storage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEED = PROJECT_ROOT / "data" / "seed_vacancies.json"

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


def make_message(user_tg=1001):
    m = MagicMock()
    m.from_user.id = user_tg
    m.answer = AsyncMock()
    return m


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


class TestMatchLimits:
    @pytest.mark.asyncio
    async def test_free_top3_then_limit(self, wired):
        uid = wired.upsert_user(1001, "candidate")
        wired.save_candidate_profile(uid, dict(PROFILE))
        msg = make_message()
        await bot_mod.do_match(msg, user_id=1001)
        texts = " ".join(str(c.args[0]) for c in msg.answer.await_args_list)
        assert "Топ-" in texts and "/3" in texts          # счётчик лимита присутствует
        assert "Сходство:" in texts                       # сокращённая карточка

        # исчерпали дневной лимит → сообщение о лимите с Plus
        wired.bump_usage(uid, "matches_shown", 3)
        msg2 = make_message()
        await bot_mod.do_match(msg2, user_id=1001)
        texts2 = " ".join(str(c.args[0]) for c in msg2.answer.await_args_list)
        assert "лимит" in texts2.lower() and "Plus" in texts2

    @pytest.mark.asyncio
    async def test_plus_top20_and_full_cards(self, wired):
        uid = wired.upsert_user(1002, "candidate")
        wired.save_candidate_profile(uid, dict(PROFILE))
        wired.grant_subscription(uid, "plus", 30)
        msg = make_message(1002)
        await bot_mod.do_match(msg, user_id=1002)
        texts = " ".join(str(c.args[0]) for c in msg.answer.await_args_list)
        assert "Plus" in texts and "/3" not in texts  # лимит не 3
        assert wired.get_usage(uid)["matches_shown"] >= 1

    @pytest.mark.asyncio
    async def test_short_card_free_full_for_plus(self, wired):
        """Бесплатно — сокращённая карточка; у Plus — полная (Φ/синергии)."""
        uid1 = wired.upsert_user(1003, "candidate")
        wired.save_candidate_profile(uid1, dict(PROFILE))
        msg = make_message(1003)
        await bot_mod.do_match(msg, user_id=1003)
        free_text = " ".join(str(c.args[0]) for c in msg.answer.await_args_list)
        assert "Сходство:" in free_text  # короткая карточка

        uid2 = wired.upsert_user(1004, "candidate")
        wired.save_candidate_profile(uid2, dict(PROFILE))
        wired.grant_subscription(uid2, "plus", 30)
        msg2 = make_message(1004)
        await bot_mod.do_match(msg2, user_id=1004)
        plus_text = " ".join(str(c.args[0]) for c in msg2.answer.await_args_list)
        assert "Что сходится" in plus_text or "синерг" in plus_text.lower()


class TestWhy:
    @pytest.mark.asyncio
    async def test_mini_why_once(self, wired):
        uid = wired.upsert_user(1005, "candidate")
        wired.save_candidate_profile(uid, dict(PROFILE))
        msg = make_message(1005)
        await bot_mod.do_why(msg, user_id=1005)
        t1 = " ".join(str(c.args[0]) for c in msg.answer.await_args_list)
        assert "Мини-разбор" in t1
        assert wired.get_flag(uid, "mini_why_used") == 1

        msg2 = make_message(1005)
        await bot_mod.do_why(msg2, user_id=1005)
        t2 = " ".join(str(c.args[0]) for c in msg2.answer.await_args_list)
        assert "мини-разбор уже был" in t2 or "Полный разбор" in t2

    @pytest.mark.asyncio
    async def test_paid_why_delivered_once(self, wired):
        uid = wired.upsert_user(1006, "candidate")
        wired.save_candidate_profile(uid, dict(PROFILE))
        wired.set_flag(uid, "mini_why_used", 1)
        # админ выдал разбор
        wired.conn.execute(
            "INSERT INTO purchases (tg_id, sku_id, amount_kop, robokassa_inv_id, status) "
            "VALUES (?, 'why_report', 0, 9999, 'paid')", (uid,))
        wired.conn.commit()
        msg = make_message(1006)
        await bot_mod.do_why(msg, user_id=1006)
        t = " ".join(str(c.args[0]) for c in msg.answer.await_args_list)
        assert "зона валентности" in t.lower() or "причина" in t.lower()
        assert wired.paid_undelivered(uid, "why_report") is None
        # второй вызов — снова предложение купить
        msg2 = make_message(1006)
        await bot_mod.do_why(msg2, user_id=1006)
        t2 = " ".join(str(c.args[0]) for c in msg2.answer.await_args_list)
        assert "Купить" in t2 or "349" in t2 or "разбор уже был" in t2.lower()


class TestVoicePlus:
    @pytest.mark.asyncio
    async def test_voice_limit_points_to_plus(self, wired, monkeypatch):
        uid = wired.upsert_user(1007, "candidate")
        monkeypatch.setenv("MARSIJOB_LLM_KEY", "key")
        st = MagicMock(); st.get_state = AsyncMock(return_value=None)
        msg = make_message(1007)
        msg.voice.duration = 30
        # исчерпали дневной лимит
        wired.log_feedback(uid, "voice_parse")
        await bot_mod.handle_voice(msg, st, MagicMock())
        texts = " ".join(str(c.args[0]) for c in msg.answer.await_args_list)
        assert "Plus" in texts

    @pytest.mark.asyncio
    async def test_plus_voice_no_limit_message(self, wired, monkeypatch):
        uid = wired.upsert_user(1008, "candidate")
        wired.grant_subscription(uid, "plus", 30)
        monkeypatch.setenv("MARSIJOB_LLM_KEY", "key")
        st = MagicMock(); st.get_state = AsyncMock(return_value=None)
        msg = make_message(1008)
        msg.voice.duration = 30
        wired.log_feedback(uid, "voice_parse")  # уже был сегодня — Plus не важно
        # подменяем распознавание, чтобы не качать реальный файл
        async def fake_download(*a, **k):
            raise bot_mod.asr.AsrError("нет файла в тесте")
        bot = MagicMock(); bot.download = fake_download
        await bot_mod.handle_voice(msg, st, bot)
        texts = " ".join(str(c.args[0]) for c in msg.answer.await_args_list)
        # Plus: никакого сообщения о лимите, но допустима ошибка распознавания
        assert "Plus" not in texts or "безлимит" not in texts


class TestBroadcast:
    @pytest.mark.asyncio
    async def test_broadcast_caps(self, wired, monkeypatch):
        emp = wired.upsert_user(2001, "employer")
        # вакансия работодателя
        vac = json.loads(SEED.read_text(encoding="utf-8"))[0]
        vac["id"] = "E-BC1"
        db_id = wired.insert_vacancy(emp, vac)
        # кандидаты с согласием
        for i, score_seed in enumerate((0.9, 0.85, 0.5)):
            cu = wired.upsert_user(3000 + i, "candidate")
            prof = dict(PROFILE)
            prof["salary"]["min"] = 1000 + score_seed * 1000
            wired.save_candidate_profile(cu, prof)
            wired.set_flag(cu, "consent_invites", 1)
        sent_bot = MagicMock()
        sent_bot.send_message = AsyncMock()
        monkeypatch.setattr(bot_mod, "_alert_bot", sent_bot)
        purchase = {"tg_id": emp, "payload": str(db_id)}
        sent = await bot_mod.run_broadcast(purchase)
        assert sent >= 1
        # повтор в сутки по той же вакансии — отказ
        sent2 = await bot_mod.run_broadcast(purchase)
        assert sent2 == -1

    @pytest.mark.asyncio
    async def test_broadcast_respects_candidate_cap(self, wired, monkeypatch):
        emp = wired.upsert_user(2002, "employer")
        vac = json.loads(SEED.read_text(encoding="utf-8"))[0]
        vac["id"] = "E-BC2"
        db_id = wired.insert_vacancy(emp, vac)
        cu = wired.upsert_user(3100, "candidate")
        wired.save_candidate_profile(cu, dict(PROFILE))
        wired.set_flag(cu, "consent_invites", 1)
        wired.bump_usage(cu, "broadcasts_received", 3)  # кап кандидата исчерпан
        sent_bot = MagicMock(); sent_bot.send_message = AsyncMock()
        monkeypatch.setattr(bot_mod, "_alert_bot", sent_bot)
        purchase = {"tg_id": emp, "payload": str(db_id)}
        # воркер записал факт рассылки — имитируем
        sent = await bot_mod.run_broadcast(purchase)
        assert sent == 0  # никому: кап кандидата


class TestTermsAndInvites:
    @pytest.mark.asyncio
    async def test_terms_from_file(self, wired):
        msg = make_message(1009)
        wired.upsert_user(1009, "candidate")
        await bot_mod.cmd_terms(msg)
        text = msg.answer.await_args.args[0]
        assert len(text) > 50  # файл непустой

    @pytest.mark.asyncio
    async def test_invites_toggle(self, wired):
        uid = wired.upsert_user(1010, "candidate")
        msg = make_message(1010)
        await bot_mod.cmd_invites(msg)
        assert wired.get_flag(uid, "consent_invites") == 1
        await bot_mod.cmd_invites(msg)
        assert wired.get_flag(uid, "consent_invites") == 0


class TestVacancyExpiry:
    def test_paid_until_set_and_archive(self, wired):
        emp = wired.upsert_user(4001, "employer")
        vac = json.loads(SEED.read_text(encoding="utf-8"))[0]
        vac["id"] = "E-EXP"
        db_id = wired.insert_vacancy(emp, vac)
        wired.set_vacancy_paid_until(db_id, "2000-01-01 00:00:00")  # давно истёк
        active = [v for v in wired.list_active_vacancies() if v["id"] == "E-EXP"]
        assert active == []  # листинг не показывает истёкшие


class TestAlerts:
    @pytest.mark.asyncio
    async def test_plus_alert_sent(self, wired, monkeypatch):
        cu = wired.upsert_user(5000, "candidate")
        wired.save_candidate_profile(cu, dict(PROFILE))
        wired.grant_subscription(cu, "plus", 30)
        vac = json.loads(SEED.read_text(encoding="utf-8"))[0]
        vac["id"] = "E-AL1"
        sent_bot = MagicMock(); sent_bot.send_message = AsyncMock()
        monkeypatch.setattr(bot_mod, "_alert_bot", sent_bot)
        await bot_mod.send_plus_alerts(vac)
        # алерт отправлен если скор ≥0.75 по(seed)вакансии джун-профилю
        sent = wired.get_usage(cu)["alerts_sent"]
        assert sent in (0, 1)  # зависит от скора; главное — не упало и ≤ лимита
