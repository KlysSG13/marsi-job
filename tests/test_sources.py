"""Тесты адаптера «Работа России» (trudvsem) и оркестратора источников."""
from unittest.mock import patch

import pytest

from marsijob import sources, tv_sync
from marsijob.config import Config
from marsijob.engine import Engine
from marsijob.storage import Storage


@pytest.fixture()
def store() -> Storage:
    s = Storage(":memory:")
    yield s
    s.close()


TV_ITEM = {
    "vacancy": {
        "id": "87e78340-63d0-11f1-b1a3-adad5cc6cbe1",
        "job-name": "Зубной врач",
        "salary_min": 50000,
        "salary_max": 56000,
        "region": {"name": "Свердловская область"},
        "addresses": {"address": [
            {"location": "Свердловская область, г Полевской, Коммунистическая улица, дом: 42;"}
        ]},
        "schedule": "Полный рабочий день",
        "requirement": {"education": "Среднее профессиональное", "experience": 3},
        "category": {"specialisation": "Здравоохранение и социальное обеспечение"},
        "company": {"name": "ГАУЗ СО \"ПОЛЕВСКАЯ СП\""},
        "vac_url": "https://trudvsem.ru/vacancy/card/1/2",
        "skills": ["Стоматология"],
    }
}


class TestTvMapping:
    def test_basic(self):
        v = tv_sync.map_tv_vacancy(TV_ITEM)
        assert v["id"].startswith("TV-")
        assert v["title"] == "Зубной врач"
        assert v["salary"] == {"min": 50000, "max": 56000, "compromise": True}
        assert v["schedule"] == "full_time"
        assert v["domain"] == "healthcare"
        assert v["education_level_min"] == "vocational"
        assert v["experience_years_min"] == 3
        assert v["location"]["city"] == "Полевской"
        assert v["location"]["tz"] == 5  # Свердловская область
        assert v["source"] == "trudvsem"
        assert v["hard_skills"]["стоматология"]["req_level"] == 0.6

    def test_salary_only_min(self):
        item = {"vacancy": dict(TV_ITEM["vacancy"], salary_max=None, salary_min=40000)}
        v = tv_sync.map_tv_vacancy(item)
        assert v["salary"]["max"] == 48000

    def test_no_salary(self):
        item = {"vacancy": dict(TV_ITEM["vacancy"], salary_min=None, salary_max=None)}
        v = tv_sync.map_tv_vacancy(item)
        assert "salary" not in v

    def test_engine_compatibility(self):
        engine = Engine(Config.load())
        candidate = {
            "hard_skills": {}, "experience_years": 5,
            "work_format": ["office"], "schedule": ["full_time", "shift"],
            "schedule_compromise": False, "employment_type": ["tk"],
            "salary": {"min": 40000, "max": 60000, "compromise": True},
            "business_trips_tolerance": 0.3, "relocation": "no",
            "location": {"city": "Полевской", "tz": 5}, "work_permit": ["RU"],
            "languages": {"russian": {"level": 1.0, "verified": 1.0}},
            "management_experience": {"has": False, "team_size": 0},
            "education_level": "vocational", "domain_experience": ["healthcare"],
            "portfolio": 0.0, "interests": [], "culture_fit": "casual",
            "mission_importance": 0.3, "career_trajectory": "stability",
            "urgency_months": 3, "start_date_months": 1,
        }
        res = engine.score(candidate, tv_sync.map_tv_vacancy(TV_ITEM))
        assert not res.blocked
        assert res.score > 0.5


