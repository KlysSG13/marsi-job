"""Тесты юридического пакета: рендер документов, маршруты, интеграция."""
import pytest

from marsijob import legal
from marsijob.payments import PaymentService
from marsijob.storage import Storage


class TestRender:
    def test_oferta_renders_with_config(self):
        text = legal.render("oferta.html")
        assert "Договор-оферта" in text or "Публичная оферта" in text
        assert "{{" not in text  # все плейсхолдеры подставлены
        assert "/legal/privacy" in text  # ссылка на политику есть

    def test_privacy_renders_with_config(self):
        text = legal.render("privacy.html")
        assert "152" in text
        assert "{{" not in text

    def test_requisites_rendered(self):
        """Реквизиты подставляются в оба документа (из data/legal.json)."""
        oferta = legal.render("oferta.html")
        privacy = legal.render("privacy.html")
        for text in (oferta, privacy):
            assert "{{" not in text  # подстановка полная
        # адрес отсутствует в публичных документах
        assert "Адрес:" not in oferta and "адрес:" not in privacy

    def test_self_employed_specifics(self):
        """Документы адаптированы под статус самозанятого (НПД)."""
        oferta = legal.render("oferta.html")
        privacy = legal.render("privacy.html")
        assert "профессиональный доход" in oferta
        assert "Мой налог" in oferta and "Мой налог" in privacy
        assert "Робочеки СМЗ" in oferta  # чеки по 422-ФЗ

    def test_legal_urls(self):
        urls = legal.legal_urls()
        assert urls["oferta"].endswith("/legal/oferta")
        assert urls["privacy"].endswith("/legal/privacy")


class TestWebRoutes:
    async def test_legal_pages_served(self, aiohttp_client):
        from marsijob.web import build_app
        store = Storage(":memory:")
        try:
            client = await aiohttp_client(build_app(PaymentService(store)))
            r1 = await client.get("/legal/oferta")
            assert r1.status == 200
            body = await r1.text()
            assert "оферта" in body.lower()
            r2 = await client.get("/legal/privacy")
            assert r2.status == 200
            assert "конфиденциальности" in (await r2.text()).lower()
            r3 = await client.get("/legal")
            assert r3.status == 200
        finally:
            store.close()
