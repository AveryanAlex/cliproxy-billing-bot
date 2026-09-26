from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx


class KeeperError(RuntimeError):
    pass


@dataclass(frozen=True)
class KeeperKey:
    id: str
    label: str
    value: str | None = None


@dataclass(frozen=True)
class KeyUsage:
    key_id: str
    label: str
    cost_usd: Decimal
    requests: int


@dataclass(frozen=True)
class UsageAnalysis:
    keys: tuple[KeyUsage, ...]
    total_cost_usd: Decimal


class KeeperClient:
    def __init__(
        self, base_url: str, password: str, client: httpx.AsyncClient | None = None
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._password = password
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(45), follow_redirects=False
        )
        self._authenticated = False

    async def __aenter__(self) -> KeeperClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _login(self) -> None:
        try:
            response = await self._client.post(
                f"{self._base_url}/auth/login",
                json={"password": self._password},
                headers={"X-CPA-Usage-Keeper-Request": "fetch"},
            )
        except httpx.HTTPError as error:
            raise KeeperError("Нет связи с CPA Usage Keeper") from error
        if response.status_code != 200 or not self._client.cookies:
            raise KeeperError("Не удалось войти в CPA Usage Keeper; проверьте пароль")
        self._authenticated = True

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        if not self._authenticated:
            await self._login()
        try:
            response = await self._client.get(f"{self._base_url}/{path}", params=params)
            if response.status_code == 401:
                self._authenticated = False
                self._client.cookies.clear()
                await self._login()
                response = await self._client.get(f"{self._base_url}/{path}", params=params)
        except httpx.HTTPError as error:
            raise KeeperError("Нет связи с CPA Usage Keeper") from error
        if response.status_code != 200:
            raise KeeperError(f"CPA Usage Keeper вернул HTTP {response.status_code} для {path}")
        try:
            payload: Any = json.loads(response.content, parse_float=Decimal)
        except (ValueError, UnicodeDecodeError) as error:
            raise KeeperError("CPA Usage Keeper вернул неверный JSON") from error
        if not isinstance(payload, dict):
            raise KeeperError("CPA Usage Keeper вернул неожиданный ответ")
        return payload

    async def active_keys(self, *, with_values: bool = False) -> tuple[KeeperKey, ...]:
        path = "usage/api-keys/settings" if with_values else "usage/api-keys"
        payload = await self._get(path)
        items = payload.get("items")
        if not isinstance(items, list):
            raise KeeperError("CPA Usage Keeper не вернул список ключей")
        result: list[KeeperKey] = []
        for item in items:
            if not isinstance(item, dict) or not item.get("id"):
                raise KeeperError("Некорректная запись ключа в CPA Usage Keeper")
            result.append(
                KeeperKey(
                    id=str(item["id"]),
                    label=str(
                        item.get("keyAlias")
                        or item.get("label")
                        or item.get("displayKey")
                        or item["id"]
                    ),
                    value=str(item.get("apiKey")) if with_values and item.get("apiKey") else None,
                )
            )
        return tuple(result)

    async def identify_key(self, supplied_key: str) -> KeeperKey | None:
        for key in await self.active_keys(with_values=True):
            if key.value is not None and hmac.compare_digest(key.value, supplied_key):
                return KeeperKey(id=key.id, label=key.label)
        return None

    async def analysis(self, start_date: date, end_exclusive: date) -> UsageAnalysis:
        if start_date >= end_exclusive:
            raise ValueError("Пустой период расчёта")
        end_date = end_exclusive - timedelta(days=1)
        payload = await self._get(
            "usage/analysis",
            {
                "range": "custom",
                "unit": "day",
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
            },
        )
        breakdown = payload.get("cost_breakdown")
        if not isinstance(breakdown, dict):
            raise KeeperError("CPA Usage Keeper не вернул стоимость")
        if not breakdown.get("cost_available"):
            missing = [
                str(item.get("label") or item.get("key") or "неизвестная модель")
                for item in payload.get("model_composition", [])
                if isinstance(item, dict) and not item.get("cost_available")
            ]
            suffix = ", ".join(missing) if missing else "проверьте цены моделей в Keeper"
            raise KeeperError(f"Неизвестна цена модели: {suffix}. Расчёт не выпущен.")
        composition = payload.get("api_key_composition")
        if not isinstance(composition, list):
            raise KeeperError("CPA Usage Keeper не вернул распределение по ключам")
        keys: list[KeyUsage] = []
        for item in composition:
            if not isinstance(item, dict):
                raise KeeperError("Некорректная запись использования ключа")
            key_id = str(item.get("key") or "")
            if not key_id:
                raise KeeperError("Использование без ID ключа; расчёт не выпущен")
            if not item.get("cost_available"):
                raise KeeperError(
                    f"Неизвестна стоимость ключа {item.get('label') or key_id}; расчёт не выпущен"
                )
            try:
                cost = Decimal(str(item.get("cost_usd") or 0))
                requests = int(item.get("requests") or 0)
            except (ValueError, TypeError) as error:
                raise KeeperError("Некорректная стоимость использования") from error
            if not cost.is_finite() or cost < 0 or requests < 0:
                raise KeeperError("Некорректная стоимость использования")
            keys.append(
                KeyUsage(
                    key_id=key_id,
                    label=str(item.get("label") or key_id),
                    cost_usd=cost,
                    requests=requests,
                )
            )
        try:
            total_cost = Decimal(str(breakdown.get("total_cost_usd") or 0))
        except ValueError as error:
            raise KeeperError("Некорректная суммарная стоимость") from error
        return UsageAnalysis(keys=tuple(keys), total_cost_usd=total_cost)
