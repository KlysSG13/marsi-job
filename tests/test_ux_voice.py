"""Тесты этапа «UX+голос»: волшебник, сила профиля, подтверждение полей,
ASR-модуль (multipart/нарезка без ffmpeg), LLM-интервьюер (моки API), UI.
"""
import json
from unittest.mock import patch

import pytest

from marsijob import asr, confirm, wizard
from marsijob.questionnaire import apply_answer


# ---------------------------------------------------------------------------
# Волшебник и сила профиля
# ---------------------------------------------------------------------------


class TestWizard:
    def test_quick_is_8_questions(self):
        keys = [q.key for q in wizard.quick_questions()]
        assert keys == wizard.QUICK_KEYS
        assert len(keys) == 8

    def test_next_quick_progress(self):
        answers = {}
        assert wizard.next_quick(answers).key == "hard_skills"
        answers["hard_skills"] = {}
        assert wizard.next_quick(answers).key == "experience_years"
        for k in wizard.QUICK_KEYS:
            answers[k] = None
        assert wizard.next_quick(answers) is None

    def test_strength_empty_and_full(self):
        empty_percent, missing = wizard.profile_strength({})
        assert empty_percent == 0
        assert "навыки" in " ".join(missing).lower() or missing
        profile = {
            "hard_skills": {"python": {"level": 0.7, "verified": 0.5}},
            "experience_years": 3, "work_format": ["remote"],
            "schedule": ["flexible"], "salary": {"min": 100, "max": 200},
            "location": {"city": "Казань", "tz": 3}, "work_permit": ["RU"],
        }
        percent, _missing = wizard.profile_strength(profile)
        assert percent >= 50  # критичные закрыты
        full = dict(profile)
        full.update({
            "languages": {"russian": {"level": 1.0, "verified": 1.0},
                          "english": {"level": 0.7, "verified": 0.3}},
            "employment_type": ["tk"], "relocation": "no", "commute_max": 40,
            "education_level": "bachelor", "portfolio": 0.5,
            "domain_experience": ["it"], "interests": ["data"],
            "culture_fit": "casual", "mission_importance": 0.5,
            "career_trajectory": "deep", "urgency_months": 2, "start_date_months": 1,
            "management_experience": {"has": False, "team_size": 0},
            "stress_tolerance": 0.5, "multitask_preference": 0.5,
            "autonomy_preference": 0.5, "team_size_preference": [2, 8],
            "communication_style": "async", "motivation": {"growth": 1.0},
            "risk_appetite": 0.5, "learning_appetite": 0.6,
            "sensory_sensitivity": 0.3, "structure_need": 0.4,
            "neuro_profile": ["none"], "family_constraints": ["none"],
            "business_trips_tolerance": 0.2,
        })
        full["salary"] = dict(full["salary"], compromise=True)
        percent_full, _m = wizard.profile_strength(full)
        assert percent_full >= 95

    def test_strength_report_mentions_quota(self):
        text = wizard.strength_report({})
        assert "Сила профиля" in text and "%" in text


# ---------------------------------------------------------------------------
# Подтверждение полей
# ---------------------------------------------------------------------------


