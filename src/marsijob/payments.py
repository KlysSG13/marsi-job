"""Монетизация МАРСИ-Job (модель B2C/B2B, промпт «Рефакторинг монетизации»).

Единый источник цен/лимитов — data/pricing.json (копейки, int).
Типы SKU: oneoff (услуга, оказывается один раз на оплату),
subscription (дни суммируются), consumable (баланс приглашений),
service (разовое действие по payload — таргет-рассылка).

Robokassa: классическая схема (OutSum в рублях с двумя знаками,
подпись MD5 login:outsum:invid:password1; ResultURL проверяется
по password2). Идемпотентность — UNIQUE(robokassa_inv_id).
"""
import hashlib
import json
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRICING_PATH = PROJECT_ROOT / "data" / "pricing.json"

ROBOKASSA_URL = "https://auth.robokassa.ru/Merchant/Index.aspx"

# sku_id подписок → код плана
PLAN_OF_SKU = {"plus_month": "plus", "pro_month": "pro", "business_month": "business"}


class PricingError(Exception):
    pass


@dataclass(frozen=True)
class Sku:
    id: str
    audience: str          # candidate | employer
    type: str              # oneoff | subscription | consumable | service
    price_kop: int
    title: str
    receipt_name: str
    description: str = ""
    days: int = 0
    quantity: int = 0
    hidden: bool = False


class Pricing:
    """Каталог SKU и лимитов из data/pricing.json."""

    def __init__(self, data: Dict[str, Any]):
        self.raw = data
        self.limits: Dict[str, Any] = data["limits"]
        self.promo: Dict[str, Any] = data.get("launch_promo", {})
        self.skus: Dict[str, Sku] = {}
        for s in data["skus"]:
            self.skus[s["id"]] = Sku(
                id=s["id"], audience=s["audience"], type=s["type"],
                price_kop=int(s["price_kop"]), title=s["title"],
                receipt_name=s["receipt_name"], description=s.get("description", ""),
                days=int(s.get("days", 0)), quantity=int(s.get("quantity", 0)),
                hidden=bool(s.get("hidden", False)),
            )

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Pricing":
        return cls(json.loads((path or PRICING_PATH).read_text(encoding="utf-8")))

    def sku(self, sku_id: str) -> Sku:
        if sku_id not in self.skus:
            raise PricingError(f"неизвестный SKU: {sku_id}")
        return self.skus[sku_id]

    def visible_skus(self, audience: str) -> List[Sku]:
        return [s for s in self.skus.values()
                if s.audience == audience and not s.hidden]

    def effective_price_kop(self, sku_id: str) -> int:
        """Цена с учётом промо (если активно для этого SKU)."""
        sku = self.sku(sku_id)
        if (self.promo.get("enabled") and self.promo.get("sku") == sku_id):
            until = str(self.promo.get("until", ""))
            if until >= "20":  # дата ещё не прошла (YYYY-MM-DD сравнение по строке)
                return int(self.promo["price_kop"])
        return sku.price_kop

    def rub(self, kop: int) -> str:
        return f"{kop / 100:.2f}"


# ---------------------------------------------------------------------------
# Robokassa
# ---------------------------------------------------------------------------


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def build_payment_url(login: str, password_1: str, out_sum_kop: int, inv_id: int,
                      description: str, is_test: bool = False) -> str:
    """Ссылка оплаты: OutSum в рублях с двумя знаками, подпись MD5."""
    out_sum = f"{out_sum_kop / 100:.2f}"
    signature = _md5(f"{login}:{out_sum}:{inv_id}:{password_1}")
    params: Dict[str, str] = {
        "MerchantLogin": login,
        "OutSum": out_sum,
        "InvId": str(inv_id),
        "Description": description[:100],
        "SignatureValue": signature,
        "Encoding": "utf-8",
    }
    if is_test:
        params["IsTest"] = "1"
    return f"{ROBOKASSA_URL}?{urllib.parse.urlencode(params)}"


def verify_result_signature(password_2: str, out_sum: str, inv_id: int,
                            signature: str) -> bool:
    """Проверка подписи ResultURL: md5(out_sum:inv_id:password_2)."""
    return signature.strip().lower() == _md5(f"{out_sum}:{inv_id}:{password_2}").lower()


# ---------------------------------------------------------------------------
# Сервис
# ---------------------------------------------------------------------------


@dataclass
class Invoice:
    invoice_id: int
    sku_id: str
    amount_kop: int
    url: Optional[str]      # None в режиме заглушки
    provider: str
    stub: bool


