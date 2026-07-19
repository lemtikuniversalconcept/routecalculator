# Lemtik Security — Route Calculator Service Specification
### Service 2 of 6 — Optimal Routing & Infrastructure Recommendations
**Classification:** Internal Engineering
**Version:** 1.0
**Status:** Build-Ready

---

## 1. What This Service Is

The Route Calculator Service computes the fastest, safest route
for officers responding to an incident — whether on foot or in
a vehicle — and alongside that route, identifies every piece of
smart infrastructure on or near the path that could be controlled
to make the response faster or safer.

It does two things:

1. Calculates the optimal route from officer/vehicle location
   to incident location using real road and path data
2. Identifies every controllable smart infrastructure element
   on that route and recommends what to do with each one

It does not control anything itself. It does not track officers.
It calculates a route, identifies infrastructure opportunities,
and sends both back to the Master Agent.

---

## 2. Routing Profiles

### 2.1 Vehicle Profile
- Uses road network only
- Avoids known bottlenecks, bad roads, flooded routes
- Optimises for time not distance
- Smart infra: traffic lights, boom barriers, smart gates, toll gates

### 2.2 Foot Profile
- Uses pedestrian network — alleys, footpaths, pedestrian bridges
- Walking speed: 5km/h, sprint speed: 15km/h
- Smart infra: smart doors, elevators, escalators, turnstiles, smart locks

### 2.3 Hybrid Profile
- Vehicle to optimal drop-off point, then foot to incident
- Used for indoor incidents in hotels, malls, hospitals
- Returns both legs with combined ETA and infrastructure for each

---

## 3. Smart Infrastructure Types Supported

The registry is extensible — any remotely commandable device can be registered.

```
TRAFFIC_LIGHT     — green_corridor, red_cross_traffic, normal
SMART_GATE        — open, close, lock, unlock, hold_open
SMART_DOOR        — unlock, lock, hold_open, hold_closed
SMART_ELEVATOR    — hold_floor, send_to_floor, reserve_for_officers, normal
SMART_ESCALATOR   — stop, reverse_direction, normal
BOOM_BARRIER      — raise, lower, lock_raised, lock_lowered
SMART_LOCK        — unlock, lock, hold_unlocked, hold_locked
CCTV_CAMERA       — activate, pan_to_location, begin_tracking, alert_mode
TURNSTILE         — unlock, lock, hold_open
AUTOMATED_TOLL    — open_lane, priority_lane
SMART_LIGHTING    — full_brightness, emergency_mode, strobe
```

For each device on the route the service produces:
- Recommended action
- Action description
- Estimated time saved (seconds)
- Priority (critical/high/medium)
- Confidence score
- Requires approval (yes/no)
- Approval level (supervisor/manager)
- Auto revert duration
- Sequence order (order to execute actions)

---

## 4. Valhalla Routing Engine

Open-source, self-hosted on DigitalOcean ($12/month droplet).
Uses OpenStreetMap Nigeria data — includes footpaths, alleys,
pedestrian bridges maintained by local OSM community.
Zero per-query API cost. You own the engine.

```bash
# Setup on DigitalOcean droplet
apt-get update && apt-get install -y docker.io
wget https://download.geofabrik.de/africa/nigeria-latest.osm.pbf
docker run -dt --name valhalla -p 8002:8002 \
  -v $(pwd):/custom_files \
  ghcr.io/gis-ops/docker-valhalla/valhalla:latest

# Monthly OSM data refresh (cron job)
wget https://download.geofabrik.de/africa/nigeria-latest.osm.pbf
docker restart valhalla
```

Custom vehicle profile — avoids unpaved roads, optimises for Lagos traffic.
Custom foot profile — sprint speed 15km/h, prefers alleys and shortcuts.

---

## 5. Radar.io Integration

Handles real-time officer and vehicle tracking.
We do not build a tracking backend from scratch.

