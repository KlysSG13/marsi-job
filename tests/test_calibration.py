"""Калибровочный прогон: 5 эталонных тест-кейсов из data/test_cases.

Эталоны (SPEC §9): порядок вакансий, блокировки, пороги топ-1.
Эти тесты — главный критерий этапа 1; при неудаче калибруются веса
(см. SPEC §7), но не эталоны.
"""
import json
from pathlib import Path

import pytest

from marsijob.config import Config
from marsijob.engine import Engine

DATA = Path(__file__).resolve().parents[1] / "data" / "test_cases"


@pytest.fixture(scope="module")
def engine() -> Engine:
    return Engine(Config.load())


def load_cases():
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(DATA.glob("tc*.json"))]


def _score_all(engine, tc):
    return {v["id"]: engine.score(tc["candidate"], v) for v in tc["vacancies"]}


def test_five_cases_present():
    cases = load_cases()
    assert len(cases) == 5
    assert [c["tc_id"] for c in cases] == ["TC1", "TC2", "TC3", "TC4", "TC5"]


@pytest.mark.parametrize("tc_file", sorted(DATA.glob("tc*.json")), ids=lambda p: p.stem)
def test_case_etalon(engine, tc_file):
    tc = json.loads(tc_file.read_text(encoding="utf-8"))
    exp = tc["expected"]
    results = _score_all(engine, tc)

    blocked_ids = {vid for vid, r in results.items() if r.blocked}
    assert blocked_ids == set(exp.get("blocked", {}).keys()), (
        f"{tc['tc_id']}: заблокированы {blocked_ids}, ожидалось {set(exp.get('blocked', {}))}"
    )

    # причины блокировок совпадают по префиксу кода
    for vid, reasons in exp.get("blocked", {}).items():
        got = "|".join(results[vid].block_reasons)
        for reason in reasons:
            code = reason.split(":")[0]
            assert code in got, f"{tc['tc_id']}/{vid}: нет причины {code}, есть {got}"

    # вакансии ниже порога показа
    below = exp.get("below_threshold", [])
    for vid in below:
        assert not results[vid].blocked, f"{tc['tc_id']}/{vid}: ожидался скор, а не блок"
        assert results[vid].score < 0.40, (
            f"{tc['tc_id']}/{vid}: скор {results[vid].score:.3f} должен быть < 0.40"
        )

    # порядок оставшихся
    ranked = sorted(
        (r for vid, r in results.items() if not r.blocked and vid not in below),
        key=lambda r: r.score,
        reverse=True,
    )
    got_order = [r.vacancy_id for r in ranked]
    assert got_order == exp["order"], (
        f"{tc['tc_id']}: порядок {got_order} (скор=[{', '.join(f'{r.score:.3f}' for r in ranked)}]), "
        f"ожидалось {exp['order']}"
    )

    # топ-1 достаточно силён
    top1 = ranked[0]
    assert top1.score >= exp["top1_min_score"], (
        f"{tc['tc_id']}: топ-1 {top1.vacancy_id} скор {top1.score:.3f} < {exp['top1_min_score']}"
    )


def test_phi_rules_fire(engine):
    """Проверка, что Φ-эффекты реально срабатывают на эталонах (TC1 V1 синергии)."""
    tc = json.loads((DATA / "tc1.json").read_text(encoding="utf-8"))
    v1 = next(v for v in tc["vacancies"] if v["id"] == "V1")
    res = engine.score(tc["candidate"], v1)
    rule_ids = {p.rule_id for p in res.phi_effects}
    assert "PH-001" in rule_ids, f"ожидались синергии СДВГ×гибкий график, есть {rule_ids}"
    assert any(p.value > 0 for p in res.phi_effects)


def test_breakdown_explainability(engine):
    """Разбор матча содержит вклады факторов — «распаковка» скаляра (p. 9)."""
    tc = json.loads((DATA / "tc1.json").read_text(encoding="utf-8"))
    v1 = next(v for v in tc["vacancies"] if v["id"] == "V1")
    bd = engine.score(tc["candidate"], v1).breakdown()
    assert bd["score"] > 0 and bd["band"] in ("strong", "decent")
    assert len(bd["factors"]) >= 20
    assert all("w" in f and "match" in f and "contribution" in f for f in bd["factors"])
