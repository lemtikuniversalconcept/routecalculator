CREATE SCHEMA IF NOT EXISTS services;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE OR REPLACE FUNCTION services.set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

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
);

DROP TRIGGER IF EXISTS trigger_infrastructure_devices_updated_at ON services.infrastructure_devices;
CREATE TRIGGER trigger_infrastructure_devices_updated_at
BEFORE UPDATE ON services.infrastructure_devices
FOR EACH ROW
EXECUTE FUNCTION services.set_updated_at();

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
);

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
);

CREATE INDEX IF NOT EXISTS idx_infrastructure_devices_org_id ON services.infrastructure_devices (org_id);
CREATE INDEX IF NOT EXISTS idx_infrastructure_devices_building_id ON services.infrastructure_devices (building_id);
CREATE INDEX IF NOT EXISTS idx_route_history_org_id ON services.route_history (org_id);
CREATE INDEX IF NOT EXISTS idx_route_history_incident_id ON services.route_history (incident_id);
CREATE INDEX IF NOT EXISTS idx_action_log_org_id ON services.infrastructure_action_log (org_id);
CREATE INDEX IF NOT EXISTS idx_action_log_route_id ON services.infrastructure_action_log (route_id);

