"""Telegram-бот МАРСИ-Job (aiogram 3).

Поток кандидата — прогрессивное профилирование:
  быстрый старт (8 вопросов) → /match уже работает → дозаполнение (/fill),
  голосом (/voice, GLM ASR), LLM-интервью (/talk) или резюме текстом (/resume);
  всё распознанное подтверждается пользователем по полям (confirm.py).

Запуск: MARSIJOB_TOKEN=... python3 -m marsijob.bot (см. README).
"""
import asyncio
import html
import logging
import os
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.bot import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.filters import CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
    Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)

from . import asr, confirm, ui, wizard
from .config import Config
from .engine import Engine
from .explain import format_candidate_card, format_match_card, format_root_causes
from .interviewer import (
    InterviewerError, is_finished, next_question as iv_next_question,
    parse_dialog,
)
from .moderation import check_discrimination
from .parser_llm import ParserError, merge_profile, parse_resume, parse_resume_heuristic
from .payments import PaymentService, Pricing, PricingError
from .questionnaire import (
    QUESTIONS, Question, apply_answer, progress, question_flow, validate_answer,
)
from .storage import Storage
from .ui import (
    ConfirmCb, MenuCb, WzMulti, WzMultiDone, WzSingle, WzSkip, main_menu_keyboard,
    multi_keyboard, question_text_ui, single_keyboard,
)
from .valence import root_causes, valence_zone
from .vacancy_q import (
    build_vacancy, parse_vacancy_skills, vac_next_question, vac_progress,
)

log = logging.getLogger("marsijob.bot")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SEED_PATH = PROJECT_ROOT / "data" / "seed_vacancies.json"

TALK_MAX_EXCHANGES = 12


def _lim(name: str) -> int:
    return int(pricing.limits[name]) if pricing else 0

storage: Optional[Storage] = None
engine: Optional[Engine] = None
payments: Optional[PaymentService] = None
pricing: Optional[Pricing] = None
_bot_username: Optional[str] = None


def ensure_deps() -> None:
    global storage, engine, payments
    if storage is None:
        db_path = os.environ.get("MARSIJOB_DB", str(PROJECT_ROOT / "marsijob.db"))
        storage = Storage(db_path)
        if not storage.list_active_vacancies() and SEED_PATH.exists():
            storage.seed_vacancies(SEED_PATH)
    if engine is None:
        engine = Engine(Config.load())
    global pricing
    if pricing is None:
        pricing = Pricing.load()
    if payments is None:
        payments = PaymentService(storage, pricing)
        _wire_payment_callbacks()
        payments.configure(
            login=os.environ.get("ROBOKASSA_LOGIN", ""),
            password_1=os.environ.get("ROBOKASSA_PASSWORD_1", ""),
            password_2=os.environ.get("ROBOKASSA_PASSWORD_2", ""),
            is_test=os.environ.get("ROBOKASSA_IS_TEST", "1") == "1",
        )


def _wire_payment_callbacks() -> None:
    """Уведомления после оплаты + таргет-рассылка (выставляются при старте)."""
    async def _notify_paid(purchase: Dict[str, Any]) -> None:
        if not _alert_bot:
            return
        sku = pricing.sku(purchase["sku_id"])
        tg_id = int(purchase["tg_id"])
        msg = f"✅ Оплата получена: {sku.title}.\n"
        if sku.type == "subscription":
            plan = {"plus_month": "plus", "pro_month": "pro",
                    "business_month": "business"}[sku.id]
            sub = storage.get_subscription(tg_id, plan)
            if sub:
                msg += ("Подписка активна до "
                        + sub["active_until"][:10].replace("-", ".") + ".")
        elif sku.type == "consumable":
            msg += (f"Начислено {sku.quantity} приглашений. Баланс: "
                    f"{storage.get_entitlements(tg_id, 'invite')}.")
        elif sku.type == "oneoff":
            msg += "Разбор доступен: отправьте /why."
        await _alert_bot.send_message(tg_id, msg)

    async def _broadcast(purchase: Dict[str, Any]) -> None:
        if _alert_bot:
            sent = await run_broadcast(purchase)
            if sent >= 0:
                await _alert_bot.send_message(
                    int(purchase["tg_id"]),
                    f"📡 Таргет-рассылка доставлена {sent} кандидатам.")

    payments.on_paid_notify = _notify_paid
    payments.on_broadcast = _broadcast


async def maybe_remind_vacancy_archive(message: Message) -> None:
    """За 7 дней до авто-архивации вакансии — напоминание работодателю (раз/сутки)."""
    try:
        user = storage.get_user(message.from_user.id)
        if not user or user["role"] != "employer":
            return
        due = storage.vacancies_archive_reminders(
            _lim("auto_archive_days"),
            warn_days=7)
        for v in due:
            if v["employer_id"] != user["id"]:
                continue
            await message.answer(
                "⚠ Вакансия «" + html.escape(v["title"][:50]) + "» скоро уйдёт в архив. "
                "Обновить: /myvacancies")
    except Exception:
        pass


async def maybe_remind_subscription(message: Message) -> None:
    """Напоминание об истечении подписки — при первом взаимодействии в сутки."""
    try:
        user = storage.get_user(message.from_user.id)
        if not user:
            return
        sub = storage.subscription_reminder_due(user["id"], _lim("subscription_remind_days"))
        if sub:
            await message.answer(
                "⏳ Подписка <b>" + sub["plan"].capitalize() + "</b> истекает "
                + sub["active_until"][:10].replace("-", ".")
                + ". Продлить: /buy")
    except Exception:
        pass


def admin_tg_id() -> Optional[int]:
    raw = os.environ.get("MARSIJOB_ADMIN_TG", "")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def llm_conf() -> Tuple[str, str, str]:
    return (
        os.environ.get("MARSIJOB_LLM_KEY", ""),
        os.environ.get("MARSIJOB_LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
        os.environ.get("MARSIJOB_LLM_MODEL", "glm-4.6"),
    )


class WizardStates(StatesGroup):
    asking = State()      # быстрый старт или дозаполнение (инлайн + текст)


class ConfirmStates(StatesGroup):
    confirming = State()  # карточка «вот что я понял»
    editing = State()     # правка одного поля


class ResumeStates(StatesGroup):
    waiting_text = State()


class TalkStates(StatesGroup):
    talking = State()     # LLM-интервью


class VacancyStates(StatesGroup):
    asking = State()
    done = State()


router = Router(name="marsijob")


def _split(text: str, limit: int):
    """Нарезка длинного текста без разрыва HTML-тегов: сначала по переводу
    строки, иначе — по ближайшей границе тега '>' после лимита."""
    while text:
        if len(text) <= limit:
            yield text
            return
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:  # нет удобной границы строки — ищем конец тега
            gt = text.find(">", limit)
            cut = gt + 1 if gt != -1 and gt < limit + 300 else limit
        yield text[:cut]
        text = text[cut:].lstrip("\n")


# ---------------------------------------------------------------------------
# Роль и меню
# ---------------------------------------------------------------------------

def chunk_parts(parts: List[str], limit: int = 3900) -> List[str]:
    """Сборка сообщений из ЦЕЛЫХ блоков (карточек): каждый блок —
    законченный HTML, разрыв тегов исключён (баг №3 аудита)."""
    chunks: List[str] = []
    buf: List[str] = []
    size = 0
    for p in parts:
        if size + len(p) > limit and buf:
            chunks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(p)
        size += len(p) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks


WELCOME = (
    "👋 <b>МАРСИ-Job</b> — подбор работы по методу МАРСИ.\n\n"
    "Алгоритм раскладывает вакансии и ваш профиль на атомарные факторы, "
    "считает вес каждого, учитывает синергии и конфликты — и объясняет, "
    "почему вакансия подходит (или что мешает идеальному матчу).\n\n"
    "Кем вы хотите пользоваться ботом?"
)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, command: CommandObject = None):
    ensure_deps()
    await state.clear()
    # реферальная ссылка: /start ref_<code>
    payload = (command.args or "") if command else ""
    if payload.startswith("ref_"):
        code = payload[4:].strip()
        tg = message.from_user.id
        existed = storage.get_user(tg) is not None
        if not existed:
            storage.upsert_user(tg, "candidate")  # первый /start создаёт аккаунт
        result = storage.attach_referrer(tg, code) if not existed else "not_new"
        if result == "bound":
            await message.answer(
                "👋 Вы пришли по ссылке друга! Заполните анкету — и вам обоим "
                "будет +7 дней Plus.")
    await maybe_remind_subscription(message)
    await maybe_remind_vacancy_archive(message)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔎 Ищу работу"), KeyboardButton(text="🏢 Ищу сотрудника")]],
        resize_keyboard=True,
    )
    await message.answer(WELCOME, reply_markup=kb)


@router.message(F.text == "🏢 Ищу сотрудника")
async def employer_menu(message: Message, state: FSMContext):
    ensure_deps()
    await state.clear()
    storage.upsert_user(message.from_user.id, "employer")
    await message.answer(EMPLOYER_MENU, reply_markup=ReplyKeyboardRemove())


# ---------------------------------------------------------------------------
# Кандидат: быстрый старт (волшебник)
# ---------------------------------------------------------------------------

@router.message(F.text == "🔎 Ищу работу")
async def candidate_start(message: Message, state: FSMContext):
    ensure_deps()
    storage.upsert_user(message.from_user.id, "candidate")
    await state.set_state(WizardStates.asking)
    await state.update_data(answers={}, mode="quick", multi_sel=[])
    await message.answer(
        "🚀 <b>Быстрый старт: 8 вопросов — и вакансии уже подберутся.</b>\n"
        "Остальное можно дозаполнить позже (или рассказать голосом 🎤).",
        reply_markup=ReplyKeyboardRemove(),
    )
    await ask_current(message, state)


def _mode_question(answers: Dict[str, Any], mode: str) -> Optional[Question]:
    if mode == "quick":
        return wizard.next_quick(answers)
    flow = question_flow(answers)
    return flow[0] if flow else None


async def ask_current(message: Message, state: FSMContext, user_id: Optional[int] = None):
    data = await state.get_data()
    answers: Dict[str, Any] = data.get("answers", {})
    mode: str = data.get("mode", "quick")
    q = _mode_question(answers, mode)
    if q is None:
        await finish_wizard(message, state, user_id=user_id)
        return
    await state.update_data(current_key=q.key, multi_sel=[])
    if mode == "quick":
        done, total = len([k for k in wizard.QUICK_KEYS if k in answers]), len(wizard.QUICK_KEYS)
    else:
        done, total = progress(answers)
    text = question_text_ui(q, (done, total))
    skippable = mode == "fill" and q.key != "sensitive_consent"
    if q.kind == "choice_single":
        await message.answer(text, reply_markup=single_keyboard(q, skippable=skippable))
    elif q.kind == "choice_multi":
        await message.answer(text, reply_markup=multi_keyboard(q, [], skippable=skippable))
    else:
        if skippable:
            await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏭️ Пропустить",
                                      callback_data=WzSkip(key=q.key).pack())]
            ]))
        else:
            await message.answer(text)


