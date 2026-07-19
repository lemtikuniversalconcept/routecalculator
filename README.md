# Lemtik Route Calculator Service

FastAPI service for responder route calculation, route storage, infrastructure registry, and route push confirmation.

## Endpoints

- `POST /route/calculate`
- `POST /route/push`
- `GET /route/active/:id`
- `POST /route/update/:id`
- `GET /infrastructure/registry`
- `POST /infrastructure/register`
- `GET /health`

Compatible aliases are also exposed under `/api/v1/...`.

## Auth

Set `X-Internal-Key` or `Authorization: Bearer <INTERNAL_API_KEY>`.

In development, the service allows requests without auth. In production, the internal key is required.

## Notes

- If Valhalla is configured, health checks attempt to reach it.
- Radar is treated as a dependency/configuration signal, not a tracking backend recreated here.
- If `RELATIONSHIP_API_URL` and `RELATIONSHIP_API_KEY` are set, route pushes are forwarded to the Relationship API.
- The service seeds a small infrastructure registry when no database rows exist.
- The local fallback route model matches the shape expected by the Relationship API.
