"""Юридические документы МАРСИ-Job: оферта и политика конфиденциальности.

Шаблоны — legal/*.html с плейсхолдерами {{token}}; значения — data/legal.json
(реквизиты заполняются владельцем при регистрации мерчанта Robokassa).
Отдаются веб-сервером по /legal/oferta и /legal/privacy — по этим адресам
модерация Robokassa проверяет документы.
"""
import json
import os
import re
from pathlib import Path
from typing import Dict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEGAL_DIR = PROJECT_ROOT / "legal"
CONFIG_PATH = PROJECT_ROOT / "data" / "legal.json"

_TOKEN_RE = re.compile(r"\{\{(\w+)\}\}")


def load_legal_config() -> Dict[str, str]:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    # публичный адрес можно переопределить окружением (домен при появлении)
    cfg["public_url"] = os.environ.get(
        "MARSIJOB_PUBLIC_URL", cfg.get("public_url", "")).rstrip("/")
    return cfg


def render(template_name: str, cfg: Dict[str, str] = None) -> str:
    """Подставляет плейсхолдеры {{token}} значениями конфига."""
    cfg = cfg or load_legal_config()
    template = (LEGAL_DIR / template_name).read_text(encoding="utf-8")

    def sub(m):
        return str(cfg.get(m.group(1), m.group(0)))

    return _TOKEN_RE.sub(sub, template)


def has_unfilled(text: str):
    """Незаполненные плейсхолдеры (для самопроверки и тестов)."""
    return _TOKEN_RE.findall(text)


def legal_urls(cfg: Dict[str, str] = None) -> Dict[str, str]:
    cfg = cfg or load_legal_config()
    base = cfg["public_url"]
    return {
        "oferta": f"{base}/legal/oferta",
        "privacy": f"{base}/legal/privacy",
        "root": f"{base}/legal",
    }
