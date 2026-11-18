"""Тесты реферальной программы: коды, анти-абьюз, лимит, команды."""
from unittest.mock import AsyncMock, MagicMock

import pytest

import marsijob.bot as bot_mod
from marsijob.config import Config
from marsijob.engine import Engine
from marsijob.payments import PaymentService, Pricing
from marsijob.storage import Storage


@pytest.fixture()
def store() -> Storage:
    s = Storage(":memory:")
    yield s
    s.close()


@pytest.fixture()
def wired(monkeypatch, store):
    monkeypatch.setattr(bot_mod, "storage", store)
    monkeypatch.setattr(bot_mod, "engine", Engine(Config.load()))
    monkeypatch.setattr(bot_mod, "pricing", Pricing.load())
    monkeypatch.setattr(bot_mod, "payments", PaymentService(store))
    monkeypatch.setattr(bot_mod, "_bot_username", "MarsiJobBot")
    yield store


def make_message(user_tg, text=""):
    m = MagicMock()
    m.from_user.id = user_tg
    m.text = text
    m.answer = AsyncMock()
    return m


class TestReferralCodes:
    def test_code_stable_and_format(self, store):
        uid = store.upsert_user(1, "candidate")
        c1 = store.get_or_create_referral_code(uid, 8)
        c2 = store.get_or_create_referral_code(uid, 8)
        assert c1 == c2
        assert len(c1) == 8
        assert all(ch.isdigit() or ch.islower() for ch in c1)

    def test_codes_unique(self, store):
        u1, u2 = store.upsert_user(1, "candidate"), store.upsert_user(2, "candidate")
        assert store.get_or_create_referral_code(u1) != store.get_or_create_referral_code(u2)


class TestAntiAbuse:
    def test_bind_new_user(self, store):
        ref = store.upsert_user(1, "candidate")
        code = store.get_or_create_referral_code(ref)
        new = store.upsert_user(2, "candidate")
        assert store.attach_referrer(new, code) == "bound"

    def test_self_referral_blocked(self, store):
        uid = store.upsert_user(1, "candidate")
        code = store.get_or_create_referral_code(uid)
        assert store.attach_referrer(uid, code) == "self"

    def test_one_referrer_forever(self, store):
        r1, r2 = store.upsert_user(1, "candidate"), store.upsert_user(2, "candidate")
        c1 = store.get_or_create_referral_code(r1)
        c2 = store.get_or_create_referral_code(r2)
        new = store.upsert_user(3, "candidate")
        assert store.attach_referrer(new, c1) == "bound"
        assert store.attach_referrer(new, c2) == "already"  # повторная привязка нет

    def test_not_new_user_no_bind(self, store):
        ref = store.upsert_user(1, "candidate")
        code = store.get_or_create_referral_code(ref)
        existing = store.upsert_user(2, "candidate")
        store.save_candidate_profile(existing, {"work_format": ["remote"]})  # есть профиль
        assert store.attach_referrer(existing, code) == "not_new"

    def test_unknown_code(self, store):
        uid = store.upsert_user(1, "candidate")
        assert store.attach_referrer(uid, "nosuchcode") == "unknown_code"


class TestReward:
    def test_credit_both_after_onboarding(self, store):
        ref, new = store.upsert_user(1, "candidate"), store.upsert_user(2, "candidate")
        store.attach_referrer(new, store.get_or_create_referral_code(ref))
        r = store.credit_referral_onboarding(new, 7, 5)
        assert r["status"] == "credited" and r["referrer_rewarded"]
        assert store.is_subscribed(ref, "plus")
        assert store.is_subscribed(new, "plus")

    def test_credit_once(self, store):
        ref, new = store.upsert_user(1, "candidate"), store.upsert_user(2, "candidate")
        store.attach_referrer(new, store.get_or_create_referral_code(ref))
        store.credit_referral_onboarding(new, 7, 5)
        r2 = store.credit_referral_onboarding(new, 7, 5)
        assert r2["status"] == "already_credited"
        sub = store.get_subscription(ref, "plus")
        assert sub["active_until"][:10] > "20"  # без двойного +7: срок не удвоился
        # точнее: 7 дней, а не 14
        from datetime import datetime, timedelta
        until = datetime.strptime(sub["active_until"], "%Y-%m-%d %H:%M:%S")
        assert until - datetime.utcnow() <= timedelta(days=7, hours=1)

    def test_monthly_limit_referrer_only(self, store):
        """Лимит 5/мес: 6-й новичок получает награду, рефerrer — нет."""
        ref = store.upsert_user(1, "candidate")
        code = store.get_or_create_referral_code(ref)
        for i in range(6):
            new = store.upsert_user(10 + i, "candidate")
            store.attach_referrer(new, code)
            r = store.credit_referral_onboarding(new, 7, 5)
            assert r["status"] == "credited"
            if i < 5:
                assert r["referrer_rewarded"]
            else:
                assert not r["referrer_rewarded"]  # лимит исчерпан
            assert store.is_subscribed(new, "plus")  # новичку — всегда

    def test_no_referral_no_reward(self, store):
        uid = store.upsert_user(1, "candidate")
        r = store.credit_referral_onboarding(uid, 7, 5)
        assert r["status"] == "no_referral"


class TestReferralCommands:
    @pytest.mark.asyncio
    async def test_referral_card(self, wired):
        uid = wired.upsert_user(77, "candidate")
        msg = make_message(77)
        await bot_mod.cmd_referral(msg)
        text = msg.answer.await_args.args[0]
        assert "https://t.me/MarsiJobBot?start=ref_" in text
        assert "0/5" in text  # месяц

    @pytest.mark.asyncio
    async def test_refstats_admin(self, wired, monkeypatch):
        wired.upsert_user(77, "candidate")
        monkeypatch.setenv("MARSIJOB_ADMIN_TG", "77")
        msg = make_message(77)
        await bot_mod.cmd_refstats(msg)
        text = msg.answer.await_args.args[0]
        assert "конверсия" in text

    @pytest.mark.asyncio
    async def test_start_ref_payload_new_user(self, wired):
        ref = wired.upsert_user(1, "candidate")
        code = wired.get_or_create_referral_code(ref)
        msg = make_message(2)
        cmd = MagicMock(args=f"ref_{code}")
        st = MagicMock(); st.clear = AsyncMock()
        await bot_mod.cmd_start(msg, st, command=cmd)
        # связка создана
        assert wired.referral_stats(ref)["invited"] == 1

    @pytest.mark.asyncio
    async def test_start_ref_payload_existing_user(self, wired):
        ref = wired.upsert_user(1, "candidate")
        code = wired.get_or_create_referral_code(ref)
        wired.upsert_user(2, "candidate")  # уже существует
        msg = make_message(2)
        cmd = MagicMock(args=f"ref_{code}")
        st = MagicMock(); st.clear = AsyncMock()
        await bot_mod.cmd_start(msg, st, command=cmd)
        assert wired.referral_stats(ref)["invited"] == 0  # не привязан
