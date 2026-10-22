import httpx
import pytest
import respx

from tests.conftest import RealNetworkBlocked


async def test_network_guard_blocks_openai():
    with pytest.raises(RealNetworkBlocked):
        async with httpx.AsyncClient() as c:
            await c.get("https://api.openai.com/v1/models")


def test_network_guard_blocks_openai_sync():
    with pytest.raises(RealNetworkBlocked), httpx.Client() as c:
        c.get("https://api.openai.com/v1/models")


def test_network_guard_blocks_raw_sockets():
    import socket

    with pytest.raises(RealNetworkBlocked):
        socket.create_connection(("api.openai.com", 443), timeout=1)
    with pytest.raises(RealNetworkBlocked):
        socket.getaddrinfo("api.openai.com", 443)


@pytest.mark.parametrize("using", ["httpcore", "httpx"])
async def test_respx_mocked_openai_route_still_works_under_guard(using):
    with respx.mock(assert_all_called=True, using=using) as router:
        router.get("https://api.openai.com/v1/models").respond(200, json={"data": []})
        async with httpx.AsyncClient() as c:
            resp = await c.get("https://api.openai.com/v1/models")
    assert resp.json() == {"data": []}
