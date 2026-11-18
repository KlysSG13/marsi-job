"""Инлайн-UI МАРСИ-Job: фабрики callback-данных и клавиатуры вопросов.

Единый рендер вопросов: choice_single/choice_multi — инлайн-кнопки,
остальные (число, текст, навыки) — ввод сообщением. Мультивыбор —
переключатели + кнопка «Далее».
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .questionnaire import Question


class WzSingle(CallbackData, prefix="wz"):
    """Ответ на вопрос с одиночным выбором."""
    key: str
    idx: int


class WzMulti(CallbackData, prefix="wm"):
    """Переключение пункта мультивыбора."""
    key: str
    idx: int


class WzMultiDone(CallbackData, prefix="wmd"):
    key: str


class WzSkip(CallbackData, prefix="wsk"):
    key: str


class ConfirmCb(CallbackData, prefix="cf"):
    action: str   # ok | edit | cancel | field
    group: str    # id группы полей или ""


class MenuCb(CallbackData, prefix="mn"):
    action: str   # voice | talk | resume | fill | match | why | menu | profile


def single_keyboard(q: Question, skippable: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=WzSingle(key=q.key, idx=i).pack())]
        for i, (_value, label) in enumerate(q.options)
    ]
    if skippable:
        rows.append([InlineKeyboardButton(text="⏭️ Пропустить",
                                          callback_data=WzSkip(key=q.key).pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def multi_keyboard(q: Question, selected: Sequence[int], skippable: bool = False) -> InlineKeyboardMarkup:
    rows = []
    for i, (_value, label) in enumerate(q.options):
        mark = "✅ " if i in selected else "▫️ "
        rows.append([InlineKeyboardButton(
            text=mark + label,
            callback_data=WzMulti(key=q.key, idx=i).pack(),
        )])
    bottom = [InlineKeyboardButton(
        text="➡️ Далее",
        callback_data=WzMultiDone(key=q.key).pack(),
    )]
    if skippable:
        bottom.append(InlineKeyboardButton(
            text="⏭️ Пропустить", callback_data=WzSkip(key=q.key).pack()))
    rows.append(bottom)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Смотреть вакансии", callback_data=MenuCb(action="match").pack())],
        [InlineKeyboardButton(text="🎤 Рассказать о себе голосом", callback_data=MenuCb(action="voice").pack()),
         InlineKeyboardButton(text="💬 Интервью с ИИ", callback_data=MenuCb(action="talk").pack())],
        [InlineKeyboardButton(text="✍️ Резюме текстом", callback_data=MenuCb(action="resume").pack()),
         InlineKeyboardButton(text="➕ Дозаполнить профиль", callback_data=MenuCb(action="fill").pack())],
        [InlineKeyboardButton(text="🧭 Почему мало откликов", callback_data=MenuCb(action="why").pack())],
    ])


def question_text_ui(q: Question, step: Tuple[int, int], selected: Optional[Sequence[int]] = None) -> str:
    done, total = step
    dots = "●" * done + "○" * max(0, total - done)
    prefix = f"<code>{dots}</code> <i>{done}/{total}</i>\n\n"
    extra = ""
    if q.kind == "choice_multi":
        extra = "\n\n<i>Отметьте пункты кнопками и нажмите «➡️ Далее».</i>"
    elif q.kind in ("number", "text", "skills"):
        extra = "\n\n<i>Напишите ответ сообщением.</i>"
    if q.hint:
        extra += f"\n💡 {q.hint}"
    return prefix + q.text + extra