# id пользователя из колбэка: callback.message — сообщение БОТА,
# его from_user.id это id бота, а не клиента (причина бага с ЧП)
def user_id_of(source: Any) -> int:
    return int(source.from_user.id)


@router.callback_query(WzSingle.filter(), WizardStates.asking)
async def cb_wz_single(callback: CallbackQuery, callback_data: WzSingle, state: FSMContext):
    data = await state.get_data()
    answers: Dict[str, Any] = data.get("answers", {})
    q = next((x for x in QUESTIONS if x.key == callback_data.key), None)
    if q is None or q.key != data.get("current_key"):
        await callback.answer("Вопрос уже закрыт", show_alert=False)
        return
    value = q.options[callback_data.idx][0]
    answers[q.key] = value
    await state.update_data(answers=answers)
    await callback.answer()
    await ask_current(callback.message, state, user_id=callback.from_user.id)


@router.callback_query(WzMulti.filter(), WizardStates.asking)
async def cb_wz_multi(callback: CallbackQuery, callback_data: WzMulti, state: FSMContext):
    data = await state.get_data()
    q = next((x for x in QUESTIONS if x.key == callback_data.key), None)
    if q is None or q.key != data.get("current_key"):
        await callback.answer()
        return
    sel: List[int] = data.get("multi_sel", [])
    if callback_data.idx in sel:
        sel.remove(callback_data.idx)
    else:
        sel.append(callback_data.idx)
    await state.update_data(multi_sel=sel)
    try:
        await callback.message.edit_reply_markup(
            reply_markup=multi_keyboard(q, sel))
    except Exception:
        pass
    await callback.answer()


@router.callback_query(WzMultiDone.filter(), WizardStates.asking)
async def cb_wz_multi_done(callback: CallbackQuery, callback_data: WzMultiDone, state: FSMContext):
    data = await state.get_data()
    answers: Dict[str, Any] = data.get("answers", {})
    q = next((x for x in QUESTIONS if x.key == callback_data.key), None)
    sel: List[int] = data.get("multi_sel", [])
    if q is None or not sel:
        await callback.answer("Отметьте хотя бы один пункт", show_alert=True)
        return
    values = [q.options[i][0] for i in sorted(sel)]
    if "none" in values:
        values = ["none"]
    answers[q.key] = values
    await state.update_data(answers=answers)
    await callback.answer()
    await ask_current(callback.message, state, user_id=callback.from_user.id)


@router.callback_query(WzSkip.filter(), WizardStates.asking)
async def cb_wz_skip(callback: CallbackQuery, callback_data: WzSkip, state: FSMContext):
    data = await state.get_data()
    answers: Dict[str, Any] = data.get("answers", {})
    answers[callback_data.key] = None  # пропущено
    await state.update_data(answers=answers)
    await callback.answer("Пропущено")
    await ask_current(callback.message, state, user_id=callback.from_user.id)


@router.message(WizardStates.asking, F.text)
async def wizard_text(message: Message, state: FSMContext):
    data = await state.get_data()
    answers: Dict[str, Any] = data.get("answers", {})
    q = next((x for x in QUESTIONS if x.key == data.get("current_key")), None)
    if q is None:
        await ask_current(message, state)
        return
    ok, value, error = validate_answer(q, message.text or "", answers)
    if not ok:
        await message.answer(f"⚠️ {error}")
        return
    answers[q.key] = value
    await state.update_data(answers=answers)
    await ask_current(message, state)


async def finish_wizard(message: Message, state: FSMContext,
                        user_id: Optional[int] = None):
    data = await state.get_data()
    answers: Dict[str, Any] = data.get("answers", {})
    uid = user_id or message.from_user.id
    user = storage.get_user(uid)
    if user is None:
        user_id_new = storage.upsert_user(uid, "candidate")
    else:
        user_id_new = user["id"]
    profile = storage.get_candidate_profile(user_id_new) or {
        "languages": {"russian": {"level": 1.0, "verified": 1.0}},
    }
    for q in QUESTIONS:
        if q.key in answers:
            apply_answer(profile, q, answers[q.key])
    if profile.get("_sensitive_consent") is not True:
        for f in ("stress_tolerance", "multitask_preference", "autonomy_preference",
                  "team_size_preference", "communication_style", "motivation",
                  "risk_appetite", "sensory_sensitivity", "structure_need",
                  "learning_appetite", "neuro_profile", "family_constraints"):
            profile.pop(f, None)
    profile["_wizard_answers"] = answers
    storage.save_candidate_profile(
        user_id_new, profile,
        sensitive_consent=bool(profile.get("_sensitive_consent")),
    )
    await state.clear()
    # реферальная награда: +7 дней Plus обоим за завершённый онбординг
    try:
        ref = storage.credit_referral_onboarding(
            uid, int(pricing.raw["referral"]["reward_days_plus"]),
            int(pricing.raw["referral"]["max_credits_per_month"]))
        if ref["status"] == "credited":
            await message.answer("🎁 Реферальная награда: +7 дней Plus вам!")
            if ref["referrer_rewarded"] and _alert_bot:
                try:
                    await _alert_bot.send_message(
                        ref["referrer_tg"],
                        "🎁 По твоей ссылке присоединился друг — вам обоим +7 дней Plus!")
                except Exception:
                    pass
            elif not ref["referrer_rewarded"]:
                await message.answer("(у пригласившего лимит наград этого месяца исчерпан)")
    except Exception as e:
        log.warning("referral credit failed: %s", e)
    await message.answer("✅ <b>Готово — профиль сохранён!</b>\n\n" + wizard.strength_report(profile))
    try:
        if not storage.get_flag(user_id_new, "consent_invites") and \
                not storage.get_flag(user_id_new, "mini_why_used"):
            await message.answer(
                "📨 <b>Разрешить работодателям присылать приглашения?</b>\n"
                "Вы будете получать карточки вакансий с персональным скором "
                "совпадения.\n\nДа — кнопка ниже · пропустите — не будет",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✅ Разрешить",
                                         callback_data=ConsentCb(agree=1).pack()),
                    InlineKeyboardButton(text="🔕 Не сейчас",
                                         callback_data=ConsentCb(agree=0).pack()),
                ]]))
    except Exception as e:
        log.warning("consent prompt failed: %s", e)
    await message.answer("Что дальше?", reply_markup=main_menu_keyboard())


@router.callback_query(MenuCb.filter())
async def cb_menu(callback: CallbackQuery, callback_data: MenuCb, state: FSMContext):
    ensure_deps()
    action = callback_data.action
    await callback.answer()
    msg = callback.message
    uid = callback.from_user.id  # id клиента, а не бота (callback.message — сообщение бота)
    if action == "match":
        await do_match(msg, user_id=uid)
    elif action == "why":
        await do_why(msg, user_id=uid)
    elif action == "fill":
        await cmd_fill(msg, state, user_id=uid)
    elif action == "voice":
        await cmd_voice(msg, state)
    elif action == "talk":
        await cmd_talk(msg, state)
    elif action == "resume":
        await cmd_resume(msg, state)
    else:
        await msg.answer("Что дальше?", reply_markup=main_menu_keyboard())


@router.message(Command("fill"))
@router.message(Command("edit"))
async def cmd_fill(message: Message, state: FSMContext, user_id: Optional[int] = None):
    ensure_deps()
    uid = user_id or message.from_user.id
    user = storage.get_user(uid)
    if not user:
        await message.answer("Сначала /start")
        return
    profile = storage.get_candidate_profile(user["id"])
    if not profile:
        await message.answer("Профиль не найден — начните с /start")
        return
    from .wizard import reset_answers_for_fill
    answers = reset_answers_for_fill(profile.get("_wizard_answers") or {})
    await state.set_state(WizardStates.asking)
    await state.update_data(answers=answers, mode="fill", multi_sel=[])
    await message.answer("➕ <b>Дозаполнение профиля.</b> Каждый вопрос можно пропустить — "
                         "но чем больше данных, тем точнее подбор.")
    await ask_current(message, state)


# ---------------------------------------------------------------------------
# Голос → профиль
# ---------------------------------------------------------------------------

