# Route Calculator API

This service calculates responder routes and returns infrastructure actions that should be handled by the Relationship API or any orchestration layer.

It is designed so another application can:

- Ask for route calculations
- Query infrastructure devices
- Register new devices
- Fetch active routes
- Recalculate mid-operation
- Push approved routes to responders
- Use the response to trigger alerts, approvals, and control actions

## Service Role

Route Calculator does not control infrastructure directly.
It produces a routing plan plus a ranked list of recommended infrastructure actions.

The downstream system, usually the Relationship API, should use those recommendations to:

- Notify supervisors or managers
- Request approval for risky actions
- Push the approved route to officers
- Fan out alerts to the correct responder channel
- Dispatch any device-control requests to the appropriate internal system

## Base URL

When deployed, the API is exposed by FastAPI.
In local fallback mode, the same routes exist through the built-in handler.

Typical base URL:

```text
https://your-routecalculator-service.onrender.com
```

## Authentication

Use the internal service key for server-to-server requests.

Recommended headers:

```http
Content-Type: application/json
Authorization: Bearer <INTERNAL_API_KEY>
```

If your organization prefers a different header format, the caller and service should agree on one stable convention.
The service itself is currently structured around internal trust rather than end-user auth.

## Request Flow

### 1. Another application asks for a route

The caller sends `POST /route/calculate` with:

- Incident location
- Incident metadata
- Officers and vehicles to consider
- Routing preferences

The service responds with:

- Recommended routing type
- One or more route objects
- Infrastructure recommendations
- Officer IDs to push to
- GeoJSON route geometry
- Timing metadata

### 2. Relationship API consumes the response

The Relationship API should read the response and decide:

- Which route to show
- Which officers should receive it
- Which infrastructure actions require approval
- Which actions can be sent to the next control layer

### 3. Relationship API sends alerts

Use the infrastructure recommendation list to trigger notifications such as:

- Supervisor approval requests
- Manager approval requests
- Officer dispatch messages
- Device-control alert records

### 4. Approved route is pushed

Once approved, the Relationship API or another orchestration service can call:

```http
POST /route/push
```

This marks the route as pushed and forwards the payload to the Relationship API push endpoint if configured.

## Endpoints

### `POST /route/calculate`

Calculate the optimal route and infrastructure recommendations.

Example request:

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

Example response:

```json
{
  "request_id": "req_abc123",
  "status": "success",
  "data": {
    "recommended_routing_type": "foot",
    "reasoning": "Incident is indoor at North West Wing, Floor 3. OFF-001 is closest, so foot response is fastest.",
    "routes": [],
    "infrastructure_recommendations": [],
    "push_route_to_officers": ["OFF-001", "OFF-003"],
    "mapbox_route_geojson": {},
    "meta": {
      "valhalla_query_ms": 0,
      "radar_query_ms": 0,
      "infra_query_ms": 0,
      "total_ms": 12
    }
  }
}
```

### `POST /route/push`

Push a confirmed route to responders and forward the route payload to the Relationship API.

Example request:

```json
{
  "route_id": "ROUTE-001",
  "officer_ids": ["OFF-001", "OFF-003"]
}
```

Behavior:

- Marks the route as pushed in storage
- Sends the route payload to the configured Relationship API endpoint
- Returns whether the Relationship API delivery succeeded

### `GET /route/active/:id`

Fetch a stored route by route ID.

Use this when the dashboard needs the current route state for an incident.

### `POST /route/update/:id`

Recalculate a route after conditions change.

Useful when:

- New traffic data arrives
- The incident moves
- A vehicle becomes unavailable
- An officer gets reassigned

### `GET /infrastructure/registry`

List devices registered for an organization.

Optional query parameter:

```text
?org_id=org_xyz
```

### `POST /infrastructure/register`

Register a new smart device.

This is how an upstream admin app can extend the infrastructure registry without changing code.

### `GET /health`

Returns dependency status for:

- Route service
- Valhalla
- Radar
- Database
- Relationship API

## Relationship API Integration

The Relationship API is the main consumer of route output.

### What Route Calculator sends back

It returns:

- Route geometry
- Estimated time
- Recommended route type
- Infrastructure actions
- Approval requirements
- Priority ordering
- Officer IDs for dispatch

### How the Relationship API should use it

The Relationship API should:

1. Call `POST /route/calculate`
2. Store the response payload
3. Render the route in the dashboard
4. Show every infrastructure recommendation
5. Separate recommendations by approval level
6. Send approval notifications to the right role
7. After approval, call `POST /route/push`
8. Track completion and completion timestamps

### Alerting model

The infrastructure recommendations already include:

- `device_id`
- `device_type`
- `recommended_action`
- `priority`
- `requires_approval`
- `approval_level`
- `sequence_order`

That means another API can treat the response as an alert queue.

Example logic:

- `critical` items become immediate supervisor alerts
- `high` items become high-priority approval tasks
- `medium` items become routine operational recommendations
- `low` items become informational actions

### Device-control integration

Route Calculator does not execute controls.
It only recommends them.

The Relationship API or a separate control service should:

- Validate the recommendation
- Check role permissions
- Apply the action to the real device system
- Record the action result
- Revert it after the auto-revert interval

## External API Usage Pattern

Any other application can integrate in the same way:

1. Obtain an internal token or API key
2. Call `GET /health` to check readiness
3. Call `GET /infrastructure/registry` to inspect devices
4. Call `POST /route/calculate` for a new incident
5. Call `GET /route/active/:id` to review stored route state
6. Call `POST /route/update/:id` when conditions change
7. Call `POST /route/push` after approval

## Example Client Behavior

A dashboard app should:

- Render the route on a map
- Highlight route actions by priority
- Show approval checkboxes for supervisor-controlled devices
- Offer a push-to-officers button after approval
- Display route ETA before and after infrastructure actions

An automation app should:

- Pull route responses
- Match actions to device capabilities
- Trigger notifications to the Relationship API
- Log the result of each action

## Notes for Implementers

- Use the `request_id` to correlate request, approval, and push flows.
- Use `route_id` as the stable route reference.
- Use `incident.id` as the incident anchor in your own system.
- Treat `requires_approval` as a hard gate before execution.
- Treat `sequence_order` as the execution order.
- Never assume infrastructure actions are already executed because they appear in the response.

