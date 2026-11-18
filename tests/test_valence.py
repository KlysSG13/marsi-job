"""Тесты зоны валентности: первопричины и подсказки."""
import json
from pathlib import Path

import pytest

from marsijob.config import Config
from marsijob.engine import Engine
from marsijob.valence import valence_zone, root_causes

DATA = Path(__file__).resolve().parents[1] / "data" / "test_cases"


@pytest.fixture(scope="module")
def engine() -> Engine:
    return Engine(Config.load())


def test_valence_zone_structure(engine):
    tc = json.loads((DATA / "tc1.json").read_text(encoding="utf-8"))
    v2 = next(v for v in tc["vacancies"] if v["id"] == "V2")
    vz = valence_zone(engine, tc["candidate"], v2)
    assert vz["vacancy_id"] == "V2"
    assert not vz["blocked"]
    assert len(vz["interventions"]) == 3
    # выигрыши убывают
    gains = [i["gain"] for i in vz["interventions"]]
    assert gains == sorted(gains, reverse=True)
    # у каждой причины есть действие
    assert all(i["action"] for i in vz["interventions"])


def test_valence_zone_finds_commute(engine):
    """В TC1 V2 дорога 45 мин при лимите 40 — фактор в списке причин (вклад мал, но положителен)."""
    tc = json.loads((DATA / "tc1.json").read_text(encoding="utf-8"))
    v2 = next(v for v in tc["vacancies"] if v["id"] == "V2")
    vz = valence_zone(engine, tc["candidate"], v2, top_n=20)
    by_id = {i["factor"]: i for i in vz["interventions"]}
    assert "commute_max" in by_id
    assert by_id["commute_max"]["gain"] > 0
    assert by_id["commute_max"]["r"] > 0  # перерасход дороги учтён ресурсом


def test_root_causes_ranking(engine):
    """По потоку TC1 главные потери кандидата — культурные/сенсорные факторы V2+V3."""
    tc = json.loads((DATA / "tc1.json").read_text(encoding="utf-8"))
    causes = root_causes(engine, tc["candidate"], tc["vacancies"], top_n=5)
    assert 1 <= len(causes) <= 5
    losses = [c["avg_loss"] for c in causes]
    assert losses == sorted(losses, reverse=True)
    assert all(c["action"] for c in causes)


def test_root_causes_empty_when_all_blocked(engine):
    """Если все вакансии заблокированы — первопричин нет (нужны другие вакансии)."""
    tc = json.loads((DATA / "tc1.json").read_text(encoding="utf-8"))
    cand = dict(tc["candidate"])
    cand["work_format"] = ["remote"]  # V2 (hybrid) и V3 (office) блокируются
    causes = root_causes(engine, cand, tc["vacancies"][1:])
    assert causes == []
