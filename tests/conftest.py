import os
import socket

import fakeredis
import pytest
import pytest_asyncio

os.environ.setdefault("GTM_ENV", "sandbox")


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


SERVICES_UP = _port_open("localhost", 5432) and _port_open("localhost", 6379)


def pytest_collection_modifyitems(config, items):
    if SERVICES_UP:
        return
    skip = pytest.mark.skip(reason="docker services (postgres/redis/neo4j) not running")
    for item in items:
        if item.get_closest_marker("integration"):
            item.add_marker(skip)


@pytest_asyncio.fixture
async def redis():
    from gtm_core.redis_client import set_redis

    client = fakeredis.FakeAsyncRedis(decode_responses=True)
    set_redis(client)
    yield client
    await client.flushall()
    await client.aclose()
    set_redis(None)
