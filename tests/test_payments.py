"""Тесты монетизации (новая модель): прайс, подписки, приглашения, вебхук, гранты."""
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from marsijob.payments import (
    PaymentService, Pricing, PricingError, build_payment_url,
    verify_result_signature,
)
from marsijob.storage import Storage

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def store() -> Storage:
    s = Storage(":memory:")
    yield s
    s.close()


@pytest.fixture()
def pricing() -> Pricing:
    return Pricing.load()


@pytest.fixture()
def user_id(store) -> int:
    return store.upsert_user(4242, "candidate")


class TestPricing:
    def test_prices_from_config(self, pricing):
        assert pricing.sku("why_report").price_kop == 34900
        assert pricing.sku("plus_month").price_kop == 19900
        assert pricing.sku("pro_month").price_kop == 490000
        assert pricing.sku("business_month").hidden is True
        assert pricing.limits["candidate_free_matches_per_day"] == 3
        assert pricing.limits["candidate_plus_matches_per_day"] == 20

    def test_no_magic_prices(self):
        """Цены только в конфиге: код не содержит рублей."""
        src = (PROJECT_ROOT / "src" / "marsijob" / "payments.py").read_text(encoding="utf-8")
        assert "349" not in src.replace("34900", "").replace("price_kop", "")
        bot = (PROJECT_ROOT / "src" / "marsijob" / "bot.py").read_text(encoding="utf-8")
        # в боте цены только в текстах-подсказках каталога не должны хардкодиться
        assert "пакет распознаваний голоса" not in bot.lower()
        assert "99 ₽/10" not in bot and "499 ₽/10" not in bot

    def test_promo_price(self, pricing):
        assert pricing.effective_price_kop("why_report") == 34900  # промо выключен
        pricing.promo["enabled"] = True
        assert pricing.effective_price_kop("why_report") == 29900
        assert pricing.effective_price_kop("plus_month") == 19900  # не для этого SKU

    def test_visible_skus_by_audience(self, pricing):
        cand = [s.id for s in pricing.visible_skus("candidate")]
        emp = [s.id for s in pricing.visible_skus("employer")]
        assert set(cand) == {"why_report", "resume_audit", "plus_month",
                             "mock_interview", "mock_interview_3", "roadmap"}
        assert set(emp) == {"invite_5", "invite_15", "invite_40",
                            "target_broadcast", "pro_month"}
        assert "business_month" not in cand + emp  # скрытый SKU


class TestRobokassa:
    LOGIN, P1, P2 = "demo", "pass1", "pass2"

    def test_payment_url_kop_to_rub(self):
        url = build_payment_url(self.LOGIN, self.P1, 34900, 5001, "тест")
        assert "OutSum=349.00" in url
        expected = hashlib.md5("demo:349.00:5001:pass1".encode()).hexdigest()
        assert f"SignatureValue={expected}" in url

    def test_result_signature(self):
        sig = hashlib.md5("199.00:5001:pass2".encode()).hexdigest()
        assert verify_result_signature(self.P2, "199.00", 5001, sig)
        assert not verify_result_signature(self.P2, "199.00", 5001, "bad")


class TestGrants:
    def test_subscription_grant_stacks(self, store, user_id):
        svc = PaymentService(store)
        store.grant_subscription(user_id, "plus", 10)
        store.grant_subscription(user_id, "plus", 15)
        sub = store.get_subscription(user_id, "plus")
        from datetime import datetime, timedelta
        until = datetime.strptime(sub["active_until"], "%Y-%m-%d %H:%M:%S")
        assert timedelta(days=24) < until - datetime.utcnow() <= timedelta(days=25)

    def test_expired_subscription_degrades(self, store, user_id):
        store.grant_subscription(user_id, "plus", 5)
        store.conn.execute(
            "UPDATE subscriptions SET active_until = datetime('now', '-1 day') "
            "WHERE tg_id = ? AND plan = 'plus'", (user_id,))
        store.conn.commit()
        assert not store.is_subscribed(user_id, "plus")

    def test_paid_webhook_grants(self, store, user_id):
        svc = PaymentService(store)
        svc.configure("demo", "p1", "p2")
        inv = svc.create_invoice(user_id, "invite_15")
        sig = hashlib.md5(f"1290.00:{inv.invoice_id}:p2".encode()).hexdigest()
        ok, msg, purchase = svc.handle_result("1290.00", inv.invoice_id, sig)
        assert ok, msg
        assert store.get_entitlements(user_id, "invite") == 15
        # идемпотентность: повтор без двойного начисления
        ok2, msg2, _ = svc.handle_result("1290.00", inv.invoice_id, sig)
        assert ok2 and msg2 == "уже оплачен"
        assert store.get_entitlements(user_id, "invite") == 15

    def test_subscription_webhook(self, store, user_id):
        svc = PaymentService(store)
        svc.configure("demo", "p1", "p2")
        inv = svc.create_invoice(user_id, "plus_month")
        sig = hashlib.md5(f"199.00:{inv.invoice_id}:p2".encode()).hexdigest()
        ok, _, _ = svc.handle_result("199.00", inv.invoice_id, sig)
        assert ok
        assert store.is_subscribed(user_id, "plus")

    def test_bad_signature_no_grant(self, store, user_id):
        svc = PaymentService(store)
        svc.configure("demo", "p1", "p2")
        inv = svc.create_invoice(user_id, "invite_5")
        ok, msg, _ = svc.handle_result("490.00", inv.invoice_id, "bad")
        assert not ok
        assert store.get_entitlements(user_id, "invite") == 0

    def test_why_report_undelivered(self, store, user_id):
        svc = PaymentService(store)
        svc.configure("demo", "p1", "p2")
        inv = svc.create_invoice(user_id, "why_report")
        sig = hashlib.md5(f"349.00:{inv.invoice_id}:p2".encode()).hexdigest()
        svc.handle_result("349.00", inv.invoice_id, sig)
        assert store.paid_undelivered(user_id, "why_report") is not None
        store.mark_delivered(store.paid_undelivered(user_id, "why_report")["id"])
        assert store.paid_undelivered(user_id, "why_report") is None

    def test_grant_sku_admin(self, store, user_id, pricing):
        svc = PaymentService(store, pricing)
        assert "подписка plus" in svc.grant_sku(user_id, "plus_month")
        assert store.is_subscribed(user_id, "plus")
        assert "15 приглашений" in svc.grant_sku(user_id, "invite_15")
        assert store.get_entitlements(user_id, "invite") == 15
        with pytest.raises(PricingError):
            svc.grant_sku(user_id, "no_such_sku")

    def test_broadcast_entitlement_via_grant(self, store, user_id, pricing):
        svc = PaymentService(store, pricing)
        svc.grant_sku(user_id, "target_broadcast")
        assert store.get_entitlements(user_id, "broadcast") == 1