class PaymentService:
    """Счета, оплата и зачисление на базе storage + pricing."""

    def __init__(self, storage: Any, pricing: Optional[Pricing] = None):
        self.storage = storage
        self.pricing = pricing or Pricing.load()
        self.login = ""
        self.password_1 = ""
        self.password_2 = ""
        self.is_test = False
        self._next_inv = int(storage.next_invoice_id())
        # колбэки, выставляются ботом (async): уведомление об оплате, рассылка
        self.on_paid_notify: Optional[Callable[[Dict[str, Any]], Any]] = None
        self.on_broadcast: Optional[Callable[[Dict[str, Any]], Any]] = None

    def configure(self, login: str, password_1: str, password_2: str,
                  is_test: bool = False) -> None:
        self.login, self.password_1, self.password_2 = login, password_1, password_2
        self.is_test = is_test

    @property
    def active(self) -> bool:
        return bool(self.login and self.password_1)

    # ------------------------------ счета --------------------------------
    def create_invoice(self, tg_id: int, sku_id: str,
                       payload: Optional[str] = None) -> Invoice:
        sku = self.pricing.sku(sku_id)
        amount_kop = self.pricing.effective_price_kop(sku_id)
        inv_id = self._next_inv
        self._next_inv += 1
        provider = "robokassa" if self.active else "stub"
        url = None
        if self.active:
            url = build_payment_url(
                self.login, self.password_1, amount_kop, inv_id,
                f"МАРСИ-Job: {sku.receipt_name}", is_test=self.is_test)
        self.storage.create_purchase(tg_id, sku_id, amount_kop, inv_id, payload)
        return Invoice(invoice_id=inv_id, sku_id=sku_id, amount_kop=amount_kop,
                       url=url, provider=provider, stub=provider == "stub")

    # ------------------------------ вебхук --------------------------------
    def handle_result(self, out_sum: str, inv_id: int, signature: str
                      ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """ResultURL: подпись → оплата → зачисление. Идемпотентно."""
        if not self.active:
            return False, "мерчант не настроен", None
        if not verify_result_signature(self.password_2, out_sum, inv_id, signature):
            return False, "подпись не совпала", None
        ok, msg, purchase = self.storage.complete_purchase(inv_id)
        if not ok or purchase is None:
            return ok, msg, purchase
        if msg == "уже оплачен":
            return True, "уже оплачен", purchase  # без повторного начисления
        self._grant(purchase)
        return True, "оплачено", purchase

    def _grant(self, purchase: Dict[str, Any]) -> None:
        """Начисление по типу SKU (вызывается один раз на оплату)."""
        sku = self.pricing.sku(purchase["sku_id"])
        tg_id = int(purchase["tg_id"])
        if sku.type == "subscription":
            plan = PLAN_OF_SKU[sku.id]
            self.storage.grant_subscription(tg_id, plan, sku.days)
        elif sku.type == "consumable":
            self.storage.add_entitlements(tg_id, "invite", sku.quantity)
        # oneoff (why_report): доставляется в /why; service: колбэк вебхука

    # ------------------------------ админ-грант ---------------------------
    def grant_sku(self, tg_id: int, sku_id: str) -> str:
        """Ручное начисление (админ): возвращает человекочитаемый результат."""
        sku = self.pricing.sku(sku_id)
        if sku.type == "subscription":
            plan = PLAN_OF_SKU[sku.id]
            until = self.storage.grant_subscription(tg_id, plan, sku.days)
            return f"подписка {plan} до {until[:10]}"
        if sku.type == "consumable":
            kind = "mock_interview" if "mock_interview" in sku.id else "invite"
            bal = self.storage.add_entitlements(tg_id, kind, sku.quantity)
            label = "мок-интервью" if kind == "mock_interview" else "приглашений"
            return f"+{sku.quantity} {label} (баланс {bal})"
        if sku_id == "target_broadcast":
            self.storage.add_entitlements(tg_id, "broadcast", 1)
            return "таргет-рассылка доступна: /broadcast <код вакансии>"
        if sku_id == "mock_interview":
            self.storage.add_entitlements(tg_id, "mock_interview", 1)
            return "мок-интервью доступно: /interview"
        # oneoff и service (audit, roadmap, why_report): через purchase
        inv_id = self.storage.next_invoice_id()
        self.storage.create_purchase(tg_id, sku_id, 0, inv_id, payload="admin")
        self.storage.complete_purchase(inv_id)
        return f"{sku.title}: доступен (1 шт.)"
        raise PricingError(f"неизвестный тип SKU: {sku.type}")
