from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

try:  # Optional dependency for production deployments.
    import psycopg  # type: ignore
    from psycopg.rows import dict_row  # type: ignore
except Exception:  # pragma: no cover - optional import
    psycopg = None  # type: ignore
    dict_row = None  # type: ignore


def _json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for key in ("capabilities", "safety_constraints", "infrastructure_recommendations", "route_geojson", "execution_result", "request_payload", "response_payload"):
        if key in result and result[key] is not None:
            try:
                result[key] = json.loads(result[key])
            except Exception:
                pass
    return result


def _coerce_json_fields(result: dict[str, Any]) -> dict[str, Any]:
    for key in ("capabilities", "safety_constraints", "infrastructure_recommendations", "route_geojson", "execution_result", "request_payload", "response_payload"):
        if key in result and result[key] is not None and not isinstance(result[key], (list, dict)):
            try:
                result[key] = json.loads(result[key])
            except Exception:
                pass
    return result


def _stable_uuid(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, str(value)))


class SQLiteStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS infrastructure_devices (
                    id TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL,
                    device_id TEXT UNIQUE NOT NULL,
                    type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    lat REAL,
                    lng REAL,
                    floor INTEGER,
                    building_id TEXT,
                    description TEXT,
                    connection_protocol TEXT,
                    connection_endpoint TEXT,
                    auth_type TEXT,
                    auth_key_reference TEXT,
                    capabilities TEXT DEFAULT '[]',
                    default_state TEXT,
                    safety_constraints TEXT DEFAULT '{}',
                    operational INTEGER DEFAULT 1,
                    last_health_check TEXT,
                    health_status TEXT DEFAULT 'unknown',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS route_history (
                    id TEXT PRIMARY KEY,
                    route_id TEXT UNIQUE NOT NULL,
                    org_id TEXT NOT NULL,
                    incident_id TEXT NOT NULL,
                    officer_id TEXT,
                    vehicle_id TEXT,
                    routing_type TEXT,
                    origin_lat REAL,
                    origin_lng REAL,
                    destination_lat REAL,
                    destination_lng REAL,
                    distance_metres INTEGER,
                    estimated_time_minutes REAL,
                    actual_time_minutes REAL,
                    infrastructure_recommendations TEXT DEFAULT '[]',
                    route_geojson TEXT,
                    pushed_to_device INTEGER DEFAULT 0,
                    pushed_at TEXT,
                    completed_at TEXT,
                    request_payload TEXT,
                    response_payload TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS infrastructure_action_log (
                    id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    org_id TEXT NOT NULL,
                    incident_id TEXT,
                    route_id TEXT,
                    recommended_action TEXT,
                    approved INTEGER DEFAULT 0,
                    approved_by TEXT,
                    approved_at TEXT,
                    executed INTEGER DEFAULT 0,
                    executed_at TEXT,
                    reverted_at TEXT,
                    execution_result TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        if self.device_count() == 0:
            self.seed_demo_devices()

    def device_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM infrastructure_devices").fetchone()
            return int(row["count"]) if row else 0

    def seed_demo_devices(self) -> None:
        demo_devices = [
            {
                "org_id": "org_xyz",
                "device_id": "DEV-ELEV-001",
                "type": "SMART_ELEVATOR",
                "name": "Main Lobby Elevator",
                "lat": 6.42812,
                "lng": 3.42194,
                "floor": 0,
                "building_id": "BLDG-HOTEL-001",
                "description": "Primary elevator from lobby to guest floors",
                "capabilities": ["hold_floor", "send_to_floor", "reserve_for_officers", "normal"],
                "default_state": "normal",
                "operational": 1,
                "health_status": "healthy",
            },
            {
                "org_id": "org_xyz",
                "device_id": "DEV-DOOR-EXIT-001",
                "type": "SMART_DOOR",
                "name": "Hotel North Exit",
                "lat": 6.42815,
                "lng": 3.42185,
                "floor": 0,
                "building_id": "BLDG-HOTEL-001",
                "description": "North exit door near service corridor",
                "capabilities": ["unlock", "lock", "hold_open", "hold_closed"],
                "default_state": "locked",
                "operational": 1,
                "health_status": "healthy",
            },
            {
                "org_id": "org_xyz",
                "device_id": "DEV-LIGHT-001",
                "type": "TRAFFIC_LIGHT",
                "name": "Marina Road Signal",
                "lat": 6.42890,
                "lng": 3.42240,
                "capabilities": ["green_corridor", "red_cross_traffic", "normal"],
                "default_state": "normal",
                "operational": 1,
                "health_status": "healthy",
            },
        ]
        for device in demo_devices:
            self.register_device(device)

    def register_device(self, payload: dict[str, Any]) -> dict[str, Any]:
        device = {
            "id": payload.get("id") or str(uuid.uuid4()),
            "org_id": payload["org_id"],
            "device_id": payload["device_id"],
            "type": payload["type"],
            "name": payload["name"],
            "lat": payload.get("lat"),
            "lng": payload.get("lng"),
            "floor": payload.get("floor"),
            "building_id": payload.get("building_id"),
            "description": payload.get("description"),
            "connection_protocol": payload.get("connection_protocol"),
            "connection_endpoint": payload.get("connection_endpoint"),
            "auth_type": payload.get("auth_type"),
            "auth_key_reference": payload.get("auth_key_reference"),
            "capabilities": _json_dumps(payload.get("capabilities", [])),
            "default_state": payload.get("default_state"),
            "safety_constraints": _json_dumps(payload.get("safety_constraints", {})),
            "operational": 1 if payload.get("operational", True) else 0,
            "last_health_check": payload.get("last_health_check"),
            "health_status": payload.get("health_status", "unknown"),
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO infrastructure_devices (
                    id, org_id, device_id, type, name, lat, lng, floor, building_id, description,
                    connection_protocol, connection_endpoint, auth_type, auth_key_reference,
                    capabilities, default_state, safety_constraints, operational, last_health_check, health_status
                ) VALUES (
                    :id, :org_id, :device_id, :type, :name, :lat, :lng, :floor, :building_id, :description,
                    :connection_protocol, :connection_endpoint, :auth_type, :auth_key_reference,
                    :capabilities, :default_state, :safety_constraints, :operational, :last_health_check, :health_status
                )
                ON CONFLICT(device_id) DO UPDATE SET
                    org_id = excluded.org_id,
                    type = excluded.type,
                    name = excluded.name,
                    lat = excluded.lat,
                    lng = excluded.lng,
                    floor = excluded.floor,
                    building_id = excluded.building_id,
                    description = excluded.description,
                    connection_protocol = excluded.connection_protocol,
                    connection_endpoint = excluded.connection_endpoint,
                    auth_type = excluded.auth_type,
                    auth_key_reference = excluded.auth_key_reference,
                    capabilities = excluded.capabilities,
                    default_state = excluded.default_state,
                    safety_constraints = excluded.safety_constraints,
                    operational = excluded.operational,
                    last_health_check = excluded.last_health_check,
                    health_status = excluded.health_status,
                    updated_at = CURRENT_TIMESTAMP
                """,
                device,
            )
        return self.get_device(device["device_id"]) or device

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM infrastructure_devices WHERE device_id = ?", (device_id,)).fetchone()
        return _row_to_dict(row)

    def list_devices(self, org_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM infrastructure_devices"
        params: tuple[Any, ...] = ()
        if org_id:
            query += " WHERE org_id = ?"
            params = (org_id,)
        query += " ORDER BY created_at ASC, device_id ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [d for d in (_row_to_dict(row) for row in rows) if d is not None]

    def save_route(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = {
            "id": str(uuid.uuid4()),
            "route_id": payload["route_id"],
            "org_id": payload["org_id"],
            "incident_id": payload["incident_id"],
            "officer_id": payload.get("officer_id"),
            "vehicle_id": payload.get("vehicle_id"),
            "routing_type": payload.get("routing_type"),
            "origin_lat": payload.get("origin_lat"),
            "origin_lng": payload.get("origin_lng"),
            "destination_lat": payload.get("destination_lat"),
            "destination_lng": payload.get("destination_lng"),
            "distance_metres": payload.get("distance_metres"),
            "estimated_time_minutes": payload.get("estimated_time_minutes"),
            "actual_time_minutes": payload.get("actual_time_minutes"),
            "infrastructure_recommendations": _json_dumps(payload.get("infrastructure_recommendations", [])),
            "route_geojson": _json_dumps(payload.get("route_geojson", {})),
            "pushed_to_device": 1 if payload.get("pushed_to_device") else 0,
            "pushed_at": payload.get("pushed_at"),
            "completed_at": payload.get("completed_at"),
            "request_payload": _json_dumps(payload.get("request_payload", {})),
            "response_payload": _json_dumps(payload.get("response_payload", {})),
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO route_history (
                    id, route_id, org_id, incident_id, officer_id, vehicle_id, routing_type,
                    origin_lat, origin_lng, destination_lat, destination_lng, distance_metres,
                    estimated_time_minutes, actual_time_minutes, infrastructure_recommendations,
                    route_geojson, pushed_to_device, pushed_at, completed_at, request_payload, response_payload
                ) VALUES (
                    :id, :route_id, :org_id, :incident_id, :officer_id, :vehicle_id, :routing_type,
                    :origin_lat, :origin_lng, :destination_lat, :destination_lng, :distance_metres,
                    :estimated_time_minutes, :actual_time_minutes, :infrastructure_recommendations,
                    :route_geojson, :pushed_to_device, :pushed_at, :completed_at, :request_payload, :response_payload
                )
                ON CONFLICT(route_id) DO UPDATE SET
                    org_id = excluded.org_id,
                    incident_id = excluded.incident_id,
                    officer_id = excluded.officer_id,
                    vehicle_id = excluded.vehicle_id,
                    routing_type = excluded.routing_type,
                    origin_lat = excluded.origin_lat,
                    origin_lng = excluded.origin_lng,
                    destination_lat = excluded.destination_lat,
                    destination_lng = excluded.destination_lng,
                    distance_metres = excluded.distance_metres,
                    estimated_time_minutes = excluded.estimated_time_minutes,
                    actual_time_minutes = excluded.actual_time_minutes,
                    infrastructure_recommendations = excluded.infrastructure_recommendations,
                    route_geojson = excluded.route_geojson,
                    pushed_to_device = excluded.pushed_to_device,
                    pushed_at = excluded.pushed_at,
                    completed_at = excluded.completed_at,
                    request_payload = excluded.request_payload,
                    response_payload = excluded.response_payload
                """,
                row,
            )
        return self.get_route(payload["route_id"]) or row

    def update_route_push(self, route_id: str, pushed_to_device: bool, pushed_at: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE route_history
                SET pushed_to_device = ?, pushed_at = ?, response_payload = response_payload
                WHERE route_id = ?
                """,
                (1 if pushed_to_device else 0, pushed_at, route_id),
            )

    def get_route(self, route_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM route_history WHERE route_id = ?", (route_id,)).fetchone()
        return _row_to_dict(row)

    def list_routes(self, org_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM route_history"
        params: tuple[Any, ...] = ()
        if org_id:
            query += " WHERE org_id = ?"
            params = (org_id,)
        query += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [d for d in (_row_to_dict(row) for row in rows) if d is not None]

    def log_action(self, payload: dict[str, Any]) -> None:
        row = {
            "id": str(uuid.uuid4()),
            "device_id": payload["device_id"],
            "org_id": payload["org_id"],
            "incident_id": payload.get("incident_id"),
            "route_id": payload.get("route_id"),
            "recommended_action": payload.get("recommended_action"),
            "approved": 1 if payload.get("approved") else 0,
            "approved_by": payload.get("approved_by"),
            "approved_at": payload.get("approved_at"),
            "executed": 1 if payload.get("executed") else 0,
            "executed_at": payload.get("executed_at"),
            "reverted_at": payload.get("reverted_at"),
            "execution_result": _json_dumps(payload.get("execution_result", {})),
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO infrastructure_action_log (
                    id, device_id, org_id, incident_id, route_id, recommended_action, approved,
                    approved_by, approved_at, executed, executed_at, reverted_at, execution_result
                ) VALUES (
                    :id, :device_id, :org_id, :incident_id, :route_id, :recommended_action, :approved,
                    :approved_by, :approved_at, :executed, :executed_at, :reverted_at, :execution_result
                )
                """,
                row,
            )


class PostgresStore:
    def __init__(self, database_url: str):
        if psycopg is None:
            raise RuntimeError("psycopg is required for DATABASE_URL-backed storage")
        self.database_url = database_url
        self._initialize()

    def _connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row)  # type: ignore[operator]

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
        if row is None:
            return None
        return _coerce_json_fields(dict(row))

    def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        return [_coerce_json_fields(dict(row)) for row in rows]

    def _initialize(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE SCHEMA IF NOT EXISTS services")
                cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS services.infrastructure_devices (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        org_id UUID NOT NULL,
                        device_id VARCHAR(100) UNIQUE NOT NULL,
                        type VARCHAR(100) NOT NULL,
                        name VARCHAR(255) NOT NULL,
                        lat DECIMAL(10,8),
                        lng DECIMAL(11,8),
                        floor INTEGER,
                        building_id VARCHAR(100),
                        description TEXT,
                        connection_protocol VARCHAR(50),
                        connection_endpoint TEXT,
                        auth_type VARCHAR(50),
                        auth_key_reference VARCHAR(100),
                        capabilities JSONB DEFAULT '[]'::jsonb,
                        default_state VARCHAR(100),
                        safety_constraints JSONB DEFAULT '{}'::jsonb,
                        operational BOOLEAN DEFAULT TRUE,
                        last_health_check TIMESTAMPTZ,
                        health_status VARCHAR(50) DEFAULT 'unknown',
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS services.route_history (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        route_id VARCHAR(100) UNIQUE NOT NULL,
                        org_id UUID NOT NULL,
                        incident_id UUID NOT NULL,
                        officer_id VARCHAR(100),
                        vehicle_id VARCHAR(100),
                        routing_type VARCHAR(50),
                        origin_lat DECIMAL(10,8),
                        origin_lng DECIMAL(11,8),
                        destination_lat DECIMAL(10,8),
                        destination_lng DECIMAL(11,8),
                        distance_metres INTEGER,
                        estimated_time_minutes DECIMAL(8,2),
                        actual_time_minutes DECIMAL(8,2),
                        infrastructure_recommendations JSONB DEFAULT '[]'::jsonb,
                        route_geojson JSONB,
                        pushed_to_device BOOLEAN DEFAULT FALSE,
                        pushed_at TIMESTAMPTZ,
                        completed_at TIMESTAMPTZ,
                        request_payload JSONB,
                        response_payload JSONB,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS services.infrastructure_action_log (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        device_id VARCHAR(100) NOT NULL,
                        org_id UUID NOT NULL,
                        incident_id UUID,
                        route_id VARCHAR(100),
                        recommended_action VARCHAR(100),
                        approved BOOLEAN DEFAULT FALSE,
                        approved_by UUID,
                        approved_at TIMESTAMPTZ,
                        executed BOOLEAN DEFAULT FALSE,
                        executed_at TIMESTAMPTZ,
                        reverted_at TIMESTAMPTZ,
                        execution_result JSONB,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE OR REPLACE FUNCTION services.set_updated_at()
                    RETURNS TRIGGER
                    LANGUAGE plpgsql
                    AS $$
                    BEGIN
                        NEW.updated_at = NOW();
                        RETURN NEW;
                    END;
                    $$;
                    """
                )
                cur.execute("DROP TRIGGER IF EXISTS trigger_infrastructure_devices_updated_at ON services.infrastructure_devices")
                cur.execute(
                    """
                    CREATE TRIGGER trigger_infrastructure_devices_updated_at
                    BEFORE UPDATE ON services.infrastructure_devices
                    FOR EACH ROW
                    EXECUTE FUNCTION services.set_updated_at()
                    """
                )
            conn.commit()
        if self.device_count() == 0:
            self.seed_demo_devices()

    def device_count(self) -> int:
        row = self._fetchone("SELECT COUNT(*) AS count FROM services.infrastructure_devices")
        return int(row["count"]) if row else 0

    def seed_demo_devices(self) -> None:
        demo_devices = [
            {
                "org_id": _stable_uuid("org_xyz"),
                "device_id": "DEV-ELEV-001",
                "type": "SMART_ELEVATOR",
                "name": "Main Lobby Elevator",
                "lat": 6.42812,
                "lng": 3.42194,
                "floor": 0,
                "building_id": "BLDG-HOTEL-001",
                "description": "Primary elevator from lobby to guest floors",
                "capabilities": ["hold_floor", "send_to_floor", "reserve_for_officers", "normal"],
                "default_state": "normal",
                "operational": True,
                "health_status": "healthy",
            },
            {
                "org_id": _stable_uuid("org_xyz"),
                "device_id": "DEV-DOOR-EXIT-001",
                "type": "SMART_DOOR",
                "name": "Hotel North Exit",
                "lat": 6.42815,
                "lng": 3.42185,
                "floor": 0,
                "building_id": "BLDG-HOTEL-001",
                "description": "North exit door near service corridor",
                "capabilities": ["unlock", "lock", "hold_open", "hold_closed"],
                "default_state": "locked",
                "operational": True,
                "health_status": "healthy",
            },
            {
                "org_id": _stable_uuid("org_xyz"),
                "device_id": "DEV-LIGHT-001",
                "type": "TRAFFIC_LIGHT",
                "name": "Marina Road Signal",
                "lat": 6.42890,
                "lng": 3.42240,
                "capabilities": ["green_corridor", "red_cross_traffic", "normal"],
                "default_state": "normal",
                "operational": True,
                "health_status": "healthy",
            },
        ]
        for device in demo_devices:
            self.register_device(device)

    def register_device(self, payload: dict[str, Any]) -> dict[str, Any]:
        device = {
            "id": payload.get("id") or str(uuid.uuid4()),
            "org_id": _stable_uuid(payload["org_id"]),
            "device_id": payload["device_id"],
            "type": payload["type"],
            "name": payload["name"],
            "lat": payload.get("lat"),
            "lng": payload.get("lng"),
            "floor": payload.get("floor"),
            "building_id": payload.get("building_id"),
            "description": payload.get("description"),
            "connection_protocol": payload.get("connection_protocol"),
            "connection_endpoint": payload.get("connection_endpoint"),
            "auth_type": payload.get("auth_type"),
            "auth_key_reference": payload.get("auth_key_reference"),
            "capabilities": json.dumps(payload.get("capabilities", [])),
            "default_state": payload.get("default_state"),
            "safety_constraints": json.dumps(payload.get("safety_constraints", {})),
            "operational": bool(payload.get("operational", True)),
            "last_health_check": payload.get("last_health_check"),
            "health_status": payload.get("health_status", "unknown"),
        }
        sql = """
        INSERT INTO services.infrastructure_devices (
            id, org_id, device_id, type, name, lat, lng, floor, building_id, description,
            connection_protocol, connection_endpoint, auth_type, auth_key_reference,
            capabilities, default_state, safety_constraints, operational, last_health_check, health_status
        ) VALUES (
            %(id)s, %(org_id)s, %(device_id)s, %(type)s, %(name)s, %(lat)s, %(lng)s, %(floor)s, %(building_id)s, %(description)s,
            %(connection_protocol)s, %(connection_endpoint)s, %(auth_type)s, %(auth_key_reference)s,
            %(capabilities)s::jsonb, %(default_state)s, %(safety_constraints)s::jsonb, %(operational)s, %(last_health_check)s, %(health_status)s
        )
        ON CONFLICT(device_id) DO UPDATE SET
            org_id = EXCLUDED.org_id,
            type = EXCLUDED.type,
            name = EXCLUDED.name,
            lat = EXCLUDED.lat,
            lng = EXCLUDED.lng,
            floor = EXCLUDED.floor,
            building_id = EXCLUDED.building_id,
            description = EXCLUDED.description,
            connection_protocol = EXCLUDED.connection_protocol,
            connection_endpoint = EXCLUDED.connection_endpoint,
            auth_type = EXCLUDED.auth_type,
            auth_key_reference = EXCLUDED.auth_key_reference,
            capabilities = EXCLUDED.capabilities,
            default_state = EXCLUDED.default_state,
            safety_constraints = EXCLUDED.safety_constraints,
            operational = EXCLUDED.operational,
            last_health_check = EXCLUDED.last_health_check,
            health_status = EXCLUDED.health_status,
            updated_at = NOW()
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, device)
            conn.commit()
        return self.get_device(device["device_id"]) or device

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        return self._fetchone("SELECT * FROM services.infrastructure_devices WHERE device_id = %s", (device_id,))

    def list_devices(self, org_id: str | None = None) -> list[dict[str, Any]]:
        if org_id:
            return self._fetchall("SELECT * FROM services.infrastructure_devices WHERE org_id = %s ORDER BY created_at ASC, device_id ASC", (_stable_uuid(org_id),))
        return self._fetchall("SELECT * FROM services.infrastructure_devices ORDER BY created_at ASC, device_id ASC")

    def save_route(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = {
            "id": str(uuid.uuid4()),
            "route_id": payload["route_id"],
            "org_id": _stable_uuid(payload["org_id"]),
            "incident_id": _stable_uuid(payload["incident_id"]),
            "officer_id": payload.get("officer_id"),
            "vehicle_id": payload.get("vehicle_id"),
            "routing_type": payload.get("routing_type"),
            "origin_lat": payload.get("origin_lat"),
            "origin_lng": payload.get("origin_lng"),
            "destination_lat": payload.get("destination_lat"),
            "destination_lng": payload.get("destination_lng"),
            "distance_metres": payload.get("distance_metres"),
            "estimated_time_minutes": payload.get("estimated_time_minutes"),
            "actual_time_minutes": payload.get("actual_time_minutes"),
            "infrastructure_recommendations": json.dumps(payload.get("infrastructure_recommendations", [])),
            "route_geojson": json.dumps(payload.get("route_geojson", {})),
            "pushed_to_device": bool(payload.get("pushed_to_device")),
            "pushed_at": payload.get("pushed_at"),
            "completed_at": payload.get("completed_at"),
            "request_payload": json.dumps(payload.get("request_payload", {})),
            "response_payload": json.dumps(payload.get("response_payload", {})),
        }
        sql = """
        INSERT INTO services.route_history (
            id, route_id, org_id, incident_id, officer_id, vehicle_id, routing_type,
            origin_lat, origin_lng, destination_lat, destination_lng, distance_metres,
            estimated_time_minutes, actual_time_minutes, infrastructure_recommendations,
            route_geojson, pushed_to_device, pushed_at, completed_at, request_payload, response_payload
        ) VALUES (
            %(id)s, %(route_id)s, %(org_id)s, %(incident_id)s, %(officer_id)s, %(vehicle_id)s, %(routing_type)s,
            %(origin_lat)s, %(origin_lng)s, %(destination_lat)s, %(destination_lng)s, %(distance_metres)s,
            %(estimated_time_minutes)s, %(actual_time_minutes)s, %(infrastructure_recommendations)s::jsonb,
            %(route_geojson)s::jsonb, %(pushed_to_device)s, %(pushed_at)s, %(completed_at)s, %(request_payload)s::jsonb, %(response_payload)s::jsonb
        )
        ON CONFLICT(route_id) DO UPDATE SET
            org_id = EXCLUDED.org_id,
            incident_id = EXCLUDED.incident_id,
            officer_id = EXCLUDED.officer_id,
            vehicle_id = EXCLUDED.vehicle_id,
            routing_type = EXCLUDED.routing_type,
            origin_lat = EXCLUDED.origin_lat,
            origin_lng = EXCLUDED.origin_lng,
            destination_lat = EXCLUDED.destination_lat,
            destination_lng = EXCLUDED.destination_lng,
            distance_metres = EXCLUDED.distance_metres,
            estimated_time_minutes = EXCLUDED.estimated_time_minutes,
            actual_time_minutes = EXCLUDED.actual_time_minutes,
            infrastructure_recommendations = EXCLUDED.infrastructure_recommendations,
            route_geojson = EXCLUDED.route_geojson,
            pushed_to_device = EXCLUDED.pushed_to_device,
            pushed_at = EXCLUDED.pushed_at,
            completed_at = EXCLUDED.completed_at,
            request_payload = EXCLUDED.request_payload,
            response_payload = EXCLUDED.response_payload
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, row)
            conn.commit()
        return self.get_route(payload["route_id"]) or row

    def update_route_push(self, route_id: str, pushed_to_device: bool, pushed_at: str | None) -> None:
        self._execute(
            """
            UPDATE services.route_history
            SET pushed_to_device = %s, pushed_at = %s
            WHERE route_id = %s
            """,
            (pushed_to_device, pushed_at, route_id),
        )

    def get_route(self, route_id: str) -> dict[str, Any] | None:
        return self._fetchone("SELECT * FROM services.route_history WHERE route_id = %s", (route_id,))

    def list_routes(self, org_id: str | None = None) -> list[dict[str, Any]]:
        if org_id:
            return self._fetchall("SELECT * FROM services.route_history WHERE org_id = %s ORDER BY created_at DESC", (org_id,))
        return self._fetchall("SELECT * FROM services.route_history ORDER BY created_at DESC")

    def log_action(self, payload: dict[str, Any]) -> None:
        row = {
            "id": str(uuid.uuid4()),
            "device_id": payload["device_id"],
            "org_id": _stable_uuid(payload["org_id"]),
            "incident_id": _stable_uuid(payload.get("incident_id")) if payload.get("incident_id") else None,
            "route_id": payload.get("route_id"),
            "recommended_action": payload.get("recommended_action"),
            "approved": bool(payload.get("approved")),
            "approved_by": payload.get("approved_by"),
            "approved_at": payload.get("approved_at"),
            "executed": bool(payload.get("executed")),
            "executed_at": payload.get("executed_at"),
            "reverted_at": payload.get("reverted_at"),
            "execution_result": json.dumps(payload.get("execution_result", {})),
        }
        self._execute(
            """
            INSERT INTO services.infrastructure_action_log (
                id, device_id, org_id, incident_id, route_id, recommended_action, approved,
                approved_by, approved_at, executed, executed_at, reverted_at, execution_result
            ) VALUES (
                %(id)s, %(device_id)s, %(org_id)s, %(incident_id)s, %(route_id)s, %(recommended_action)s, %(approved)s,
                %(approved_by)s, %(approved_at)s, %(executed)s, %(executed_at)s, %(reverted_at)s, %(execution_result)s::jsonb
            )
            """,
            row,  # type: ignore[arg-type]
        )


def create_store(database_url: str | None, database_path: Path) -> SQLiteStore | PostgresStore:
    if database_url:
        try:
            return PostgresStore(database_url)
        except Exception:
            pass
    return SQLiteStore(database_path)