@router.message(Command("voice"))
async def cmd_voice(message: Message, state: FSMContext):
    ensure_deps()
    await state.clear()
    await message.answer(
        "🎤 <b>Расскажите о себе голосом</b> — как в интервью:\n"
        "чем занимаетесь, ключевые навыки, опыт в годах, желаемый формат и график, "
        "зарплатная вилка, город, готовность к переезду, английский.\n\n"
        "Достаточно 30–60 секунд. Я распознаю речь, соберу профиль и покажу "
        "его на подтверждение — каждое поле можно поправить.\n\n"
        "<i>Просто отправьте голосовое сообщение.</i>",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(F.voice)
async def handle_voice(message: Message, state: FSMContext, bot: Bot):
    ensure_deps()
    # Голос обрабатываем только из «нейтрального» состояния: иначе он
    # молча ломал бы идущий сценарий (анкету/интервью/подтверждение)
    current_state = await state.get_state()
    if current_state is not None:
        await message.answer(
            "⏳ Сначала завершите текущий сценарий (или /stop), потом голосовое. "
            "Если вы посреди анкеты — просто продолжайте отвечать."
        )
        return
    user = storage.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start")
        return
    api_key, _base, _model = llm_conf()
    if not api_key:
        await message.answer(
            "🎤 Распознавание голоса пока не настроено на этом сервере "
            "(нет LLM-ключа). Используйте /resume или /fill."
        )
        return
    if message.voice and message.voice.duration and message.voice.duration > 360:
        await message.answer(
            f"🎙 Голосовое слишком длинное ({message.voice.duration // 60} мин). "
            "Лимит распознавания — ~6 минут. Расскажите короче или пришлите "
            "подробности текстом: /resume"
        )
        return
    is_plus = storage.is_subscribed(user["id"], "plus")
    if not is_plus and storage.daily_count(user["id"], "voice_parse") >= _lim("voice_free_per_day"):
        await message.answer(
            f"🎫 Бесплатных распознаваний на сегодня больше нет "
            f"({_lim('voice_free_per_day')}/день).\n\n"
            "✨ <b>Подписка Plus</b> — безлимитный голос, топ-20 матчей и алерты: "
            f"{pricing.rub(pricing.sku('plus_month').price_kop)} ₽/мес — /buy\n\n"
            "Безлимитно и бесплатно: ✍️ /resume, 💬 /talk, ➕ /fill"
        )
        return
    if not message.voice:
        return
    status = await message.answer("🎧 Слушаю и распознаю…")
    try:
        buf = BytesIO()
        await bot.download(message.voice, destination=buf)
        transcript = await asyncio.to_thread(asr.transcribe_voice, buf.getvalue(), api_key)
    except asr.AsrError as e:
        log.warning("ASR failed: %s", e)
        await status.edit_text(
            "⚠️ Не удалось распознать голосовое. Попробуйте ещё раз "
            "(говорите чуть дольше паузы в начале) или пришлите резюме текстом: /resume"
        )
        return
    storage.log_feedback(user["id"], "voice_parse")
    quoted = transcript[:1500] + ("…" if len(transcript) > 1500 else "")
    await status.edit_text(f"📝 <b>Расшифровка:</b>\n<i>{quoted}</i>")
    await parse_and_confirm(message, transcript, state)


# ---------------------------------------------------------------------------
# Резюме текстом
# ---------------------------------------------------------------------------

@router.message(Command("resume"))
async def cmd_resume(message: Message, state: FSMContext):
    ensure_deps()
    await state.set_state(ResumeStates.waiting_text)
    await message.answer(
        "📄 Пришлите резюме одним сообщением (текстом — скопируйте из файла hh/LinkedIn).",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(ResumeStates.waiting_text, F.text)
async def resume_text_handler(message: Message, state: FSMContext):
    ensure_deps()
    text = message.text or ""
    await parse_and_confirm(message, text, state)


async def parse_and_confirm(message: Message, raw_text: str, state: FSMContext):
    api_key, base_url, model = llm_conf()
    try:
        parsed = await asyncio.to_thread(
            parse_resume, raw_text, api_key, base_url, model)
    except ParserError:
        await message.answer("⚠️ ИИ-парсер не справился — использую встроенный разбор.")
        parsed = parse_resume_heuristic(raw_text)
    if not parsed:
        await message.answer(
            "Не удалось извлечь данные. Попробуйте рассказать подробнее "
            "(навыки, опыт, формат, зарплата, город) или заполните по полям: /fill"
        )
        return
    await show_confirmation(message, parsed, state)


# ---------------------------------------------------------------------------
# Подтверждение полей (голос / резюме / интервью)
# ---------------------------------------------------------------------------

def confirmation_keyboard(parsed: Dict[str, Any]) -> InlineKeyboardMarkup:
    rows = []
    for i, (group, title) in enumerate(confirm.FIELD_GROUPS):
        if group in parsed:
            rows.append([InlineKeyboardButton(
                text=f"✏️ {title}",
                callback_data=ConfirmCb(action="edit", group=group).pack(),
            )])
    rows.append([
        InlineKeyboardButton(text="✅ Всё верно — сохранить",
                             callback_data=ConfirmCb(action="ok", group="").pack()),
        InlineKeyboardButton(text="❌ Отменить",
                             callback_data=ConfirmCb(action="cancel", group="").pack()),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_confirmation(message: Message, parsed: Dict[str, Any], state: FSMContext):
    await state.set_state(ConfirmStates.confirming)
    await state.update_data(confirm_parsed=parsed)
    await message.answer(confirm.render_confirmation(parsed),
                         reply_markup=confirmation_keyboard(parsed))


@router.callback_query(ConfirmCb.filter(), ConfirmStates.confirming)
async def cb_confirm(callback: CallbackQuery, callback_data: ConfirmCb, state: FSMContext):
    data = await state.get_data()
    parsed: Dict[str, Any] = data.get("confirm_parsed", {})
    user = storage.get_user(callback.from_user.id)
    if callback_data.action == "ok":
        current = storage.get_candidate_profile(user["id"]) or {
            "languages": {"russian": {"level": 1.0, "verified": 1.0}},
            "neuro_profile": ["none"], "family_constraints": ["none"],
        }
        merged = merge_profile(parsed, current)
        # синхронизируем снимок ответов, чтобы /fill не переспрашивал подтверждённое
        answers = confirm.sync_parsed_to_answers(parsed, dict(merged.get("_wizard_answers") or {}))
        merged["_wizard_answers"] = answers
        storage.save_candidate_profile(
            user["id"], merged,
            sensitive_consent=bool(merged.get("_sensitive_consent")),
        )
        await state.clear()
        await callback.answer("Сохранено")
        await callback.message.answer("✅ <b>Профиль обновлён!</b>\n\n" +
                                      wizard.strength_report(merged))
        await callback.message.answer("Что дальше?", reply_markup=main_menu_keyboard())
        return
    if callback_data.action == "cancel":
        await state.clear()
        await callback.answer()
        await callback.message.answer("Отменено. Профиль не менялся.",
                                      reply_markup=main_menu_keyboard())
        return
    # edit
    group = callback_data.group
    title = next((t for g, t in confirm.FIELD_GROUPS if g == group), group)
    current_val = confirm.format_value(group, parsed.get(group))
    await state.set_state(ConfirmStates.editing)
    await state.update_data(edit_group=group)
    await callback.answer()
    await callback.message.answer(
        f"✏️ <b>{title}</b>\nСейчас: {current_val}\n\n"
        "Введите новое значение. Примеры форматов:\n"
        "• навыки: <code>python: 70!, sql: 50</code>\n"
        "• зарплата: <code>80000 130000</code>\n"
        "• город: <code>Казань 3</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="↩️ Назад",
                                 callback_data=ConfirmCb(action="back", group="").pack())
        ]]),
    )


@router.callback_query(ConfirmCb.filter(), ConfirmStates.editing)
async def cb_confirm_back(callback: CallbackQuery, callback_data: ConfirmCb, state: FSMContext):
    if callback_data.action != "back":
        await callback.answer()
        return
    data = await state.get_data()
    parsed: Dict[str, Any] = data.get("confirm_parsed", {})
    await state.set_state(ConfirmStates.confirming)
    await callback.answer()
    await show_confirmation(callback.message, parsed, state)


@router.message(ConfirmStates.editing, F.text)
async def confirm_edit_text(message: Message, state: FSMContext):
    data = await state.get_data()
    parsed: Dict[str, Any] = data.get("confirm_parsed", {})
    group: str = data.get("edit_group", "")
    ok, msg = confirm.apply_edit(parsed, group, message.text or "")
    if not ok:
        await message.answer(f"⚠️ {msg}")
        return
    await message.answer(f"✅ {msg}")
    await show_confirmation(message, parsed, state)


# ---------------------------------------------------------------------------
# LLM-интервью (/talk)
# ---------------------------------------------------------------------------

@router.message(Command("talk"))
async def cmd_talk(message: Message, state: FSMContext):
    ensure_deps()
    api_key, _base, _model = llm_conf()
    if not api_key:
        await message.answer("💬 Интервью с ИИ недоступно: не задан LLM-ключ. "
                             "Используйте /voice, /resume или /fill.")
        return
    await state.set_state(TalkStates.talking)
    await state.update_data(iv_msgs=[], iv_count=0)
    await message.answer(
        "💬 <b>Интервью с ИИ.</b> Я задам несколько коротких вопросов о вас "
        "как о специалисте — отвечайте свободно, одним сообщением на вопрос.\n"
        "Остановиться в любой момент: /stop. Поехали!",
        reply_markup=ReplyKeyboardRemove(),
    )
    await message.answer(
        "Расскажите, чем вы занимаетесь и какие у вас ключевые навыки?"
    )


@router.message(Command("stop"))
async def cmd_stop(message: Message, state: FSMContext):
    await state.clear()
    ensure_deps()
    await message.answer("Остановлено. Профиль не менялся.", reply_markup=main_menu_keyboard())


@router.message(TalkStates.talking, F.text)
async def talk_handler(message: Message, state: FSMContext):
    ensure_deps()
    api_key, base_url, model = llm_conf()
    data = await state.get_data()
    msgs: List[Dict[str, str]] = data.get("iv_msgs", [])
    count: int = data.get("iv_count", 0)
    text = (message.text or "").strip()
    if not text:
        return
    msgs = msgs + [{"role": "user", "content": text}]
    count += 1
    if count > TALK_MAX_EXCHANGES:
        await finish_interview(message, state, msgs)
        return
    status = await message.answer("…")
    try:
        reply = await asyncio.to_thread(iv_next_question, msgs, api_key, base_url, model)
    except InterviewerError as e:
        log.warning("interviewer failed: %s", e)
        await status.edit_text("⚠️ Интервьюер временно недоступен. Попробуйте /talk позже "
                               "или заполните профиль: /fill")
        return
    if is_finished(reply):
        await status.delete()
        await finish_interview(message, state, msgs)
        return
    await status.edit_text(reply[:3800])
    msgs = msgs + [{"role": "assistant", "content": reply}]
    await state.update_data(iv_msgs=msgs, iv_count=count)


async def finish_interview(message: Message, state: FSMContext, msgs: List[Dict[str, str]]):
    api_key, base_url, model = llm_conf()
    status = await message.answer("🧠 Собираю профиль из ваших ответов…")
    dialog = "\n".join(
        ("Кандидат: " if m["role"] == "user" else "Интервьюер: ") + m["content"]
        for m in msgs
    )
    try:
        parsed = await asyncio.to_thread(parse_dialog, dialog, api_key, base_url, model)
    except InterviewerError as e:
        log.warning("parse_dialog failed: %s", e)
        await status.edit_text("⚠️ Не удалось собрать профиль из диалога. "
                               "Ответы сохранены — попробуйте /fill или /resume.")
        return
    await status.delete()
    if not parsed:
        await message.answer("Из диалога не извлеклось данных — заполните, пожалуйста, /fill")
        return
    await show_confirmation(message, parsed, state)


# ---------------------------------------------------------------------------
# Матчинг кандидата
# ---------------------------------------------------------------------------

@router.message(Command("match"))
async def cmd_match(message: Message, state: FSMContext):
    ensure_deps()
    await do_match(message, user_id=message.from_user.id)


