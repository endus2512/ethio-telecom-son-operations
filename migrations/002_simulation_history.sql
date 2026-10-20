CREATE TABLE IF NOT EXISTS simulated_kpi_history (
    tower_id VARCHAR(64) NOT NULL REFERENCES towers(tower_id) ON DELETE CASCADE,
    timestamp TIMESTAMPTZ NOT NULL,
    connected_users INTEGER NOT NULL CHECK (connected_users >= 0),
    prb_usage_pct DOUBLE PRECISION NOT NULL CHECK (prb_usage_pct BETWEEN 0 AND 100),
    throughput_mbps DOUBLE PRECISION NOT NULL CHECK (throughput_mbps >= 0),
    latency_ms DOUBLE PRECISION NOT NULL CHECK (latency_ms >= 0),
    call_drop_rate_pct DOUBLE PRECISION NOT NULL CHECK (call_drop_rate_pct BETWEEN 0 AND 100),
    hardware_status VARCHAR(32) NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tower_id, timestamp)
);
CREATE INDEX IF NOT EXISTS ix_sim_kpi_timestamp ON simulated_kpi_history (timestamp);

CREATE TABLE IF NOT EXISTS simulation_anomalies (
    id UUID PRIMARY KEY,
    tower_id VARCHAR(64) NOT NULL REFERENCES towers(tower_id) ON DELETE CASCADE,
    detected_at TIMESTAMPTZ NOT NULL,
    resolved_at TIMESTAMPTZ,
    severity VARCHAR(32) NOT NULL,
    detail TEXT NOT NULL,
    source VARCHAR(32) NOT NULL DEFAULT 'simulated'
);
CREATE INDEX IF NOT EXISTS ix_sim_anomaly_detected ON simulation_anomalies (detected_at);
CREATE INDEX IF NOT EXISTS ix_sim_anomaly_tower_open
    ON simulation_anomalies (tower_id, resolved_at);

CREATE TABLE IF NOT EXISTS simulated_control_actions (
    id UUID PRIMARY KEY,
    tower_id VARCHAR(64) NOT NULL REFERENCES towers(tower_id) ON DELETE CASCADE,
    actor VARCHAR(100) NOT NULL,
    action_type VARCHAR(64) NOT NULL,
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(32) NOT NULL,
    execution_log TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_sim_control_created ON simulated_control_actions (created_at);
CREATE INDEX IF NOT EXISTS ix_sim_control_tower_created
    ON simulated_control_actions (tower_id, created_at);
