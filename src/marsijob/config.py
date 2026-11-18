"""Загрузка конфигурации доменного HR-адаптера МАРСИ-Job.

Читает data/factors.json (словарь факторов, веса параметров, карта полей,
модели r и ε) и data/phi_rules.json (Φ-матрица и блокировки).
Контракт полей — spec/SPEC.md.
"""
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"

# Поля вакансии, допустимые в данных (для валидации входа).
VACANCY_EXTRA_FIELDS = {
    "relocation_supported", "relocation_required", "business_trips_required",
    "start_date_required_months", "domain", "education_level_min",
    "experience_years_min", "portfolio_required", "product_tags",
    "urgency_months", "hard_skills", "work_format", "schedule",
    "employment_type", "salary", "languages", "location",
    "work_permit_required", "commute_minutes", "title", "id",
}

# Поля кандидата вне словаря факторов (флаги компромисса и пр.).
CANDIDATE_EXTRA_FIELDS = {
    "neuro_profile", "family_constraints", "schedule_compromise",
    "business_trips_tolerance",
}


class Config:
    """Иммутабельная в рантайме конфигурация HR-адаптера."""

    def __init__(self, factors_raw: Dict[str, Any], phi_raw: Dict[str, Any]):
        self.raw: Dict[str, Any] = factors_raw
        self.phi_raw: Dict[str, Any] = phi_raw
        self.factors: Dict[str, Dict[str, Any]] = {
            f["id"]: f for f in factors_raw["factors"]
        }
        self.factor_ids: List[str] = [f["id"] for f in factors_raw["factors"]]
        self.field_map: Dict[str, Dict[str, Any]] = factors_raw["field_map"]
        self.param_weights: Dict[str, float] = factors_raw["param_weights"]
        self.bands: Dict[str, float] = factors_raw["bands"]
        self.epsilon_cfg: Dict[str, Any] = factors_raw["epsilon"]
        self.resource_costs: Dict[str, float] = factors_raw["resource_costs"]
        self.vacancy_attrs: Dict[str, Dict[str, Any]] = {
            a["id"]: a for a in factors_raw["vacancy_attributes"]
        }
        self.phi_rules: List[Dict[str, Any]] = phi_raw["rules"]
        self.blockers: List[Dict[str, Any]] = phi_raw["blockers"]
        self.phi_only: Dict[str, Dict[str, Any]] = {
            p["id"]: p for p in factors_raw["phi_only_fields"]
        }

    @classmethod
    def load(
        cls,
        factors_path: Optional[Path] = None,
        phi_path: Optional[Path] = None,
    ) -> "Config":
        factors_path = factors_path or DATA_DIR / "factors.json"
        phi_path = phi_path or DATA_DIR / "phi_rules.json"
        with open(factors_path, encoding="utf-8") as fh:
            factors_raw = json.load(fh)
        with open(phi_path, encoding="utf-8") as fh:
            phi_raw = json.load(fh)
        return cls(factors_raw, phi_raw)

    # ------------------------------------------------------------------
    def candidate_field(self, factor_id: str) -> Optional[str]:
        return (self.field_map.get(factor_id) or {}).get("candidate")

    def vacancy_field(self, factor_id: str) -> Optional[str]:
        return (self.field_map.get(factor_id) or {}).get("vacancy")

    def factor_of_candidate_field(self, field: str) -> Optional[str]:
        for fid, m in self.field_map.items():
            if not isinstance(m, dict):
                continue  # служебные ключи вроде "comment"
            if m.get("candidate") == field:
                return fid
        return None

    def factor_of_vacancy_field(self, field: str) -> Optional[str]:
        for fid, m in self.field_map.items():
            if not isinstance(m, dict):
                continue
            if m.get("vacancy") == field:
                return fid
        return None

    def band_of(self, score: float) -> str:
        if score >= self.bands["strong"]:
            return "strong"
        if score >= self.bands["decent"]:
            return "decent"
        if score >= self.bands["conditional"]:
            return "conditional"
        return "below"
