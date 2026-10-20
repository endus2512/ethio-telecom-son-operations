# Ethio Telecom SON Operations Platform

PostgreSQL-backed Streamlit and FastAPI service for tower inventory, PM/FM
ingestion, anomaly analytics, controlled SON configuration workflows, and
operational audit trails.

## Important production boundary

This repository provides an **integration-ready control-plane foundation**, not
a certified or pre-connected Ethio Telecom production deployment. It has no
access to Ethio Telecom credentials, network topology, OSS/NMS, supported
NETCONF capabilities, vendor YANG modules, nor approved PM/FM schemas. Live
network execution is disabled by default and must remain disabled until the
operator has supplied and validated:

- Vendor and software-release-specific YANG edit payloads and action
  constraints.
- An approved device inventory, routed management network, host keys, and
  restricted NETCONF service account.
- NMS/PM/FM delivery agreements, timestamp semantics, and source mappings.
- An enterprise identity/secret-management and external TLS/reverse-proxy
  deployment.
- Lab, canary, maintenance-window, rollback, and NOC change-approval evidence.

The application never sends arbitrary client-provided XML or commands. Only
server-configured YANG templates and two narrowly defined action families can
reach NETCONF. Requests require distinct requestor, NOC approver, system
administrator executor, and NOC confirmed-commit verifier accounts. NETCONF
uses SSH host-key verification, a configured IP/CIDR allowlist, candidate
configuration validation, and confirmed commits that automatically revert if
not confirmed. These guards reduce risk; they do not replace vendor testing,
network engineering review, or operational change controls.

## Architecture

```text
Streamlit UI :8501
      │ short-lived JWT / RBAC
FastAPI API :8000 ─── PM/FM ingest endpoints
      │                       │
      ├── PostgreSQL 16       └── NMS/collector integration
      ├── Isolation Forest + Random Forest (process memory)
      └── restricted NETCONF/YANG driver ─── approved cell sites
```

The Docker stack runs separate frontend, backend, and database services.
PostgreSQL stores tower inventory/configuration, KPI observations, fault events,
users, optimization requests, approvals, and an audit trail. There is no CSV or
Joblib persistence. The in-process ML cache is rebuilt from PostgreSQL after
restart or telemetry change; the backend is intentionally configured with one
worker to keep that cache coherent.

## Files

```text
ethio_telecom_son/
├── app.py                    # Streamlit frontend
├── src/
│   ├── api.py                # FastAPI API, ingestion, RBAC and action workflow
│   ├── cli.py                # Secure initial-user administration
│   ├── db.py                 # PostgreSQL connection/session lifecycle
│   ├── dbmodels.py           # SQLAlchemy persistence models
│   ├── ingestion.py          # JSON / CSV / XML PM/FM parsers
│   ├── migrate.py            # Ordered SQL migration runner
│   ├── ml_engine.py          # In-memory anomaly and forecast models
│   ├── network_driver.py     # Guarded NETCONF confirmed-commit driver
│   ├── production_ui.py      # Authenticated operational dashboard
│   ├── security.py           # Argon2, JWT, RBAC
│   └── settings.py           # Environment configuration
├── migrations/
│   └── 001_initial.sql
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## Quick local startup (no Docker)

This is the easiest way to run the complete dashboard on a development
machine. It starts the API and browser UI, creates a local SQLite database,
prompts you to set the first administrator password, and adds clearly labeled
synthetic KPI/fault data so analytics and dashboard views work immediately.

```bash
cd ethio_telecom_son
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install -r requirements-dev.txt
bash run_local.sh
```

Open `http://127.0.0.1:8501` and sign in with the administrator credentials
you created. The API readiness endpoint is `http://127.0.0.1:8000/health/ready`.
Press Ctrl+C in the terminal to stop both services. On first start the script
creates a private `.env.local` with fresh signing and ingestion secrets, plus
`son-local.db`; both are excluded from Git. Later starts reuse the same database
and administrator. Demo telemetry is seeded only when the database has no KPI
observations, and is never mixed into a database that already has telemetry.
Local configuration and database files are created with owner-only permissions.

