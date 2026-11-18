"""Юнит-тесты примитивов нормализации и близости (SPEC §2.1)."""
import pytest

from marsijob.engine import (
    clamp01, log_norm, match_scalar, match_scalar_stress, match_scalar_inverse,
    match_range_overlap, match_duration, match_team_size, cosine,
)


class TestNormalize:
    def test_clamp(self):
        assert clamp01(-1) == 0.0
        assert clamp01(0.5) == 0.5
        assert clamp01(2) == 1.0

    def test_log_norm_bounds(self):
        assert log_norm(0, 100) == 0.0
        assert log_norm(100, 100) == 1.0
        assert 0.0 < log_norm(10, 100) < 1.0

    def test_log_norm_flattens_outliers(self):
        # 10 из 100 должно быть заметно больше 0.1 (логарифм «сплющивает» масштаб)
        assert log_norm(10, 100) > 0.1


class TestCloseness:
    def test_scalar(self):
        assert match_scalar(0.2, 0.2) == 1.0
        assert match_scalar(0.0, 1.0) == 0.0
        assert match_scalar(0.3, 0.6) == pytest.approx(0.7)

    def test_scalar_stress(self):
        assert match_scalar_stress(0.8, 0.5) == 1.0      # толерантности хватает
        assert match_scalar_stress(0.5, 0.5) == 1.0
        # перегруз 0.2 при k=2 → 1 − 0.4 = 0.6
        assert match_scalar_stress(0.5, 0.7, k=2) == pytest.approx(0.6)
        # k=3 (сенсорика): перегруз 0.4 → 1 − 1.2 → 0
        assert match_scalar_stress(0.1, 0.5, k=3) == 0.0

    def test_scalar_inverse(self):
        assert match_scalar_inverse(0.8, 0.2) == 1.0      # автономия высокая, контроль низкий
        assert match_scalar_inverse(0.8, 0.8) == pytest.approx(0.4)  # 1 − (0.6)
        assert match_scalar_inverse(1.0, 1.0) == 0.0

    def test_range_overlap(self):
        assert match_range_overlap([0, 10], [5, 15]) == pytest.approx(0.5)
        assert match_range_overlap([0, 10], [10, 20]) == 0.0
        assert match_range_overlap([0, 10], [0, 10]) == 1.0
        assert match_range_overlap([350, 450], [300, 400]) == pytest.approx(0.5)

    def test_duration(self):
        assert match_duration(8, 6) == 1.0
        assert match_duration(3, 6) == pytest.approx(0.5)
        assert match_duration(0, 6) == 0.0

    def test_team_size(self):
        assert match_team_size([2, 10], 6) == 1.0
        assert match_team_size([2, 10], 15) == pytest.approx(0.5)  # dist=5 → 1/(1+1)
        assert match_team_size([1, 6], 30) < 0.2

    def test_cosine(self):
        assert cosine({"a": 1.0}, {"a": 1.0}) == 1.0
        assert cosine({"a": 1.0}, {"b": 1.0}) == 0.0
        assert 0 < cosine({"a": 1, "b": 1}, {"a": 1, "b": 0.5}) < 1


class TestCaseInsensitiveSkills:
    def test_llm_capitalized_skill_matches(self):
        """LLM-парсер может вернуть «Python» — движок сравнивает без регистра."""
        from marsijob.engine import Engine as E
        from marsijob.config import Config as C
        import pytest as _pt
        eng = E(C.load())
        cand_skills = {"Python": {"level": 0.7, "verified": 0.5}}
        vac_skills = {"python": {"req_level": 0.7, "must": True}}
        ok, must = eng._match_set("hard_skills", cand_skills, vac_skills)
        assert _pt.approx(ok, abs=1e-6) == 0.8  # coverage 1.0 × trust (0.6+0.4·0.5)
        assert must is True

    def test_blocker_case_insensitive(self):
        from marsijob.engine import Engine as E
        from marsijob.config import Config as C
        eng = E(C.load())
        cand_skills = {"Python": {"level": 0.7, "verified": 0.5}}
        # уровень 0.7 ≥ 0.8·0.8 = 0.64 — блокировки нет
        vac_pass = {"python": {"req_level": 0.8, "must": True}}
        assert eng._check_blocker("hard_skills", cand_skills, vac_pass, 1.0, {}, {}) is None
        # уровень 0.7 < 0.8·0.9 = 0.72 — блок, навык с заглавной найден
        vac_block = {"python": {"req_level": 0.9, "must": True}}
        assert eng._check_blocker("hard_skills", cand_skills, vac_block, 0.5, {}, {}) == "BL-001:hard_skills:python"
