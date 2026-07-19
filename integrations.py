from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

try:  # Optional dependency in this sandbox.
    import httpx  # type: ignore
except Exception:  # pragma: no cover - optional import
    httpx = None  # type: ignore


@dataclass(frozen=True)
class RoutePlan:
    points: list[tuple[float, float]]
    distance_metres: float
    estimated_time_minutes: float
    turn_by_turn: list[str]
    source: str
    raw: dict[str, Any]


def _decode_polyline6(encoded: str) -> list[tuple[float, float]]:
    index = 0
    lat = 0
    lng = 0
    coordinates: list[tuple[float, float]] = []

    while index < len(encoded):
        result = 0
        shift = 0
        b = 0x20
        while b >= 0x20:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
        d_lat = ~(result >> 1) if result & 1 else result >> 1

        result = 0
        shift = 0
        b = 0x20
        while b >= 0x20:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
        d_lng = ~(result >> 1) if result & 1 else result >> 1

        lat += d_lat
        lng += d_lng
        coordinates.append((lat / 1e6, lng / 1e6))

    return coordinates


class ValhallaClient:
    def __init__(self, base_url: str | None, timeout_seconds: float) -> None:
        self.base_url = base_url.rstrip("/") if base_url else None
        self.timeout_seconds = timeout_seconds

    @property
    def available(self) -> bool:
        return bool(self.base_url and httpx is not None)

    def route(
        self,
        *,
        routing_type: str,
        origin: tuple[float, float],
        destination: tuple[float, float],
        prioritise: str = "speed",
    ) -> RoutePlan | None:
        if not self.available:
            return None

        costing = "pedestrian" if routing_type == "foot" else "auto"
        costing_options: dict[str, Any] = {}
        if routing_type in {"vehicle", "hybrid"}:
            costing_options["auto"] = {
                "top_speed": 90,
                "use_tolls": prioritise != "avoid_cost",
                "use_highways": True,
            }
        else:
            costing_options["pedestrian"] = {
                "walking_speed": 5.0 if prioritise != "speed" else 15.0,
                "prefer_shortest": prioritise == "distance",
            }

        payload = {
            "locations": [
                {"lat": origin[0], "lon": origin[1]},
                {"lat": destination[0], "lon": destination[1]},
            ],
            "costing": costing,
            "directions_options": {"units": "kilometers"},
            "costing_options": costing_options,
        }
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:  # type: ignore[operator]
                response = client.post(f"{self.base_url}/route", json=payload)
                response.raise_for_status()
                data = response.json()
        except Exception:
            return None

        trip = (data.get("trip") or {})
        legs = trip.get("legs") or []
        leg = legs[0] if legs else {}
        summary = leg.get("summary") or {}
        shape = leg.get("shape")
        points = _decode_polyline6(shape) if isinstance(shape, str) else [origin, destination]
        maneuvers = leg.get("maneuvers") or []
        turn_by_turn = [m.get("instruction") for m in maneuvers if m.get("instruction")]
        return RoutePlan(
            points=points,
            distance_metres=float(summary.get("length", 0)) * 1000.0,
            estimated_time_minutes=float(summary.get("time", 0)) / 60.0,
            turn_by_turn=turn_by_turn or [
                "Head toward the destination using the routed network.",
                "Follow the routed path to the incident.",
            ],
            source="valhalla",
            raw=data,
        )

    def health(self) -> dict[str, Any]:
        if not self.available:
            return {"status": "unconfigured"}
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:  # type: ignore[operator]
                response = client.get(self.base_url)
                return {
                    "status": "ok" if response.status_code < 500 else "degraded",
                    "code": response.status_code,
                }
        except Exception as exc:
            return {"status": "down", "error": str(exc)}


class RadarClient:
    def __init__(
        self,
        *,
        secret_key: str | None,
        publishable_key: str | None,
        base_url: str | None,
        timeout_seconds: float,
        fixtures: dict[str, dict[str, float]] | None = None,
    ) -> None:
        self.secret_key = secret_key
        self.publishable_key = publishable_key
        self.base_url = base_url.rstrip("/") if base_url else None
        self.timeout_seconds = timeout_seconds
        self.fixtures = fixtures or {}

    @property
    def available(self) -> bool:
        return bool((self.secret_key or self.publishable_key) and self.base_url and httpx is not None)

    def get_position(self, identifier: str) -> tuple[float, float] | None:
        fixture = self.fixtures.get(identifier)
        if fixture and "lat" in fixture and "lng" in fixture:
            return float(fixture["lat"]), float(fixture["lng"])
        if not self.available:
            return None

        headers = {}
        if self.secret_key:
            headers["Authorization"] = f"Bearer {self.secret_key}"
        elif self.publishable_key:
            headers["X-Radar-Publishable-Key"] = self.publishable_key

        try:
            with httpx.Client(timeout=self.timeout_seconds, headers=headers) as client:  # type: ignore[operator]
                response = client.get(f"{self.base_url}/officers/{identifier}")
                if response.status_code >= 400:
                    return None
                data = response.json()
        except Exception:
            return None

        location = data.get("location") or data
        lat = location.get("lat") if isinstance(location, dict) else None
        lng = location.get("lng") if isinstance(location, dict) else None
        if lat is None or lng is None:
            return None
        return float(lat), float(lng)

    def health(self) -> dict[str, Any]:
        if self.fixtures:
            return {"status": "fixture"}
        if not self.available:
            return {"status": "unconfigured"}
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:  # type: ignore[operator]
                response = client.get(f"{self.base_url}/health")
                return {"status": "ok" if response.status_code < 500 else "degraded", "code": response.status_code}
        except Exception as exc:
            return {"status": "down", "error": str(exc)}


class RelationshipClient:
    def __init__(self, base_url: str | None, api_key: str | None, timeout_seconds: float, push_path: str = "/route/push") -> None:
        self.base_url = base_url.rstrip("/") if base_url else None
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.push_path = push_path if push_path.startswith("/") else f"/{push_path}"

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.api_key and httpx is not None)

    def push_route(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.available:
            return {"configured": False, "delivered": False}
        try:
            with httpx.Client(timeout=self.timeout_seconds, headers={"Authorization": f"Bearer {self.api_key}"}) as client:  # type: ignore[operator]
                response = client.post(f"{self.base_url}{self.push_path}", json=payload)
                response.raise_for_status()
                data = response.json()
                return {"configured": True, "delivered": True, "response": data}
        except Exception as exc:
            return {"configured": True, "delivered": False, "error": str(exc)}

    def health(self) -> dict[str, Any]:
        if not self.available:
            return {"status": "unconfigured"}
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:  # type: ignore[operator]
                response = client.get(f"{self.base_url}/health")
                return {"status": "ok" if response.status_code < 500 else "degraded", "code": response.status_code}
        except Exception as exc:
            return {"status": "down", "error": str(exc)}