SQLite and seeded synthetic data are for local evaluation only. Use the
PostgreSQL Docker setup below for deployment; the NETCONF control path remains
disabled unless explicitly configured and operationally validated.

## Local Docker startup

Docker Compose requires strong, unique secrets. From this directory:

```bash
cp .env.example .env
printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 32)" >> .env
printf 'SON_JWT_SECRET=%s\n' "$(openssl rand -hex 32)" >> .env
printf 'SON_INGESTION_API_TOKEN=%s\n' "$(openssl rand -hex 32)" >> .env
docker compose up --build -d
docker compose ps
```

The API is mapped to loopback port 8000; the UI is at
`http://localhost:8501`. PostgreSQL and its data volume are not exposed on the
host. The backend runs SQL migrations before starting. Before deploying, replace
the example secrets with unique production values stored in the deployment
platform's secret manager; never commit `.env`, `.env.local`, local databases,
or a real NETCONF known-hosts file. Keep NETCONF disabled unless vendor models,
device allowlists, lab validation, and network change controls are approved.

This Compose file deliberately publishes both ports only on host loopback. For
remote access, configure a host-level TLS reverse proxy to the UI, protect it
with the deployment's access controls, and expose the API only to trusted
clients or the private network. Do not publish the unencrypted API or Streamlit
port directly to the public internet. Use managed or hardened PostgreSQL with
tested backups/PITR. Domain, TLS certificate, proxy/access policy, secrets,
database backup/restore, and production NMS/vendor configuration must be
provided by the deployment owner; they cannot be safely generated by this repo.
Pin container images to organization-approved digests in the release pipeline.

GitHub Actions checks run the unit suite, Python compilation, startup script
syntax, Docker Compose validation, and backend/frontend image builds for pushes
and pull requests affecting this project.

Run the focused regression tests using the optional test dependencies:

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

Create the first administrator interactively so the password is not stored in
shell history:

```bash
docker compose exec backend python -m src.cli create-user \
  --username son-admin --role system_admin
```

Administrators create other users in the dashboard or the secured user API.
Login access tokens expire after 20 minutes. Accounts are locked for 15 minutes
after five consecutive failed logins. A disabled user is rejected on every API
request, even with an otherwise unexpired token.

## PM/FM ingestion

The `/v1/ingest/pm` and `/v1/ingest/fm` endpoints accept `json`, `csv`, or `xml`
using `?format=json|csv|xml`. Authenticate using an authorized bearer token or
the dedicated `X-Ingestion-Key` header configured as `SON_INGESTION_API_TOKEN`.
Deliveries are limited to 20 MiB and 10,000 records.

Normalized PM records require these fields:

```json
{
  "records": [{
    "tower_id": "ET-SON-001",
    "region": "Addis Ababa",
    "timestamp": "2026-10-19T18:30:00Z",
    "connected_users": 450,
    "prb_usage_pct": 72.5,
    "throughput_mbps": 95.2,
    "latency_ms": 58.1,
    "call_drop_rate_pct": 1.2,
    "handover_failure_rate_pct": 0.8,
    "hardware_status": "Optimal"
  }]
}
```

FM records require `tower_id`, `timestamp`, `alarm_code`, `severity`, and
`description`; optional fields are `external_id` and `cleared_at`. XML input
accepts record elements named `measurement`, `pmrecord`, `fmrecord`, `alarm`, or
`record`. CSV uses those same normalized field names. Aliases for common PM/FM
field names are normalized in `src/ingestion.py`; records missing required FM
fields are rejected with HTTP 422. Load-balancing requests are accepted only
when every neighbor is a different, enabled tower in the inventory. Vendor-specific
performance counter formulas, XML nesting, and alarm semantics must be mapped
at the network's feed adapter before delivery. Timestamp values must have an
explicit timezone. Duplicate PM tower/time observations are upserted; repeated
FM external IDs are ignored.

