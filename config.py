from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    valhalla_url: str | None
    valhalla_route_path: str
    radar_secret_key: str | None
    radar_publishable_key: str | None
    radar_base_url: str | None
    mapbox_access_token: str | None
    database_url: str | None
    database_path: Path
    internal_api_key: str | None
    relationship_api_url: str | None
    relationship_api_key: str | None
    relationship_push_path: str
    infrastructure_corridor_metres: int
    max_officers_per_route_query: int
    valhalla_timeout_seconds: float
    radar_timeout_seconds: float
    relationship_timeout_seconds: float
    environment: str
    port: int
    host: str
    seed_demo_devices: bool
    radar_fixture_json: dict[str, dict[str, float]]


def _json_env(name: str, default: dict[str, dict[str, float]] | None = None) -> dict[str, dict[str, float]]:
    raw = os.getenv(name)
    if not raw:
        return default or {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return default or {}
    if isinstance(parsed, dict):
        return parsed  # type: ignore[return-value]
    return default or {}


def load_settings(base_dir: Path | None = None) -> Settings:
    root = base_dir or Path(__file__).resolve().parent
    return Settings(
        valhalla_url=os.getenv("VALHALLA_URL"),
        valhalla_route_path=os.getenv("VALHALLA_ROUTE_PATH", "/route"),
        radar_secret_key=os.getenv("RADAR_SECRET_KEY"),
        radar_publishable_key=os.getenv("RADAR_PUBLISHABLE_KEY"),
        radar_base_url=os.getenv("RADAR_BASE_URL"),
        mapbox_access_token=os.getenv("MAPBOX_ACCESS_TOKEN"),
        database_url=os.getenv("DATABASE_URL"),
        database_path=Path(os.getenv("DATABASE_PATH", root / "routecalculator.db")),
        internal_api_key=os.getenv("INTERNAL_API_KEY"),
        relationship_api_url=os.getenv("RELATIONSHIP_API_URL"),
        relationship_api_key=os.getenv("RELATIONSHIP_API_KEY"),
        relationship_push_path=os.getenv("RELATIONSHIP_PUSH_PATH", "/route/push"),
        infrastructure_corridor_metres=int(os.getenv("INFRASTRUCTURE_CORRIDOR_METRES", "100")),
        max_officers_per_route_query=int(os.getenv("MAX_OFFICERS_PER_ROUTE_QUERY", "20")),
        valhalla_timeout_seconds=float(os.getenv("VALHALLA_TIMEOUT_SECONDS", "5")),
        radar_timeout_seconds=float(os.getenv("RADAR_TIMEOUT_SECONDS", "5")),
        relationship_timeout_seconds=float(os.getenv("RELATIONSHIP_TIMEOUT_SECONDS", "5")),
        environment=os.getenv("ENVIRONMENT", "production"),
        port=int(os.getenv("PORT", "8000")),
        host=os.getenv("HOST", "127.0.0.1"),
        seed_demo_devices=os.getenv("SEED_DEMO_DEVICES", "true").lower() in {"1", "true", "yes", "on"},
        radar_fixture_json=_json_env("RADAR_FIXTURES_JSON"),
    )
