import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import pytest


@pytest.fixture()
async def aiohttp_client():
    """Самодостаточный клиент aiohttp (без плагина pytest-aiohttp).

    Общая фикстура для тестов веб-маршрутов (платежи, юридические страницы).
    """
    from aiohttp.test_utils import TestClient, TestServer

    clients = []

    async def _make(app):
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    yield _make

    for c in clients:
        await c.close()
