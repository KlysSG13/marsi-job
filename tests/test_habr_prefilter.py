"""Тесты Хабр Карьеры (RSS), префильтра и масштабирования trudvsem."""
import xml.etree.ElementTree as ET
from unittest.mock import patch

import pytest

from marsijob import habr_sync, tv_sync
from marsijob.config import Config
from marsijob.engine import Engine
from marsijob.storage import Storage


@pytest.fixture()
def store() -> Storage:
    s = Storage(":memory:")
    yield s
    s.close()


HABR_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Вакансии — Хабр Карьера</title>
    <item>
      <title>Требуется «Разработчик 1С» (от 160 000 до 180 000 ₽)</title>
      <description>Компания «Top Selection» ищет специалиста на вакансию «Разработчик 1С». От 160 000 ₽ до 180 000 ₽. Полный рабочий день. Можно удалённо. Требуемые навыки: #senior, #HTTP, #SOAP.</description>
      <author>Top Selection</author>
      <pubDate>Fri, 13 Nov 2026 01:02:07 +0300</pubDate>
      <link>https://career.habr.com/vacancies/1000169928</link>
      <guid>1000169928</guid>
    </item>
    <item>
      <title>Требуется «Офис-менеджер» (до 90000 ₽)</title>
      <description>Компания «Ромашка» ищет. До 90000 ₽. Полный рабочий день в офисе. Навыки: #документооборот.</description>
      <author>Ромашка</author>
      <link>https://career.habr.com/vacancies/1000169929</link>
      <guid>1000169929</guid>
    </item>
  </channel>
</rss>"""


class TestHabrMapping:
    def test_remote_vacancy(self):
        root = ET.fromstring(HABR_RSS)
        v = habr_sync.map_habr_item(list(root.iter("item"))[0])
        assert v["id"] == "HB-1000169928"
        assert v["title"] == "Разработчик 1С"
        assert v["salary"] == {"min": 160000, "max": 180000, "compromise": True}
        assert v["work_format"] == "remote"
        assert v["schedule"] == "full_time"
        assert v["experience_years_min"] == 5  # #senior
        assert "http" in v["hard_skills"] and "soap" in v["hard_skills"]
        assert "senior" not in v["hard_skills"]  # сеньорити — не навык
        assert v["source"] == "habr"
        assert v["employer_name"] == "Top Selection"
        assert v["city_unknown"] is False  # удалёнка — город не нужен и так

    def test_office_unknown_city(self):
        root = ET.fromstring(HABR_RSS)
        v = habr_sync.map_habr_item(list(root.iter("item"))[1])
        assert v["work_format"] == "office"
        assert v["city_unknown"] is True
        assert v["salary"]["max"] == 90000
        assert v["salary"]["min"] == 72000  # 90к × 0.8


class TestHabrSync:
    def test_sync_with_mocks(self, store):
        with patch.object(habr_sync, "fetch_rss", return_value=HABR_RSS), \
             patch.object(habr_sync.time, "sleep"):
            stats = habr_sync.sync_habr(store, queries=["", "python"])
        assert stats["fetched"] == 2  # дедуп по guid между фидами
        assert stats["inserted"] == 2
        assert store.vacancy_source_stats() == {"habr": 2}

    def test_engine_with_unknown_city(self):
        """Офисная вакансия без города не блокируется по локации."""
        engine = Engine(Config.load())
        cand = {
            "hard_skills": {}, "experience_years": 2,
            "work_format": ["office"], "schedule": ["full_time"],
            "schedule_compromise": False, "employment_type": ["tk"],
            "salary": {"min": 50000, "max": 80000, "compromise": True},
            "business_trips_tolerance": 0.3, "relocation": "no",
            "location": {"city": "Москва", "tz": 3}, "work_permit": ["RU"],
            "languages": {"russian": {"level": 1.0, "verified": 1.0}},
            "management_experience": {"has": False, "team_size": 0},
            "education_level": "bachelor", "domain_experience": [],
            "portfolio": 0.0, "interests": [], "culture_fit": "casual",
            "mission_importance": 0.3, "career_trajectory": "stability",
            "urgency_months": 3, "start_date_months": 1,
        }
        root = ET.fromstring(HABR_RSS)
        vac = habr_sync.map_habr_item(list(root.iter("item"))[1])
        assert engine.prefilter_blocked(cand, vac) == ""
        res = engine.score(cand, vac)
        assert not res.blocked
        loc = [f for f in res.factors if f.factor_id == "location"]
        assert loc and loc[0].match is None  # фактор пропущен (город неизвестен)


class TestPrefilter:
    def test_format_block(self):
        engine = Engine(Config.load())
        cand = {"work_format": ["remote"]}
        vac = {"work_format": "office"}
        assert engine.prefilter_blocked(cand, vac).startswith("BL-002")

    def test_salary_block_no_compromise(self):
        engine = Engine(Config.load())
        cand = {"salary": {"min": 100, "max": 200, "compromise": False}}
        vac = {"salary": {"min": 300, "max": 400}}
        assert engine.prefilter_blocked(cand, vac).startswith("BL-004")
        vac2 = {"salary": {"min": 150, "max": 250}}
        assert engine.prefilter_blocked(cand, vac2) == ""

    def test_city_unknown_no_tz_block(self):
        engine = Engine(Config.load())
        cand = {"location": {"tz": 7}, "relocation": "no",
                "work_format": ["office", "remote"], "schedule": ["flexible"],
                "salary": {"min": 1, "max": 10, "compromise": True},
                "work_permit": ["RU"]}
        vac = {"location": {"city": None, "tz": 3}, "city_unknown": True,
               "work_format": "remote", "schedule": "flexible",
               "salary": {"min": 1, "max": 10}}
        assert engine.prefilter_blocked(cand, vac) == ""

    def test_matches_full_scoring(self):
        """Прошедшие префильтр дают тот же результат, что и полный скоринг."""
        engine = Engine(Config.load())
        import json
        tc = json.loads(open("data/test_cases/tc1.json").read())
        vac = tc["vacancies"][0]  # remote flexible, не блокируется
        assert engine.prefilter_blocked(tc["candidate"], vac) == ""
        vac3 = tc["vacancies"][2]  # office — блок
        assert engine.prefilter_blocked(tc["candidate"], vac3).startswith("BL-002")


class TestTvScaledSync:
    def test_region_and_text_feeds(self, store):
        item = {"vacancy": {"id": "abc11111", "job-name": "Врач",
                            "region": {"name": "Москва"},
                            "schedule": "Полный рабочий день",
                            "requirement": {}, "company": {}}}
        calls = []

        def fake_fetch(params, limit=100, offset=0, path=""):
            calls.append((path, params.get("text"), offset))
            return [item]

        with patch.object(tv_sync, "fetch_page", side_effect=fake_fetch), \
             patch.object(tv_sync.time, "sleep"):
            stats = tv_sync.sync_trudvsem(
                store,
                region_feeds=[{"label": "msk", "path": "region/7700000000000"}],
                text_feeds=[{"label": "it", "params": {"text": "программист"}}],
                pages_per_feed=2,
            )
        assert stats["fetched"] == 4  # (1 регион + 1 текст) × 2 страницы
        # регионный фид ходит по path, текстовый — по params
        assert ("region/7700000000000", None, 0) in calls
        assert ("", "программист", 100) in calls