async def do_match(message: Message, user_id: Optional[int] = None):
    uid = user_id or message.from_user.id
    user = storage.get_user(uid)
    if not user:
        await message.answer("Сначала /start и быстрый старт 🙂")
        return
    profile = storage.get_candidate_profile(user["id"])
    if not profile:
        await message.answer("Профиль не найден — пройдите быстрый старт: /start")
        return
    vacancies = storage.list_active_vacancies()
    if not vacancies:
        await message.answer("Активных вакансий пока нет — загляните позже.")
        return

    weight_overrides = storage.get_candidate_weights(user["id"]) or {}
    vacancies = [v for v in vacancies if not storage.is_match_frozen(user["id"], v["db_id"])]

    # быстрый префильтр жёстких блокировок, полный скоринг — только выжившим
    # свежесть: старше stale_days не показываем; fallback — если выживших < N
    stale_days = int(pricing.raw["freshness"]["stale_days"])
    min_fallback = int(pricing.raw["freshness"]["min_fallback_results"])

    def _age(v):
        return storage.vacancy_age_days(v)

    fresh_pool = [v for v in vacancies if _age(v) <= stale_days]
    stale_pool = sorted([v for v in vacancies if _age(v) > stale_days],
                        key=lambda v: _age(v))
    scoring_pool = fresh_pool

    results = []
    quick_blocked = 0
    viable = []
    for v in scoring_pool:
        if engine.prefilter_blocked(profile, v):
            quick_blocked += 1
            continue
        viable.append(v)
        results.append(engine.score(profile, v, weight_overrides=weight_overrides))
    # fallback: кандидата нельзя оставлять с пустой выдачей
    fallback_used = False
    if len(viable) < min_fallback:
        for v in stale_pool:
            if len(viable) >= min_fallback:
                break
            if engine.prefilter_blocked(profile, v):
                quick_blocked += 1
                continue
            viable.append(v)
            results.append(engine.score(profile, v, weight_overrides=weight_overrides))
            fallback_used = True
    for res, v in zip(results, viable):
        storage.save_match(user["id"], v["db_id"], res.breakdown())
    by_id = {v["id"]: v for v in vacancies}
    age_of = {v["id"]: storage.vacancy_age_days(v) for v in viable}
    is_plus = storage.is_subscribed(user["id"], "plus")
    day_limit = _lim("candidate_plus_matches_per_day") if is_plus \
        else _lim("candidate_free_matches_per_day")
    used = storage.get_usage(user["id"])["matches_shown"]
    left = max(0, day_limit - used)

    ranked_all = sorted(
        [r for r in results if not r.blocked and r.band != "below"],
        key=lambda r: r.score, reverse=True,
    )
    shown = ranked_all[:left]
    blocked_count = quick_blocked + sum(1 for r in results if r.blocked)

    if not shown and not ranked_all:
        await message.answer(
            "Подходящих вакансий не нашлось. Посмотрите /why — алгоритм подскажет, "
            "какие требования сужают выбор."
        )
        return
    if not shown and ranked_all:
        await message.answer(
            f"🎫 Дневной лимит показа матчей исчерпан ({day_limit}/сутки).\n\n"
            "✨ <b>Plus</b> — топ-20/сутки, полные карточки и алерты: "
            f"{pricing.rub(pricing.sku('plus_month').price_kop)} ₽/мес — /buy\n"
            "Новое сгенерируется завтра."
        )
        return
    storage.bump_usage(user["id"], "matches_shown", len(shown))

    header = f"🎯 <b>Топ-{len(shown)} вакансий для вас</b> (из {len(vacancies)}"
    if blocked_count:
        header += f", {blocked_count} отсечено противопоказаниями"
    if is_plus:
        header += " · ✨ Plus"
    header += "):\n"
    parts = [header]
    from .explain import format_match_short
    for i, res in enumerate(shown, 1):
        vac = by_id.get(res.vacancy_id, {})
        vac["_age_days"] = age_of.get(res.vacancy_id, 0)
        if is_plus:
            interventions = valence_zone(engine, profile, vac, top_n=2)["interventions"]
            parts.append(f"<b>{i}.</b> {format_match_card(res, vac, interventions)}")
        else:
            parts.append(f"<b>{i}.</b> {format_match_short(res, vac)}")
        parts.append("―" * 24)
    parts.append(f"🧭 /why — что мешает матчам · ➕ /fill — усилить профиль\n"
                 f"Показано сегодня: {used + len(shown)}/{day_limit}")
    for chunk in chunk_parts(parts):
        await message.answer(chunk)


async def send_mini_why(message: Message, user_row_id: int, profile: Dict[str, Any]):
    """Бесплатный мини-разбор (один раз): топ-3 причины + 1 шаг."""
    vacancies = [v for v in storage.list_active_vacancies()
                 if not engine.prefilter_blocked(profile, v)]
    causes = root_causes(engine, profile, vacancies, top_n=3)
    storage.set_flag(user_row_id, "mini_why_used", 1)
    lines = ["🧭 <b>Мини-разбор</b> (бесплатный, один раз)\n"]
    if not causes:
        lines.append("Все вакансии сейчас отсечены жёсткими фильтрами — смягчите "
                     "условия (формат, зарплата, география) в /fill или /profile.")
    else:
        for i, c in enumerate(causes, 1):
            lines.append(f"{i}. <b>{c['name']}</b> — {c['action']}")
    if causes:
        lines.append(f"\n➡️ Начните с пункта 1: {causes[0]['action']}")
    lines.append("\n🔍 Полный разбор (первопричина, карта пробелов, план +X очков) — /buy")
    await message.answer("\n".join(lines))


@router.message(Command("why"))
async def cmd_why(message: Message, state: FSMContext):
    ensure_deps()
    await do_why(message, user_id=message.from_user.id)


async def do_why(message: Message, user_id: Optional[int] = None):
    uid = user_id or message.from_user.id
    user = storage.get_user(uid)
    profile = storage.get_candidate_profile(user["id"]) if user else None
    if not profile:
        await message.answer("Сначала быстрый старт: /start")
        return
    # полный разбор доступен, если есть оплаченный и неоказанный why_report
    paid = storage.paid_undelivered(user["id"], "why_report")
    if not paid:
        if not storage.get_flag(user["id"], "mini_why_used"):
            await send_mini_why(message, user["id"], profile)
            return
        await message.answer(
            "🔍 <b>Полный разбор «Почему не зовут»</b> — 349 ₽ (промо-цена может действовать):\n"
            "первопричина, карта пробелов по факторам, план «что изменить для +X очков» "
            "и ближайшие альтернативы.\n\nКупить: /buy\n\n"
            "(бесплатный мини-разбор уже был использован)"
        )
        return
    storage.mark_delivered(paid["id"])
    vacancies = [v for v in storage.list_active_vacancies()
                 if not engine.prefilter_blocked(profile, v)]
    causes = root_causes(engine, profile, vacancies, top_n=3)
    await message.answer(format_root_causes(causes))


@router.message(Command("profile"))
async def cmd_profile(message: Message):
    ensure_deps()
    user = storage.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start")
        return
    profile = storage.get_candidate_profile(user["id"])
    if not profile:
        await message.answer("Профиль не заполнен: /start")
        return
    salary = profile.get("salary") or {}
    lines = ["🗂 <b>Ваш профиль</b>"]
    plus = storage.get_subscription(user["id"], "plus")
    if plus:
        lines.insert(0, "✨ <b>Plus</b> до " + plus["active_until"][:10].replace("-", ".")
                     + " — топ-20, алерты, безлимит голоса")
    lines.append(f"Навыки: {', '.join(sorted((profile.get('hard_skills') or {}).keys())) or '—'}")
    lines.append(f"Опыт: {profile.get('experience_years', '—')} лет · "
                 f"Город: {(profile.get('location') or {}).get('city', '—')}")
    lines.append(f"Формат: {', '.join(profile.get('work_format', [])) or '—'} · "
                 f"График: {', '.join(profile.get('schedule', [])) or '—'}")
    lines.append(f"Зарплата: {salary.get('min', '—')}–{salary.get('max', '—')} ₽")
    lines.append(f"Релокация: {profile.get('relocation', '—')}")
    lines.append("")
    lines.append(wizard.strength_report(profile))
    await message.answer("\n".join(lines))


# ---------------------------------------------------------------------------
# Фидбек (итеративный цикл)
# ---------------------------------------------------------------------------

@router.message(Command("dislike"))
async def cmd_dislike(message: Message, state: FSMContext):
    ensure_deps()
    user = storage.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start")
        return
    parts = (message.text or "").split()
    if len(parts) < 3 or parts[2] not in ("format", "domain", "salary"):
        await message.answer(
            "Формат: /dislike &lt;ID вакансии&gt; &lt;format|domain|salary&gt;\n"
            "format — не мой формат/график, domain — не та сфера, salary — зарплата."
        )
        return
    vac = storage.get_vacancy_by_code(parts[1])
    if not vac:
        await message.answer("Вакансия не найдена (ID из /match).")
        return
    kind = f"dislike_{parts[2]}"
    from .feedback import apply_candidate_feedback
    defaults = {fid: f["w0"] for fid, f in engine.cfg.factors.items()}
    profile = storage.get_candidate_profile(user["id"]) or {}
    fb = apply_candidate_feedback(
        storage.get_candidate_weights(user["id"]), kind, defaults,
        vacancy=vac, profile=profile,
    )
    storage.save_candidate_weights(user["id"], fb["weights"])
    if fb["profile_edits"]:
        profile.update(fb["profile_edits"])
        storage.save_candidate_profile(user["id"], profile)
    storage.log_feedback(user["id"], kind, vacancy_id=vac["db_id"])
    await message.answer(
        "📝 Учтено — следующий /match ранжирует строже (шаг цикла МАРСИ)."
    )


@router.message(Command("respond"))
async def cmd_respond(message: Message, state: FSMContext):
    ensure_deps()
    user = storage.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start")
        return
    parts = (message.text or "").split()
    if len(parts) < 3 or parts[2] not in ("noanswer", "interview"):
        await message.answer(
            "Формат: /respond &lt;ID вакансии&gt; &lt;noanswer|interview&gt;"
        )
        return
    vac = storage.get_vacancy_by_code(parts[1])
    if not vac:
        await message.answer("Вакансия не найдена (ID из /match).")
        return
    from .feedback import epsilon_penalty, trust_delta
    kind = "no_response" if parts[2] == "noanswer" else "interview_ok"
    storage.log_feedback(user["id"], kind, vacancy_id=vac["db_id"])
    storage.update_vacancy_trust(vac["db_id"], trust_delta(kind))
    if kind == "no_response":
        count = storage.count_feedback(vac["db_id"], kind)
        storage.bump_vacancy_epsilon(vac["db_id"], epsilon_penalty(kind, 1))
        await message.answer(
            f"📉 Индекс доверия вакансии снижен (событий «без ответа»: {count})."
        )
    else:
        await message.answer("📈 Индекс доверия вакансии повышен.")


# ---------------------------------------------------------------------------
# Работодатель
# ---------------------------------------------------------------------------

EMPLOYER_MENU = (
    "🏢 <b>Раздел работодателя</b>\n\n"
    "/post — опубликовать вакансию (анкета ~3 минуты)\n"
    "/search — топ кандидатов по вашей вакансии\n"
    "/my — мои вакансии\n\n"
    "Алгоритм тот же, что у кандидатов: детерминированный скоринг по формуле МАРСИ, "
    "с учётом синергий и противопоказаний. Чувствительные данные кандидатов "
    "(здоровье, нейропрофиль) видны только как агрегированный индекс совместимости среды."
)


@router.message(Command("post"))
async def cmd_post(message: Message, state: FSMContext):
    ensure_deps()
    user = storage.get_user(message.from_user.id)
    if not user or user["role"] != "employer":
        storage.upsert_user(message.from_user.id, "employer")
        user = storage.get_user(message.from_user.id)
    await state.set_state(VacancyStates.asking)
    await state.update_data(vac_answers={})
    await vac_ask_next(message, state)


async def vac_ask_next(message: Message, state: FSMContext):
    data = await state.get_data()
    answers: Dict[str, Any] = data.get("vac_answers", {})
    q = vac_next_question(answers)
    if q is None:
        await finish_vacancy(message, state)
        return
    done, total = vac_progress(answers)
    if q.kind in ("choice_single", "choice_multi"):
        rows = [[KeyboardButton(text=label)] for _v, label in q.options]
        kb = ReplyKeyboardMarkup(keyboard=[r for r in rows], resize_keyboard=True)
        extra = ("\n<i>Можно несколько — через запятую.</i>" if q.kind == "choice_multi" else "")
        hint = f"\n💡 {q.hint}" if q.hint else ""
        await message.answer(f"[{done + 1}/{total}] {q.text}{extra}{hint}", reply_markup=kb)
    else:
        await message.answer(f"[{done + 1}/{total}] {q.text}", reply_markup=ReplyKeyboardRemove())


