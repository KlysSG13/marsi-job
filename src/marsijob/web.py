"""HTTP-сервер вебхуков Robokassa (aiohttp, рядом с polling бота).

Маршруты:
  GET/POST /pay/robokassa/result  — ResultURL: проверка подписи, зачисление
                                     кредитов; ответ `OK<inv_id>` (требование Robokassa)
  GET      /pay/robokassa/success — страница успеха (редирект пользователя)
  GET      /pay/robokassa/fail    — страница неудачи
  GET      /health                — для мониторинга

Сервер поднимается вместе с ботом (один event loop, порт MARSIJOB_WEB_PORT,
по умолчанию 8080; слушает 127.0.0.1 — наружу открывается reverse-proxy'ем,
URL вебхука: https://<домен>/pay/robokassa/result).
"""
import logging
from typing import Optional

from aiohttp import web

log = logging.getLogger("marsijob.web")


def build_app(payment_service) -> web.Application:
    async def result(request: web.Request) -> web.Response:
        params = request.query if request.method == "GET" else await request.post()
        out_sum = str(params.get("OutSum", ""))
        inv_id_raw = str(params.get("InvId", ""))
        signature = str(params.get("SignatureValue", ""))
        try:
            inv_id = int(inv_id_raw)
        except ValueError:
            return web.Response(text="bad inv_id", status=400)
        ok, msg, purchase = payment_service.handle_result(out_sum, inv_id, signature)
        if ok and purchase and msg == "оплачено":
            log.info("purchase paid: inv_id=%s sku=%s", inv_id, purchase.get("sku_id"))
            try:
                if payment_service.on_paid_notify:
                    await payment_service.on_paid_notify(purchase)
                if purchase.get("sku_id") == "target_broadcast" and payment_service.on_broadcast:
                    await payment_service.on_broadcast(purchase)
            except Exception as e:
                log.warning("paid post-actions failed: %s", e)
            return web.Response(text=f"OK{inv_id}")
        if ok:
            return web.Response(text=f"OK{inv_id}")  # повторный вебхук идемпотентен
        log.warning("result rejected: inv_id=%s msg=%s", inv_id, msg)
        return web.Response(text=f"REJECT: {msg}", status=400)

    async def success(request: web.Request) -> web.Response:
        return web.Response(
            text="<h2>✅ Оплата прошла!</h2>Возвращайтесь в бота — пакет уже начислен. "
                 "<a href=\"https://t.me/MarsiJobBot\">@MarsiJobBot</a>",
            content_type="text/html", charset="utf-8")

    async def fail(request: web.Request) -> web.Response:
        return web.Response(
            text="<h2>Оплата не завершена</h2>Пакет не начислен. Можно попробовать снова в боте.",
            content_type="text/html", charset="utf-8")

    async def health(request: web.Request) -> web.Response:
        mode = "robokassa" if payment_service.active else "stub"
        return web.json_response({"ok": True, "payments": mode})

    async def legal_index(request: web.Request) -> web.Response:
        from .legal import legal_urls
        urls = legal_urls()
        return web.Response(
            text=("<h2>Юридические документы МАРСИ-Job</h2>"
                  f'<p><a href="{urls["oferta"]}">Договор-оферта</a></p>'
                  f'<p><a href="{urls["privacy"]}">Политика конфиденциальности</a></p>'),
            content_type="text/html", charset="utf-8")

    async def legal_oferta(request: web.Request) -> web.Response:
        from .legal import render
        return web.Response(text=render("oferta.html"),
                            content_type="text/html", charset="utf-8")

    async def legal_privacy(request: web.Request) -> web.Response:
        from .legal import render
        return web.Response(text=render("privacy.html"),
                            content_type="text/html", charset="utf-8")

    app = web.Application()
    app.router.add_route("*", "/pay/robokassa/result", result)
    app.router.add_get("/pay/robokassa/success", success)
    app.router.add_get("/pay/robokassa/fail", fail)
    app.router.add_get("/health", health)
    app.router.add_get("/legal", legal_index)
    app.router.add_get("/legal/oferta", legal_oferta)
    app.router.add_get("/legal/privacy", legal_privacy)
    return app


async def start_web(payment_service, port: int, host: str = "127.0.0.1"):
    """Запускает вебхук-сервер; вызывается из main() бота."""
    runner = web.AppRunner(build_app(payment_service))
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    log.info("webhooks listening on %s:%s (payments=%s)", host, port,
             "robokassa" if payment_service.active else "stub")
    return runner
