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
    capabilities jsonb default '[]'::jsonb,
    default_state varchar(100),
    safety_constraints jsonb default '{}'::jsonb,
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
    infrastructure_recommendations jsonb default '[]'::jsonb,
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