@router.message(VacancyStates.asking, F.text)
async def vac_answer_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    answers: Dict[str, Any] = data.get("vac_answers", {})
    q = vac_next_question(answers)
    if q is None:
        await finish_vacancy(message, state)
        return
    if q.key == "hard_skills":
        value = parse_vacancy_skills(message.text or "")
        if not value:
            await message.answer("⚠️ Не удалось разобрать навыки. Формат: python: 70!, docker: 60")
            return
        answers[q.key] = value
    else:
        ok, value, error = validate_answer(q, message.text or "", answers)
        if not ok:
            await message.answer(f"⚠️ {error}")
            return
        answers[q.key] = value
    await state.update_data(vac_answers=answers)
    await vac_ask_next(message, state)


async def finish_vacancy(message: Message, state: FSMContext):
    import time as _time

    data = await state.get_data()
    answers: Dict[str, Any] = data.get("vac_answers", {})
    user = storage.get_user(message.from_user.id)
    mod = check_discrimination(str(answers.get("title", "")))
    if not mod["ok"]:
        await state.set_state(VacancyStates.done)
        reasons = "\n".join(f"   ⛔ {b['message']} ({b['rule']})" for b in mod["blocked"])
        await message.answer(
            "Публикация отклонена авто-модерацией (ТК РФ ст. 64 — запрет дискриминации):\n"
            f"{reasons}\n\nИзмените формулировку и попробуйте снова: /post"
        )
        return
    vacancy_id = f"E{user['id']}-{int(_time.time()) % 100000}"
    vacancy = build_vacancy(answers, vacancy_id)
    db_id = storage.insert_vacancy(user["id"], vacancy, status="active", moderation="approved")
    await state.set_state(VacancyStates.done)
    warn = ""
    if mod["warnings"]:
        warn = "\n⚠️ Замечания модерации: " + "; ".join(w["message"] for w in mod["warnings"])

    # срок размещения: 30 дней / партнёр 365 / Pro — до конца подписки
    from datetime import datetime, timedelta
    if storage.get_flag(user["id"], "partner_employer"):
        days = _lim("partner_vacancy_days")
    else:
        pro = storage.get_subscription(user["id"], "pro") or \
            storage.get_subscription(user["id"], "business")
        days = 30
        paid_until = None
        if pro:
            paid_until = pro["active_until"]
    if storage.get_flag(user["id"], "partner_employer"):
        until = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        storage.set_vacancy_paid_until(db_id, until)
    elif storage.get_subscription(user["id"], "pro") or storage.get_subscription(user["id"], "business"):
        pro = storage.get_subscription(user["id"], "pro") or \
            storage.get_subscription(user["id"], "business")
        storage.set_vacancy_paid_until(db_id, pro["active_until"])
    else:
        until = (datetime.utcnow() + timedelta(days=_lim("employer_free_vacancy_days"))
                 ).strftime("%Y-%m-%d %H:%M:%S")
        storage.set_vacancy_paid_until(db_id, until)

    # 3 стартовых приглашения при первой публикации
    first = not storage.get_entitlements(user["id"], "invite") and \
        not storage.conn.execute(
            "SELECT 1 FROM vacancies WHERE employer_id = ? AND id != ?", (user["id"], db_id)
        ).fetchone()
    bonus = ""
    if first:
        storage.add_entitlements(user["id"], "invite", _lim("employer_free_invites_first"))
        bonus = f"\n🎁 Стартовый бонус: {_lim('employer_free_invites_first')} приглашения"

    await message.answer(
        f"✅ Вакансия опубликована: <b>{vacancy['title']}</b>\n"
        f"ID: <code>{vacancy_id}</code>{warn}{bonus}\n\n"
        "Искать кандидатов: /search · Мои вакансии: /my"
    )
    # алерты Plus-подписчикам (скор ≥ порога, ≤ N/день)
    try:
        await send_plus_alerts(vacancy)
    except Exception as e:
        log.warning("plus alerts failed: %s", e)


@router.message(Command("my"))
async def cmd_my(message: Message):
    ensure_deps()
    user = storage.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start → «Ищу сотрудника»")
        return
    vacancies = storage.list_employer_vacancies(user["id"])
    if not vacancies:
        await message.answer("Вакансий пока нет. Создать: /post")
        return
    lines = ["📋 <b>Мои вакансии</b>"]
    for v in vacancies[:10]:
        lines.append(f"• <code>{v['id']}</code> — {v['title']} [{v['status']}]")
    await message.answer("\n".join(lines))


@router.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext):
    ensure_deps()
    user = storage.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start → «Ищу сотрудника»")
        return
    vacancies = storage.list_employer_vacancies(user["id"])
    active = [v for v in vacancies if v["status"] == "active"]
    if not active:
        await message.answer("Нет активных вакансий. Создать: /post")
        return
    await run_search(message, active[0])


async def send_plus_alerts(vacancy: Dict[str, Any]):
    """Алерты Plus-кандидатам о новой вакансии (скор ≥ порога, ≤ N/день)."""
    threshold = float(pricing.limits["alert_score_min"])
    max_alerts = _lim("alert_max_per_day")
    for c in storage.list_candidate_profiles():
        sub = storage.get_subscription(c["user_id"], "plus")
        if not sub:
            continue
        if storage.get_usage(c["user_id"])["alerts_sent"] >= max_alerts:
            continue
        profile = c["profile"]
        if engine.prefilter_blocked(profile, vacancy):
            continue
        res = engine.score(profile, vacancy)
        if res.blocked or res.score < threshold:
            continue
        storage.bump_usage(c["user_id"], "alerts_sent")
        from .explain import format_match_short
        card = format_match_short(res, vacancy)
        try:
            await _alert_bot.send_message(
                c["tg_id"],
                "🔔 <b>Новый матч для вас (Plus)</b>\n\n" + card,
            )
        except Exception:
            pass


_alert_bot: Optional[Bot] = None


@router.message(Command("invite"))
async def cmd_invite(message: Message, bot: Bot):
    """Приглашение: списание 1 шт, согласие кандидата обязательно."""
    ensure_deps()
    user = storage.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start → «🏢 Ищу сотрудника»")
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Формат: /invite &lt;номер кандидата&gt; (из /search)")
        return
    target = storage.get_candidate_by_user_id(int(parts[1]))
    if not target:
        await message.answer("Кандидат не найден.")
        return
    if not storage.get_flag(int(parts[1]), "consent_invites"):
        await message.answer(
            f"🔕 Кандидат #{parts[1]} не разрешил приглашения — контакт не раскрывается.\n\n"
            "(кандидат включает приглашения командой /invites)"
        )
        return
    # списание: сначала купленный баланс, затем квота Pro/Business
    if not storage.consume_entitlement(user["id"], "invite", 1):
        quota_left = storage.pro_invite_quota_left(user["id"], _lim("pro_invites_per_month"))
        if quota_left > 0:
            storage.pro_invite_used(user["id"])
        else:
            await message.answer(
                "🎫 Приглашения закончились.\n\nПакеты: 5 шт — 490 ₽ · 15 шт — 1 290 ₽ · "
                "40 шт — 2 990 ₽\nИли подписка Pro: 50 приглашений/мес — 4 900 ₽\n\n/buy"
            )
            return
    storage.log_feedback(user["id"], "invite", candidate_id=int(parts[1]))
    employer_name = storage.get_employer_name(user["id"]) or "Работодатель"
    notified = False
    try:
        await bot.send_message(
            target["tg_id"],
            f"📨 <b>{employer_name}</b> приглашает вас откликнуться.\n\n"
            f"Написать напрямую: <a href=\"tg://user?id={message.from_user.id}\">"
            f"открыть чат</a>\n\n"
            "Не хочется таких приглашений? Отключите: /invites",
        )
        notified = True
    except Exception as e:
        log.warning("invite notify failed for %s: %s", target["tg_id"], e)
    balance_now = storage.get_entitlements(user["id"], "invite")
    quota_now = storage.pro_invite_quota_left(user["id"], _lim("pro_invites_per_month"))
    tail = ""
    if storage.is_subscribed(user["id"], "pro") or storage.is_subscribed(user["id"], "business"):
        tail = f" · Pro-квота: {quota_now}"
    await message.answer(
        (f"✅ Приглашение отправлено кандидату #{parts[1]}." if notified
         else f"⚠️ Приглашение записано, но сообщение не доставлено (кандидат заблокировал бота).")
        + f"\nБаланс приглашений: {balance_now}" + tail
    )


async def run_search(message: Message, vacancy: Dict[str, Any]):
    """Бесплатно: топ-5 без контактов; Pro/Business: полный отчёт топ-10."""
    uid = storage.get_user(message.from_user.id)["id"]
    is_pro = storage.is_subscribed(uid, "pro") or storage.is_subscribed(uid, "business")
    candidates = storage.list_candidate_profiles()
    if not candidates:
        await message.answer("Кандидатов в базе пока нет — вернитесь позже.")
        return
    results = [(engine.score(c["profile"], vacancy), c) for c in candidates]
    top_n = 10 if is_pro else _lim("employer_free_candidates")
    shown = sorted(
        [(r, c) for r, c in results if not r.blocked and r.band != "below"],
        key=lambda pair: pair[0].score, reverse=True,
    )[:top_n]
    from .explain import format_candidate_short
    header = (f"🔎 <b>Топ кандидатов</b> · «{html.escape(str(vacancy['title']))}» "
              f"(из {len(candidates)}" + (" · ✨ Pro)" if is_pro else ")") + ":\n")
    parts = [header]
    for r, c in shown:
        if is_pro:
            parts.append(format_candidate_card(r, c))
        else:
            parts.append(format_candidate_short(r, c))
        parts.append("―" * 24)
    if not shown:
        parts.append("Подходящих кандидатов пока нет.")
    if not is_pro:
        parts.append("\nℹ️ Контакты раскрываются приглашением: /invite №\n"
                     "Полные отчёты, 3 вакансии, 50 приглашений/мес — Pro: /buy")
    for chunk in chunk_parts(parts):
        await message.answer(chunk)


# ---------------------------------------------------------------------------
# Помощь и точка входа
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Магазин и квоты
# ---------------------------------------------------------------------------


class BuyCbData(CallbackData, prefix="buy"):
    product: str


class ConsentCb(CallbackData, prefix="cns"):
    agree: int


