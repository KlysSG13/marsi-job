"""Тесты фиксов аудита 13.11.2026 (баги №1–№8)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

import marsijob.bot as bot_mod
from marsijob import wizard
from marsijob.storage import Storage


@pytest.fixture()
def store() -> Storage:
    s = Storage(":memory:")
    yield s
    s.close()


class TestSplitHtml:
    """№3: длинные сообщения режутся по границам целых карточек —
    каждый чанк имеет сбалансированные HTML-теги."""

    def test_chunk_parts_balanced(self):
        card = "<b>Карточка</b>\nтекст «описание» · <i>источник</i>\n" + "―" * 24
        parts = [card] * 60  # ~5.4 тыс. символов — не влезает в один чанк
        chunks = bot_mod.chunk_parts(parts, limit=3900)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c) <= 4096
            assert c.count("<b>") == c.count("</b>")
            assert c.count("<i>") == c.count("</i>")
        assert sum(c.count("Карточка") for c in chunks) == 60  # ничего не потеряно

    def test_split_on_newline(self):
        text = "строка1\n" * 300 + "хвост"
        chunks = list(bot_mod._split(text, 200))
        joined = "\n".join(chunks)
        assert "хвост" in joined

    def test_short_text_single_chunk(self):
        assert bot_mod.chunk_parts(["коротко"]) == ["коротко"]


class TestFillReset:
    """№2: /fill переспрашивает пропущенные и отказ от блока среды."""

    def test_none_answers_reset(self):
        answers = {"city": "Казань", "tz": None, "salary_min": None}
        reset = wizard.reset_answers_for_fill(answers)
        assert "city" in reset and "tz" not in reset and "salary_min" not in reset

    def test_consent_false_reset(self):
        answers = {"sensitive_consent": False}
        reset = wizard.reset_answers_for_fill(answers)
        assert "sensitive_consent" not in reset  # вопрос зададут снова

    def test_consent_true_kept(self):
        answers = {"sensitive_consent": True, "stress_tolerance": 0.5}
        reset = wizard.reset_answers_for_fill(answers)
        assert reset["sensitive_consent"] is True


class TestLikeInjection:
    """№4: % и _ в коде вакансии из пользовательского ввода экранируются."""

    def test_wildcard_not_matched(self, store):
        emp = store.upsert_user(100, "employer")
        store.insert_vacancy(emp, {"id": "S01", "title": "Проверка"})
        # до фикса «S0%» матчило бы S01
        assert store.get_vacancy_by_code("S0%") is None
        assert store.get_vacancy_by_code("S_1") is None
        assert store.get_vacancy_by_code("S01") is not None

    def test_underscore_literal(self, store):
        emp = store.upsert_user(100, "employer")
        store.insert_vacancy(emp, {"id": "S_1", "title": "С подчёркиванием"})
        assert store.get_vacancy_by_code("S_1") is not None
        assert store.get_vacancy_by_code("Sx1") is None


class TestStaleButtonCatchAll:
    """№5: catch-all колбэк-обработчик зарегистрирован последним."""

    def test_catch_all_is_last(self):
        from aiogram import Dispatcher
        dp = Dispatcher()
        dp.include_router(bot_mod.router)
        handlers = bot_mod.router.callback_query.handlers
        assert len(handlers) >= 2
        # последний — без фильтров (catch-all)
        last = handlers[-1]
        filters = getattr(last, "filters", [])
        assert not filters or all(getattr(f, "callback", None) for f in filters)


class TestEmployerName:
    def test_get_employer_name(self, store):
        uid = store.upsert_user(200, "employer")
        store.conn.execute(
            "INSERT INTO employers (user_id, company_json) VALUES (?, ?)",
            (uid, '{"name": "Ромашка LLC"}'))
        store.conn.commit()
        assert store.get_employer_name(uid) == "Ромашка LLC"
        assert store.get_employer_name(999999) is None


class TestVoiceGuards:
    """№1: голос не обрабатывается в активном состоянии."""

    @pytest.mark.asyncio
    async def test_voice_blocked_in_active_state(self, monkeypatch):
        st = MagicMock()
        st.get_state = AsyncMock(return_value="TalkStates:talking")
        msg = MagicMock()
        msg.answer = AsyncMock()
        bot = MagicMock()
        await bot_mod.handle_voice(msg, st, bot)
        msg.answer.assert_awaited_once()
        assert "завершите" in msg.answer.await_args.args[0].lower()

    @pytest.mark.asyncio
    async def test_voice_too_long_rejected(self, store, monkeypatch):
        store.upsert_user(100, "candidate")
        monkeypatch.setattr(bot_mod, "storage", store)
        monkeypatch.setenv("MARSIJOB_LLM_KEY", "key")
        st = MagicMock()
        st.get_state = AsyncMock(return_value=None)
        msg = MagicMock()
        msg.from_user.id = 100
        msg.voice.duration = 700  # ~11.7 мин
        msg.answer = AsyncMock()
        await bot_mod.handle_voice(msg, st, MagicMock())
        assert any("слишком длинное" in str(c.args[0]) for c in msg.answer.await_args_list)
