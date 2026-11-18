"""Тесты автосинка hh.ru: маппинг, дедуп, чистка, статистика."""
import json
from unittest.mock import patch

import pytest

from marsijob import hh_sync
from marsijob.config import Config
from marsijob.engine import Engine
from marsijob.storage import Storage


@pytest.fixture()
def store() -> Storage:
    s = Storage(":memory:")
    yield s
    s.close()


HH_DETAIL = {
    "id": "12345",
    "name": "Python-разработчик",
    "alternate_url": "https://hh.ru/vacancy/12345",
    "salary": {"from": 150000, "to": 250000, "currency": "RUR", "gross": True},
    "area": {"name": "Москва"},
    "experience": {"id": "between3And6"},
    "schedule": {"id": "fullDay"},
    "employment": {"id": "full"},
    "employer": {"name": "ООО Ромашка"},
    "key_skills": [{"name": "Python"}, {"name": "Django"}, {"name": "SQL"}],
}


class TestMapping:
    def test_basic_mapping(self):
        v = hh_sync.map_hh_vacancy(HH_DETAIL)
        assert v["id"] == "HH-12345"
        assert v["title"] == "Python-разработчик"
        assert v["experience_years_min"] == 3
        assert v["salary"] == {"min": 150000, "max": 250000, "compromise": True}
        assert v["work_format"] == "office"
        assert v["schedule"] == "full_time"
        assert v["location"] == {"city": "Москва", "tz": 3}
        assert v["hard_skills"]["python"] == {"req_level": 0.6, "must": False}
        assert v["source"] == "hh"
        assert v["trust_index"] == 0.55
        assert v["employer_name"] == "ООО Ромашка"

    def test_remote_mapping(self):
        d = dict(HH_DETAIL, schedule={"id": "remote"})
        v = hh_sync.map_hh_vacancy(d)
        assert v["work_format"] == "remote"
        assert v["schedule"] == "flexible"
        assert v["location"]["city"] is None

    def test_non_rur_salary_omitted(self):
        d = dict(HH_DETAIL, salary={"from": 3000, "currency": "USD"})
        v = hh_sync.map_hh_vacancy(d)
        assert "salary" not in v

    def test_partial_salary_rur(self):
        d = dict(HH_DETAIL, salary={"to": 200000, "currency": "RUR"})
        v = hh_sync.map_hh_vacancy(d)
        assert v["salary"]["min"] == 160000  # 200к × 0.8
        assert v["salary"]["max"] == 200000

    def test_part_time_employment(self):
        d = dict(HH_DETAIL, employment={"id": "part"})
        v = hh_sync.map_hh_vacancy(d)
        assert v["schedule"] == "part_time"

    def test_mapped_vacancy_scores_in_engine(self):
        """Смапленная вакансия совместима с движком (не падает, даёт скор).\"\"\""""
        engine = Engine(Config.load())
        candidate = {
            "hard_skills": {"python": {"level": 0.8, "verified": 0.5}},
            "experience_years": 5, "work_format": ["remote", "hybrid", "office"],
            "schedule": ["full_time", "flexible"], "schedule_compromise": False,
            "employment_type": ["tk", "gph"], "salary": {"min": 100000, "max": 300000,
                                                          "compromise": True},
            "business_trips_tolerance": 0.3, "relocation": "no",
            "location": {"city": "Москва", "tz": 3}, "work_permit": ["RU"],
            "languages": {"russian": {"level": 1.0, "verified": 1.0}},
            "experience": None, "management_experience": {"has": False, "team_size": 0},
            "education_level": "bachelor", "domain_experience": ["it"],
            "portfolio": 0.5, "interests": [], "culture_fit": "casual",
            "mission_importance": 0.4, "career_trajectory": "deep",
            "urgency_months": 2, "start_date_months": 1,
        }
        res = engine.score(candidate, hh_sync.map_hh_vacancy(HH_DETAIL))
        assert not res.blocked
        assert res.score > 0.4


class TestSyncFlow:
    def test_sync_once_with_mocks(self, store):
        list_payload = {"items": [
            {"id": "1", "name": "V1"}, {"id": "2", "name": "V2"},
        ]}
        details = {
            "1": dict(HH_DETAIL, id="1"),
            "2": dict(HH_DETAIL, id="2", salary={"from": 100, "currency": "USD"}),
        }
        calls = {"n": 0}

        def fake_get(url, timeout=30, token=None):
            calls["n"] += 1
            if "/vacancies?" in url:
                return list_payload
            vid = url.rsplit("/", 1)[-1]
            return details[vid]

        with patch.object(hh_sync, "_get", side_effect=fake_get), \
             patch.object(hh_sync.time, "sleep"):
            stats = hh_sync.sync_once(store, feeds=[{"label": "t", "params": {"area": 1}}],
                                      fetch_details=True, token="tok")
        assert stats["fetched"] == 2
        assert stats["inserted"] == 2
        assert store.count_active_vacancies() == 2

        # повторный прогон — обновление, не дубли
        with patch.object(hh_sync, "_get", side_effect=fake_get), \
             patch.object(hh_sync.time, "sleep"):
            stats2 = hh_sync.sync_once(store, feeds=[{"label": "t", "params": {"area": 1}}],
                                       fetch_details=True, token="tok")
        assert stats2["inserted"] == 0
        assert stats2["updated"] == 2
        assert store.count_active_vacancies() == 2

    def test_prune_closes_stale(self, store):
        emp = store.ensure_external_employer("hh.ru")
        vac = hh_sync.map_hh_vacancy(HH_DETAIL)
        store.upsert_external_vacancy(vac, emp)
        # подделываем дату создания
        store.conn.execute(
            "UPDATE vacancies SET created_at = datetime('now', '-20 days') "
            "WHERE data_json LIKE '%HH-12345%'")
        store.conn.commit()
        closed = store.prune_external_vacancies(stale_days=14, delete_after_days=60)
        assert closed == 1
        assert store.count_active_vacancies() == 0

    def test_direct_vacancies_not_pruned(self, store):
        user = store.upsert_user(100, "employer")
        store.insert_vacancy(user, {"id": "E-1", "title": "Прямая вакансия",
                                    "source": None})
        store.conn.execute("UPDATE vacancies SET created_at = datetime('now', '-30 days')")
        store.conn.commit()
        closed = store.prune_external_vacancies(stale_days=14, delete_after_days=60)
        assert closed == 0
        assert store.count_active_vacancies() == 1

    def test_source_stats(self, store):
        user = store.upsert_user(100, "employer")
        store.insert_vacancy(user, {"id": "E-1", "title": "Прямая"})
        emp = store.ensure_external_employer("hh.ru")
        store.upsert_external_vacancy(hh_sync.map_hh_vacancy(HH_DETAIL), emp)
        stats = store.vacancy_source_stats()
        assert stats == {"direct": 1, "hh": 1}


class TestSlug:
    def test_slug(self):
        assert hh_sync.slug("CI/CD") == "ci/cd"
        assert hh_sync.slug("Machine   Learning") == "machine_learning"
