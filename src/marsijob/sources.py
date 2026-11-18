"""Оркестратор внешних источников вакансий.

Порядок: «Работа России» (trudvsem, открытый) и Хабр Карьера (публичный RSS)
— всегда; hh.ru — при наличии токена приложения (HH_CLIENT_ID/HH_CLIENT_SECRET,
регистрация на dev.hh.ru).
"""
import logging
import os
from typing import Any, Dict

from . import habr_sync, hh_sync, tv_sync

log = logging.getLogger("marsijob.sources")


def hh_token_from_env() -> str:
    client_id = os.environ.get("HH_CLIENT_ID", "")
    secret = os.environ.get("HH_CLIENT_SECRET", "")
    if not (client_id and secret):
        return ""
    try:
        return hh_sync.get_app_token(client_id, secret)
    except hh_sync.HhError as e:
        log.warning("hh token failed: %s", e)
        return ""


def sync_all(storage: Any) -> Dict[str, Dict[str, int]]:
    """Синхронизирует все доступные источники. Возвращает статистику по ним."""
    report: Dict[str, Dict[str, int]] = {}
    try:
        report["trudvsem"] = tv_sync.sync_trudvsem(storage)
    except Exception as e:
        log.warning("trudvsem sync error: %s", e)
        report["trudvsem"] = {"error": 1}
    try:
        report["habr"] = habr_sync.sync_habr(storage)
    except Exception as e:
        log.warning("habr sync error: %s", e)
        report["habr"] = {"error": 1}
    token = hh_token_from_env()
    if token:
        try:
            report["hh"] = hh_sync.sync_once(storage, token=token)
        except Exception as e:
            log.warning("hh sync error: %s", e)
            report["hh"] = {"error": 1}
    else:
        report["hh"] = {"skipped": 1}
    # чистка внешних вакансий (оба источника)
    storage.prune_external_vacancies()
    return report
