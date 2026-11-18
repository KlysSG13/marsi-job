"""Детерминированный движок матчинга МАРСИ-Job.

Реализует пайплайн по документу МАРСИ-DN1 (§2–4, §6.1):
  1) сбор значений факторов пары «кандидат × вакансия» (HR-адаптер);
  2) нормализация и близость (match) по типу фактора;
  3) вес фактора W = Σ(w_p·param_p)·R_obj + ε_n (взвешенная формула, p. 11);
  4) Φ-матрица: синергии (>0), конфликты (<0), блокировки (Φ=−∞);
  5) скор пары: КЛЕЙ = Σ(match·W) − Σr + ΣΦ − ε, нормировка к 0..1.

Модуль не делает I/O. Все функции чистые (Config загружается снаружи).
"""
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import Config

# ---------------------------------------------------------------------------
# Утилиты нормализации (log-шкалы по МАРСИ-DN1, «Доработка модели» п. 1)
# ---------------------------------------------------------------------------


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def log_norm(x: float, x_max: float) -> float:
    """Логарифмическая нормализация 0..x_max → 0..1 («сплющить выбросы»)."""
    if x_max <= 0:
        return 0.0
    return clamp01(math.log1p(max(0.0, x)) / math.log1p(x_max))


# ---------------------------------------------------------------------------
# Функции близости по типам факторов (SPEC §2.1)
# ---------------------------------------------------------------------------


def match_scalar(c: float, v: float) -> float:
    return clamp01(1.0 - abs(c - v))


def match_scalar_stress(tolerance: float, load: float, k: float = 2.0) -> float:
    """Толерантность покрывает нагрузку — полный матч; перегруз — падение с коэффициентом k."""
    if tolerance >= load:
        return 1.0
    return clamp01(1.0 - (load - tolerance) * k)


def match_scalar_inverse(pref: float, load: float) -> float:
    """Обе величины высокие одновременно — конфликт (автономия×микроменеджмент и т.п.)."""
    return clamp01(1.0 - clamp01(pref + load - 1.0))


def match_range_overlap(c: Sequence[float], v: Sequence[float]) -> float:
    """Пересечение диапазонов / длина меньшего диапазона."""
    lo = max(c[0], v[0])
    hi = min(c[1], v[1])
    overlap = hi - lo
    if overlap <= 0:
        return 0.0
    smaller = min(c[1] - c[0], v[1] - v[0])
    if smaller <= 0:
        return 1.0
    return clamp01(overlap / smaller)


def match_duration(cand_value: float, req_value: float) -> float:
    """cand ≥ req → 1; иначе линейный недобор относительно req."""
    if req_value <= 0:
        return 1.0
    if cand_value >= req_value:
        return 1.0
    return clamp01(1.0 - (req_value - cand_value) / req_value)


def match_team_size(pref: Sequence[float], actual: float) -> float:
    """В диапазоне → 1; вне — мягкое затухание 1/(1+dist/5)."""
    low, high = pref[0], pref[1]
    dist = max(0.0, low - actual, actual - high)
    if dist == 0:
        return 1.0
    return 1.0 / (1.0 + dist / 5.0)


def cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
    keys = set(a) | set(b)
    dot = sum(a.get(k, 0.0) * b.get(k, 0.0) for k in keys)
    na = math.sqrt(sum(x * x for x in a.values()))
    nb = math.sqrt(sum(x * x for x in b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return clamp01(dot / (na * nb))


# ---------------------------------------------------------------------------
# Результаты
# ---------------------------------------------------------------------------


@dataclass
class FactorResult:
    factor_id: str
    name_ru: str
    w: float
    match: Optional[float]  # None = фактор не скорится (нет данных или неприменим)
    contribution: float
    r: float
    missing: Optional[str] = None  # 'candidate' | 'vacancy' | None
    note: str = ""


@dataclass
class PhiEffect:
    rule_id: str
    comment: str
    value: float  # уже умножено на min(W вовлечённых факторов)


@dataclass
class MatchResult:
    vacancy_id: str
    score: float
    band: str
    blocked: bool
    block_reasons: List[str] = field(default_factory=list)
    factors: List[FactorResult] = field(default_factory=list)
    phi_effects: List[PhiEffect] = field(default_factory=list)
    epsilon: float = 0.0
    sums: Dict[str, float] = field(default_factory=dict)

    def top_factors(self, n: int = 5) -> List[FactorResult]:
        scored = [f for f in self.factors if f.match is not None]
        return sorted(scored, key=lambda f: f.contribution, reverse=True)[:n]

    def worst_factors(self, n: int = 5) -> List[FactorResult]:
        scored = [f for f in self.factors if f.match is not None]
        return sorted(scored, key=lambda f: f.match * f.w)[:n]

    def breakdown(self) -> Dict[str, Any]:
        return {
            "vacancy_id": self.vacancy_id,
            "score": round(self.score, 4),
            "band": self.band,
            "blocked": self.blocked,
            "block_reasons": self.block_reasons,
            "factors": [
                {
                    "id": f.factor_id,
                    "name": f.name_ru,
                    "w": round(f.w, 4),
                    "match": None if f.match is None else round(f.match, 4),
                    "contribution": round(f.contribution, 4),
                    "r": round(f.r, 4),
                    "missing": f.missing,
                }
                for f in self.factors
            ],
            "phi": [
                {"rule": p.rule_id, "comment": p.comment, "value": round(p.value, 4)}
                for p in self.phi_effects
            ],
            "epsilon": round(self.epsilon, 4),
            "sums": {k: round(v, 4) for k, v in self.sums.items()},
        }


# ---------------------------------------------------------------------------
# Движок
# ---------------------------------------------------------------------------

# Отсутствующее значение стороны вакансии для этих факторов — «требования нет»,
# а не пропуск данных (образование, управленческий опыт).
VAC_NULL_OK = {"education_level"}


class Engine:
    def __init__(self, config: Optional[Config] = None):
        self.cfg = config or Config.load()

    # ------------------------- значения сторон -------------------------
    def _cand_raw(self, factor_id: str, cand: Dict[str, Any]) -> Any:
        fld = self.cfg.candidate_field(factor_id)
        return cand.get(fld) if fld else None

    def _vac_raw(self, factor_id: str, vac: Dict[str, Any]) -> Any:
        fld = self.cfg.vacancy_field(factor_id)
        return vac.get(fld) if fld else None

    # ------------------------- W фактора (p. 11) ------------------------
    def weight_of(
        self,
        factor: Dict[str, Any],
        pair: Dict[str, Any],
        r_cost: float,
        personal_w: Optional[float] = None,
    ) -> float:
        """W = w0 · (Σ w_p·param_p)·R_obj + ε_n.

        Итоговый вес масштабируется базовой важностью фактора w0 (персональные
        калибровки после фидбека подменяют w0). y поднимается для must-have,
        f/g уточняются достоверностью данных пары, R_obj снижается
        пропорционально цене сближения r и индексу доверия вакансии
        (SPEC §8: trust умножает R_obj, база 0.7 → 1.0).
        """
        params = dict(factor["params"])
        if pair.get("must"):
            params["y"] = max(params.get("y", 0.5), 0.9)
        if pair.get("clarity") is not None:
            params["f"] = (params.get("f", 0.5) + pair["clarity"]) / 2.0
        if pair.get("confidence") is not None:
            params["g"] = (params.get("g", 0.5) + pair["confidence"]) / 2.0
        base = sum(
            self.cfg.param_weights[p] * params.get(p, 0.0)
            for p in self.cfg.param_weights
        )
        r_obj = 1.0 - 0.4 * clamp01(r_cost)
        trust = float(pair.get("trust", 0.7))
        r_obj *= min(1.0, trust / 0.7)
        w0 = personal_w if personal_w is not None else factor["w0"]
        eps_n = self.cfg.epsilon_cfg["per_factor_missing"] * w0 if pair.get("missing") else 0.0
        return w0 * base * r_obj + eps_n

    # ------------------------- скоринг пары -----------------------------
    def score(
        self,
        candidate: Dict[str, Any],
        vacancy: Dict[str, Any],
        weight_overrides: Optional[Dict[str, float]] = None,
        match_overrides: Optional[Dict[str, float]] = None,
    ) -> MatchResult:
        weight_overrides = weight_overrides or {}
        match_overrides = match_overrides or {}
        cand, vac = candidate, vacancy
        clarity = float(vac.get("vacancy_clarity", 0.7))
        trust = float(vac.get("trust_index", 0.7))

        factor_results: List[FactorResult] = []
        missing_cand = 0
        missing_vac = 0
        scored_count = 0
        sensitive_selfreport = 0

        for fid in self.cfg.factor_ids:
            factor = self.cfg.factors[fid]
            res = self._score_factor(
                fid, factor, cand, vac, clarity,
                weight_overrides.get(fid), match_overrides.get(fid),
            )
            if res.missing == "candidate":
                missing_cand += 1
            elif res.missing == "vacancy":
                missing_vac += 1
            if res.match is not None:
                scored_count += 1
                if factor.get("sensitive"):
                    sensitive_selfreport += 1
            factor_results.append(res)

        # ε_total (SPEC §6): доля неизвестного и самоотчётных данных
        eps_cfg = self.cfg.epsilon_cfg["weights"]
        denom = max(1, scored_count + missing_cand + missing_vac)
        eps_total = clamp01(
            eps_cfg["cand_missing"] * (missing_cand / denom)
            + eps_cfg["vac_missing"] * (missing_vac / denom)
            + eps_cfg["self_report_only"] * (sensitive_selfreport / denom)
        )
        eps_total = min(eps_total, self.cfg.epsilon_cfg["total_cap"])
        # накопленный шум вакансии от событий «отклик без ответа» (SPEC §8)
        eps_total = clamp01(eps_total + float(vac.get("epsilon_extra", 0.0)))

        # Φ-матрица
        phi_effects, phi_sum = self._apply_phi(cand, vac, factor_results)

        sum_w = sum(f.w for f in factor_results if f.match is not None)
        sum_contrib = sum(f.contribution for f in factor_results if f.match is not None)
        sum_r = sum(f.r for f in factor_results)

        # КЛЕЙ = Σ(match·W) − Σr + ΣΦ, нормировка на ΣW, минус ε
        raw = (sum_contrib - sum_r + phi_sum) / sum_w if sum_w > 0 else 0.0
        score = clamp01(raw - eps_total)

        block_reasons = [
            part[len("BLOCK"):]
            for f in factor_results
            for part in f.note.split("|")
            if part.startswith("BLOCK")
        ]
        # межфакторные противопоказания из конфига (Φ=−∞)
        for blocker in self.cfg.blockers:
            if blocker.get("kind") == "sensory_contraindication":
                sensitivity = float(cand.get("sensory_sensitivity", 0.0))
                load = float(vac.get("sensory_load", 0.0))
                if sensitivity >= float(blocker["sensitivity_min"]) and load >= float(blocker["load_min"]):
                    block_reasons.append(blocker["code"])
        blocked = bool(block_reasons)
        band = "below" if blocked else self.cfg.band_of(score)

        return MatchResult(
            vacancy_id=str(vacancy.get("id", "?")),
            score=score,
            band=band,
            blocked=blocked,
            block_reasons=block_reasons,
            factors=factor_results,
            phi_effects=phi_effects,
            epsilon=eps_total,
            sums={
                "sum_w": sum_w,
                "sum_contrib": sum_contrib,
                "sum_r": sum_r,
                "phi_sum": phi_sum,
                "epsilon": eps_total,
            },
        )

    # ------------------------- отдельный фактор -------------------------
    def _score_factor(
        self,
        fid: str,
        factor: Dict[str, Any],
        cand: Dict[str, Any],
        vac: Dict[str, Any],
        clarity: float,
        w_override: Optional[float],
        match_override: Optional[float],
    ) -> FactorResult:
        ftype = factor["type"]
        c_raw = self._cand_raw(fid, cand)
        v_raw = self._vac_raw(fid, vac)
        name = factor.get("name_ru", fid)
        rc = self.cfg.resource_costs

        # --- неприменимые факторы ----------------------------------------
        if fid == "commute_max" and (v_raw is None or vac.get("work_format") == "remote"):
            # удалённая вакансия — дорога до офиса неактуальна
            return FactorResult(fid, name, 0.0, None, 0.0, 0.0)
        if fid == "management_experience" and not vac.get("management_required"):
            return FactorResult(fid, name, 0.0, None, 0.0, 0.0)
        if fid == "location" and vac.get("city_unknown"):
            # внешний источник без города (RSS): локацию не оцениваем и не блокируем
            return FactorResult(fid, name, 0.0, None, 0.0, 0.0)

        # --- пропуски данных ---------------------------------------------
        missing = None
        if c_raw is None:
            missing = "candidate"
        elif v_raw is None and fid not in VAC_NULL_OK:
            missing = "vacancy"

        match_value: Optional[float] = None
        r_cost = 0.0
        must_flag = False
        note = ""
        c_val = c_raw

        if not missing:
            invert = self.cfg.field_map.get(fid, {}).get("invert_candidate", False)
            if invert and isinstance(c_val, (int, float)):
                c_val = 1.0 - c_val

            if ftype == "set":
                match_value, must_flag = self._match_set(fid, c_val, v_raw)

            elif ftype == "categorical":
                if fid == "relocation":
                    match_value, r_cost = self._match_relocation(c_val, vac, cand)
                else:
                    match_value, r_cost, note = self._match_categorical(fid, c_val, v_raw, cand)

            elif ftype == "categorical_ordered":
                match_value = self._match_ordered(factor, c_val, v_raw)

            elif ftype == "scalar":
                match_value = match_scalar(float(c_val), float(v_raw))

            elif ftype == "scalar_stress":
                match_value = match_scalar_stress(
                    float(c_val), float(v_raw), k=float(factor.get("k", 2.0)))

            elif ftype == "scalar_inverse":
                match_value = match_scalar_inverse(float(c_val), float(v_raw))

            elif ftype == "range_overlap":
                if fid == "salary":
                    match_value, r_cost, note = self._match_salary(c_val, v_raw)
                elif fid == "team_size_preference":
                    match_value = match_team_size(list(c_val), float(v_raw))

            elif ftype == "duration":
                if fid == "experience_years":
                    match_value = match_duration(float(c_val), float(v_raw or 0))
                elif fid in ("urgency", "start_date"):
                    # кандидат укладывается в срок вакансии?
                    c_num, v_num = float(c_val), float(v_raw)
                    match_value = 1.0 if c_num <= v_num else clamp01(
                        1.0 - (c_num - v_num) / max(c_num, 1.0))
                elif fid == "commute_max":
                    minutes, allowed = float(v_raw), float(c_val)
                    if minutes <= allowed:
                        match_value = 1.0
                    else:
                        over = (minutes - allowed) / allowed
                        match_value = clamp01(1.0 - over)
                        r_cost = rc["commute_overrun_coef"] * over

            elif ftype == "location":
                match_value, r_cost = self._match_location(c_val, v_raw, cand, vac)

            elif ftype == "management":
                match_value = 1.0 if (c_val or {}).get("has") else 0.5

            elif ftype == "motivation_cosine":
                match_value = cosine(dict(c_val), dict(v_raw or {}))

            elif ftype == "tags":
                overlap = set(c_val or []) & set(v_raw or [])
                match_value = clamp01(len(overlap) / len(set(v_raw))) if v_raw else 1.0

            # блокировки по значению (Φ=−∞)
            block = self._check_blocker(fid, c_val, v_raw, match_value, cand, vac)
            if block and not note.startswith("BLOCK"):
                note = "BLOCK" + block

        if match_override is not None:
            match_value = match_override
            r_cost = 0.0

        pair_meta = {
            "must": must_flag,
            "clarity": clarity,
            "confidence": self._pair_confidence(fid, cand),
            "missing": bool(missing),
            "trust": float(vac.get("trust_index", 0.7)),
        }
        w = self.weight_of(factor, pair_meta, r_cost, w_override)
        contribution = 0.0 if missing else (match_value or 0.0) * w

        return FactorResult(
            factor_id=fid,
            name_ru=name,
            w=w,
            match=None if missing else match_value,
            contribution=contribution,
            r=r_cost,
            missing=missing,
            note=note,
        )

    # ------------------------- быстрый префильтр -------------------------
    def prefilter_blocked(self, cand: Dict[str, Any], vac: Dict[str, Any]) -> str:
        """Быстрая проверка жёстких блокировок без полного скоринга.

        Для больших внешних баз: сначала префильтр, полный скоринг — только
        для выживших. Возврат: код блокировки ('' = не заблокирована).
        """
        formats = cand.get("work_format") or []
        v_fmt = vac.get("work_format")
        if formats and v_fmt and v_fmt not in formats:
            return "BL-002:work_format"
        scheds = cand.get("schedule") or []
        v_sched = vac.get("schedule")
        if scheds and v_sched and v_sched not in scheds \
                and not cand.get("schedule_compromise"):
            return "BL-003:schedule"
        permits = set(cand.get("work_permit") or [])
        v_permits = set(vac.get("work_permit_required") or [])
        if permits and v_permits and not (permits & v_permits):
            return "BL-005:work_permit"
        c_sal = cand.get("salary") or {}
        v_sal = vac.get("salary")
        if c_sal and v_sal and not c_sal.get("compromise", False):
            lo = max(float(c_sal.get("min", 0)), float(v_sal.get("min", 0)))
            hi = min(float(c_sal.get("max", 1e12)), float(v_sal.get("max", 0)))
            if hi < lo:
                return "BL-004:salary"
        v_loc = vac.get("location") or {}
        if not v_loc.get("city") and not vac.get("city_unknown") \
                and not vac.get("work_format") == "remote" \
                and abs(float((cand.get("location") or {}).get("tz", 3))
                        - float(v_loc.get("tz", 3))) > 3 \
                and cand.get("relocation", "no") == "no":
            return "BL-006:tz_window"
        return ""

    # ------------------------- матчеры по типам -------------------------
    def _match_set(self, fid: str, c_val: Any, v_val: Any) -> Tuple[float, bool]:
        """Навыки/языки: покрытие требований с учётом подтверждённости (g).

        Ключи сравниваются без учёта регистра (LLM-парсер может вернуть «Python»).
        must-have ниже порога блокируется в _check_blocker (BL-001/BL-007).
        """
        if not isinstance(c_val, dict):
            c_val = {}
        c_val = {str(k).strip().lower(): v for k, v in c_val.items()}
        if not isinstance(v_val, dict) or not v_val:
            return 1.0, False
        weighted_sum = 0.0
        weight_total = 0.0
        has_must = False
        for item_raw, req in v_val.items():
            item = str(item_raw).strip().lower()
            req_level = float(req.get("req_level", 0.5))
            must = bool(req.get("must", False))
            has_must = has_must or must
            weight = req_level * (2.0 if must else 1.0)
            skill = c_val.get(item) or {}
            level = float(skill.get("level", 0.0))
            verified = float(skill.get("verified", 0.0))
            coverage = clamp01(level / req_level) if req_level > 0 else 1.0
            trust = 0.6 + 0.4 * verified
            weighted_sum += coverage * trust * weight
            weight_total += weight
        match_value = weighted_sum / weight_total if weight_total else 1.0
        return match_value, has_must

    def _match_categorical(
        self, fid: str, c_val: Any, v_val: Any, cand: Dict[str, Any]
    ) -> Tuple[float, float, str]:
        rc = self.cfg.resource_costs
        if isinstance(c_val, list) and isinstance(v_val, list):
            ok = bool(set(c_val) & set(v_val))  # юрисдикции: достаточно пересечения
        elif isinstance(c_val, list):
            ok = v_val in c_val
        else:
            ok = c_val == v_val
        if ok:
            return 1.0, 0.0, ""
        if fid == "work_format":
            return 0.0, 0.0, "BLOCKBL-002:work_format"
        if fid == "work_permit":
            return 0.0, 0.0, "BLOCKBL-005:work_permit"
        if fid == "schedule":
            if not cand.get("schedule_compromise"):
                return 0.0, 0.0, "BLOCKBL-003:schedule"
            return 0.0, rc["schedule_compromise"], ""
        return 0.0, 0.0, ""

    def _match_relocation(self, c_val: Any, vac: Dict[str, Any], cand: Dict[str, Any]) -> Tuple[float, float]:
        """Релокация: сторона вакансии — relocation_supported (bool)."""
        rc = self.cfg.resource_costs
        supported = bool(vac.get("relocation_supported"))
        same_city = self._same_city(cand, vac)
        remote = not (vac.get("location") or {}).get("city")
        if same_city or remote:
            return 1.0, 0.0  # переезд не требуется
        if supported:
            if c_val in ("yes", "yes_with_package"):
                return 1.0, rc["relocation_with_package"]
            return 0.0, 0.0  # кандидат не готов, вакансия ждёт переезда
        # вакансия не поддерживает переезд, города разные
        if c_val in ("yes", "yes_with_package"):
            return 0.6, rc["relocation_no_package"]
        return 0.0, 0.0

    def _match_ordered(self, factor: Dict[str, Any], c_val: Any, v_val: Any) -> float:
        order = factor.get("order", [])
        if v_val is None:
            return 1.0  # требования нет
        if c_val not in order or v_val not in order:
            return 0.5
        ci, vi = order.index(c_val), order.index(v_val)
        if ci >= vi:
            return 1.0
        return clamp01(1.0 - (vi - ci) / len(order))

    def _match_salary(self, c_val: Any, v_val: Any) -> Tuple[float, float, str]:
        rc = self.cfg.resource_costs
        c_min, c_max = float(c_val["min"]), float(c_val["max"])
        v_min, v_max = float(v_val["min"]), float(v_val["max"])
        match_value = match_range_overlap([c_min, c_max], [v_min, v_max])
        if match_value > 0:
            r = rc["salary_compromise"] if match_value < 0.5 else 0.0
            return match_value, r, ""
        gap = max(v_min - c_max, c_min - v_max)
        if not c_val.get("compromise", False):
            return 0.0, 0.0, "BLOCKBL-004:salary"
        ratio = gap / max(c_max, 1.0)
        return 0.0, rc["salary_compromise"] * clamp01(ratio * 3.0), ""

    def _match_location(self, c_val: Any, v_val: Any, cand: Dict, vac: Dict) -> Tuple[float, float]:
        rc = self.cfg.resource_costs
        tz_window = float(self.cfg.factors["location"].get("tz_window_hours", 3))
        cand_tz = float((c_val or {}).get("tz", 0))
        vac_city = (v_val or {}).get("city")
        vac_tz = float((v_val or {}).get("tz", cand_tz))
        if not vac_city:  # удалённая вакансия: важно окно часовых поясов
            if abs(cand_tz - vac_tz) <= tz_window:
                return 1.0, 0.0
            if cand.get("relocation", "no") == "no":
                return 0.0, 0.0  # блокируется в _check_blocker (BL-006)
            return 0.3, 0.0
        same = self._same_city(cand, vac)
        if same:
            return 1.0, 0.0
        reloc = cand.get("relocation", "no")
        if reloc in ("yes", "yes_with_package"):
            r = (rc["relocation_with_package"] if vac.get("relocation_supported")
                 else rc["relocation_no_package"])
            return 0.6, r
        return 0.0, 0.0  # блокируется в _check_blocker (BL-006:location_city)

    def _same_city(self, cand: Dict, vac: Dict) -> bool:
        c_loc = cand.get("location") or {}
        v_loc = vac.get("location") or {}
        c_city = str(c_loc.get("city", "")).strip().lower()
        v_city = str(v_loc.get("city", "")).strip().lower()
        return bool(c_city) and c_city == v_city

    def _pair_confidence(self, fid: str, cand: Dict[str, Any]) -> Optional[float]:
        """g-параметр пары: подтверждённость данных фактора кандидата."""
        factor = self.cfg.factors[fid]
        if factor.get("sensitive"):
            return 0.6  # самоотчёты E/F групп
        if fid in ("hard_skills", "languages"):
            vals = (cand.get(fid) or {}).values()
            ver = [float(v.get("verified", 0.0)) for v in vals]
            return (sum(ver) / len(ver)) if ver else None
        return None

    # ------------------------- блокировки (Φ=−∞) ------------------------
    def _check_blocker(
        self, fid: str, c_val: Any, v_val: Any, match_value: Optional[float],
        cand: Dict[str, Any], vac: Dict[str, Any],
    ) -> Optional[str]:
        if fid == "hard_skills" and isinstance(v_val, dict):
            skills = c_val if isinstance(c_val, dict) else {}
            skills = {str(k).strip().lower(): v for k, v in skills.items()}
            for item_raw, req in v_val.items():
                item = str(item_raw).strip().lower()
                if req.get("must"):
                    req_level = float(req.get("req_level", 0.5))
                    lvl = float((skills.get(item) or {}).get("level", 0.0))
                    if lvl < 0.8 * req_level:
                        return f"BL-001:hard_skills:{item}"
        if fid == "languages" and isinstance(v_val, dict):
            langs = c_val if isinstance(c_val, dict) else {}
            langs = {str(k).strip().lower(): v for k, v in langs.items()}
            for item_raw, req in v_val.items():
                item = str(item_raw).strip().lower()
                if req.get("must"):
                    lvl = float((langs.get(item) or {}).get("level", 0.0))
                    if lvl < float(req.get("req_level", 0.6)):
                        return f"BL-007:language:{item}"
        if fid == "location" and match_value == 0.0:
            v_loc = vac.get("location") or {}
            if not v_loc.get("city"):
                return "BL-006:tz_window"
            return "BL-006:location_city"
        return None

    # ------------------------- Φ-матрица --------------------------------
    VACANCY_RULE_FIELDS = {
        "sensory_load", "stage_risk", "communication_mode", "trajectory_offered",
        "micromanagement_degree", "multitask_intensity", "role_ambiguity",
        "training_offered", "relocation_supported", "relocation_required",
        "business_trips_required", "start_date_required_months",
        "work_format", "schedule",
    }

    def _rule_field_value(self, fld: str, cand: Dict, vac: Dict, ctx: Dict[str, Any]) -> Any:
        if fld == "interests_overlap_count":
            return ctx.get("interests_overlap", 0)
        if fld == "motivation_stability":
            return float((cand.get("motivation") or {}).get("stability", 0.0))
        if fld in self.VACANCY_RULE_FIELDS:
            return vac.get(fld)
        return cand.get(fld)

    def _rule_weight(self, fld: str, wmap: Dict[str, float]) -> float:
        """Вес правила = min(W вовлечённых факторов); phi_only поля — 0.6."""
        if fld in self.cfg.phi_only:
            return 0.6
        fid = self.cfg.factor_of_candidate_field(fld) or self.cfg.factor_of_vacancy_field(fld)
        if fid and wmap.get(fid, 0.0) > 0.0:
            return wmap[fid]
        return 0.6

    @staticmethod
    def _cond_ok(cond: Dict[str, Any], value: Any) -> bool:
        op, target = cond["op"], cond.get("value")
        if op == "contains":
            return isinstance(value, (list, tuple)) and target in value
        if op == "in":
            return value in (target or [])
        if op == "eq":
            return value == target
        if op == "neq":
            if isinstance(value, (list, tuple)):
                return target not in value
            return value != target
        if op == "gte":
            return value is not None and value >= target
        if op == "lte":
            return value is not None and value <= target
        if op == "between":
            return value is not None and target[0] <= value <= target[1]
        return False

    def _apply_phi(
        self, cand: Dict, vac: Dict, factor_results: List[FactorResult]
    ) -> Tuple[List[PhiEffect], float]:
        wmap = {f.factor_id: f.w for f in factor_results}
        ctx = {
            "interests_overlap": len(
                set(cand.get("interests") or []) & set(vac.get("product_tags") or [])
            )
        }
        effects: List[PhiEffect] = []
        total = 0.0
        for rule in self.cfg.phi_rules:
            ok = True
            weights: List[float] = []
            for cond in rule["when"]:
                value = self._rule_field_value(cond["field"], cand, vac, ctx)
                if not self._cond_ok(cond, value):
                    ok = False
                    break
                weights.append(self._rule_weight(cond["field"], wmap))
            if not ok:
                continue
            scale = min(weights) if weights else 0.5
            value = float(rule["phi"]) * scale
            effects.append(PhiEffect(rule["id"], rule.get("comment", ""), value))
            total += value
        return effects, total
