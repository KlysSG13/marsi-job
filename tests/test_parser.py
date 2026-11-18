"""Тесты парсера резюме: эвристика, валидация схемы, слияние, ошибки API."""
import json
from typing import Any, Dict
from unittest.mock import patch

import pytest

from marsijob.parser_llm import (
    ParserError, merge_profile, parse_resume, parse_resume_api,
    parse_resume_heuristic, validate_parsed,
)

RESUME = """
Python-разработчик, middle. Опыт 4 года в финтехе.
Навыки: Python (уверенный), SQL, Docker, Git. Есть сертификат по SQL.
Город: Казань. Хочу удалёнку или гибрид, гибкий график.
Зарплата от 150000 до 220000. Английский B1.
Руководил командой из 5 человек.
"""

RESUME_POOR = "Ищу работу, готов обсудить условия."


class TestHeuristic:
    def test_skills_extracted(self):
        p = parse_resume_heuristic(RESUME)
        skills = p["hard_skills"]
        assert "python" in skills and "sql" in skills and "docker" in skills
        assert skills["python"]["level"] == 0.7  # «уверенный»

    def test_experience(self):
        p = parse_resume_heuristic(RESUME)
        assert p["experience_years"] == 4

    def test_city(self):
        p = parse_resume_heuristic(RESUME)
        assert p["location"]["city"] == "Казань"

    def test_formats_and_schedule(self):
        p = parse_resume_heuristic(RESUME)
        assert set(p["work_format"]) == {"remote", "hybrid"}
        assert p["schedule"] == ["flexible"]

    def test_salary(self):
        p = parse_resume_heuristic(RESUME)
        assert p["salary"]["min"] == 150000
        assert p["salary"]["max"] == 220000

    def test_english(self):
        p = parse_resume_heuristic(RESUME)
        assert p["languages"]["english"]["level"] == 0.5

    def test_management(self):
        p = parse_resume_heuristic(RESUME)
        assert p["management_experience"] == {"has": True, "team_size": 5}

    def test_domain(self):
        p = parse_resume_heuristic(RESUME)
        assert "fintech" in p["domain_experience"]

    def test_poor_text_yields_little(self):
        p = parse_resume_heuristic(RESUME_POOR)
        assert "hard_skills" not in p
        assert validate_parsed(p)[0] is True


class TestValidate:
    def test_valid_profile_passes(self):
        p = parse_resume_heuristic(RESUME)
        ok, errors = validate_parsed(p)
        assert ok, errors
    def test_unknown_field_rejected(self):
        ok, errors = validate_parsed({"age": 30, "gender": "m"})
        assert not ok

    def test_bad_skill_level_rejected(self):
        ok, _e = validate_parsed({"hard_skills": {"python": {"level": 5, "verified": 0.3}}})
        assert not ok

    def test_bad_domain_rejected(self):
        ok, _e = validate_parsed({"domain_experience": ["space"]})
        assert not ok

    def test_salary_only_min_normalised(self):
        """Речь часто называет только минимум — достраиваем максимум."""
        parsed = {"salary": {"min": "150000"}}
        ok, errors = validate_parsed(parsed)
        assert ok, errors
        assert parsed["salary"]["max"] == 180000  # 150k × 1.2

    def test_salary_only_max_normalised(self):
        parsed = {"salary": {"max": "220к"}}
        ok, _e = validate_parsed(parsed)
        assert ok
        assert parsed["salary"]["min"] == 176000  # 220k × 0.8
        assert parsed["salary"]["max"] == 220000

    def test_salary_no_numbers_rejected(self):
        ok, _e = validate_parsed({"salary": {"min": "договорная"}})
        assert not ok


class TestMerge:
    def test_merge_preserves_unrelated(self):
        current = {
            "languages": {"russian": {"level": 1.0, "verified": 1.0}},
            "neuro_profile": ["adhd"],
            "interests": ["games"],
        }
        parsed = parse_resume_heuristic(RESUME)
        merged = merge_profile(parsed, current)
        assert merged["neuro_profile"] == ["adhd"]
        assert merged["languages"]["russian"]["level"] == 1.0
        assert merged["languages"]["english"]["level"] == 0.5
        assert merged["hard_skills"]["python"]["level"] == 0.7

    def test_merge_rejects_invalid(self):
        merged = merge_profile({"age": 30}, {"interests": ["games"]})
        assert "age" not in merged


class TestApi:
    def _mock_response(self, content: Dict[str, Any]):
        return {"choices": [{"message": {"content": json.dumps(content)}}]}

    def test_api_payload_has_max_tokens(self):
        """Полезный payload: max_tokens задан, для bigmodel — thinking отключён."""
        import marsijob.parser_llm as pl
        captured = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(self._mock_response({"experience_years": 1})).encode()

        with patch("marsijob.parser_llm.urllib.request.urlopen") as mock_urlopen:
            cm = mock_urlopen.return_value
            cm.__enter__.return_value.read.return_value = json.dumps(
                self._mock_response({"experience_years": 1})).encode("utf-8")
            parse_resume_api("резюме", api_key="k",
                             base_url="https://open.bigmodel.cn/api/paas/v4",
                             model="glm-4.6")
            sent = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        assert sent["max_tokens"] >= 1000
        assert sent["thinking"] == {"type": "disabled"}

    def test_api_happy_path(self):
        good = {"experience_years": 3, "work_format": ["remote"]}
        with patch("marsijob.parser_llm.urllib.request.urlopen") as mock_urlopen:
            cm = mock_urlopen.return_value
            cm.__enter__.return_value.read.return_value = json.dumps(
                self._mock_response(good)).encode("utf-8")
            result = parse_resume_api("резюме", api_key="k")
        assert result == good

    def test_api_invalid_json_raises(self):
        with patch("marsijob.parser_llm.urllib.request.urlopen") as mock_urlopen:
            cm = mock_urlopen.return_value
            cm.__enter__.return_value.read.return_value = b"not json"
            with pytest.raises(ParserError):
                parse_resume_api("резюме", api_key="k")

    def test_api_schema_violation_raises(self):
        bad = {"age": 99}
        with patch("marsijob.parser_llm.urllib.request.urlopen") as mock_urlopen:
            cm = mock_urlopen.return_value
            cm.__enter__.return_value.read.return_value = json.dumps(
                self._mock_response(bad)).encode("utf-8")
            with pytest.raises(ParserError):
                parse_resume_api("резюме", api_key="k")

    def test_fallback_when_no_key(self):
        p = parse_resume(RESUME, api_key=None)
        assert "hard_skills" in p