class TestInvites:
    def test_consent_required(self, store):
        cand = store.upsert_user(100, "candidate")
        assert store.get_flag(cand, "consent_invites") == 0
        store.set_flag(cand, "consent_invites", 1)
        assert store.get_flag(cand, "consent_invites") == 1

    def test_consume_order_and_no_negative(self, store):
        emp = store.upsert_user(200, "employer")
        assert store.consume_entitlement(emp, "invite") is False  # пустой баланс
        store.add_entitlements(emp, "invite", 2)
        assert store.consume_entitlement(emp, "invite")
        assert store.consume_entitlement(emp, "invite")
        assert store.consume_entitlement(emp, "invite") is False
        assert store.get_entitlements(emp, "invite") == 0

    def test_pro_quota(self, store):
        emp = store.upsert_user(200, "employer")
        assert store.pro_invite_quota_left(emp, 50) == 0  # без подписки
        store.grant_subscription(emp, "pro", 30)
        assert store.pro_invite_quota_left(emp, 50) == 50
        store.pro_invite_used(emp)
        assert store.pro_invite_quota_left(emp, 50) == 49
        store.grant_subscription(emp, "pro", 30)  # продление обнуляет
        assert store.pro_invite_quota_left(emp, 50) == 50


class TestDailyUsage:
    def test_matches_limit_free_vs_plus(self, store):
        uid = store.upsert_user(300, "candidate")
        assert store.get_usage(uid)["matches_shown"] == 0
        store.bump_usage(uid, "matches_shown", 3)
        assert store.get_usage(uid)["matches_shown"] == 3
        # Plus не расширяет счётчик сам — лимит считается ботом по подписке
        store.grant_subscription(uid, "plus", 30)
        assert store.is_subscribed(uid, "plus")

    def test_reminder_once_per_day(self, store):
        uid = store.upsert_user(300, "candidate")
        store.grant_subscription(uid, "plus", 2)  # истекает через 2 дня
        first = store.subscription_reminder_due(uid, 3)
        assert first is not None and first["plan"] == "plus"
        second = store.subscription_reminder_due(uid, 3)
        assert second is None  # уже напоминали сегодня


class TestWebhookRoutes:
    async def test_result_endpoint_grants(self, store, aiohttp_client):
        from marsijob.web import build_app
        svc = PaymentService(store)
        svc.configure("demo", "p1", "p2")
        uid = store.upsert_user(500, "employer")
        inv = svc.create_invoice(uid, "invite_5")
        sig = hashlib.md5(f"490.00:{inv.invoice_id}:p2".encode()).hexdigest()
        client = await aiohttp_client(build_app(svc))
        resp = await client.get("/pay/robokassa/result", params={
            "OutSum": "490.00", "InvId": str(inv.invoice_id),
            "SignatureValue": sig})
        assert resp.status == 200
        assert await resp.text() == f"OK{inv.invoice_id}"
        assert store.get_entitlements(uid, "invite") == 5

    async def test_terms_page(self, aiohttp_client):
        from marsijob.web import build_app
        store = Storage(":memory:")
        try:
            client = await aiohttp_client(build_app(PaymentService(store)))
            resp = await client.get("/legal/oferta")
            assert resp.status == 200
        finally:
            store.close()