class TestConfirm:
    PARSED = {
        "hard_skills": {"python": {"level": 0.7, "verified": 0.3}},
        "experience_years": 4,
        "salary": {"min": 150000, "max": 220000},
        "location": {"city": "Казань", "tz": 3},
        "work_format": ["remote"],
    }

    def test_render(self):
        text = confirm.render_confirmation(self.PARSED)
        assert "Навыки" in text and "python 70%" in text
        assert "150 000–220 000" in text
        assert "Казань (UTC+3)" in text

    def test_edit_skills(self):
        parsed = dict(self.PARSED)
        ok, msg = confirm.apply_edit(parsed, "hard_skills", "go: 60!, sql: 40")
        assert ok
        assert parsed["hard_skills"]["go"] == {"level": 0.6, "verified": 0.7}

    def test_edit_salary_two_numbers(self):
        parsed = dict(self.PARSED)
        ok, _msg = confirm.apply_edit(parsed, "salary", "100000 150000")
        assert ok and parsed["salary"]["min"] == 100000 and parsed["salary"]["max"] == 150000

    def test_edit_salary_single_number(self):
        parsed = dict(self.PARSED)
        ok, _msg = confirm.apply_edit(parsed, "salary", "100к")
        assert ok and parsed["salary"]["min"] == 100000

    def test_edit_location(self):
        parsed = dict(self.PARSED)
        ok, _msg = confirm.apply_edit(parsed, "location", "Санкт-Петербург 3")
        assert ok and parsed["location"]["city"] == "Санкт-Петербург"

    def test_edit_english(self):
        parsed = dict(self.PARSED)
        ok, _msg = confirm.apply_edit(parsed, "languages", "английский b2")
        assert ok and parsed["languages"]["english"]["level"] == 0.7

    def test_edit_relocation(self):
        parsed = dict(self.PARSED)
        ok, _msg = confirm.apply_edit(parsed, "relocation", "с пакетом")
        assert ok and parsed["relocation"] == "yes_with_package"

    def test_edit_experience_bad(self):
        parsed = dict(self.PARSED)
        ok, msg = confirm.apply_edit(parsed, "experience_years", "много")
        assert not ok

    def test_sync_to_answers(self):
        answers = confirm.sync_parsed_to_answers(self.PARSED, {})
        assert answers["experience_years"] == 4
        assert answers["salary_min"] == 150000
        assert answers["city"] == "Казань"
        assert answers["tz"] == 3
        assert answers["work_format"] == ["remote"]


# ---------------------------------------------------------------------------
# ASR (без внешних запросов; ffmpeg-зависимое пропускается)
# ---------------------------------------------------------------------------


class TestAsr:
    def test_multipart_encoding(self):
        body, ctype = asr.multipart_encode(
            {"model": "glm-asr-2512", "stream": "false"},
            "file", "audio.wav", b"RIFFxxxx")
        assert "multipart/form-data" in ctype
        assert b'name="model"' in body and b"glm-asr-2512" in body
        assert b'filename="audio.wav"' in body
        assert body.endswith(b"\r\n--" + b"----marsijobboundary7f3a9c2d--\r\n")

    def test_split_points_short_audio(self):
        assert asr.split_points(20.0, []) == []

    def test_split_points_long_audio_no_silence(self):
        points = asr.split_points(120.0, [])
        assert len(points) >= 3
        # сегменты ≤ 28 с
        bounds = [0.0] + points + [120.0]
        for a, b in zip(bounds, bounds[1:]):
            assert b - a <= 28.0 + 0.01

    def test_split_points_prefers_silences(self):
        # паузы на 14–16 и 40–42: режем по 15 и 41, а не по жёстким 28/56
        points = asr.split_points(100.0, [(14.0, 16.0), (40.0, 42.0)])
        assert 14.0 <= points[0] <= 16.0
        assert 40.0 <= points[1] <= 42.0
        bounds = [0.0] + points + [100.0]
        for a, b in zip(bounds, bounds[1:]):
            assert b - a <= 28.0 + 0.01

    @pytest.mark.skipif(not asr.ffmpeg_available(), reason="ffmpeg не установлен")
    def test_ogg_to_wav_needs_ffmpeg(self):
        with pytest.raises(asr.AsrError):
            asr.ogg_to_wav(b"not an ogg")  # мусорный вход → понятная ошибка

    def test_transcribe_segment_mock(self):
        payload = {"text": "привет"}
        with patch("marsijob.asr.urllib.request.urlopen") as mock:
            cm = mock.return_value
            cm.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            text = asr.transcribe_segment(b"RIFF", "key")
        assert text == "привет"
        sent = mock.call_args[0][0]
        assert sent.headers["Authorization"] == "Bearer key"

    def test_transcribe_segment_error(self):
        bad = {"error": {"code": "1211", "message": "model not found"}}
        with patch("marsijob.asr.urllib.request.urlopen") as mock:
            cm = mock.return_value
            cm.__enter__.return_value.read.return_value = json.dumps(bad).encode()
            with pytest.raises(asr.AsrError):
                asr.transcribe_segment(b"RIFF", "key")

    def test_transcribe_voice_chains_prompts(self):
        segments = [b"seg1", b"seg2", b"seg3"]
        with patch("marsijob.asr.ogg_to_wav", return_value=b"wav"), \
             patch("marsijob.asr.split_wav", return_value=segments), \
             patch("marsijob.asr.transcribe_segment", side_effect=["Раз.", "Два.", "Три."]) as ts:
            text = asr.transcribe_voice(b"ogg", "key")
        assert text == "Раз. Два. Три."
        # второй сегмент получает контекст первого
        second_prompt = ts.call_args_list[1].kwargs.get("prompt", "")
        assert "Раз." in second_prompt