Radar handles:
- Officer GPS collection from mobile app pwa
- Vehicle GPS collection
- Geofencing (alert when officer enters/leaves zone)
- Trip tracking during operations
- Distance matrix (who is closest to incident from list of 20 officers)

Route Calculator queries Radar for:
- Current officer positions (route start points)
- Current vehicle positions
- Real-time trip updates during active operation

---

## 6. Input Contract (from Relationship API)

```json
{
  "request_type": "route_calculate",
  "request_id": "req_abc123",
  "org_id": "org_xyz",
  "incident": {
    "id": "INC-2024-001",
    "location": {
      "lat": 6.4281,
      "lng": 3.4219,
      "description": "North West Wing, Floor 3"
    },
    "type": "stabbing",
    "indoor": true,
    "building_id": "BLDG-HOTEL-001"
  },
  "responders": {
    "officers": ["OFF-001", "OFF-003", "OFF-007"],
    "vehicles": ["V001", "V003"]
  },
  "routing_preferences": {
    "type": "hybrid",
    "prioritise": "speed"
  }
}
```

---

## 7. Output Contract (to Relationship API)

```json
{
  "request_id": "req_abc123",
  "status": "success",
  "data": {
    "recommended_routing_type": "foot",
    "reasoning": "Incident is indoor Floor 3. Officers OFF-001 and OFF-003 within 80 metres. No vehicle needed.",
    "routes": [
      {
        "route_id": "ROUTE-001",
        "type": "foot",
        "officer_id": "OFF-001",
        "distance_metres": 78,
        "estimated_time_minutes": 1.2,
        "estimated_time_with_infra_minutes": 0.8,
        "time_saved_seconds": 24,
        "turn_by_turn": [
          "Head north through lobby",
          "Take elevator to Floor 3",
          "Turn left at Floor 3 landing",
          "Incident 15 metres ahead on right"
        ],
        "confidence": 0.96
      }
    ],
    "infrastructure_recommendations": [
      {
        "device_id": "DEV-ELEV-001",
        "device_type": "SMART_ELEVATOR",
        "name": "Main Lobby Elevator",
        "recommended_action": "reserve_for_officers",
        "action_description": "Hold at Ground Floor, send directly to Floor 3 when officers board.",
        "estimated_time_saved_seconds": 24,
        "priority": "high",
        "confidence": 0.93,
        "requires_approval": true,
        "approval_level": "supervisor",
        "auto_revert_after_seconds": 180,
        "sequence_order": 1
      },
      {
        "device_id": "DEV-DOOR-EXIT-001",
        "device_type": "SMART_DOOR",
        "name": "Hotel North Exit",
        "recommended_action": "hold_closed",
        "action_description": "Lock north exit to prevent suspect escape.",
        "estimated_time_saved_seconds": 0,
        "priority": "critical",
        "confidence": 0.91,
        "requires_approval": true,
        "approval_level": "supervisor",
        "auto_revert_after_seconds": 600,
        "sequence_order": 2
      }
    ],
    "push_route_to_officers": ["OFF-001", "OFF-003"],
    "mapbox_route_geojson": {},
    "meta": {
      "valhalla_query_ms": 180,
      "radar_query_ms": 95,
      "infra_query_ms": 45,
      "total_ms": 320
    }
  }
}
```

---

## 8. Service Endpoints

```
POST /route/calculate        — Main routing endpoint
POST /route/push             — Push confirmed route to officer device
GET  /route/active/:id       — Active route for an incident
POST /route/update/:id       — Recalculate mid-operation
GET  /infrastructure/registry — List registered devices for org
POST /infrastructure/register — Register new smart device
GET  /health                 — Service + Valhalla + Radar status
```

---

## 9. Database Schema (services schema)