## RBAC and operations

| Role | View | Request network change | Approve/reject | Execute NETCONF | Confirm commit | User/inventory/audit admin |
|---|---:|---:|---:|---:|---:|---:|
| Network Engineer | Yes | Yes | No | No | No | No |
| NOC Supervisor | Yes | Yes | Yes | No | Yes | No |
| System Admin | Yes | Yes | No | Yes | No | Yes |

All action and identity changes are recorded in PostgreSQL `audit_logs`.
Network actions are deliberately separated into request, independent approval,
independent execution, and independent confirmation. No endpoint exposes a
generic shell, RPC, raw XML, or arbitrary command interface.

## NETCONF/YANG enablement

The default `SON_NETCONF_ENABLED=false` blocks live commands. Do not enable it
with an empty or untested configuration. Before a lab integration:

1. Provision each tower with a static management IP, TCP port, and YANG profile
   through the administrator inventory. Device IPs must be literal IP
   addresses, not DNS names.
2. Add verified SSH server host keys to a protected `known_hosts` file and set
   `NETCONF_KNOWN_HOSTS_FILE` to its host path. The container mounts it
   read-only as `/run/son/known_hosts`.
3. Configure a dedicated low-privilege NETCONF service account, the allowed
   management CIDRs, and secret values in the deployment secret manager:
   `SON_NETCONF_USERNAME`, `SON_NETCONF_PASSWORD`,
   `SON_ALLOWED_DEVICE_CIDRS`, and `SON_NETCONF_KNOWN_HOSTS`.
4. Provide `SON_NETCONF_ACTION_TEMPLATES_JSON`, mapping
   `<yang_profile>:ret_adjustment` and/or
   `<yang_profile>:load_balancing` to XML `<config>` payloads from the
   **vendor/release's approved YANG model**. Supported template substitutions
   are `__TOWER_ID__`, `__RET_TILT_DEGREES__`,
   `__NEIGHBOR_CELL_IDS__`, and `__OFFLOAD_PERCENT__`. Templates are
   server-side configuration, never request data. Validate namespaces, leaf
   paths, ranges, neighbor eligibility, and commit behavior against vendor
   documentation and a lab device before enabling the feature.
5. Set `SON_NETCONF_ENABLED=true` only for the approved deployment after
   change-control, rollback, and monitoring validation.

The generic client validates and locks candidate configuration, sends an edit,
validates the candidate, and requests a confirmed commit. A separate NOC user
must verify the device and explicitly confirm before the configured timer
expires; absent confirmation, the device should revert. Device implementations
may vary, so test this behavior for every supported vendor/release before use.

## REST endpoints

- `GET /health/live`, `GET /health/ready`
- `POST /v1/auth/login`, `POST /v1/users`,
  `PATCH /v1/users/{username}/disable`
- `GET/POST/PATCH /v1/towers`
- `POST /v1/ingest/pm`, `POST /v1/ingest/fm`
- `GET /v1/kpis/latest`, `GET /v1/faults`, `GET /v1/analytics`
- `GET/POST /v1/actions`,
  `POST /v1/actions/{id}/approve|reject|execute|confirm`
- `GET /v1/audit` (system admin)

The API's interactive OpenAPI documentation is disabled. Configure an
authenticated internal API reference as part of the production deployment.

## Operations and limitations

- Back up PostgreSQL and rehearse restore and point-in-time recovery. Audit rows
  are append-only by application design; production database permissions and
  retention controls should enforce immutability.
- Add deployment-specific request-rate limiting, TLS, centralized logs/metrics,
  database HA, secret rotation, vulnerability scanning, and incident alerts.
- The sample Compose file is a single-host deployment starter, **not a
  high-availability, certified production topology**.
- The in-memory ML cache retrains when input history changes. Production scale
  should use a separately operated model-serving/training component and
  validated time-series partitions rather than increasing API workers without
  designing model consistency.
- The frontend reports only persisted live PM/FM data. It no longer generates
  synthetic KPI data or writes local model/data files.