# ---------------------------------------------------------------------------
# Интервьюер (моки LLM)
# ---------------------------------------------------------------------------


class TestInterviewer:
    def test_is_finished(self):
        from marsijob.interviewer import is_finished
        assert is_finished("ГОТОВО")
        assert is_finished("Спасибо! Готово")
        assert not is_finished("Какой у вас опыт?")

    def test_next_question_mock(self):
        reply = {"choices": [{"message": {"content": "Какой у вас опыт работы?"}}]}
        from marsijob import interviewer as iv
        with patch("marsijob.interviewer.urllib.request.urlopen") as mock:
            cm = mock.return_value
            cm.__enter__.return_value.read.return_value = json.dumps(reply).encode()
            out = iv.next_question([{"role": "user", "content": "я разработчик"}], "k",
                                   "https://open.bigmodel.cn/api/paas/v4", "glm-4.6")
        assert "опыт" in out

    def test_parse_dialog_valid(self):
        parsed_json = {"experience_years": 3, "work_format": ["remote"]}
        reply = {"choices": [{"message": {"content": json.dumps(parsed_json)}}]}
        from marsijob import interviewer as iv
        with patch("marsijob.interviewer.urllib.request.urlopen") as mock:
            cm = mock.return_value
            cm.__enter__.return_value.read.return_value = json.dumps(reply).encode()
            parsed = iv.parse_dialog("диалог", "k", "https://open.bigmodel.cn/api/paas/v4", "glm-4.6")
        assert parsed == parsed_json

    def test_parse_dialog_invalid_schema(self):
        parsed_json = {"age": 33}
        reply = {"choices": [{"message": {"content": json.dumps(parsed_json)}}]}
        from marsijob import interviewer as iv
        with patch("marsijob.interviewer.urllib.request.urlopen") as mock:
            cm = mock.return_value
            cm.__enter__.return_value.read.return_value = json.dumps(reply).encode()
            with pytest.raises(iv.InterviewerError):
                iv.parse_dialog("диалог", "k", "https://open.bigmodel.cn/api/paas/v4", "glm-4.6")


# ---------------------------------------------------------------------------
# UI-клавиатуры
# ---------------------------------------------------------------------------


class TestUi:
    def test_single_keyboard_packs_callback(self):
        from marsijob.ui import single_keyboard
        q = wizard.q_by_key("education_level")
        kb = single_keyboard(q, skippable=True)
        labels = [b.text for row in kb.inline_keyboard for b in row]
        assert any("Пропустить" in t for t in labels)

    def test_multi_keyboard_toggle_marks(self):
        from marsijob.ui import multi_keyboard
        q = wizard.q_by_key("work_format")
        kb = multi_keyboard(q, [0], skippable=False)
        flat = [b.text for row in kb.inline_keyboard for b in row]
        assert any(t.startswith("✅") for t in flat)
        assert any(t.startswith("▫️") for t in flat)
        assert any("Далее" in t for t in flat)

    def test_apply_answer_nested_salary(self):
        profile = {}
        apply_answer(profile, wizard.q_by_key("salary_min"), 80000)
        apply_answer(profile, wizard.q_by_key("salary_max"), 130000)
        assert profile["salary"]["min"] == 80000
        assert profile["salary"]["max"] == 130000

    def test_apply_answer_motivation_weights(self):
        profile = {}
        apply_answer(profile, wizard.q_by_key("motivation_1"), "growth")
        apply_answer(profile, wizard.q_by_key("motivation_2"), "money")
        assert profile["motivation"]["growth"] == 1.0
        assert profile["motivation"]["money"] == 0.7
        assert profile["motivation"]["stability"] == 0.3
