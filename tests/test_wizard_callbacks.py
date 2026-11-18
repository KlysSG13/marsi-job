"""Регрессионный тест бага с часовым поясом (инлайн-кнопки в волшебнике).

Причина бага 13.11.2026: cb_wz_single передавал callback.message (сообщение
БОТА) в finish_wizard, где брался message.from_user.id → id бота →
storage.get_user(bot_id) → None → TypeError, кнопки «не реагировали».
Колбэки обязаны передавать callback.from_user.id.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

import marsijob.bot as bot_mod
from marsijob import ui
from marsijob.config import Config
from marsijob.engine import Engine
from marsijob.storage import Storage

USER_TG = 555000111
BOT_TG = 8765982912


class FakeState:
    def __init__(self, data):
        self._data = dict(data)
        self.cleared = False

    async def get_data(self):
        return self._data

    async def update_data(self, **kw):
        self._data.update(kw)

    async def set_state(self, s):
        pass

    async def clear(self):
        self.cleared = True


def make_callback():
    cb = MagicMock()
    cb.from_user.id = USER_TG          # настоящий пользователь
    cb.message.from_user.id = BOT_TG   # сообщение — от бота (как в Telegram)
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def quick_answers_all_but_tz():
    return {
        "hard_skills": {"python": {"level": 0.7, "verified": 0.5}},
        "experience_years": 2,
        "work_format": ["remote"],
        "schedule": ["flexible"],
        "salary_min": 80000,
        "salary_max": 130000,
        "city": "Казань",
    }


@pytest.fixture()
def wired_bot(monkeypatch):
    store = Storage(":memory:")
    monkeypatch.setattr(bot_mod, "storage", store)
    monkeypatch.setattr(bot_mod, "engine", Engine(Config.load()))
    yield store
    store.close()


@pytest.mark.asyncio
async def test_tz_button_finishes_wizard_for_real_user(wired_bot):
    """Нажатие ЧП (последний вопрос квиза) сохраняет профиль пользователя,
    а не падает с TypeError на id бота."""
    wired_bot.upsert_user(USER_TG, "candidate")
    state = FakeState({
        "answers": quick_answers_all_but_tz(),
        "mode": "quick",
        "multi_sel": [],
        "current_key": "tz",
    })
    callback = make_callback()
    callback_data = ui.WzSingle(key="tz", idx=1)  # UTC+3 (Москва)

    await bot_mod.cb_wz_single(callback, callback_data, state)

    # профиль сохранён настоящему пользователю
    user = wired_bot.get_user(USER_TG)
    profile = wired_bot.get_candidate_profile(user["id"])
    assert profile is not None, "профиль не сохранился — баг воспроизвёлся"
    assert profile["location"] == {"city": "Казань", "tz": 3}
    assert profile["salary"]["min"] == 80000
    assert state.cleared
    callback.answer.assert_awaited()


@pytest.mark.asyncio
async def test_tz_button_creates_user_if_missing(wired_bot):
    """Пользователь нажал кнопку, но записи в users нет — не падаем."""
    state = FakeState({
        "answers": quick_answers_all_but_tz(),
        "mode": "quick", "multi_sel": [], "current_key": "tz",
    })
    callback = make_callback()
    await bot_mod.cb_wz_single(callback, ui.WzSingle(key="tz", idx=1), state)
    user = wired_bot.get_user(USER_TG)
    assert user is not None
    assert wired_bot.get_candidate_profile(user["id"]) is not None


@pytest.mark.asyncio
async def test_skip_button_uses_real_user(wired_bot):
    wired_bot.upsert_user(USER_TG, "candidate")
    state = FakeState({
        "answers": quick_answers_all_but_tz(),
        "mode": "quick", "multi_sel": [], "current_key": "tz",
    })
    callback = make_callback()
    await bot_mod.cb_wz_skip(callback, ui.WzSkip(key="tz"), state)
    user = wired_bot.get_user(USER_TG)
    assert wired_bot.get_candidate_profile(user["id"]) is not None


@pytest.mark.asyncio
async def test_multi_done_uses_real_user(wired_bot):
    """Мультивыбор в середине квиза тоже идёт через callback.message."""
    wired_bot.upsert_user(USER_TG, "candidate")
    state = FakeState({
        "answers": {"hard_skills": {}, "experience_years": 1},
        "mode": "quick", "multi_sel": [0, 1], "current_key": "work_format",
    })
    callback = make_callback()
    await bot_mod.cb_wz_multi_done(callback, ui.WzMultiDone(key="work_format"), state)
    # wizard перешёл к следующему вопросу (schedule), сообщение бота ок
    assert state._data["answers"]["work_format"] == ["remote", "hybrid"]