def product_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key, p in PRODUCTS.items():
        rows.append([InlineKeyboardButton(
            text=f"{p.title} — {p.price_rub} ₽",
            callback_data=BuyCbData(product=key).pack(),
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("buy"))
async def cmd_buy(message: Message, state: FSMContext):
    ensure_deps()
    user = storage.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start")
        return
    audience = "employer" if user["role"] == "employer" else "candidate"
    lines = ["💳 <b>Каталог МАРСИ-Job</b>", ""]
    for sku in pricing.visible_skus(audience):
        price = pricing.rub(pricing.effective_price_kop(sku.id))
        promo = ""
        if pricing.effective_price_kop(sku.id) < sku.price_kop:
            promo = " (промо!)"
        lines.append(f"• <b>{sku.title}</b> — {price} ₽{promo}\n  {sku.description}")
    lines.append("")
    from .legal import legal_urls
    urls = legal_urls()
    lines.append(
        f"Оплата — принятие <a href=\"{urls['oferta']}\">оферты</a>. "
        "Подписки без автосписаний. /terms — краткая оферта."
    )
    await message.answer("\n".join(lines), reply_markup=product_keyboard(audience),
                         disable_web_page_preview=True)


def product_keyboard(audience: str) -> InlineKeyboardMarkup:
    rows = []
    for sku in pricing.visible_skus(audience):
        price = pricing.rub(pricing.effective_price_kop(sku.id))
        rows.append([InlineKeyboardButton(
            text=f"{sku.title} — {price} ₽",
            callback_data=BuyCbData(product=sku.id).pack(),
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(BuyCbData.filter())
async def cb_buy(callback: CallbackQuery, callback_data: BuyCbData, state: FSMContext):
    ensure_deps()
    user = storage.get_user(callback.from_user.id)
    sku = pricing.sku(callback_data.product)
    payload = None
    if sku.id == "target_broadcast":
        await state.set_state(BroadcastStates.waiting_code)
        await state.update_data(broadcast_sku=sku.id)
        await callback.answer()
        await callback.message.answer(
            "📡 Для таргет-рассылки пришлите <b>код вакансии</b> из /my\n"
            "(например, <code>E2-48371</code>)"
        )
        return
    invoice = payments.create_invoice(user["id"], sku.id, payload)
    from .legal import legal_urls
    urls = legal_urls()
    price = pricing.rub(invoice.amount_kop)
    legal_note = (
        f"Оплачивая, вы принимаете <a href=\"{urls['oferta']}\">оферту</a> "
        f"и <a href=\"{urls['privacy']}\">политику</a>.\n"
    )
    await callback.answer()
    if invoice.stub:
        await callback.message.answer(
            f"🧾 <b>Счёт #{invoice.invoice_id}</b> · {sku.title} · {price} ₽\n\n"
            + legal_note +
            "\n⏳ Robokassa ещё не активирована (мерчант в регистрации). "
            "Счёт создан и будет действителен.", disable_web_page_preview=True)
        return
    await callback.message.answer(
        f"🧾 <b>Счёт #{invoice.invoice_id}</b> · {sku.title} · {price} ₽\n\n"
        + legal_note +
        f"\nОплатить: {invoice.url}\n\nНачисление — автоматически после оплаты.",
        disable_web_page_preview=True)


class BroadcastStates(StatesGroup):
    waiting_code = State()


class InterviewStates(StatesGroup):
    choosing_focus = State()
    active = State()


@router.message(BroadcastStates.waiting_code, F.text)
async def broadcast_code_handler(message: Message, state: FSMContext):
    """Код вакансии для рассылки → счёт с payload."""
    ensure_deps()
    user = storage.get_user(message.from_user.id)
    code = (message.text or "").strip()
    vac = storage.get_vacancy_by_code(code)
    if not vac or vac.get("status") != "active":
        await message.answer("Вакансия с таким кодом не найдена или неактивна. "
                             "Код — из /my. Попробуйте ещё раз или /start")
        return
    data = await state.get_data()
    invoice = payments.create_invoice(user["id"], "target_broadcast", payload=str(vac["db_id"]))
    from .legal import legal_urls
    urls = legal_urls()
    await state.clear()
    price = pricing.rub(invoice.amount_kop)
    if invoice.stub:
        await message.answer(
            f"🧾 Счёт #{invoice.invoice_id} · Таргет-рассылка «{vac['title'][:40]}» · {price} ₽\n"
            "⏳ Robokassa ещё не активирована — счёт создан и будет действителен.")
        return
    await message.answer(
        f"🧾 <b>Счёт #{invoice.invoice_id}</b> · Таргет-рассылка «{vac['title'][:40]}» · {price} ₽\n\n"
        f"Оплатить: {invoice.url}\n\n"
        f"После оплаты вакансия уйдёт топ-{_lim('broadcast_top_n')} совместимым кандидатам "
        "с персональным скором.", disable_web_page_preview=True)


async def run_broadcast(purchase: Dict[str, Any]) -> int:
    """Таргет-рассылка: топ-N по скору среди давших согласие. Анти-спам."""
    employer_tg = int(purchase["tg_id"])
    vac_db_id = int(purchase["payload"])
    vac = storage.get_vacancy_by_db_id(vac_db_id)
    if not vac:
        return 0
    # не более 1 рассылки на вакансию в сутки
    key = f"broadcast:{vac_db_id}:{storage._today()}"
    sent = {str(r[0]) for r in storage.conn.execute(
        "SELECT payload_json FROM feedback_events WHERE kind = 'broadcast' "
        "AND payload_json LIKE ?", (f"%{key}%",)).fetchall()} if False else None
    row = storage.conn.execute(
        "SELECT COUNT(*) AS c FROM feedback_events WHERE kind = 'broadcast' "
        "AND payload_json LIKE ?", (f'%"{storage._today()}"%',)).fetchone()
    # простая защита: логируем факт и считаем по вакансии за день
    sent_before = storage.conn.execute(
        "SELECT COUNT(*) AS c FROM feedback_events WHERE kind = 'broadcast' "
        "AND payload_json LIKE ?",
        (f'%"vid": {vac_db_id}, "day": "{storage._today()}"%',)
    ).fetchone()["c"]
    if sent_before >= _lim("broadcast_max_per_vacancy_per_day"):
        return -1
    storage.log_feedback(employer_tg, "broadcast",
                         payload={"vid": vac_db_id, "day": storage._today()})
    from .explain import format_match_short
    top_n = _lim("broadcast_top_n")
    cap = _lim("broadcast_max_per_candidate_per_day")
    scored = []
    for c in storage.list_candidate_profiles():
        if not storage.get_flag(c["user_id"], "consent_invites"):
            continue
        if storage.get_usage(c["user_id"])["broadcasts_received"] >= cap:
            continue
        if engine.prefilter_blocked(c["profile"], vac):
            continue
        res = engine.score(c["profile"], vac)
        if not res.blocked and res.band != "below":
            scored.append((res, c))
    scored.sort(key=lambda pair: pair[0].score, reverse=True)
    sent_count = 0
    for res, c in scored[:top_n]:
        storage.bump_usage(c["user_id"], "broadcasts_received")
        try:
            await _alert_bot.send_message(
                c["tg_id"],
                "📡 <b>Вакансия, похожая на ваш профиль</b>\n\n"
                + format_match_short(res, vac) +
                "\n\nОткликнуться можно напрямую работодателю — контакт в следующем "
                "сообщении после вашего /respond. Отключить рассылки: /invites")
            sent_count += 1
        except Exception:
            pass
    return sent_count


@router.message(Command("referral"))
@router.message(Command("ref"))
async def cmd_referral(message: Message):
    """Карточка реферальной программы: ссылка, счётчики, поделиться."""
    ensure_deps()
    user = storage.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start")
        return
    code = storage.get_or_create_referral_code(
        user["id"], int(pricing.raw["referral"]["code_length"]))
    username = _bot_username or "MarsiJobBot"
    link = f"https://t.me/{username}?start=ref_{code}"
    stats = storage.referral_stats(user["id"])
    reward = pricing.raw["referral"]["reward_days_plus"]
    limit = pricing.raw["referral"]["max_credits_per_month"]
    share = ("https://t.me/share/url?url=" + link +
             "&text=" + "Ищешь работу? Этот бот подбирает вакансии под твой профиль — "
             "попробуй: ")
    await message.answer(
        "🤝 <b>Приведи друга — получите по " + str(reward) + " дней Plus</b>\n\n"
        f"Твоя ссылка:\n<code>{link}</code>\n\n"
        f"Приглашено: {stats['invited']} · засчитано: {stats['credited']} · "
        f"наград в этом месяце: {stats['month_used']}/{limit}\n\n"
        "Награда начисляется, когда друг завершит анкету.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📤 Поделиться", url=share),
        ]]),
        disable_web_page_preview=True)


@router.message(Command("refstats"))
async def cmd_refstats(message: Message):
    ensure_deps()
    admin = admin_tg_id()
    if not admin or message.from_user.id != admin:
        await message.answer("Недоступно.")
        return
    stats = storage.refstats_global()
    lines = ["🤝 <b>Реферальная программа</b>",
             f"Всего рефералов: {stats['total']} · засчитано: {stats['credited']}",
             f"Кандидатов: {stats['candidates']} · "
             f"конверсия старт→онбординг: {stats['conversion'] * 100:.0f}%",
             "", "<b>Топ-10 пригласивших:</b>"]
    if not stats["top"]:
        lines.append("— пока пусто")
    for tg, cnt in stats["top"]:
        lines.append(f"• <code>{tg}</code> — {cnt}")
    await message.answer("\n".join(lines))


@router.message(Command("myvacancies"))
async def cmd_myvacancies(message: Message):
    """Вакансии работодателя со свежестью и кнопками."""
    ensure_deps()
    user = storage.get_user(message.from_user.id)
    if not user or user["role"] != "employer":
        await message.answer("Раздел работодателя: /start → «🏢 Ищу сотрудника»")
        return
    vacancies = storage.list_employer_vacancies(user["id"])
    if not vacancies:
        await message.answer("Вакансий пока нет. Создать: /post")
        return
    auto_days = int(pricing.raw["freshness"]["auto_archive_days"])
    lines = ["📋 <b>Мои вакансии</b>", ""]
    rows = []
    for v in vacancies[:8]:
        age = storage.vacancy_age_days(v)
        if v["status"] == "archived":
            status = "архив"
        elif age > auto_days:
            status = "уйдёт в архив"
        else:
            left = max(0, auto_days - age)
            status = f"активна · до архива {left} дн."
        title = html.escape(str(v.get("title", "?"))[:40])
        lines.append(f"• <code>{v['id']}</code> {title} — {status} (обновлено {age} дн. назад)")
        if v["status"] == "archived":
            rows.append([InlineKeyboardButton(
                text=f"♻️ Реактивировать: {v['id']}",
                callback_data=VacCb(action="react", vid=v["db_id"]).pack())])
        else:
            rows.append([InlineKeyboardButton(
                text=f"🔄 Обновить: {v['id']}",
                callback_data=VacCb(action="touch", vid=v["db_id"]).pack())])
    kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    await message.answer("\n".join(lines), reply_markup=kb)


class VacCb(CallbackData, prefix="vac"):
    action: str  # touch | react
    vid: int


@router.callback_query(VacCb.filter())
async def cb_vacancy_action(callback: CallbackQuery, callback_data: VacCb):
    ensure_deps()
    user = storage.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Сначала /start")
        return
    vac = storage.get_vacancy_by_db_id(callback_data.vid)
    if not vac or vac.get("employer_id") != user["id"] if "employer_id" in vac else True:
        # get_vacancy_by_db_id не отдаёт employer_id — проверим через список
        own = [v for v in storage.list_employer_vacancies(user["id"])
               if v["db_id"] == callback_data.vid]
        if not own:
            await callback.answer("Это не ваша вакансия", show_alert=True)
            return
    if callback_data.action == "touch":
        storage.touch_vacancy(callback_data.vid)
        await callback.answer("✅ Обновлено — счётчик свежести сброшен")
        await callback.message.answer("🔄 Вакансия отмечена обновлённой "
                                      "(«свежая» на следующие 7 дней).")
    elif callback_data.action == "react":
        cooldown = int(pricing.raw["freshness"]["reactivate_cooldown_days"])
        is_paid = storage.is_subscribed(user["id"], "pro") or \
            storage.is_subscribed(user["id"], "business")
        ok, why = storage.reactivate_vacancy(callback_data.vid, cooldown,
                                             is_paid=is_paid)
        if ok:
            await callback.answer("Вакансия снова активна")
            await callback.message.answer("♻️ Вакансия реактивирована и активна.")
        elif why == "cooldown":
            await callback.answer()
            await callback.message.answer(
                f"⏳ Бесплатная реактивация — раз в {cooldown} дней. "
                "Подписка Pro снимает ограничение: /buy")
        else:
            await callback.answer("Вакансия не в архиве", show_alert=True)


@router.message(Command("vacstats"))
async def cmd_vacstats(message: Message):
    ensure_deps()
    admin = admin_tg_id()
    if not admin or message.from_user.id != admin:
        await message.answer("Недоступно.")
        return
    # сперва плановая архивация+дедуп, затем статистика
    archived = storage.archive_stale_vacancies(
        int(pricing.raw["freshness"]["auto_archive_days"]))
    deduped = storage.dedup_imported_vacancies()
    stats = storage.freshness_stats()
    f = stats["freshness"]
    await message.answer(
        "📊 <b>Свежесть вакансий</b>\n"
        f"Всего: {stats['total']} · активных: {stats['active']} · "
        f"архив: {stats['archived']}\n"
        f"🆕 0–7 дней: {f['0-7']} · 8–30: {f['8-30']} · "
        f"31–45: {f['31-45']} · >45: {f['>45']}\n"
        f"За этот проход: авто-архив {archived}, дедуп {deduped}")




@router.message(Command("audit"))
async def cmd_audit(message: Message):
    ensure_deps()
    user = storage.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start")
        return
    profile = storage.get_candidate_profile(user["id"])
    if not profile:
        await message.answer("Сначала анкета: /start")
        return
    from .resume_audit import format_full, format_teaser, llm_remarks, run_audit
    vacs = [v for v in storage.list_active_vacancies()
            if not engine.prefilter_blocked(profile, v)][:5]
    paid = storage.paid_undelivered(user["id"], "resume_audit")
    if not paid:
        if not storage.get_flag(user["id"], "audit_teaser_used"):
            audit = run_audit(profile, top_vacancies=vacs)
            storage.set_flag(user["id"], "audit_teaser_used", 1)
            await message.answer(format_teaser(audit))
            return
        await message.answer(
            "📋 <b>Аудит резюме</b> — 199 ₽: скор 0–100, разбор по 7 измерениям. /buy")
        return
    storage.mark_delivered(paid["id"])
    audit = run_audit(profile, top_vacancies=vacs)
    text = format_full(audit)
    api_key, base_url, model = llm_conf()
    if api_key:
        remarks = await asyncio.to_thread(
            llm_remarks, profile, api_key, base_url, model)
        if remarks:
            text += "\n\n<b>📝 Формулировки:</b>\n" + "\n".join(f"• {r}" for r in remarks)
    await message.answer(text)
    await message.answer("Повтор в 48 ч — бесплатен. /audit")


@router.message(Command("interview"))
async def cmd_interview(message: Message, state: FSMContext):
    ensure_deps()
    user = storage.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start")
        return
    api_key, _b, _m = llm_conf()
    if not api_key:
        await message.answer("💬 Мок-интервью временно недоступно.")
        return
    profile = storage.get_candidate_profile(user["id"])
    if not profile:
        await message.answer("Сначала анкета: /start")
        return
    paid = storage.paid_undelivered(user["id"], "mock_interview")
    has_pack = storage.get_entitlements(user["id"], "mock_interview") > 0
    if not paid and not has_pack:
        await message.answer(
            "💬 <b>Мок-интервью</b> — 299 ₽/сессия или 3 за 799 ₽.\n\n/buy")
        return
    vacs = [v for v in storage.list_active_vacancies()
            if not engine.prefilter_blocked(profile, v)][:3]
    rows = [[InlineKeyboardButton(
        text="🎯 По моим слабым местам",
        callback_data=InterviewCb(focus="weak").pack())]]
    for v in vacs:
        rows.append([InlineKeyboardButton(
            text=f"💼 {str(v.get('title', '?'))[:30]}",
            callback_data=InterviewCb(focus=f"vac_{v['db_id']}").pack())])
    await state.set_state(InterviewStates.choosing_focus)
    await message.answer("💬 <b>Мок-интервью</b>\n\nВыберите фокус:",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


class InterviewCb(CallbackData, prefix="ivw"):
    focus: str


@router.callback_query(InterviewCb.filter(), InterviewStates.choosing_focus)
async def cb_interview_focus(callback: CallbackQuery, callback_data: InterviewCb,
                             state: FSMContext):
    ensure_deps()
    user = storage.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Сначала /start")
        return
    if storage.get_entitlements(user["id"], "mock_interview") > 0:
        storage.consume_entitlement(user["id"], "mock_interview", 1)
    else:
        paid = storage.paid_undelivered(user["id"], "mock_interview")
        if paid:
            storage.mark_delivered(paid["id"])
        else:
            await callback.answer("Нет оплаченных сессий: /buy", show_alert=True)
            return
    from datetime import datetime, timedelta
    ttl = int(pricing.raw["mock_interview"]["session_ttl_min"])
    focus = callback_data.focus
    vac_id = None
    if focus.startswith("vac_"):
        vac_id = int(focus.split("_")[1])
        focus = "vacancy"
    expires = (datetime.utcnow() + timedelta(minutes=ttl)).strftime("%Y-%m-%d %H:%M:%S")
    cursor = storage.conn.execute(
        "INSERT INTO interview_sessions (tg_id, status, focus, vacancy_id, expires_at) "
        "VALUES (?, 'active', ?, ?, ?)",
        (user["id"], focus, vac_id, expires))
    storage.conn.commit()
    session_id = cursor.lastrowid
    await state.set_state(InterviewStates.active)
    await state.update_data(iv_session_id=session_id, iv_qa=[], iv_asked=[])
    await callback.answer()
    qmax = pricing.raw["mock_interview"]["questions_max"]
    await callback.message.answer(
        f"💬 <b>Сессия #{session_id}</b>! Лимит: {qmax} вопросов, {ttl} мин. /cancel — выход")
    await _ask_iv_question(callback.message, state, user["id"], focus, vac_id)


async def _ask_iv_question(message: Message, state: FSMContext,
                           user_row_id: int, focus: str, vac_id):
    from .mock_interview import generate_question
    api_key, base_url, model = llm_conf()
    profile = storage.get_candidate_profile(user_row_id) or {}
    data = await state.get_data()
    asked = data.get("iv_asked", [])
    weak_points = []
    if focus == "weak":
        vacs = [v for v in storage.list_active_vacancies()
                if not engine.prefilter_blocked(profile, v)][:5]
        causes = root_causes(engine, profile, vacs, top_n=3)
        weak_points = [c["name"] for c in causes]
    vac_reqs = None
    if focus == "vacancy" and vac_id:
        vac = storage.get_vacancy_by_db_id(vac_id)
        if vac:
            skills = ", ".join((vac.get("hard_skills") or {}).keys())
            vac_reqs = f"{vac.get('title', '?')}: {skills}"
    q = await asyncio.to_thread(
        generate_question, api_key, base_url, model, profile,
        weak_points, vac_reqs, asked,
        int(pricing.raw["mock_interview"]["gen_max_tokens"]))
    if not q:
        await message.answer("⏳ ИИ недоступен — повторите ответ.")
        return
    asked.append(q["question"])
    await state.update_data(iv_asked=asked)
    await message.answer(f"❓ <b>{q['question']}</b>")


@router.message(InterviewStates.active, F.text)
async def interview_answer_handler(message: Message, state: FSMContext, bot: Bot):
    ensure_deps()
    user = storage.get_user(message.from_user.id)
    if not user:
        return
    data = await state.get_data()
    session_id = data.get("iv_session_id")
    if not session_id:
        await state.clear()
        return
    row = storage.conn.execute(
        "SELECT status FROM interview_sessions WHERE id = ?", (session_id,)).fetchone()
    if not row or row["status"] != "active":
        await state.clear()
        await message.answer("Сессия завершена.")
        return
    qa = data.get("iv_qa", [])
    asked = data.get("iv_asked", [])
    if asked and (not qa or qa[-1].get("answer")):
        qa.append({"question": asked[-1], "answer": message.text or ""})
    elif qa and not qa[-1].get("answer"):
        qa[-1]["answer"] = message.text or ""
    qmax = int(pricing.raw["mock_interview"]["questions_max"])
    if len(qa) >= qmax:
        await _finish_interview(message, state, user["id"], session_id, qa, "finished")
        return
    await state.update_data(iv_qa=qa)
    focus_row = storage.conn.execute(
        "SELECT focus, vacancy_id FROM interview_sessions WHERE id = ?",
        (session_id,)).fetchone()
    await _ask_iv_question(message, state, user["id"],
                           focus_row["focus"] if focus_row else "weak",
                           focus_row["vacancy_id"] if focus_row else None)
    storage.conn.execute(
        "UPDATE interview_sessions SET questions_asked = ? WHERE id = ?",
        (len(qa), session_id))
    storage.conn.commit()


async def _finish_interview(message: Message, state: FSMContext,
                            user_row_id: int, session_id: int,
                            qa: list, status: str):
    from .mock_interview import format_report, generate_report
    min_a = int(pricing.raw["mock_interview"]["min_answers_for_report"])
    if len(qa) < min_a:
        storage.conn.execute(
            "UPDATE interview_sessions SET status = ? WHERE id = ?",
            (status, session_id))
        storage.conn.commit()
        await state.clear()
        await message.answer(f"Сессия завершена ({len(qa)} ответов, минимум {min_a}).")
        return
    status_msg = await message.answer("📋 Формирую отчёт…")
    api_key, base_url, model = llm_conf()
    profile = storage.get_candidate_profile(user_row_id) or {}
    report = await asyncio.to_thread(
        generate_report, api_key, base_url, model, qa, profile,
        int(pricing.raw["mock_interview"]["report_max_tokens"]))
    summary = {"criteria": report.get("criteria", {}), "answers": len(qa)} \
        if report else {"answers": len(qa), "error": True}
    storage.conn.execute(
        "UPDATE interview_sessions SET status = ?, summary_json = ? WHERE id = ?",
        (status, json.dumps(summary, ensure_ascii=False), session_id))
    storage.conn.commit()
    await state.clear()
    if report:
        await status_msg.edit_text(format_report(report))
    else:
        await status_msg.edit_text("⚠ Отчёт не сформирован. /buy")


@router.message(Command("roadmap"))
async def cmd_roadmap(message: Message):
    ensure_deps()
    user = storage.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start")
        return
    profile = storage.get_candidate_profile(user["id"])
    if not profile:
        await message.answer("Сначала анкета: /start")
        return
    from .roadmap import build_roadmap, format_full, format_preview
    vacs = [v for v in storage.list_active_vacancies()
            if not engine.prefilter_blocked(profile, v)]
    paid = storage.paid_undelivered(user["id"], "roadmap")
    rm = build_roadmap(engine, profile, vacs,
                       int(pricing.raw["roadmap"]["horizon_months"]),
                       int(pricing.raw["roadmap"]["max_items_per_month"]))
    if not paid:
        await message.answer(format_preview(rm))
        return
    storage.mark_delivered(paid["id"])
    storage.conn.execute(
        "INSERT INTO roadmaps (tg_id, target_type, report_json) VALUES (?, 'overall', ?)",
        (user["id"], json.dumps(rm, ensure_ascii=False)))
    storage.conn.commit()
    await message.answer(format_full(rm))
    await message.answer("Пересчёт 30 дней — бесплатен. /roadmap")


@router.message(Command("balance"))
@router.message(Command("status"))
async def cmd_balance(message: Message):
    ensure_deps()
    user = storage.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start")
        return
    lines = ["💳 <b>Ваш статус</b>", ""]
    if user["role"] == "employer":
        invites = storage.get_entitlements(user["id"], "invite")
        lines.append(f"📨 Приглашений: <b>{invites}</b>")
        for plan in ("pro", "business"):
            sub = storage.get_subscription(user["id"], plan)
            if sub:
                lines.append(f"✨ {plan.capitalize()} до "
                             + sub["active_until"][:10].replace("-", "."))
                lines.append(f"   квота приглашений: "
                             f"{storage.pro_invite_quota_left(user['id'], _lim('pro_invites_per_month'))}"
                             f"/{_lim('pro_invites_per_month')}")
        vacs = storage.list_employer_vacancies(user["id"])
        active = [v for v in vacs if v["status"] == "active"]
        lines.append(f"📋 Активных вакансий: {len(active)}")
    else:
        usage = storage.get_usage(user["id"])
        is_plus = storage.is_subscribed(user["id"], "plus")
        limit = _lim("candidate_plus_matches_per_day") if is_plus \
            else _lim("candidate_free_matches_per_day")
        lines.append(f"🎯 Матчей сегодня: {usage['matches_shown']}/{limit}"
                     + (" · ✨ Plus" if is_plus else ""))
        voice_used = storage.daily_count(user["id"], "voice_parse")
        if is_plus:
            lines.append("🎤 Голос: безлимит (Plus)")
        else:
            lines.append(f"🎤 Голос: {voice_used}/{_lim('voice_free_per_day')} сегодня")
        lines.append(f"🧭 Мини-разбор: "
                     + ("использован" if storage.get_flag(user["id"], "mini_why_used")
                        else "доступен"))
        if is_plus:
            sub = storage.get_subscription(user["id"], "plus")
            lines.append("✨ Plus до " + sub["active_until"][:10].replace("-", "."))
        lines.append(f"📨 Приглашения работодателей: "
                     + ("разрешены" if storage.get_flag(user["id"], "consent_invites")
                        else "выключены (/invites — включить)"))
    # реферальный блок
    rstats = storage.referral_stats(user["id"])
    if rstats["invited"] or rstats["credited"]:
        lines.append(f"🤝 Рефералы: {rstats['invited']} приглашено, "
                     f"{rstats['credited']} засчитано")
    else:
        lines.append("🤝 Реферальная программа: /referral")
    await message.answer("\n".join(lines))


@router.message(Command("invites"))
async def cmd_invites(message: Message):
    """Согласие кандидата на приглашения/рассылки."""
    ensure_deps()
    user = storage.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start")
        return
    new_val = 0 if storage.get_flag(user["id"], "consent_invites") else 1
    storage.set_flag(user["id"], "consent_invites", new_val)
    if new_val:
        await message.answer(
            "✅ Приглашения разрешены: работодатели смогут прислать вам карточку "
            "вакансии с персональным скором и контакт для отклика.\n"
            "В любой момент отключить: /invites")
    else:
        await message.answer("🔕 Приглашения выключены. Включить снова: /invites")


@router.message(Command("terms"))
async def cmd_terms(message: Message):
    """Краткая публичная оферта из data/terms.txt."""
    terms_path = PROJECT_ROOT / "data" / "terms.txt"
    text = terms_path.read_text(encoding="utf-8")
    await message.answer(text)


@router.message(Command("grant"))
async def cmd_grant(message: Message):
    """/grant <tg_id> <sku_id|flag> [count] — админ."""
    ensure_deps()
    admin = admin_tg_id()
    if not admin or message.from_user.id != admin:
        await message.answer("Недоступно.")
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        skus = ", ".join(sorted(pricing.skus))
        await message.answer(
            "Формат: /grant &lt;tg_id&gt; &lt;sku_id|flag&gt; [count]\n"
            f"SKU: {skus}\nФлаги: partner_employer, consent_invites, mini_why_used")
        return
    target = storage.get_user(int(parts[1]))
    if not target:
        await message.answer("Пользователь не найден (должен был нажать /start).")
        return
    key = parts[2]
    try:
        if key in pricing.skus:
            if key == "target_broadcast":
                storage.add_entitlements(target["id"], "broadcast", 1)
                result = "таргет-рассылка доступна: /broadcast &lt;код вакансии&gt;"
            else:
                result = payments.grant_sku(target["id"], key)
        elif key in ("partner_employer", "consent_invites", "mini_why_used"):
            value = int(parts[3]) if len(parts) > 3 and parts[3].lstrip("-").isdigit() else 1
            storage.set_flag(target["id"], key, value)
            result = f"флаг {key}={value}"
        else:
            await message.answer(f"Неизвестный SKU/флаг: {key}")
            return
        await message.answer(f"✅ {parts[1]}: {result}")
    except PricingError as e:
        await message.answer(f"⚠️ {e}")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    """Запуск оплаченной (или выданной админом) рассылки: /broadcast <код вакансии>."""
    ensure_deps()
    user = storage.get_user(message.from_user.id)
    if not user or user["role"] != "employer":
        await message.answer("Раздел работодателя.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Формат: /broadcast &lt;код вакансии&gt; (из /my)")
        return
    if storage.get_entitlements(user["id"], "broadcast") <= 0:
        await message.answer("Нет доступных рассылок. Купить: /buy → «Таргет-рассылка»")
        return
    vac = storage.get_vacancy_by_code(parts[1])
    if not vac or vac.get("status") != "active":
        await message.answer("Вакансия не найдена или неактивна.")
        return
    storage.consume_entitlement(user["id"], "broadcast")
    fake_purchase = {"tg_id": user["id"], "payload": str(vac["db_id"])}
    status = await message.answer("📡 Рассылаю…")
    sent = await run_broadcast(fake_purchase)
    if sent == -1:
        await status.edit_text("⏳ По этой вакансии уже была рассылка за последние сутки.")
    else:
        await status.edit_text(f"📡 Готово: доставлено {sent} кандидатам.")


@router.callback_query(ConsentCb.filter())
async def cb_consent(callback: CallbackQuery, callback_data: ConsentCb):
    ensure_deps()
    user = storage.get_user(callback.from_user.id)
    if user:
        storage.set_flag(user["id"], "consent_invites", callback_data.agree)
    await callback.answer()
    if callback_data.agree:
        await callback.message.answer("✅ Приглашения разрешены. Отключить: /invites")
    else:
        await callback.message.answer("Хорошо — приглашений не будет. "
                                      "Включить когда захотите: /invites")


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "<b>МАРСИ-Job</b> — матчинг по методу МАРСИ.\n\n"
        "<b>Кандидату:</b>\n"
        "/match — топ матчей (3/день бесплатно, Plus — 20)\n"
        "/why — почему не зовут (мини — бесплатно)\n"
        "/voice — рассказать о себе голосом (1/день, Plus — безлимит)\n"
        "/talk — интервью с ИИ\n/resume — резюме текстом\n/fill — дозаполнить\n"
        "/invites — приглашения вкл/выкл\n\n"
        "<b>Работодателю:</b>\n"
        "/post — вакансия · /my — мои вакансии\n"
        "/search — топ-5 кандидатов\n/invite № — пригласить (1 шт)\n"
        "/broadcast — таргет-рассылка\n\n"
        "📋 /audit — аудит резюме (мини бесплатно)\n"
        "💬 /interview — мок-интервью с ИИ (299 ₽)\n"
        "🗺 /roadmap — дорожная карта +X очков (349 ₽)\n"
        "🤝 /referral — пригласи друга, +7 дней Plus обоим\n\n"
        "💳 /buy · /balance · 📜 /terms · /legal"
    )


@router.callback_query()
async def cb_stale_button(callback: CallbackQuery):
    """Catch-all: кнопки на старых сообщениях (сценарий уже завершён) —
    раньше такие нажатия висели «часиками» ~10 секунд без ответа."""
    await callback.answer("⏳ Кнопка устарела — вызовите команду заново (/match, /fill…)")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    token = os.environ.get("MARSIJOB_TOKEN")
    if not token:
        raise SystemExit(
            "Задайте токен бота: export MARSIJOB_TOKEN=... (получить у @BotFather)"
        )
    ensure_deps()

    # вебхуки платежей (Robokassa): тот же event loop, что и polling
    from .web import start_web
    web_port = int(os.environ.get("MARSIJOB_WEB_PORT", "8080"))
    web_host = os.environ.get("MARSIJOB_WEB_HOST", "127.0.0.1")
    await start_web(payments, web_port, host=web_host)

    # фоновая синхронизация внешних вакансий (trudvsem всегда, hh — при токене)
    if os.environ.get("MARSIJOB_SYNC_ENABLED", "1") == "1":
        asyncio.create_task(sync_loop())

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    global _alert_bot, _bot_username
    _alert_bot = bot
    try:
        me = await bot.get_me()
        _bot_username = me.username
    except Exception:
        _bot_username = "MarsiJobBot"
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


async def sync_loop() -> None:
    """Периодический автосинк внешних источников вакансий."""
    from .sources import sync_all
    interval_hours = float(os.environ.get("MARSIJOB_SYNC_HOURS", "4"))
    while True:
        try:
            db_path = storage.db_path  # своё соединение в потоке синка
            def _run():
                st = Storage(db_path)
                report = sync_all(st)
                # свежесть: авто-архив >45 дней + дедуп импорта
                fresh = pricing.raw.get("freshness", {})
                report["_maintenance"] = {
                    "archived": st.archive_stale_vacancies(
                        int(fresh.get("auto_archive_days", 45))),
                    "deduped": st.dedup_imported_vacancies(),
                }
                return report
            report = await asyncio.to_thread(_run)
            for source, stats in report.items():
                log.info("sync %s: %s", source, stats)
        except Exception as e:
            log.warning("sync loop error: %s", e)
        await asyncio.sleep(interval_hours * 3600)


if __name__ == "__main__":
    asyncio.run(main())
