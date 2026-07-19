from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict

try:
    import psycopg2
    import psycopg2.extras
except Exception:  # pragma: no cover - optional dependency in local dev
    psycopg2 = None


APP_STARTED_AT = datetime.now(timezone.utc)
DEFAULT_ORG_ID = "org_abc123"
RELATIONSHIP_API_URL = os.getenv("RELATIONSHIP_API_URL", "").rstrip("/")
RELATIONSHIP_API_KEY = os.getenv("RELATIONSHIP_API_KEY", "")
VALHALLA_URL = os.getenv("VALHALLA_URL", "").rstrip("/")
RADAR_SECRET_KEY = os.getenv("RADAR_SECRET_KEY", "")
RADAR_PUBLISHABLE_KEY = os.getenv("RADAR_PUBLISHABLE_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "").strip()
if ENVIRONMENT == "production" and not INTERNAL_API_KEY:
    raise RuntimeError("INTERNAL_API_KEY is required in production.")
if not INTERNAL_API_KEY:
    INTERNAL_API_KEY = "dev-internal-key"
INFRASTRUCTURE_CORRIDOR_METRES = float(os.getenv("INFRASTRUCTURE_CORRIDOR_METRES", "100"))
MAX_OFFICERS_PER_ROUTE_QUERY = int(os.getenv("MAX_OFFICERS_PER_ROUTE_QUERY", "20"))
VALHALLA_TIMEOUT_SECONDS = float(os.getenv("VALHALLA_TIMEOUT_SECONDS", "5"))
RADAR_TIMEOUT_SECONDS = float(os.getenv("RADAR_TIMEOUT_SECONDS", "3"))

app = FastAPI(title="Lemtik Route Calculator", version="1.0")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def random_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def deep_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def haversine_metres(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    to_radians = math.radians
    earth_radius_metres = 6_371_000
    delta_lat = to_radians(lat2 - lat1)
    delta_lng = to_radians(lng2 - lng1)
    a = math.sin(delta_lat / 2) ** 2 + math.cos(to_radians(lat1)) * math.cos(to_radians(lat2)) * math.sin(delta_lng / 2) ** 2
    return 2 * earth_radius_metres * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def interpolate_point(start: dict[str, float], end: dict[str, float], fraction: float) -> dict[str, float]:
    fraction = clamp(fraction, 0.0, 1.0)
    return {
        "lat": start["lat"] + (end["lat"] - start["lat"]) * fraction,
        "lng": start["lng"] + (end["lng"] - start["lng"]) * fraction,
    }


def route_segments(points: list[dict[str, float]]) -> list[tuple[dict[str, float], dict[str, float]]]:
    return list(zip(points, points[1:]))


def point_to_segment_distance_metres(
    point: dict[str, float],
    start: dict[str, float],
    end: dict[str, float],
) -> float:
    # Approximate the local geometry in meters. This is sufficient for short Lagos routes.
    ref_lat = math.radians((start["lat"] + end["lat"] + point["lat"]) / 3)
    metres_per_lat = 111_320
    metres_per_lng = 111_320 * math.cos(ref_lat)

    px = point["lng"] * metres_per_lng
    py = point["lat"] * metres_per_lat
    sx = start["lng"] * metres_per_lng
    sy = start["lat"] * metres_per_lat
    ex = end["lng"] * metres_per_lng
    ey = end["lat"] * metres_per_lat

    dx = ex - sx
    dy = ey - sy
    if dx == 0 and dy == 0:
        return math.hypot(px - sx, py - sy)

    t = ((px - sx) * dx + (py - sy) * dy) / (dx * dx + dy * dy)
    t = clamp(t, 0.0, 1.0)
    proj_x = sx + t * dx
    proj_y = sy + t * dy
    return math.hypot(px - proj_x, py - proj_y)


def duration_minutes(distance_metres: float, speed_kmh: float) -> float:
    if distance_metres <= 0:
        return 0.0
    metres_per_minute = speed_kmh * 1000 / 60
    return distance_metres / metres_per_minute


def format_eta(minutes: float) -> str:
    total_seconds = max(0, round(minutes * 60))
    mins, secs = divmod(total_seconds, 60)
    if mins == 0:
        return f"{secs} sec"
    return f"{mins} min {secs} sec"


def pseudo_position(identifier: str) -> dict[str, float]:
    digest = hashlib.sha256(identifier.encode("utf-8")).digest()
    lat_offset = (int.from_bytes(digest[:4], "big") / 2**32 - 0.5) * 0.08
    lng_offset = (int.from_bytes(digest[4:8], "big") / 2**32 - 0.5) * 0.12
    return {
        "lat": 6.45 + lat_offset,
        "lng": 3.40 + lng_offset,
    }


def route_geometry(points: list[dict[str, float]]) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "service": "routecalculator",
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[p["lng"], p["lat"]] for p in points],
                },
            }
        ],
    }


def route_turns(route_type: str, incident: dict[str, Any]) -> list[str]:
    description = str((incident.get("location") or {}).get("description") or "incident location")
    building = str(incident.get("building_id") or (incident.get("location") or {}).get("building_id") or "")
    if route_type == "foot":
        turns = ["Head toward the incident via the fastest pedestrian path"]
        if incident.get("indoor"):
            turns.extend(
                [
                    f"Enter {building or description}",
                    "Use the most direct access route to the incident floor",
                    f"Proceed to {description}",
                ]
            )
        else:
            turns.extend(
                [
                    "Use alleys, footpaths, and pedestrian shortcuts where available",
                    f"Approach {description}",
                ]
            )
        return turns

    if route_type == "vehicle":
        return [
            "Leave current position via the fastest drivable road",
            "Follow the quickest traffic corridor toward the incident",
            f"Stop at the closest safe drop-off point for {description}",
        ]

    return [
        "Drive to the optimal drop-off point",
        "Transfer to foot response",
        "Proceed on foot to the incident location",
    ]


class Location(BaseModel):
    model_config = ConfigDict(extra="allow")

    lat: float
    lng: float
    description: str | None = None
    name: str | None = None
    address: str | None = None
    building_id: str | None = None
    floor: int | None = None
    indoor: bool | None = None