```sql
CREATE TABLE services.infrastructure_devices (
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
    capabilities JSONB DEFAULT '[]',
    default_state VARCHAR(100),
    safety_constraints JSONB DEFAULT '{}',
    operational BOOLEAN DEFAULT TRUE,
    last_health_check TIMESTAMPTZ,
    health_status VARCHAR(50) DEFAULT 'unknown',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE services.route_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    route_id VARCHAR(100) UNIQUE NOT NULL,
    org_id UUID NOT NULL,
    incident_id UUID NOT NULL,
    officer_id UUID,
    vehicle_id UUID,
    routing_type VARCHAR(50),
    origin_lat DECIMAL(10,8),
    origin_lng DECIMAL(11,8),
    destination_lat DECIMAL(10,8),
    destination_lng DECIMAL(11,8),
    distance_metres INTEGER,
    estimated_time_minutes DECIMAL(8,2),
    actual_time_minutes DECIMAL(8,2),
    infrastructure_recommendations JSONB DEFAULT '[]',
    route_geojson JSONB,
    pushed_to_device BOOLEAN DEFAULT FALSE,
    pushed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE services.infrastructure_action_log (
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
);
```

---

## 10. Tech Stack

```
Language:       Python 3.11+
Framework:      FastAPI
Routing Engine: Valhalla (self-hosted Docker — DigitalOcean $12/month)
Tracking:       Radar.io API
Map Data:       OpenStreetMap Nigeria (monthly refresh)
Officer Maps:   Mapbox SDK (on officer device only — not this service)
Database:       Supabase PostgreSQL (services schema)
HTTP Client:    httpx (async)
Hosting:        Render web service ($7/month)
Total:          ~$19/month (Render + DigitalOcean)
```

---

## 11. Environment Variables

```env
VALHALLA_URL=http://your-droplet-ip:8002
RADAR_SECRET_KEY=
RADAR_PUBLISHABLE_KEY=
MAPBOX_ACCESS_TOKEN=
DATABASE_URL=
INTERNAL_API_KEY=
RELATIONSHIP_API_URL=
RELATIONSHIP_API_KEY=
INFRASTRUCTURE_CORRIDOR_METRES=100
MAX_OFFICERS_PER_ROUTE_QUERY=20
VALHALLA_TIMEOUT_SECONDS=5
ENVIRONMENT=production
PORT=8000
```

---

## 12. Build Checklist

- [ ] Valhalla running on DigitalOcean with Nigeria OSM data
- [ ] Vehicle routing tested: Lagos address to Lagos address
- [ ] Foot routing tested: includes alleys and pedestrian paths
- [ ] Hybrid routing tested: vehicle leg + foot leg combined
- [ ] Radar.io account set up and officer tracking tested
- [ ] Infrastructure registry schema created in Supabase
- [ ] 3+ test devices registered in infrastructure registry
- [ ] Infrastructure recommendation query tested
- [ ] Full route response assembled and validated
- [ ] Route pushed to test officer device via Relationship API
- [ ] Response time under 500ms verified
- [ ] Health endpoint returns Valhalla + Radar status
- [ ] Internal API key validation working

---

## 13. What the Dashboard Will See

```
┌──────────────────────────────────────────────────┐
│ RESPONSE ROUTE — INC-2024-001                   │
│                                                  │
│ Recommended: FOOT RESPONSE                       │
│ Officers: Ahmed Bello + Chidi Okafor            │
│ Distance: 78 metres                             │
│ ETA: 1 min 12 sec (sprint)                     │
│ With infrastructure cleared: 48 sec             │
│                                                  │
│ INFRASTRUCTURE ACTIONS PENDING APPROVAL:        │
│                                                  │
│ [CRITICAL] Lock North Exit Door         [✓] [✗]│
│ [HIGH]     Reserve Elevator Floor 3     [✓] [✗]│
│ [MEDIUM]   Unlock NW Wing Access Door   [✓] [✗]│
│                                                  │
│ [APPROVE ALL] [APPROVE SELECTED] [DENY ALL]    │
│ [PUSH ROUTE TO OFFICERS]                       │
└──────────────────────────────────────────────────┘
```

---

*Version 1.0 — Lemtik Security Engineering*
*Build Valhalla on DigitalOcean first.*
*Everything depends on the routing engine being live.*