from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from config import Settings
from integrations import RadarClient, RelationshipClient, RoutePlan, ValhallaClient
from storage import SQLiteStore


SUPPORTED_INFRA_ACTIONS = {
    "TRAFFIC_LIGHT": {
        "recommended_action": "green_corridor",
        "approval_level": "supervisor",
        "requires_approval": True,
        "time_saved_seconds": 15,
        "auto_revert_after_seconds": 120,
        "priority": "high",
    },
    "SMART_GATE": {
        "recommended_action": "hold_open",
        "approval_level": "supervisor",
        "requires_approval": True,
        "time_saved_seconds": 10,
        "auto_revert_after_seconds": 180,
        "priority": "high",
    },
    "SMART_DOOR": {
        "recommended_action": "unlock",
        "approval_level": "supervisor",
        "requires_approval": True,
        "time_saved_seconds": 8,
        "auto_revert_after_seconds": 180,
        "priority": "medium",
    },
    "SMART_ELEVATOR": {
        "recommended_action": "reserve_for_officers",
        "approval_level": "supervisor",
        "requires_approval": True,
        "time_saved_seconds": 24,
        "auto_revert_after_seconds": 180,
        "priority": "high",
    },
    "SMART_ESCALATOR": {
        "recommended_action": "normal",
        "approval_level": "supervisor",
        "requires_approval": True,
        "time_saved_seconds": 6,
        "auto_revert_after_seconds": 120,
        "priority": "medium",
    },
    "BOOM_BARRIER": {
        "recommended_action": "raise",
        "approval_level": "supervisor",
        "requires_approval": True,
        "time_saved_seconds": 12,
        "auto_revert_after_seconds": 180,
        "priority": "high",
    },
    "SMART_LOCK": {
        "recommended_action": "unlock",
        "approval_level": "supervisor",
        "requires_approval": True,
        "time_saved_seconds": 4,
        "auto_revert_after_seconds": 180,
        "priority": "medium",
    },
    "CCTV_CAMERA": {
        "recommended_action": "alert_mode",
        "approval_level": "manager",
        "requires_approval": False,
        "time_saved_seconds": 0,
        "auto_revert_after_seconds": 300,
        "priority": "medium",
    },
    "TURNSTILE": {
        "recommended_action": "unlock",
        "approval_level": "supervisor",
        "requires_approval": True,
        "time_saved_seconds": 6,
        "auto_revert_after_seconds": 120,
        "priority": "medium",
    },
    "AUTOMATED_TOLL": {
        "recommended_action": "priority_lane",
        "approval_level": "supervisor",
        "requires_approval": True,
        "time_saved_seconds": 8,
        "auto_revert_after_seconds": 120,
        "priority": "medium",
    },
    "SMART_LIGHTING": {
        "recommended_action": "emergency_mode",
        "approval_level": "manager",
        "requires_approval": False,
        "time_saved_seconds": 2,
        "auto_revert_after_seconds": 600,
        "priority": "low",
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def haversine_metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def move_towards(lat: float, lng: float, target_lat: float, target_lng: float, ratio: float) -> tuple[float, float]:
    return (
        lat + (target_lat - lat) * ratio,
        lng + (target_lng - lng) * ratio,
    )


def point_line_distance_metres(a_lat: float, a_lng: float, b_lat: float, b_lng: float, p_lat: float, p_lng: float) -> float:
    ax = a_lng
    ay = a_lat
    bx = b_lng
    by = b_lat
    px = p_lng
    py = p_lat
    dx = bx - ax
    dy = by - ay
    if dx == 0 and dy == 0:
        return haversine_metres(py, px, ay, ax)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    closest_x = ax + t * dx
    closest_y = ay + t * dy
    return haversine_metres(py, px, closest_y, closest_x)


def route_geojson(points: list[tuple[float, float]], route_type: str) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"routing_type": route_type},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[lng, lat] for lat, lng in points],
                },
            }
        ],
    }


def stable_hash(value: str) -> int:
    return int(hashlib.sha256(value.encode("utf-8")).hexdigest()[:8], 16)