class Incident(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    location: Location
    type: str | None = None
    indoor: bool | None = None
    building_id: str | None = None


class Responders(BaseModel):
    model_config = ConfigDict(extra="allow")

    officers: list[str] = Field(default_factory=list)
    vehicles: list[str] = Field(default_factory=list)


class RoutingPreferences(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str | None = None
    prioritise: str | None = None


class RouteCalculateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    request_type: str
    request_id: str
    org_id: str = DEFAULT_ORG_ID
    incident: Incident
    responders: Responders = Field(default_factory=Responders)
    routing_preferences: RoutingPreferences = Field(default_factory=RoutingPreferences)


class RoutePushRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    route_id: str
    officer_ids: list[str] = Field(default_factory=list)
    org_id: str | None = None
    request_id: str | None = None


class InfrastructureRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str | None = None
    org_id: str | None = None
    name: str | None = None
    type: str | None = None
    device_type: str | None = None
    status: str | None = None
    supported_actions: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    lat: float | None = None
    lng: float | None = None
    floor: int | None = None
    building_id: str | None = None
    description: str | None = None
    operational: bool | None = True


@dataclass
class DevicePosition:
    lat: float
    lng: float


DEFAULT_DEVICE_SEED: list[dict[str, Any]] = [
    {
        "device_id": "DEV-ELEV-001",
        "type": "SMART_ELEVATOR",
        "name": "Main Lobby Elevator",
        "lat": 6.42810,
        "lng": 3.42190,
        "floor": 0,
        "building_id": "BLDG-HOTEL-001",
        "description": "Primary guest elevator",
        "capabilities": ["hold_floor", "send_to_floor", "reserve_for_officers", "normal"],
        "default_state": "normal",
    },
    {
        "device_id": "DEV-DOOR-EXIT-001",
        "type": "SMART_DOOR",
        "name": "Hotel North Exit",
        "lat": 6.42820,
        "lng": 3.42170,
        "floor": 0,
        "building_id": "BLDG-HOTEL-001",
        "description": "Controlled northern exit",
        "capabilities": ["unlock", "lock", "hold_open", "hold_closed"],
        "default_state": "lock",
    },
    {
        "device_id": "DEV-TL-001",
        "type": "TRAFFIC_LIGHT",
        "name": "North Road Junction",
        "lat": 6.43020,
        "lng": 3.42310,
        "description": "Signal on the main approach road",
        "capabilities": ["green_corridor", "red_cross_traffic", "normal"],
        "default_state": "normal",
    },
    {
        "device_id": "DEV-BARRIER-001",
        "type": "BOOM_BARRIER",
        "name": "Service Drive Barrier",
        "lat": 6.42780,
        "lng": 3.42080,
        "building_id": "BLDG-HOTEL-001",
        "description": "Controlled access barrier",
        "capabilities": ["raise", "lower", "lock_raised", "lock_lowered"],
        "default_state": "lower",
    },
    {
        "device_id": "DEV-LOCK-001",
        "type": "SMART_LOCK",
        "name": "West Wing Access Door",
        "lat": 6.42835,
        "lng": 3.42235,
        "floor": 3,
        "building_id": "BLDG-HOTEL-001",
        "description": "Interior access control",
        "capabilities": ["unlock", "lock", "hold_unlocked", "hold_locked"],
        "default_state": "lock",
    },
]


ACTION_LIBRARY: dict[str, dict[str, dict[str, Any]]] = {
    "TRAFFIC_LIGHT": {
        "green_corridor": {
            "description": "Set the light sequence to clear the response corridor.",
            "time_saved_seconds": 18,
            "priority": "high",
            "requires_approval": True,
            "approval_level": "supervisor",
            "auto_revert_after_seconds": 180,
        },
        "red_cross_traffic": {
            "description": "Hold conflicting traffic at red to open a safe crossing window.",
            "time_saved_seconds": 12,
            "priority": "critical",
            "requires_approval": True,
            "approval_level": "supervisor",
            "auto_revert_after_seconds": 180,
        },
    },
    "SMART_GATE": {
        "open": {
            "description": "Open the gate for responder access.",
            "time_saved_seconds": 10,
            "priority": "high",
            "requires_approval": True,
            "approval_level": "manager",
            "auto_revert_after_seconds": 300,
        },
        "hold_open": {
            "description": "Keep the gate open while officers pass through.",
            "time_saved_seconds": 8,
            "priority": "high",
            "requires_approval": True,
            "approval_level": "manager",
            "auto_revert_after_seconds": 300,
        },
        "unlock": {
            "description": "Unlock the gate for responder entry.",
            "time_saved_seconds": 8,
            "priority": "high",
            "requires_approval": True,
            "approval_level": "supervisor",
            "auto_revert_after_seconds": 300,
        },
    },
    "SMART_DOOR": {
        "unlock": {
            "description": "Unlock the access door for responders.",
            "time_saved_seconds": 6,
            "priority": "high",
            "requires_approval": True,
            "approval_level": "supervisor",
            "auto_revert_after_seconds": 180,
        },
        "hold_open": {
            "description": "Keep the door open while officers move through.",
            "time_saved_seconds": 5,
            "priority": "medium",
            "requires_approval": True,
            "approval_level": "manager",
            "auto_revert_after_seconds": 180,
        },
        "hold_closed": {
            "description": "Hold the door closed to limit suspect movement.",
            "time_saved_seconds": 0,
            "priority": "critical",
            "requires_approval": True,
            "approval_level": "supervisor",
            "auto_revert_after_seconds": 600,
        },
        "lock": {
            "description": "Lock the door to prevent unauthorized access.",
            "time_saved_seconds": 0,
            "priority": "critical",
            "requires_approval": True,
            "approval_level": "supervisor",
            "auto_revert_after_seconds": 600,
        },
    },
    "SMART_ELEVATOR": {
        "reserve_for_officers": {
            "description": "Reserve the elevator for officers and send it to the required floor.",
            "time_saved_seconds": 24,
            "priority": "high",
            "requires_approval": True,
            "approval_level": "supervisor",
            "auto_revert_after_seconds": 180,
        },
        "hold_floor": {
            "description": "Hold the elevator at the selected floor for responder use.",
            "time_saved_seconds": 12,
            "priority": "medium",
            "requires_approval": True,
            "approval_level": "manager",
            "auto_revert_after_seconds": 180,
        },
        "send_to_floor": {
            "description": "Dispatch the elevator directly to the responder floor.",
            "time_saved_seconds": 12,
            "priority": "high",
            "requires_approval": True,
            "approval_level": "supervisor",
            "auto_revert_after_seconds": 180,
        },
        "normal": {
            "description": "Leave the elevator in normal operation.",
            "time_saved_seconds": 0,
            "priority": "low",
            "requires_approval": False,
            "approval_level": "none",
            "auto_revert_after_seconds": 0,
        },
    },
    "SMART_ESCALATOR": {
        "stop": {
            "description": "Stop the escalator for controlled movement.",
            "time_saved_seconds": 5,
            "priority": "medium",
            "requires_approval": True,
            "approval_level": "manager",
            "auto_revert_after_seconds": 120,
        },
        "reverse_direction": {
            "description": "Reverse escalator direction to improve access.",
            "time_saved_seconds": 8,
            "priority": "high",
            "requires_approval": True,
            "approval_level": "supervisor",
            "auto_revert_after_seconds": 120,
        },
    },
    "BOOM_BARRIER": {
        "raise": {
            "description": "Raise the barrier for responder access.",
            "time_saved_seconds": 12,
            "priority": "high",
            "requires_approval": True,
            "approval_level": "supervisor",
            "auto_revert_after_seconds": 180,
        },
        "lower": {
            "description": "Lower the barrier to restrict movement.",
            "time_saved_seconds": 0,
            "priority": "critical",
            "requires_approval": True,
            "approval_level": "supervisor",
            "auto_revert_after_seconds": 300,
        },
        "lock_raised": {
            "description": "Lock the barrier in the raised position.",
            "time_saved_seconds": 10,
            "priority": "high",
            "requires_approval": True,
            "approval_level": "manager",
            "auto_revert_after_seconds": 300,
        },
        "lock_lowered": {
            "description": "Lock the barrier in the lowered position.",
            "time_saved_seconds": 0,
            "priority": "critical",
            "requires_approval": True,
            "approval_level": "supervisor",
            "auto_revert_after_seconds": 300,
        },
    },
    "SMART_LOCK": {
        "unlock": {
            "description": "Unlock the smart lock for responder access.",
            "time_saved_seconds": 4,
            "priority": "medium",
            "requires_approval": True,
            "approval_level": "manager",
            "auto_revert_after_seconds": 180,
        },
        "lock": {
            "description": "Lock the smart lock to deny access.",
            "time_saved_seconds": 0,
            "priority": "critical",
            "requires_approval": True,
            "approval_level": "supervisor",
            "auto_revert_after_seconds": 300,
        },
        "hold_unlocked": {
            "description": "Keep the lock open while responders move through.",
            "time_saved_seconds": 4,
            "priority": "medium",
            "requires_approval": True,
            "approval_level": "manager",
            "auto_revert_after_seconds": 180,
        },
        "hold_locked": {
            "description": "Hold the lock in the secured state.",
            "time_saved_seconds": 0,
            "priority": "critical",
            "requires_approval": True,
            "approval_level": "supervisor",
            "auto_revert_after_seconds": 300,
        },
    },
    "CCTV_CAMERA": {
        "alert_mode": {
            "description": "Switch the camera to alert mode and focus on the route.",
            "time_saved_seconds": 0,
            "priority": "medium",
            "requires_approval": False,
            "approval_level": "none",
            "auto_revert_after_seconds": 300,
        },
        "pan_to_location": {
            "description": "Pan the camera toward the incident route.",
            "time_saved_seconds": 0,
            "priority": "medium",
            "requires_approval": False,
            "approval_level": "none",
            "auto_revert_after_seconds": 300,
        },
        "begin_tracking": {
            "description": "Begin tracking activity near the route.",
            "time_saved_seconds": 0,
            "priority": "medium",
            "requires_approval": False,
            "approval_level": "none",
            "auto_revert_after_seconds": 300,
        },
    },
    "TURNSTILE": {
        "unlock": {
            "description": "Unlock the turnstile for officer passage.",
            "time_saved_seconds": 5,
            "priority": "medium",
            "requires_approval": True,
            "approval_level": "manager",
            "auto_revert_after_seconds": 120,
        },
        "hold_open": {
            "description": "Keep the turnstile open temporarily.",
            "time_saved_seconds": 4,
            "priority": "medium",
            "requires_approval": True,
            "approval_level": "manager",
            "auto_revert_after_seconds": 120,
        },
        "lock": {
            "description": "Lock the turnstile to control movement.",
            "time_saved_seconds": 0,
            "priority": "critical",
            "requires_approval": True,
            "approval_level": "supervisor",
            "auto_revert_after_seconds": 300,
        },
    },
    "AUTOMATED_TOLL": {
        "open_lane": {
            "description": "Open a toll lane for the response vehicle.",
            "time_saved_seconds": 10,
            "priority": "high",
            "requires_approval": True,
            "approval_level": "manager",
            "auto_revert_after_seconds": 180,
        },
        "priority_lane": {
            "description": "Route the vehicle through the priority toll lane.",
            "time_saved_seconds": 12,
            "priority": "high",
            "requires_approval": True,
            "approval_level": "manager",
            "auto_revert_after_seconds": 180,
        },
    },
    "SMART_LIGHTING": {
        "full_brightness": {
            "description": "Raise lighting to full brightness on the route.",
            "time_saved_seconds": 0,
            "priority": "low",
            "requires_approval": False,
            "approval_level": "none",
            "auto_revert_after_seconds": 0,
        },
        "emergency_mode": {
            "description": "Switch lighting to emergency mode for safer movement.",
            "time_saved_seconds": 2,
            "priority": "low",
            "requires_approval": False,
            "approval_level": "none",
            "auto_revert_after_seconds": 300,
        },
        "strobe": {
            "description": "Activate strobe lighting for incident signalling.",
            "time_saved_seconds": 0,
            "priority": "medium",
            "requires_approval": True,
            "approval_level": "supervisor",
            "auto_revert_after_seconds": 180,
        },
    },
}


def route_type_candidates(request: RouteCalculateRequest) -> list[str]:
    preferred = (request.routing_preferences.type or "").strip().lower()
    indoor = bool(request.incident.indoor if request.incident.indoor is not None else request.incident.location.indoor)
    has_vehicle = bool(request.responders.vehicles)
    if preferred in {"foot", "vehicle", "hybrid"}:
        if preferred == "hybrid" and indoor and has_vehicle:
            return ["hybrid", "foot", "vehicle"]
        return [preferred]
    if indoor:
        return ["foot", "hybrid"] if has_vehicle else ["foot"]
    return ["hybrid", "vehicle", "foot"] if has_vehicle else ["foot"]


def closest_candidates(
    incident: dict[str, Any],
    responders: list[str],
    limit: int,
) -> list[tuple[str, float, dict[str, float]]]:
    location = incident["location"]
    result: list[tuple[str, float, dict[str, float]]] = []
    for responder_id in responders[: max(1, limit)]:
        pos = pseudo_position(responder_id)
        distance = haversine_metres(location["lat"], location["lng"], pos["lat"], pos["lng"])
        result.append((responder_id, distance, pos))
    return sorted(result, key=lambda item: item[1])


def route_points(route_type: str, origin: dict[str, float], destination: dict[str, float]) -> list[dict[str, float]]:
    if route_type == "hybrid":
        dropoff = interpolate_point(origin, destination, 0.82 if haversine_metres(origin["lat"], origin["lng"], destination["lat"], destination["lng"]) > 250 else 0.9)
        return [origin, dropoff, destination]
    return [origin, destination]


def first_last_coordinates(points: list[dict[str, float]]) -> tuple[float | None, float | None, float | None, float | None]:
    if not points:
        return None, None, None, None
    first = points[0]
    last = points[-1]
    return first["lat"], first["lng"], last["lat"], last["lng"]


def leg_speed_kmh(route_type: str, distance_metres: float, indoor: bool) -> float:
    if route_type == "foot":
        return 15.0 if indoor or distance_metres <= 200 else 5.0
    if route_type == "vehicle":
        return 32.0 if distance_metres <= 2500 else 40.0
    return 20.0


def leg_distance(route_type: str, origin: dict[str, float], destination: dict[str, float]) -> float:
    straight = haversine_metres(origin["lat"], origin["lng"], destination["lat"], destination["lng"])
    if route_type == "foot":
        return straight * 1.08
    if route_type == "vehicle":
        return straight * 1.28
    # Hybrid is handled as two legs.
    return straight


def route_action_priority(device_type: str, action_key: str) -> str:
    return str(ACTION_LIBRARY.get(device_type, {}).get(action_key, {}).get("priority", "medium"))


def choose_action(device_type: str, capabilities: list[str], route_type: str) -> str | None:
    if not capabilities:
        capabilities = []
    candidates: dict[str, list[str]] = {
        "TRAFFIC_LIGHT": ["green_corridor", "red_cross_traffic", "normal"],
        "SMART_GATE": ["hold_open", "open", "unlock"],
        "SMART_DOOR": ["unlock", "hold_open", "hold_closed", "lock"],
        "SMART_ELEVATOR": ["reserve_for_officers", "send_to_floor", "hold_floor", "normal"],
        "SMART_ESCALATOR": ["stop", "reverse_direction"],
        "BOOM_BARRIER": ["raise", "lock_raised", "lower", "lock_lowered"],
        "SMART_LOCK": ["unlock", "hold_unlocked", "hold_locked", "lock"],
        "CCTV_CAMERA": ["alert_mode", "begin_tracking", "pan_to_location"],
        "TURNSTILE": ["unlock", "hold_open", "lock"],
        "AUTOMATED_TOLL": ["priority_lane", "open_lane"],
        "SMART_LIGHTING": ["emergency_mode", "full_brightness", "strobe"],
    }
    for candidate in candidates.get(device_type, []):
        if candidate in capabilities or not capabilities:
            return candidate
    if device_type == "SMART_ELEVATOR" and route_type == "foot":
        return "reserve_for_officers"
    return None


def compatible_with_route(device_type: str, route_type: str, indoor: bool) -> bool:
    vehicle_types = {"TRAFFIC_LIGHT", "SMART_GATE", "BOOM_BARRIER", "AUTOMATED_TOLL", "SMART_LIGHTING", "CCTV_CAMERA"}
    foot_types = {"SMART_DOOR", "SMART_ELEVATOR", "SMART_ESCALATOR", "SMART_LOCK", "TURNSTILE", "SMART_LIGHTING", "CCTV_CAMERA"}
    if route_type == "vehicle":
        return device_type in vehicle_types or device_type == "SMART_LIGHTING" or device_type == "CCTV_CAMERA"
    if route_type == "foot":
        return device_type in foot_types or (indoor and device_type in vehicle_types and device_type in {"SMART_LIGHTING", "CCTV_CAMERA"})
    return True


def route_compatibility_leg(route_type: str, device_type: str) -> str:
    if route_type == "vehicle" and device_type in {"TRAFFIC_LIGHT", "SMART_GATE", "BOOM_BARRIER", "AUTOMATED_TOLL"}:
        return "vehicle"
    if route_type == "foot" and device_type in {"SMART_DOOR", "SMART_ELEVATOR", "SMART_ESCALATOR", "SMART_LOCK", "TURNSTILE"}:
        return "foot"
    return "both"


def choose_reasoning(request: RouteCalculateRequest, route_type: str, routes: list[dict[str, Any]]) -> str:
    incident = request.incident
    location_desc = incident.location.description or incident.location.name or "incident location"
    if incident.indoor or request.incident.indoor:
        if route_type == "foot":
            if routes:
                best = routes[0]
                return f"Incident is indoor at {location_desc}. The closest officer is {best.get('officer_id')}, so foot response is fastest."
            return f"Incident is indoor at {location_desc}, so officers should approach on foot."
        return f"Incident is indoor at {location_desc}. A hybrid response is safer because the vehicle can stage at the nearest drop-off point."
    if route_type == "vehicle":
        return f"Outdoor incident at {location_desc}. Vehicle response gives the lowest ETA."
    if route_type == "hybrid":
        return f"Outdoor incident at {location_desc}. A hybrid response balances speed and final approach distance."
    return f"Incident at {location_desc}. Foot response is optimal based on responder availability."


def generate_route_id(request_id: str, leg: str | None = None) -> str:
    base = request_id.replace("req_", "ROUTE-").replace("req-", "ROUTE-")
    if base == request_id:
        base = f"ROUTE-{request_id}"
    if leg:
        return f"{base}-{leg.upper()}"
    return base


def device_action_details(device: dict[str, Any], route_type: str) -> dict[str, Any] | None:
    device_type = str(device.get("type") or device.get("device_type") or "").upper()
    capabilities = [str(item) for item in device.get("capabilities") or device.get("supported_actions") or [] if str(item)]
    action_key = choose_action(device_type, capabilities, route_type)
    if not action_key:
        return None
    action = ACTION_LIBRARY.get(device_type, {}).get(action_key)
    if not action:
        return None
    if route_type == "vehicle" and device_type not in {"TRAFFIC_LIGHT", "SMART_GATE", "BOOM_BARRIER", "AUTOMATED_TOLL", "SMART_LIGHTING", "CCTV_CAMERA"}:
        return None
    if route_type == "foot" and device_type not in {"SMART_DOOR", "SMART_ELEVATOR", "SMART_ESCALATOR", "SMART_LOCK", "TURNSTILE", "SMART_LIGHTING", "CCTV_CAMERA"}:
        return None
    return {
        "recommended_action": action_key,
        "action_description": action["description"],
        "estimated_time_saved_seconds": action["time_saved_seconds"],
        "priority": action["priority"],
        "confidence": 0.92,
        "requires_approval": action["requires_approval"],
        "approval_level": action["approval_level"],
        "auto_revert_after_seconds": action["auto_revert_after_seconds"],
    }


def select_route_devices(
    devices: list[dict[str, Any]],
    points: list[dict[str, float]],
    route_type: str,
    indoor: bool,
) -> list[tuple[dict[str, Any], dict[str, Any], float]]:
    selected: list[tuple[dict[str, Any], dict[str, Any], float]] = []
    segments = route_segments(points)
    for device in devices:
        if not bool(device.get("operational", True)):
            continue
        device_type = str(device.get("type") or "").upper()
        if not compatible_with_route(device_type, route_type, indoor):
            continue
        device_point = {
            "lat": as_float(device.get("lat"), 0.0),
            "lng": as_float(device.get("lng"), 0.0),
        }
        if device_point["lat"] == 0.0 and device_point["lng"] == 0.0:
            continue
        nearest = min(
            (point_to_segment_distance_metres(device_point, start, end) for start, end in segments),
            default=float("inf"),
        )
        if nearest <= INFRASTRUCTURE_CORRIDOR_METRES:
            selected.append((device, device_point, nearest))
    return sorted(selected, key=lambda item: (item[2], item[0].get("type", ""), item[0].get("name", "")))


def build_route_response(
    request: RouteCalculateRequest,
    devices: list[dict[str, Any]],
) -> dict[str, Any]:
    started = time.perf_counter()
    incident = request.incident.model_dump()
    incident_location = incident["location"]
    indoor = bool(incident.get("indoor") if incident.get("indoor") is not None else incident_location.get("indoor"))
    route_type = next(iter(route_type_candidates(request)))

    officer_candidates = closest_candidates(incident, request.responders.officers, MAX_OFFICERS_PER_ROUTE_QUERY)
    vehicle_candidates = closest_candidates(incident, request.responders.vehicles, MAX_OFFICERS_PER_ROUTE_QUERY)
    push_route_to_officers = [item[0] for item in officer_candidates[: max(1, min(2, len(officer_candidates)))]] or request.responders.officers[:1]

    routes: list[dict[str, Any]] = []
    map_features: list[dict[str, Any]] = []

    def build_leg(leg_type: str, source_id: str | None, source_pos: dict[str, float], target_pos: dict[str, float]) -> dict[str, Any]:
        leg_distance_metres = leg_distance(leg_type, source_pos, target_pos)
        speed = leg_speed_kmh(leg_type, leg_distance_metres, indoor)
        eta_minutes = duration_minutes(leg_distance_metres, speed)
        route_id = generate_route_id(request.request_id, leg_type if route_type == "hybrid" else None)
        points = route_points(leg_type, source_pos, target_pos)
        leg_devices = select_route_devices(devices, points, leg_type, indoor)
        infra = []
        saved_seconds = 0.0
        for sequence_order, (device, device_point, distance_to_route) in enumerate(leg_devices, start=1):
            details = device_action_details(device, leg_type)
            if not details:
                continue
            priority = details["priority"]
            if priority == "critical":
                priority = "critical"
            elif priority == "high":
                priority = "high"
            elif priority == "medium":
                priority = "medium"
            else:
                priority = "low"
            confidence = clamp(0.84 + (0.08 if device.get("operational", True) else 0.0) - (distance_to_route / max(INFRASTRUCTURE_CORRIDOR_METRES, 1.0)) * 0.08, 0.65, 0.98)
            details.update(
                {
                    "device_id": device["device_id"],
                    "device_type": device["type"],
                    "name": device["name"],
                    "priority": priority,
                    "confidence": round(confidence, 2),
                    "sequence_order": sequence_order,
                }
            )
            details["requires_approval"] = bool(details["requires_approval"])
            details["approval_level"] = str(details["approval_level"])
            details["estimated_time_saved_seconds"] = as_int(details["estimated_time_saved_seconds"])
            infra.append(details)
            saved_seconds += as_int(details["estimated_time_saved_seconds"])

        route_geojson = route_geometry(points)
        map_features.extend(
            {
                "type": "Feature",
                "properties": {"route_id": route_id, "route_type": leg_type, "source_id": source_id},
                "geometry": {"type": "LineString", "coordinates": [[p["lng"], p["lat"]] for p in points]},
            }
            for _ in [0]
        )
        return {
            "route_id": route_id,
            "type": leg_type,
            "officer_id": source_id if leg_type != "vehicle" else (push_route_to_officers[0] if push_route_to_officers else None),
            "vehicle_id": source_id if leg_type == "vehicle" else (request.responders.vehicles[0] if request.responders.vehicles else None),
            "distance_metres": round(leg_distance_metres),
            "estimated_time_minutes": round(eta_minutes, 2),
            "estimated_time_with_infra_minutes": round(max(eta_minutes - saved_seconds / 60.0, 0.1), 2),
            "time_saved_seconds": round(saved_seconds),
            "turn_by_turn": route_turns(leg_type, incident),
            "confidence": round(clamp(0.9 - min(0.12, leg_distance_metres / 50_000), 0.75, 0.98), 2),
            "infrastructure_recommendations": infra,
            "route_geojson": route_geojson,
        }

    if route_type == "hybrid":
        vehicle_source_id = vehicle_candidates[0][0] if vehicle_candidates else (request.responders.vehicles[0] if request.responders.vehicles else None)
        officer_source_id = officer_candidates[0][0] if officer_candidates else (request.responders.officers[0] if request.responders.officers else None)
        vehicle_origin = (vehicle_candidates[0][2] if vehicle_candidates else pseudo_position(vehicle_source_id or "vehicle")) if vehicle_source_id else pseudo_position("vehicle")
        officer_origin = (officer_candidates[0][2] if officer_candidates else pseudo_position(officer_source_id or "officer")) if officer_source_id else pseudo_position("officer")
        dropoff = interpolate_point(vehicle_origin, incident_location, 0.82 if not indoor else 0.9)
        vehicle_leg = build_leg("vehicle", vehicle_source_id, vehicle_origin, dropoff)
        foot_leg = build_leg("foot", officer_source_id, officer_origin, incident_location)
        routes.extend([vehicle_leg, foot_leg])
    elif route_type == "vehicle":
        vehicle_source_id = vehicle_candidates[0][0] if vehicle_candidates else (request.responders.vehicles[0] if request.responders.vehicles else None)
        vehicle_origin = vehicle_candidates[0][2] if vehicle_candidates else pseudo_position(vehicle_source_id or "vehicle")
        vehicle_leg = build_leg("vehicle", vehicle_source_id, vehicle_origin, incident_location)
        routes.append(vehicle_leg)
    else:
        officer_source_id = officer_candidates[0][0] if officer_candidates else (request.responders.officers[0] if request.responders.officers else None)
        officer_origin = officer_candidates[0][2] if officer_candidates else pseudo_position(officer_source_id or "officer")
        foot_leg = build_leg("foot", officer_source_id, officer_origin, incident_location)
        routes.append(foot_leg)

    route_id = routes[0]["route_id"] if routes else generate_route_id(request.request_id)
    all_infra = []
    for route in routes:
        all_infra.extend(route["infrastructure_recommendations"])

    if route_type == "hybrid":
        total_eta = sum(route["estimated_time_minutes"] for route in routes)
        total_saved = sum(route["time_saved_seconds"] for route in routes)
    else:
        total_eta = routes[0]["estimated_time_minutes"] if routes else 0.0
        total_saved = routes[0]["time_saved_seconds"] if routes else 0.0

    result = {
        "route_id": route_id,
        "request_id": request.request_id,
        "org_id": request.org_id,
        "incident_id": request.incident.id,
        "incident": incident,
        "responders": {
            "officers": request.responders.officers,
            "vehicles": request.responders.vehicles,
        },
        "recommended_routing_type": route_type,
        "reasoning": choose_reasoning(request, route_type, routes),
        "routes": routes,
        "infrastructure_recommendations": sorted(
            all_infra,
            key=lambda item: (as_int(item.get("sequence_order"), 99), {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(str(item.get("priority")), 9)),
        ),
        "push_route_to_officers": push_route_to_officers,
        "mapbox_route_geojson": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "route_id": route["route_id"],
                        "route_type": route["type"],
                        "officer_id": route.get("officer_id"),
                        "vehicle_id": route.get("vehicle_id"),
                    },
                    "geometry": route["route_geojson"]["features"][0]["geometry"],
                }
                for route in routes
            ],
        },
        "meta": {
            "valhalla_query_ms": 0,
            "radar_query_ms": 0,
            "infra_query_ms": 0,
            "total_ms": round((time.perf_counter() - started) * 1000),
        },
        "pushed": False,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    return result


class RouteStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.use_db = bool(DATABASE_URL and psycopg2 is not None)
        self.devices: dict[str, list[dict[str, Any]]] = {}
        self.routes: dict[str, dict[str, Any]] = {}
        self.action_logs: list[dict[str, Any]] = []
        if self.use_db:
            try:
                self._init_db()
                self._seed_if_needed()
            except Exception:
                self.use_db = False
                self._seed_memory()
        else:
            self._seed_memory()

    @contextmanager
    def _db_conn(self):
        conn = psycopg2.connect(  # type: ignore[union-attr]
            DATABASE_URL,
            connect_timeout=5,
            sslmode="require",
        )
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        schema = """
        create schema if not exists services;
        create table if not exists services.infrastructure_devices (
            id uuid primary key default gen_random_uuid(),
            org_id text not null,
            device_id varchar(100) unique not null,
            type varchar(100) not null,
            name varchar(255) not null,
            lat decimal(10,8),
            lng decimal(11,8),
            floor integer,
            building_id varchar(100),
            description text,
            connection_protocol varchar(50),
            connection_endpoint text,
            auth_type varchar(50),
            auth_key_reference varchar(100),
            capabilities jsonb default '[]',
            default_state varchar(100),
            safety_constraints jsonb default '{}',
            operational boolean default true,
            last_health_check timestamptz,
            health_status varchar(50) default 'unknown',
            created_at timestamptz default now(),
            updated_at timestamptz default now()
        );
        create table if not exists services.route_history (
            id uuid primary key default gen_random_uuid(),
            route_id varchar(100) unique not null,
            org_id text not null,
            incident_id varchar(100) not null,
            officer_id varchar(100),
            vehicle_id varchar(100),
            routing_type varchar(50),
            origin_lat decimal(10,8),
            origin_lng decimal(11,8),
            destination_lat decimal(10,8),
            destination_lng decimal(11,8),
            distance_metres integer,
            estimated_time_minutes decimal(8,2),
            actual_time_minutes decimal(8,2),
            infrastructure_recommendations jsonb default '[]',
            route_geojson jsonb,
            pushed_to_device boolean default false,
            pushed_at timestamptz,
            completed_at timestamptz,
            created_at timestamptz default now(),
            updated_at timestamptz default now()
        );
        create table if not exists services.infrastructure_action_log (
            id uuid primary key default gen_random_uuid(),
            device_id varchar(100) not null,
            org_id text not null,
            incident_id varchar(100),
            route_id varchar(100),
            recommended_action varchar(100),
            approved boolean default false,
            approved_by text,
            approved_at timestamptz,
            executed boolean default false,
            executed_at timestamptz,
            reverted_at timestamptz,
            execution_result jsonb,
            created_at timestamptz default now()
        );
        """
        with self._db_conn() as conn, conn.cursor() as cur:
            cur.execute(schema)

    def _seed_if_needed(self) -> None:
        with self._db_conn() as conn, conn.cursor() as cur:
            cur.execute("select exists(select 1 from services.infrastructure_devices limit 1)")
            has_rows = bool(cur.fetchone()[0])
        if not has_rows:
            self._seed_db(DEFAULT_ORG_ID)

    def _seed_db(self, org_id: str) -> None:
        with self._db_conn() as conn, conn.cursor() as cur:
            for device in DEFAULT_DEVICE_SEED:
                cur.execute(
                    """
                    insert into services.infrastructure_devices (
                        org_id, device_id, type, name, lat, lng, floor, building_id,
                        description, capabilities, default_state, operational, created_at, updated_at
                    )
                    values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),now())
                    on conflict (device_id) do nothing
                    """,
                    (
                        org_id,
                        device["device_id"],
                        device["type"],
                        device["name"],
                        device.get("lat"),
                        device.get("lng"),
                        device.get("floor"),
                        device.get("building_id"),
                        device.get("description"),
                        json.dumps(device.get("capabilities", [])),
                        device.get("default_state"),
                        True,
                    ),
                )

    def _seed_memory(self) -> None:
        self.devices[DEFAULT_ORG_ID] = [deep_copy(item) | {"org_id": DEFAULT_ORG_ID, "operational": True, "health_status": "healthy"} for item in DEFAULT_DEVICE_SEED]

    def _get_memory_devices(self, org_id: str) -> list[dict[str, Any]]:
        return self.devices.setdefault(org_id, [])

    def list_devices(self, org_id: str) -> list[dict[str, Any]]:
        if self.use_db:
            with self._db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:  # type: ignore[union-attr]
                cur.execute(
                    """
                    select org_id, device_id, type, name, lat, lng, floor, building_id, description,
                           connection_protocol, connection_endpoint, auth_type, auth_key_reference,
                           capabilities, default_state, safety_constraints, operational, last_health_check,
                           health_status, created_at, updated_at
                    from services.infrastructure_devices
                    where org_id = %s
                    order by type, name
                    """,
                    (org_id,),
                )
                rows = [dict(row) for row in cur.fetchall()]
                if rows:
                    return rows
                if org_id != DEFAULT_ORG_ID:
                    cur.execute(
                        """
                        select org_id, device_id, type, name, lat, lng, floor, building_id, description,
                               connection_protocol, connection_endpoint, auth_type, auth_key_reference,
                               capabilities, default_state, safety_constraints, operational, last_health_check,
                               health_status, created_at, updated_at
                        from services.infrastructure_devices
                        where org_id = %s
                        order by type, name
                        """,
                        (DEFAULT_ORG_ID,),
                    )
                    return [dict(row) for row in cur.fetchall()]
                return []
        devices = self._get_memory_devices(org_id)
        if devices:
            return deep_copy(devices)
        if org_id != DEFAULT_ORG_ID:
            return deep_copy(self._get_memory_devices(DEFAULT_ORG_ID))
        return []

    def register_device(self, record: dict[str, Any]) -> dict[str, Any]:
        if self.use_db:
            with self._db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:  # type: ignore[union-attr]
                cur.execute(
                    """
                    insert into services.infrastructure_devices (
                        org_id, device_id, type, name, lat, lng, floor, building_id, description,
                        capabilities, default_state, operational, created_at, updated_at
                    ) values (
                        %(org_id)s, %(device_id)s, %(type)s, %(name)s, %(lat)s, %(lng)s, %(floor)s, %(building_id)s,
                        %(description)s, %(capabilities)s, %(default_state)s, %(operational)s, now(), now()
                    )
                    on conflict (device_id) do update set
                        org_id = excluded.org_id,
                        type = excluded.type,
                        name = excluded.name,
                        lat = excluded.lat,
                        lng = excluded.lng,
                        floor = excluded.floor,
                        building_id = excluded.building_id,
                        description = excluded.description,
                        capabilities = excluded.capabilities,
                        default_state = excluded.default_state,
                        operational = excluded.operational,
                        updated_at = now()
                    returning org_id, device_id, type, name, lat, lng, floor, building_id, description,
                              capabilities, default_state, operational, health_status, created_at, updated_at
                    """,
                    {
                        **record,
                        "capabilities": json.dumps(record.get("capabilities") or []),
                    },
                )
                row = cur.fetchone()
                return dict(row)
        devices = self._get_memory_devices(record["org_id"])
        for index, existing in enumerate(devices):
            if existing["device_id"] == record["device_id"]:
                devices[index] = deep_copy(record)
                return deep_copy(record)
        devices.append(deep_copy(record))
        return deep_copy(record)

    def save_route(self, route: dict[str, Any]) -> dict[str, Any]:
        if self.use_db:
            first_route = route["routes"][0] if route.get("routes") else {}
            last_route = route["routes"][-1] if route.get("routes") else {}
            total_distance = sum(as_int(item.get("distance_metres"), 0) for item in route.get("routes") or [])
            total_eta = sum(as_float(item.get("estimated_time_minutes"), 0.0) for item in route.get("routes") or [])
            first_points = (first_route.get("route_geojson") or {}).get("features", [{}])[0].get("geometry", {}).get("coordinates") or []
            last_points = (last_route.get("route_geojson") or {}).get("features", [{}])[0].get("geometry", {}).get("coordinates") or []
            if first_points:
                origin_lng, origin_lat = first_points[0]
            else:
                origin_lat = route["incident"]["location"]["lat"]
                origin_lng = route["incident"]["location"]["lng"]
            if last_points:
                destination_lng, destination_lat = last_points[-1]
            else:
                destination_lat = route["incident"]["location"]["lat"]
                destination_lng = route["incident"]["location"]["lng"]
            with self._db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:  # type: ignore[union-attr]
                cur.execute(
                    """
                    insert into services.route_history (
                        route_id, org_id, incident_id, officer_id, vehicle_id, routing_type,
                        origin_lat, origin_lng, destination_lat, destination_lng, distance_metres,
                        estimated_time_minutes, infrastructure_recommendations, route_geojson,
                        pushed_to_device, created_at, updated_at
                    ) values (
                        %(route_id)s, %(org_id)s, %(incident_id)s, %(officer_id)s, %(vehicle_id)s, %(routing_type)s,
                        %(origin_lat)s, %(origin_lng)s, %(destination_lat)s, %(destination_lng)s, %(distance_metres)s,
                        %(estimated_time_minutes)s, %(infrastructure_recommendations)s, %(route_geojson)s,
                        %(pushed_to_device)s, now(), now()
                    )
                    on conflict (route_id) do update set
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
                        infrastructure_recommendations = excluded.infrastructure_recommendations,
                        route_geojson = excluded.route_geojson,
                        pushed_to_device = excluded.pushed_to_device,
                        updated_at = now()
                    returning *
                    """,
                    {
                        "route_id": route["route_id"],
                        "org_id": route["org_id"],
                        "incident_id": route["incident_id"],
                        "officer_id": first_route.get("officer_id"),
                        "vehicle_id": first_route.get("vehicle_id"),
                        "routing_type": route["recommended_routing_type"],
                        "origin_lat": origin_lat,
                        "origin_lng": origin_lng,
                        "destination_lat": destination_lat,
                        "destination_lng": destination_lng,
                        "distance_metres": total_distance or first_route.get("distance_metres"),
                        "estimated_time_minutes": total_eta or first_route.get("estimated_time_minutes"),
                        "infrastructure_recommendations": json.dumps(route.get("infrastructure_recommendations") or []),
                        "route_geojson": json.dumps(route.get("mapbox_route_geojson") or {}),
                        "pushed_to_device": bool(route.get("pushed", False)),
                    },
                )
                row = dict(cur.fetchone())
                self._write_action_logs(route)
                merged = self._merge_route_record(route, row)
                self.routes[route["route_id"]] = deep_copy(merged)
                return merged
        self.routes[route["route_id"]] = deep_copy(route)
        self._write_action_logs(route)
        return deep_copy(route)

    def _merge_route_record(self, route: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
        merged = deep_copy(route)
        merged.update(
            {
                "pushed": bool(row.get("pushed_to_device")),
                "updated_at": row.get("updated_at") or merged.get("updated_at"),
                "created_at": row.get("created_at") or merged.get("created_at"),
            }
        )
        return merged

    def _write_action_logs(self, route: dict[str, Any]) -> None:
        for recommendation in route.get("infrastructure_recommendations") or []:
            entry = {
                "id": random_id("log"),
                "device_id": recommendation.get("device_id"),
                "org_id": route["org_id"],
                "incident_id": route["incident_id"],
                "route_id": route["route_id"],
                "recommended_action": recommendation.get("recommended_action"),
                "approved": False,
                "executed": False,
                "created_at": now_iso(),
            }
            if self.use_db:
                with self._db_conn() as conn, conn.cursor() as cur:  # type: ignore[union-attr]
                    cur.execute(
                        """
                        insert into services.infrastructure_action_log (
                            device_id, org_id, incident_id, route_id, recommended_action, approved,
                            executed, created_at
                        ) values (%s,%s,%s,%s,%s,%s,%s,now())
                        """,
                        (
                            entry["device_id"],
                            entry["org_id"],
                            entry["incident_id"],
                            entry["route_id"],
                            entry["recommended_action"],
                            False,
                            False,
                        ),
                    )
            else:
                self.action_logs.append(entry)

    def get_route(self, route_id: str) -> dict[str, Any] | None:
        cached = self.routes.get(route_id)
        if cached:
            return deep_copy(cached)
        if self.use_db:
            with self._db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:  # type: ignore[union-attr]
                cur.execute("select * from services.route_history where route_id = %s", (route_id,))
                row = cur.fetchone()
                if not row:
                    return None
                return dict(row)
        route = self.routes.get(route_id)
        return deep_copy(route) if route else None

    def mark_pushed(self, route_id: str, officer_ids: list[str]) -> dict[str, Any] | None:
        route = self.get_route(route_id)
        if not route:
            return None
        route["pushed"] = True
        route["pushed_at"] = now_iso()
        route["updated_at"] = now_iso()
        if officer_ids:
            route["push_route_to_officers"] = officer_ids
            for item in route.get("routes") or []:
                if item.get("type") == "foot" or item.get("type") == "vehicle":
                    item["officer_id"] = officer_ids[0]
        if self.use_db:
            with self._db_conn() as conn, conn.cursor() as cur:  # type: ignore[union-attr]
                cur.execute(
                    """
                    update services.route_history
                    set pushed_to_device = true, pushed_at = now(), updated_at = now()
                    where route_id = %s
                    """,
                    (route_id,),
                )
            self.routes[route_id] = deep_copy(route)
        else:
            self.routes[route_id] = deep_copy(route)
        return route


store = RouteStore()


def validate_internal_key(authorization: str | None = Header(default=None), x_internal_key: str | None = Header(default=None)) -> None:
    if ENVIRONMENT.lower() == "development":
        return
    bearer = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization.split(" ", 1)[1].strip()
    if bearer == INTERNAL_API_KEY or (x_internal_key and x_internal_key == INTERNAL_API_KEY):
        return
    raise HTTPException(status_code=401, detail="Invalid internal credentials")


def api_org(request: Request, body: dict[str, Any] | None = None) -> str:
    body = body or {}
    query_org = request.query_params.get("org_id")
    header_org = request.headers.get("x-org-id")
    return str(body.get("org_id") or query_org or header_org or DEFAULT_ORG_ID)


async def valhalla_health() -> dict[str, Any]:
    if not VALHALLA_URL:
        return {"status": "unconfigured", "available": False}
    try:
        async with httpx.AsyncClient(timeout=VALHALLA_TIMEOUT_SECONDS) as client:
            response = await client.get(f"{VALHALLA_URL}/health")
            return {
                "status": "ok" if response.is_success else "degraded",
                "available": response.is_success,
                "status_code": response.status_code,
            }
    except Exception as exc:  # pragma: no cover - network dependent
        return {"status": "down", "available": False, "error": str(exc)}


async def radar_health() -> dict[str, Any]:
    if not (RADAR_SECRET_KEY or RADAR_PUBLISHABLE_KEY):
        return {"status": "unconfigured", "available": False}
    return {
        "status": "configured",
        "available": True,
        "secret_key": bool(RADAR_SECRET_KEY),
        "publishable_key": bool(RADAR_PUBLISHABLE_KEY),
    }


async def relationship_health() -> dict[str, Any]:
    if not RELATIONSHIP_API_URL:
        return {"status": "unconfigured", "available": False}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{RELATIONSHIP_API_URL}/health", headers={"X-Internal-Key": RELATIONSHIP_API_KEY} if RELATIONSHIP_API_KEY else {})
            return {
                "status": "ok" if response.is_success else "degraded",
                "available": response.is_success,
                "status_code": response.status_code,
            }
    except Exception as exc:  # pragma: no cover - network dependent
        return {"status": "down", "available": False, "error": str(exc)}


async def push_route_to_relationship_api(route: dict[str, Any], officer_ids: list[str]) -> bool:
    if not RELATIONSHIP_API_URL or not RELATIONSHIP_API_KEY:
        return False
    payload = {
        "route_id": route["route_id"],
        "org_id": route["org_id"],
        "officer_ids": officer_ids or route.get("push_route_to_officers") or [],
        "route": route,
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(
                f"{RELATIONSHIP_API_URL}/internal/route-push",
                headers={
                    "Content-Type": "application/json",
                    "X-Internal-Key": RELATIONSHIP_API_KEY,
                },
                json=payload,
            )
            return response.is_success
    except Exception:
        return False


@app.middleware("http")
async def timing_header_middleware(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Process-Time-Ms"] = str(round((time.perf_counter() - started) * 1000))
    return response


@app.get("/health")
@app.get("/route/health")
@app.get("/api/v1/route/health")
async def health() -> dict[str, Any]:
    valhalla = await valhalla_health()
    radar = await radar_health()
    relationship = await relationship_health()
    return {
        "status": "ok",
        "service": "route-calculator",
        "environment": ENVIRONMENT,
        "uptime_seconds": round((datetime.now(timezone.utc) - APP_STARTED_AT).total_seconds(), 2),
        "dependencies": {
            "database": {
                "status": "ok" if store.use_db else "memory",
                "available": store.use_db,
            },
            "valhalla": valhalla,
            "radar": radar,
            "relationship_api": relationship,
        },
        "timestamp": now_iso(),
    }


@app.post("/route/calculate")
@app.post("/api/v1/route/calculate")
async def calculate_route(request: Request, _: None = Depends(validate_internal_key)) -> dict[str, Any]:
    body = await request.json()
    payload = RouteCalculateRequest.model_validate(body)
    org_id = api_org(request, body)
    if payload.org_id != org_id:
        payload = payload.model_copy(update={"org_id": org_id})

    devices = store.list_devices(org_id)
    route = build_route_response(payload, devices)
    route["org_id"] = org_id
    stored = store.save_route(route)
    return {
        "request_id": payload.request_id,
        "status": "success",
        "data": stored,
    }


@app.post("/route/push")
@app.post("/api/v1/route/push")
async def push_route(request: Request, _: None = Depends(validate_internal_key)) -> dict[str, Any]:
    body = RoutePushRequest.model_validate(await request.json())
    route = store.mark_pushed(body.route_id, body.officer_ids)
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")
    delivered = await push_route_to_relationship_api(route, body.officer_ids)
    if route.get("routes"):
        route["push_route_to_officers"] = body.officer_ids or route.get("push_route_to_officers") or []
    return {
        "request_id": body.request_id or route["request_id"],
        "status": "success",
        "data": {
            "route_id": route["route_id"],
            "pushed": True,
            "relationship_api_delivered": delivered,
            "route": route,
        },
    }


@app.get("/route/active/{route_id}")
@app.get("/api/v1/route/active/{route_id}")
async def active_route(route_id: str, _: None = Depends(validate_internal_key)) -> dict[str, Any]:
    route = store.get_route(route_id)
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")
    return {"status": "success", "data": route}


@app.post("/route/update/{route_id}")
@app.post("/api/v1/route/update/{route_id}")
async def update_route(route_id: str, request: Request, _: None = Depends(validate_internal_key)) -> dict[str, Any]:
    existing = store.get_route(route_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Route not found")
    body = await request.json()
    combined = deep_copy(existing.get("incident", {}))
    combined.update(body.get("incident") or {})
    recalculated = RouteCalculateRequest.model_validate(
        {
            "request_type": "route_calculate",
            "request_id": body.get("request_id") or existing.get("request_id") or random_id("req"),
            "org_id": existing.get("org_id") or body.get("org_id") or DEFAULT_ORG_ID,
            "incident": combined,
            "responders": body.get("responders") or existing.get("responders") or {},
            "routing_preferences": body.get("routing_preferences") or existing.get("routing_preferences") or {},
        }
    )
    recalculated_route = build_route_response(recalculated, store.list_devices(str(existing.get("org_id") or DEFAULT_ORG_ID)))
    recalculated_route["route_id"] = route_id
    recalculated_route["org_id"] = str(existing.get("org_id") or DEFAULT_ORG_ID)
    recalculated_route["pushed"] = False
    recalculated_route["pushed_at"] = None
    stored = store.save_route(recalculated_route)
    return {"request_id": recalculated.request_id, "status": "success", "data": stored}


@app.get("/infrastructure/registry")
@app.get("/api/v1/infrastructure/registry")
async def infrastructure_registry(request: Request, _: None = Depends(validate_internal_key)) -> dict[str, Any]:
    org_id = api_org(request)
    return {
        "status": "success",
        "data": {
            "org_id": org_id,
            "devices": store.list_devices(org_id),
        },
    }


@app.post("/infrastructure/register")
@app.post("/api/v1/infrastructure/register")
async def register_infrastructure(request: Request, _: None = Depends(validate_internal_key)) -> dict[str, Any]:
    body = InfrastructureRegisterRequest.model_validate(await request.json())
    org_id = body.org_id or api_org(request)
    record = {
        "org_id": org_id,
        "device_id": body.id or random_id("DEV"),
        "type": (body.type or body.device_type or "SMART_DEVICE").upper(),
        "name": body.name or "Unnamed smart device",
        "lat": body.lat,
        "lng": body.lng,
        "floor": body.floor,
        "building_id": body.building_id,
        "description": body.description,
        "connection_protocol": None,
        "connection_endpoint": None,
        "auth_type": None,
        "auth_key_reference": None,
        "capabilities": body.supported_actions or [],
        "default_state": body.metadata.get("default_state"),
        "safety_constraints": body.metadata.get("safety_constraints") or {},
        "operational": bool(body.operational if body.operational is not None else True),
        "last_health_check": None,
        "health_status": "unknown",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "metadata": body.metadata,
    }
    saved = store.register_device(record)
    return {"status": "success", "data": saved}


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"status": "error", "error": exc.detail})
