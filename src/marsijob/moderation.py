"""Автопроверка вакансий на дискриминационные формулировки (ТК РФ ст. 64).

Блокирует публикацию карточек с прямыми требованиями, запрещёнными законом
(пол, возраст, семейное положение и т.п.), и предупреждает о пограничных.
"""
import re
from typing import Any, Dict, List

# severity: "block" — публикация запрещена; "warn" — предупреждение
RULES: List[Dict[str, Any]] = [
    {"id": "MD-01", "severity": "block", "pattern": r"до\s*\d{2}\s*лет|от\s*\d{2}\s*до\s*\d{2}|возраст[ае]?\s*(?:до|не\s*старше)",
     "message": "ограничение по возрасту"},
    {"id": "MD-02", "severity": "block", "pattern": r"\bмужск|\bженск|девушк|мужчин[ыае]?\b|женщин[ыае]?\b|парень\b|девочка\b",
     "message": "требование пола"},
    {"id": "MD-03", "severity": "block", "pattern": r"без\s+детей|без\s+семейных|не\s+в\s+декрете|без\s+беременн",
     "message": "ограничение по семейному положению / детям"},
    {"id": "MD-04", "severity": "block", "pattern": r"национальност|раса|славянск|вероисповедан|русск(?:ая|ой)\s+внешност",
     "message": "ограничение по национальности/расе/религии"},
    {"id": "MD-05", "severity": "block", "pattern": r"инвалид[а-я]*\s+не\s+принимаем|только\s+здоровые|медкнижк[ау]\s+без\s+ограничений\s+по\s+здоровью",
     "message": "ограничение по состоянию здоровья"},
    {"id": "MD-06", "severity": "warn", "pattern": r"молодая\s+команда|молодёжн|до\s*35|после\s*45|предпенсион",
     "message": "формулировка с возрастным подтекстом — переформулируйте нейтрально"},
    {"id": "MD-07", "severity": "warn", "pattern": r"презентабельн|внешност|строгий\s+дресс",
     "message": "требование к внешности: допустим дресс-код, но не «внешность»"},
    {"id": "MD-08", "severity": "warn", "pattern": r"стрессоустойчивост[ьи].{0,30}(?:женщин|мужчин)|энергичн(?:ая|ый)\s+(?:девушка|парень)",
     "message": "смешение требований качеств и пола"},
]


def check_discrimination(text: str) -> Dict[str, Any]:
    """Возвращает {ok, blocked: [...], warnings: [...]}."""
    low = (text or "").lower()
    blocked: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    for rule in RULES:
        if re.search(rule["pattern"], low):
            finding = {"rule": rule["id"], "message": rule["message"]}
            if rule["severity"] == "block":
                blocked.append(finding)
            else:
                warnings.append(finding)
    return {"ok": not blocked, "blocked": blocked, "warnings": warnings}