class TestTvSync:
    def test_sync_trudvsem_with_mocks(self, store):
        payload = [TV_ITEM, {"vacancy": {**TV_ITEM["vacancy"], "id": "2222",
                                             "job-name": "Врач-терапевт"}}]
        with patch.object(tv_sync, "fetch_page", return_value=payload), \
             patch.object(tv_sync.time, "sleep"):
            stats = tv_sync.sync_trudvsem(
                store, region_feeds=[{"label": "msk", "path": "region/7700000000000"}],
                text_feeds=[], pages_per_feed=1)
        assert stats == {"fetched": 2, "inserted": 2, "updated": 0}
        assert store.count_active_vacancies() == 2

        with patch.object(tv_sync, "fetch_page", return_value=payload), \
             patch.object(tv_sync.time, "sleep"):
            stats2 = tv_sync.sync_trudvsem(
                store, region_feeds=[{"label": "msk", "path": "region/7700000000000"}],
                text_feeds=[], pages_per_feed=1)
        assert stats2["updated"] == 2 and stats2["inserted"] == 0

    def test_bad_items_skipped(self, store):
        with patch.object(tv_sync, "fetch_page",
                          return_value=[{"vacancy": {"id": "x"}}, TV_ITEM]), \
             patch.object(tv_sync.time, "sleep"):
            stats = tv_sync.sync_trudvsem(
                store, region_feeds=[{"label": "t", "path": "region/1"}],
                text_feeds=[], pages_per_feed=1)
        assert stats["fetched"] == 2
        assert store.count_active_vacancies() == 2

    def test_prune_covers_trudvsem(self, store):
        emp = store.ensure_external_employer("trudvsem.ru")
        store.upsert_external_vacancy(tv_sync.map_tv_vacancy(TV_ITEM), emp)
        store.conn.execute(
            "UPDATE vacancies SET created_at = datetime('now', '-20 days') "
            "WHERE data_json LIKE '%trudvsem%'")
        store.conn.commit()
        assert store.prune_external_vacancies() == 1


class TestOrchestrator:
    def test_sync_all_reports(self, store):
        with patch.object(tv_sync, "fetch_page", return_value=[TV_ITEM]), \
             patch.object(tv_sync.time, "sleep"), \
             patch.object(sources.habr_sync, "fetch_rss", return_value=""), \
             patch.object(sources.habr_sync.time, "sleep"), \
             patch.object(sources, "hh_token_from_env", return_value=""):
            report = sources.sync_all(store)
        assert report["trudvsem"]["inserted"] >= 1
        assert report["habr"].get("fetched", 0) == 0  # пустой RSS
        assert report["hh"] == {"skipped": 1}

    def test_sync_all_with_hh_token(self, store):
        hh_detail = {
            "id": "999", "name": "Dev", "salary": {"from": 100, "to": 200,
                                                    "currency": "RUR"},
            "area": {"name": "Москва"}, "experience": {"id": "noExperience"},
            "schedule": {"id": "remote"}, "employment": {"id": "full"},
            "employer": {"name": "Co"}, "key_skills": [],
        }
        with patch.object(tv_sync, "fetch_page", return_value=[]), \
             patch.object(tv_sync.time, "sleep"), \
             patch.object(sources.habr_sync, "fetch_rss", return_value=""), \
             patch.object(sources.habr_sync.time, "sleep"), \
             patch.object(sources, "hh_token_from_env", return_value="tok"), \
             patch.object(sources.hh_sync, "fetch_feed",
                          return_value=[{"id": "999", "name": "Dev"}]), \
             patch.object(sources.hh_sync, "fetch_detail", return_value=hh_detail), \
             patch.object(sources.hh_sync.time, "sleep"):
            report = sources.sync_all(store)
        assert report["hh"]["inserted"] == 1
        stats = store.vacancy_source_stats()
        assert stats.get("hh") == 1

    def test_sync_once_requires_token(self, store):
        from marsijob.hh_sync import HhError, sync_once
        with pytest.raises(HhError):
            sync_once(store)


class TestTvExperienceParsing:
    def test_experience_string(self):
        item = {"vacancy": {**TV_ITEM["vacancy"],
                            "requirement": {"experience": "от 1 года"}}}
        v = tv_sync.map_tv_vacancy(item)
        assert v["experience_years_min"] == 1

    def test_experience_from_zero(self):
        item = {"vacancy": {**TV_ITEM["vacancy"],
                            "requirement": {"experience": "от 0"}}}
        v = tv_sync.map_tv_vacancy(item)
        assert v["experience_years_min"] == 0

    def test_experience_number(self):
        item = {"vacancy": {**TV_ITEM["vacancy"],
                            "requirement": {"experience": 3}}}
        v = tv_sync.map_tv_vacancy(item)
        assert v["experience_years_min"] == 3
