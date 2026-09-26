from __future__ import annotations

from datetime import date

import httpx
import pytest

from cliproxy_billing.keeper import KeeperClient, KeeperError


@pytest.mark.asyncio
async def test_keeper_auth_key_lookup_and_day_range() -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path.endswith("/auth/login"):
            assert request.headers["X-CPA-Usage-Keeper-Request"] == "fetch"
            return httpx.Response(
                204,
                headers={"Set-Cookie": "cpa_usage_keeper_session=abc; Path=/usage"},
            )
        assert request.headers["Cookie"] == "cpa_usage_keeper_session=abc"
        if request.url.path.endswith("/usage/api-keys/settings"):
            return httpx.Response(
                200,
                json={"items": [{"id": "7", "apiKey": "secret-key", "keyAlias": "One"}]},
            )
        if request.url.path.endswith("/usage/analysis"):
            assert request.url.params["unit"] == "day"
            assert request.url.params["start"] == "2026-08-01"
            assert request.url.params["end"] == "2026-08-31"
            return httpx.Response(
                200,
                json={
                    "cost_breakdown": {"cost_available": True, "total_cost_usd": 12.345},
                    "api_key_composition": [
                        {
                            "key": "7",
                            "label": "One",
                            "cost_available": True,
                            "cost_usd": 12.345,
                            "requests": 3,
                        }
                    ],
                },
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    async with KeeperClient("http://keeper/usage/api/v1", "password", client) as keeper:
        key = await keeper.identify_key("secret-key")
        assert key is not None and key.id == "7" and key.value is None
        assert await keeper.identify_key("wrong") is None
        analysis = await keeper.analysis(date(2026, 8, 1), date(2026, 9, 1))
        assert str(analysis.keys[0].cost_usd) == "12.345"
    assert sum(url.endswith("/auth/login") for url in requests) == 1


@pytest.mark.asyncio
async def test_keeper_missing_price_blocks_billing() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(
                200,
                headers={"Set-Cookie": "cpa_usage_keeper_session=abc; Path=/usage"},
            )
        return httpx.Response(
            200,
            json={
                "cost_breakdown": {"cost_available": False},
                "model_composition": [
                    {"key": "local-model", "label": "local-model", "cost_available": False}
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    async with KeeperClient("http://keeper/usage/api/v1", "password", client) as keeper:
        with pytest.raises(KeeperError, match="local-model"):
            await keeper.analysis(date(2026, 8, 1), date(2026, 9, 1))