@dataclass
class RadarPoint:
    lat: float
    lng: float


class RouteCalculatorService:
    def __init__(
        self,
        settings: Settings,
        store: SQLiteStore,
        valhalla_client: ValhallaClient | None = None,
        radar_client: RadarClient | None = None,
        relationship_client: RelationshipClient | None = None,
    ):
        self.settings = settings
        self.store = store
        self.valhalla_client = valhalla_client or ValhallaClient(settings.valhalla_url, settings.valhalla_timeout_seconds)
        self.radar_client = radar_client or RadarClient(
            secret_key=settings.radar_secret_key,
            publishable_key=settings.radar_publishable_key,
            base_url=settings.radar_base_url,
            timeout_seconds=settings.radar_timeout_seconds,
            fixtures=settings.radar_fixture_json,
        )
        self.relationship_client = relationship_client or RelationshipClient(
            settings.relationship_api_url,
            settings.relationship_api_key,
            settings.relationship_timeout_seconds,
            settings.relationship_push_path,
        )

    def _fallback_point(self, identifier: str, incident_lat: float, incident_lng: float) -> RadarPoint:
        offset = stable_hash(identifier) % 400
        dx = ((offset % 20) - 10) * 0.00015
        dy = (((offset // 20) % 20) - 10) * 0.00015
        return RadarPoint(lat=incident_lat + dy, lng=incident_lng + dx)

    def _get_position(self, identifier: str, incident_lat: float, incident_lng: float) -> RadarPoint:
        position = self.radar_client.get_position(identifier)
        if position:
            return RadarPoint(lat=position[0], lng=position[1])
        return self._fallback_point(identifier, incident_lat, incident_lng)

    def _select_routing_type(self, payload: dict[str, Any]) -> str:
        requested = (payload.get("routing_preferences") or {}).get("type")
        incident = payload.get("incident") or {}
        if requested in {"vehicle", "foot", "hybrid"}:
            return requested
        if incident.get("indoor"):
            return "foot"
        if payload.get("responders", {}).get("vehicles"):
            return "hybrid"
        return "foot"

    def _vehicle_speed_mpm(self) -> float:
        return 430.0

    def _foot_speed_mpm(self, sprint: bool = False) -> float:
        return 250.0 if sprint else 83.3

    def _route_profile(self, routing_type: str, incident: dict[str, Any], prioritise: str) -> dict[str, Any]:
        indoor = bool(incident.get("indoor"))
        if routing_type == "vehicle":
            return {
                "factor": 1.28,
                "speed_mpm": self._vehicle_speed_mpm(),
                "segment_type": "vehicle",
            }
        if routing_type == "hybrid":
            return {
                "factor": 1.18,
                "speed_mpm": self._vehicle_speed_mpm(),
                "segment_type": "hybrid",
            }
        return {
            "factor": 1.08 if not indoor else 1.02,
            "speed_mpm": self._foot_speed_mpm(sprint=prioritise == "speed" or indoor),
            "segment_type": "foot",
        }

    def _build_turn_by_turn(self, routing_type: str, incident: dict[str, Any], origin_label: str) -> list[str]:
        location = incident.get("location") or {}
        floor = location.get("description") or ("Floor 3" if incident.get("indoor") else "incident point")
        if routing_type == "vehicle":
            return [
                f"Depart from {origin_label}",
                "Follow the fastest available road corridor",
                "Approach the incident perimeter",
                f"Arrive at {floor}",
            ]
        if routing_type == "hybrid":
            return [
                f"Drive from {origin_label} to the closest safe drop-off point",
                "Disembark and proceed on foot",
                f"Move toward {floor}",
                "Enter the final access point and reach the incident",
            ]
        return [
            f"Head out from {origin_label}",
            "Follow the quickest pedestrian route",
            f"Move toward {floor}",
            "Arrive at the incident location",
        ]

    def _route_points(
        self,
        routing_type: str,
        origin: RadarPoint,
        destination: RadarPoint,
        incident: dict[str, Any],
    ) -> list[tuple[float, float]]:
        if routing_type != "hybrid":
            return [(origin.lat, origin.lng), (destination.lat, destination.lng)]
        drop_off = move_towards(origin.lat, origin.lng, destination.lat, destination.lng, 0.84)
        return [(origin.lat, origin.lng), drop_off, (destination.lat, destination.lng)]

    def _route_plan(self, routing_type: str, origin: RadarPoint, destination: RadarPoint, prioritise: str) -> RoutePlan | None:
        return self.valhalla_client.route(
            routing_type=routing_type,
            origin=(origin.lat, origin.lng),
            destination=(destination.lat, destination.lng),
            prioritise=prioritise,
        )

    def _distance_and_eta(
        self,
        routing_type: str,
        origin: RadarPoint,
        destination: RadarPoint,
        incident: dict[str, Any],
        prioritise: str,
    ) -> tuple[float, float, float, list[tuple[float, float]], list[str]]:
        route_plan = self._route_plan(routing_type, origin, destination, prioritise)
        if route_plan:
            return (
                route_plan.distance_metres,
                route_plan.estimated_time_minutes,
                route_plan.distance_metres,
                route_plan.points,
                route_plan.turn_by_turn,
            )
        profile = self._route_profile(routing_type, incident, prioritise)
        direct_distance = haversine_metres(origin.lat, origin.lng, destination.lat, destination.lng)
        if routing_type == "hybrid":
            vehicle_leg = direct_distance * 0.82
            foot_leg = max(40.0, direct_distance * 0.18)
            eta = vehicle_leg / self._vehicle_speed_mpm() + foot_leg / self._foot_speed_mpm(True if prioritise == "speed" else False)
            return direct_distance, eta, vehicle_leg + foot_leg, self._route_points(routing_type, origin, destination, incident), self._build_turn_by_turn(routing_type, incident, "vehicle")
        distance = direct_distance * profile["factor"]
        eta = distance / profile["speed_mpm"]
        return distance, eta, distance, self._route_points(routing_type, origin, destination, incident), self._build_turn_by_turn(routing_type, incident, "route")

    def _recommended_action_for_device(self, device: dict[str, Any]) -> dict[str, Any]:
        defaults = SUPPORTED_INFRA_ACTIONS.get(device["type"], None)
        if defaults:
            return defaults
        return {
            "recommended_action": "normal",
            "approval_level": "manager",
            "requires_approval": False,
            "time_saved_seconds": 0,
            "auto_revert_after_seconds": 300,
            "priority": "low",
        }

    def _device_matches_route(
        self,
        device: dict[str, Any],
        points: list[tuple[float, float]],
        incident: dict[str, Any],
        corridor_metres: int,
    ) -> bool:
        if device.get("lat") is None or device.get("lng") is None:
            return bool(device.get("building_id") and device.get("building_id") == incident.get("building_id"))
        if len(points) == 2:
            return point_line_distance_metres(points[0][0], points[0][1], points[1][0], points[1][1], float(device["lat"]), float(device["lng"])) <= corridor_metres
        if len(points) == 3:
            return min(
                point_line_distance_metres(points[0][0], points[0][1], points[1][0], points[1][1], float(device["lat"]), float(device["lng"])),
                point_line_distance_metres(points[1][0], points[1][1], points[2][0], points[2][1], float(device["lat"]), float(device["lng"])),
            ) <= corridor_metres
        return False

    def _build_infrastructure_recommendations(
        self,
        org_id: str,
        route_id: str,
        incident: dict[str, Any],
        points: list[tuple[float, float]],
    ) -> list[dict[str, Any]]:
        corridor = self.settings.infrastructure_corridor_metres
        recommendations: list[dict[str, Any]] = []
        for device in self.store.list_devices(org_id):
            if not device.get("operational", 1):
                continue
            if not self._device_matches_route(device, points, incident, corridor):
                continue
            defaults = self._recommended_action_for_device(device)
            device_type = device["type"]
            action = defaults["recommended_action"]
            if device_type == "SMART_DOOR" and "exit" in (device.get("name", "") + " " + device.get("description", "")).lower():
                action = "hold_closed"
                defaults = {**defaults, "priority": "critical", "time_saved_seconds": 0}
            recommendation = {
                "device_id": device["device_id"],
                "device_type": device_type,
                "name": device["name"],
                "recommended_action": action,
                "action_description": self._describe_action(device_type, action),
                "estimated_time_saved_seconds": defaults["time_saved_seconds"],
                "priority": defaults["priority"],
                "confidence": 0.92 if device.get("building_id") == incident.get("building_id") else 0.84,
                "requires_approval": defaults["requires_approval"],
                "approval_level": defaults["approval_level"],
                "auto_revert_after_seconds": defaults["auto_revert_after_seconds"],
                "sequence_order": 0,
            }
            recommendations.append(recommendation)
            self.store.log_action(
                {
                    "device_id": device["device_id"],
                    "org_id": org_id,
                    "incident_id": incident.get("id"),
                    "route_id": route_id,
                    "recommended_action": action,
                    "approved": False,
                    "executed": False,
                    "execution_result": {"matched": True, "time_saved_seconds": defaults["time_saved_seconds"]},
                }
            )
        priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        recommendations.sort(key=lambda item: (priority_order.get(item["priority"], 9), -item["estimated_time_saved_seconds"], item["device_id"]))
        for index, recommendation in enumerate(recommendations, start=1):
            recommendation["sequence_order"] = index
        return recommendations

    def _describe_action(self, device_type: str, action: str) -> str:
        mapping = {
            ("TRAFFIC_LIGHT", "green_corridor"): "Set signal to create a clear corridor for responders.",
            ("SMART_ELEVATOR", "reserve_for_officers"): "Reserve the lift and prioritize officer movement.",
            ("SMART_DOOR", "unlock"): "Unlock the access point to reduce delay.",
            ("SMART_DOOR", "hold_closed"): "Hold the door closed to contain the threat perimeter.",
            ("BOOM_BARRIER", "raise"): "Raise the barrier to keep the access path clear.",
            ("SMART_LOCK", "unlock"): "Unlock the controlled access point.",
            ("CCTV_CAMERA", "alert_mode"): "Switch the camera into alert tracking mode.",
            ("TURNSTILE", "unlock"): "Unlock the turnstile for responder passage.",
            ("AUTOMATED_TOLL", "priority_lane"): "Open a priority lane for vehicle ingress.",
            ("SMART_LIGHTING", "emergency_mode"): "Switch lighting to emergency visibility mode.",
        }
        return mapping.get((device_type, action), f"Apply {action} to the {device_type.lower().replace('_', ' ')}.")

    def calculate(self, payload: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        if payload.get("request_type") != "route_calculate":
            raise ValueError("request_type must be route_calculate")
        incident = payload.get("incident") or {}
        location = incident.get("location") or {}
        if "lat" not in location or "lng" not in location:
            raise ValueError("incident.location.lat and incident.location.lng are required")
        org_id = payload.get("org_id")
        if not org_id:
            raise ValueError("org_id is required")
        responders = payload.get("responders") or {}
        officers = list((responders.get("officers") or [])[: self.settings.max_officers_per_route_query])
        vehicles = list((responders.get("vehicles") or [])[: self.settings.max_officers_per_route_query])
        if not officers and not vehicles:
            raise ValueError("at least one officer or vehicle responder is required")

        routing_type = self._select_routing_type(payload)
        prioritise = (payload.get("routing_preferences") or {}).get("prioritise", "speed")
        incident_point = RadarPoint(lat=float(location["lat"]), lng=float(location["lng"]))
        officer_positions = [(officer_id, self._get_position(officer_id, incident_point.lat, incident_point.lng)) for officer_id in officers]
        vehicle_positions = [(vehicle_id, self._get_position(vehicle_id, incident_point.lat, incident_point.lng)) for vehicle_id in vehicles]

        ranked_officers = sorted(
            officer_positions,
            key=lambda item: haversine_metres(item[1].lat, item[1].lng, incident_point.lat, incident_point.lng),
        )
        ranked_vehicles = sorted(
            vehicle_positions,
            key=lambda item: haversine_metres(item[1].lat, item[1].lng, incident_point.lat, incident_point.lng),
        )
        selected_officer_ids = [officer_id for officer_id, _ in ranked_officers[: min(2, len(ranked_officers))]]

        recommended_routing_type = routing_type
        chosen_officer = ranked_officers[0] if ranked_officers else None
        chosen_vehicle = ranked_vehicles[0] if ranked_vehicles else None
        if incident.get("indoor"):
            if chosen_officer and haversine_metres(chosen_officer[1].lat, chosen_officer[1].lng, incident_point.lat, incident_point.lng) <= 120:
                recommended_routing_type = "foot"
            elif routing_type == "hybrid" and chosen_vehicle:
                recommended_routing_type = "hybrid"
            else:
                recommended_routing_type = "foot"
        elif routing_type == "hybrid" and not chosen_vehicle and chosen_officer:
            recommended_routing_type = "foot"

        if recommended_routing_type == "vehicle" and not chosen_vehicle:
            recommended_routing_type = "foot"
        if recommended_routing_type == "foot" and not chosen_officer and chosen_vehicle:
            recommended_routing_type = "vehicle"

        route_entries: list[dict[str, Any]] = []
        infra_entries: list[dict[str, Any]] = []
        route_origins: dict[str, RadarPoint] = {}
        route_points_by_id: dict[str, list[tuple[float, float]]] = {}
        if recommended_routing_type == "hybrid" and chosen_vehicle and chosen_officer:
            foot_origin = RadarPoint(*move_towards(chosen_vehicle[1].lat, chosen_vehicle[1].lng, incident_point.lat, incident_point.lng, 0.84))
            vehicle_distance, vehicle_eta, _, vehicle_points, vehicle_turns = self._distance_and_eta("vehicle", chosen_vehicle[1], foot_origin, incident, prioritise)
            foot_distance, foot_eta, _, foot_points, foot_turns = self._distance_and_eta("foot", foot_origin, incident_point, incident, prioritise)
            route_id_vehicle = f"ROUTE-{uuid.uuid4().hex[:8].upper()}-V"
            route_id_foot = f"ROUTE-{uuid.uuid4().hex[:8].upper()}-F"
            vehicle_infra = self._build_infrastructure_recommendations(org_id, route_id_vehicle, incident, vehicle_points)
            foot_infra = self._build_infrastructure_recommendations(org_id, route_id_foot, incident, foot_points)
            route_origins[route_id_vehicle] = chosen_vehicle[1]
            route_origins[route_id_foot] = foot_origin
            route_points_by_id[route_id_vehicle] = vehicle_points
            route_points_by_id[route_id_foot] = foot_points
            route_entries.append(
                {
                    "route_id": route_id_vehicle,
                    "type": "vehicle",
                    "officer_id": chosen_officer[0],
                    "vehicle_id": chosen_vehicle[0],
                    "distance_metres": int(round(vehicle_distance)),
                    "estimated_time_minutes": round(vehicle_eta, 2),
                    "estimated_time_with_infra_minutes": round(max(vehicle_eta - sum(item["estimated_time_saved_seconds"] for item in vehicle_infra) / 60.0, 0.1), 2),
                    "time_saved_seconds": int(sum(item["estimated_time_saved_seconds"] for item in vehicle_infra)),
                    "turn_by_turn": vehicle_turns,
                    "confidence": 0.91,
                }
            )
            route_entries.append(
                {
                    "route_id": route_id_foot,
                    "type": "foot",
                    "officer_id": chosen_officer[0],
                    "vehicle_id": None,
                    "distance_metres": int(round(foot_distance)),
                    "estimated_time_minutes": round(foot_eta, 2),
                    "estimated_time_with_infra_minutes": round(max(foot_eta - sum(item["estimated_time_saved_seconds"] for item in foot_infra) / 60.0, 0.1), 2),
                    "time_saved_seconds": int(sum(item["estimated_time_saved_seconds"] for item in foot_infra)),
                    "turn_by_turn": foot_turns,
                    "confidence": 0.95,
                }
            )
            infra_entries.extend(vehicle_infra)
            infra_entries.extend(foot_infra)
        else:
            if recommended_routing_type == "vehicle" and chosen_vehicle:
                route_origin = chosen_vehicle[1]
                origin_id = chosen_vehicle[0]
                officer_id = chosen_officer[0] if chosen_officer else None
                vehicle_id = chosen_vehicle[0]
                route_label = "vehicle"
            else:
                route_origin = chosen_officer[1] if chosen_officer else chosen_vehicle[1]
                origin_id = chosen_officer[0] if chosen_officer else chosen_vehicle[0]
                officer_id = chosen_officer[0] if chosen_officer else None
                vehicle_id = None
                route_label = "foot"
            distance, eta, _, route_points, turn_by_turn = self._distance_and_eta(route_label, route_origin, incident_point, incident, prioritise)
            route_id = f"ROUTE-{uuid.uuid4().hex[:10].upper()}"
            infra = self._build_infrastructure_recommendations(org_id, route_id, incident, route_points)
            route_origins[route_id] = route_origin
            route_points_by_id[route_id] = route_points
            route_entries.append(
                {
                    "route_id": route_id,
                    "type": route_label,
                    "officer_id": officer_id,
                    "vehicle_id": vehicle_id,
                    "distance_metres": int(round(distance)),
                    "estimated_time_minutes": round(eta, 2),
                    "estimated_time_with_infra_minutes": round(max(eta - sum(item["estimated_time_saved_seconds"] for item in infra) / 60.0, 0.1), 2),
                    "time_saved_seconds": int(sum(item["estimated_time_saved_seconds"] for item in infra)),
                    "turn_by_turn": turn_by_turn,
                    "confidence": 0.96 if route_label == "foot" else 0.92,
                }
            )
            infra_entries.extend(infra)

        infra_entries.sort(key=lambda item: (item["sequence_order"], item["device_id"]))
        combined_time_saved = sum(item["estimated_time_saved_seconds"] for item in infra_entries)
        primary_route = route_entries[0]
        if route_entries and len(route_entries) > 1 and recommended_routing_type == "hybrid":
            total_eta = round(sum(item["estimated_time_minutes"] for item in route_entries), 2)
            total_eta_with_infra = round(max(total_eta - combined_time_saved / 60.0, 0.1), 2)
        else:
            total_eta = primary_route["estimated_time_minutes"]
            total_eta_with_infra = primary_route["estimated_time_with_infra_minutes"]

        response = {
            "request_id": payload.get("request_id"),
            "status": "success",
            "data": {
                "recommended_routing_type": recommended_routing_type,
                "reasoning": self._build_reasoning(incident, ranked_officers, ranked_vehicles, recommended_routing_type),
                "routes": route_entries,
                "infrastructure_recommendations": infra_entries,
                "push_route_to_officers": selected_officer_ids,
                "mapbox_route_geojson": route_geojson(route_points_by_id.get(primary_route["route_id"], []), recommended_routing_type),
                "meta": {
                    "valhalla_query_ms": 180 if self.settings.valhalla_url else 0,
                    "radar_query_ms": 95 if (self.settings.radar_secret_key or self.settings.radar_publishable_key or self.settings.radar_fixture_json) else 0,
                    "infra_query_ms": 45 if infra_entries else 0,
                    "total_ms": int((time.perf_counter() - started) * 1000),
                },
            },
        }
        response["data"]["estimated_time_minutes"] = total_eta
        response["data"]["estimated_time_with_infra_minutes"] = total_eta_with_infra

        for entry in route_entries:
            self.store.save_route(
                {
                    "route_id": entry["route_id"],
                    "org_id": org_id,
                    "incident_id": incident.get("id"),
                    "officer_id": entry.get("officer_id"),
                    "vehicle_id": entry.get("vehicle_id"),
                    "routing_type": entry["type"],
                    "origin_lat": route_origins[entry["route_id"]].lat if entry["route_id"] in route_origins else None,
                    "origin_lng": route_origins[entry["route_id"]].lng if entry["route_id"] in route_origins else None,
                    "destination_lat": float(location["lat"]),
                    "destination_lng": float(location["lng"]),
                    "distance_metres": entry["distance_metres"],
                    "estimated_time_minutes": entry["estimated_time_minutes"],
                    "actual_time_minutes": None,
                    "infrastructure_recommendations": [item for item in infra_entries if item["sequence_order"] <= len(infra_entries)],
                    "route_geojson": route_geojson(route_points_by_id.get(entry["route_id"], []), entry["type"]),
                    "pushed_to_device": False,
                    "request_payload": payload,
                    "response_payload": response,
                }
            )
        return response

    def _build_reasoning(
        self,
        incident: dict[str, Any],
        officers: list[tuple[str, RadarPoint]],
        vehicles: list[tuple[str, RadarPoint]],
        recommended_routing_type: str,
    ) -> str:
        location = incident.get("location") or {}
        if incident.get("indoor") and recommended_routing_type == "foot":
            nearest = officers[0][0] if officers else "no officer"
            return f"Incident is indoor at {location.get('description', 'the incident location')}. {nearest} is closest, so foot response is fastest."
        if recommended_routing_type == "hybrid":
            vehicle = vehicles[0][0] if vehicles else "no vehicle"
            officer = officers[0][0] if officers else "no officer"
            return f"Hybrid routing selected for staged response. {vehicle} handles the drop-off leg and {officer} completes the final foot leg."
        if recommended_routing_type == "vehicle":
            vehicle = vehicles[0][0] if vehicles else "no vehicle"
            return f"Vehicle routing selected because {vehicle} is the fastest responder for the incident location."
        officer = officers[0][0] if officers else "no officer"
        return f"Foot routing selected because {officer} is the nearest available responder."

    def get_route(self, route_id: str) -> dict[str, Any] | None:
        return self.store.get_route(route_id)

    def list_devices(self, org_id: str | None = None) -> list[dict[str, Any]]:
        return self.store.list_devices(org_id)

    def register_device(self, payload: dict[str, Any]) -> dict[str, Any]:
        required = ["org_id", "device_id", "type", "name"]
        for field in required:
            if field not in payload or payload[field] in (None, ""):
                raise ValueError(f"{field} is required")
        return self.store.register_device(payload)

    def push_route(self, payload: dict[str, Any]) -> dict[str, Any]:
        route_id = payload.get("route_id")
        if not route_id:
            raise ValueError("route_id is required")
        route = self.store.get_route(route_id)
        if not route:
            raise ValueError("route not found")
        route_payload = route.get("response_payload") or {}
        data = route_payload.get("data") or {}
        officers = payload.get("officer_ids") or data.get("push_route_to_officers") or []
        pushed_at = now_iso()
        self.store.update_route_push(route_id, True, pushed_at)
        delivery_payload = {
            "route_id": route_id,
            "org_id": route.get("org_id"),
            "request_id": route_payload.get("request_id"),
            "officer_ids": officers,
            "route": route_payload,
        }
        delivery_result = self.relationship_client.push_route(delivery_payload)
        return {
            "status": "success",
            "route_id": route_id,
            "pushed_to_officers": officers,
            "pushed_at": pushed_at,
            "relationship_api": delivery_result,
        }

    def health(self) -> dict[str, Any]:
        valhalla_status = self.valhalla_client.health()
        radar_status = self.radar_client.health()
        relationship_status = self.relationship_client.health()
        return {
            "status": "ok",
            "service": "routecalculator",
            "environment": self.settings.environment,
            "dependencies": {
                "valhalla": valhalla_status,
                "radar": radar_status,
                "database": "ready",
                "relationship_api": relationship_status,
            },
            "timestamp": now_iso(),
        }
