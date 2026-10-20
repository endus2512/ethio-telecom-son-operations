"""Authenticated REST API for network inventory, ingestion, and SON workflows."""

from __future__ import annotations

import hmac
import hashlib
import math
import re
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db import get_db
from src.dbmodels import (
    AuditLog,
    FaultEvent,
    KpiMetric,
    OptimizationAction,
    SimulatedControlAction,
    SimulatedKpiHistory,
    SimulationAnomaly,
    Tower,
    User,
)
from src.ingestion import parse_feed
from src.ml_engine import NetworkMLEngine
from src.network_driver import (
    NetconfDriver,
    NetworkDriverError,
    SimulatedControlDriver,
)
from src.security import (
    CurrentPrincipal,
    Role,
    create_access_token,
    get_current_principal,
    hash_password,
    require_roles,
    verify_password,
)
from src.settings import settings

app = FastAPI(
    title="Ethio Telecom SON Operations API",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",")],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Authorization", "Content-Type", "X-Ingestion-Key"],
)
_model_cache: tuple[str, NetworkMLEngine] | None = None
_model_cache_lock = RLock()
db_dependency = Depends(get_db)
engineer_dependency = Depends(
    require_roles(Role.ENGINEER, Role.SUPERVISOR, Role.ADMIN)
)
supervisor_dependency = Depends(require_roles(Role.SUPERVISOR))
admin_dependency = Depends(require_roles(Role.ADMIN))
simulation_control_dependency = Depends(require_roles(Role.SUPERVISOR, Role.ADMIN))


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=12, max_length=256)
    role: Role


class TowerCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tower_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$",
    )
    region: str = Field(min_length=1, max_length=128)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    netconf_host: str | None = Field(default=None, max_length=255)
    netconf_port: int = Field(default=830, ge=1, le=65535)
    yang_profile: str | None = Field(default=None, max_length=128)
    enabled: bool = True


class ActionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tower_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$",
    )
    action_type: Literal["ret_adjustment", "load_balancing"]
    parameters: dict[str, Any]

    @model_validator(mode="after")
    def validate_parameters(self) -> "ActionCreateRequest":
        """Reject malformed or out-of-bounds configuration requests at submission."""
        if self.action_type == "ret_adjustment":
            if set(self.parameters) != {"tilt_degrees"}:
                raise ValueError("RET action requires only tilt_degrees.")
            try:
                tilt = float(self.parameters["tilt_degrees"])
            except (TypeError, ValueError) as exc:
                raise ValueError("tilt_degrees must be numeric.") from exc
            if not math.isfinite(tilt) or not 0 <= tilt <= 15:
                raise ValueError("tilt_degrees must be between 0 and 15 degrees.")
        else:
            if set(self.parameters) != {"neighbor_cell_ids", "offload_percent"}:
                raise ValueError(
                    "Load balancing requires neighbor_cell_ids and offload_percent."
                )
            neighbors = self.parameters["neighbor_cell_ids"]
            if (
                not isinstance(neighbors, list)
                or not 1 <= len(neighbors) <= 8
                or any(
                    not isinstance(neighbor, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", neighbor)
                    for neighbor in neighbors
                )
                or len(set(neighbors)) != len(neighbors)
            ):
                raise ValueError("Provide 1-8 unique, valid neighbor cell IDs.")
            try:
                offload = float(self.parameters["offload_percent"])
            except (TypeError, ValueError) as exc:
                raise ValueError("offload_percent must be numeric.") from exc
            if not math.isfinite(offload) or not 1 <= offload <= 40:
                raise ValueError("offload_percent must be between 1 and 40.")
        return self


class TowerUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    region: str | None = Field(default=None, min_length=1, max_length=128)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    netconf_host: str | None = Field(default=None, max_length=255)
    netconf_port: int | None = Field(default=None, ge=1, le=65535)
    yang_profile: str | None = Field(default=None, max_length=128)
    enabled: bool | None = None


class SimulatedTelemetryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tower_id: str = Field(pattern=r"^SIM-ADD-00[1-6]$")
    region: Literal["Addis Ababa (simulated)"]
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    timestamp: datetime
    connected_users: int = Field(ge=0, le=1_000_000)
    prb_usage_pct: float = Field(ge=0, le=100, allow_inf_nan=False)
    throughput_mbps: float = Field(ge=0, le=100_000, allow_inf_nan=False)
    latency_ms: float = Field(ge=0, le=60_000, allow_inf_nan=False)
    call_drop_rate_pct: float = Field(ge=0, le=100, allow_inf_nan=False)
    hardware_status: Literal["Optimal", "Warning", "Critical"]

    @model_validator(mode="after")
    def require_timezone(self) -> "SimulatedTelemetryRecord":
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("Simulated telemetry timestamps must include a timezone.")
        return self


class SimulatedTelemetryBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    records: list[SimulatedTelemetryRecord] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def reject_duplicate_cells(self) -> "SimulatedTelemetryBatch":
        cell_ids = [record.tower_id for record in self.records]
        if len(cell_ids) != len(set(cell_ids)):
            raise ValueError("A simulation batch may contain each cell only once.")
        return self


class SimulatedActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cell_id: str = Field(pattern=r"^SIM-ADD-00[1-6]$")


def audit(
    db: Session,
    actor: str,
    event_type: str,
    resource_type: str,
    resource_id: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Add an immutable operational event to the audit log."""
    db.add(
        AuditLog(
            actor=actor,
            event_type=event_type,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details or {},
        )
    )


def _insert_for_database(db: Session, table: Any) -> Any:
    """Build an upsert statement for either supported database dialect."""
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        return pg_insert(table)
    if dialect == "sqlite":
        return sqlite_insert(table)
    raise RuntimeError(f"Unsupported database dialect: {dialect}")


def parse_timestamp(value: Any, field_name: str) -> datetime:
    """Require an ISO 8601 timestamp with an explicit timezone."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"{field_name} must be an ISO 8601 timestamp."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must include a UTC offset or Z timezone.",
        )
    return parsed.astimezone(timezone.utc)


async def read_bounded_body(request: Request, maximum_bytes: int = 20 * 1024 * 1024) -> bytes:
    """Read an incoming feed incrementally and fail before buffering oversized bodies."""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > maximum_bytes:
                raise HTTPException(status_code=413, detail="Feed exceeds the 20 MiB limit.")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header.") from exc
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > maximum_bytes:
            raise HTTPException(status_code=413, detail="Feed exceeds the 20 MiB limit.")
        chunks.append(chunk)
    return b"".join(chunks)


def require_ingestion_identity(
    authorization: Annotated[str | None, Header()] = None,
    x_ingestion_key: Annotated[str | None, Header()] = None,
    db: Session = db_dependency,
) -> str:
    """Accept either a scoped engineer token or the dedicated ingestion key."""
    if settings.ingestion_api_token and x_ingestion_key and hmac.compare_digest(
        x_ingestion_key, settings.ingestion_api_token.get_secret_value()
    ):
        return "nms-ingestion-service"
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Ingestion authentication required.")
    token = authorization.split(" ", 1)[1]
    import jwt

    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            issuer=settings.jwt_issuer,
            options={"require": ["sub", "role", "exp", "iss"]},
        )
        role = Role(payload["role"])
        user = db.scalar(select(User).where(User.username == payload["sub"]))
        if user is None or not user.enabled or user.role != role.value:
            raise ValueError("Inactive user.")
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid ingestion identity.") from exc
    principal = CurrentPrincipal(user.username, role)
    if principal.role not in {Role.ENGINEER, Role.SUPERVISOR, Role.ADMIN}:
        raise HTTPException(status_code=403, detail="Role cannot ingest network feeds.")
    return principal.username


def _get_action(db: Session, action_id: UUID, lock: bool = False) -> OptimizationAction:
    query = select(OptimizationAction).where(OptimizationAction.id == action_id)
    if lock:
        query = query.with_for_update()
    action = db.scalar(query)
    if action is None:
        raise HTTPException(status_code=404, detail="Optimization action not found.")
    return action


@app.get("/health/live")
def liveness() -> dict[str, str]:
    """Report process liveness without exposing dependencies or configuration."""
    return {"status": "alive"}


@app.get("/health/ready")
def readiness(db: Session = db_dependency) -> dict[str, str]:
    """Report readiness only when PostgreSQL is reachable."""
    try:
        db.execute(select(1))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database is not ready.") from exc
    return {"status": "ready"}


@app.post("/v1/auth/login")
def login(payload: LoginRequest, db: Session = db_dependency) -> dict[str, Any]:
    """Authenticate a user and issue a short-lived role-bearing token."""
    user = db.scalar(
        select(User).where(User.username == payload.username).with_for_update()
    )
    now = datetime.now(timezone.utc)
    locked_until = user.locked_until if user is not None else None
    if locked_until is not None and locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    if (
        user is None
        or not user.enabled
        or (locked_until is not None and locked_until > now)
        or not verify_password(payload.password, user.password_hash)
    ):
        if user is not None and user.enabled:
            user.failed_login_attempts += 1
            if user.failed_login_attempts >= 5:
                user.locked_until = now + timedelta(minutes=15)
                user.failed_login_attempts = 0
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    user.failed_login_attempts = 0
    user.locked_until = None
    audit(db, user.username, "auth.login", "user", user.username)
    db.commit()
    return {
        "access_token": create_access_token(user.username, Role(user.role)),
        "token_type": "bearer",
        "expires_in": settings.access_token_minutes * 60,
        "role": user.role,
    }


@app.post("/v1/users", status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreateRequest,
    principal: Annotated[CurrentPrincipal, admin_dependency],
    db: Session = db_dependency,
) -> dict[str, str]:
    """Create an account; role assignment is restricted to system administrators."""
    if db.scalar(select(User.id).where(User.username == payload.username)):
        raise HTTPException(status_code=409, detail="Username already exists.")
    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        role=payload.role.value,
    )
    db.add(user)
    audit(db, principal.username, "user.created", "user", payload.username, {"role": payload.role.value})
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Username already exists.") from exc
    return {"username": payload.username, "role": payload.role.value}


@app.post("/v1/towers", status_code=status.HTTP_201_CREATED)
def create_tower(
    payload: TowerCreateRequest,
    principal: Annotated[CurrentPrincipal, admin_dependency],
    db: Session = db_dependency,
) -> dict[str, Any]:
    """Provision a tower inventory entry with an optional NETCONF endpoint."""
    if db.get(Tower, payload.tower_id):
        raise HTTPException(status_code=409, detail="Tower already exists.")
    tower = Tower(**payload.model_dump())
    db.add(tower)
    audit(db, principal.username, "tower.created", "tower", tower.tower_id)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Tower already exists.") from exc
    return {"tower_id": tower.tower_id, "region": tower.region, "enabled": tower.enabled}


@app.patch("/v1/towers/{tower_id}")
def update_tower(
    tower_id: str,
    payload: TowerUpdateRequest,
    principal: Annotated[CurrentPrincipal, admin_dependency],
    db: Session = db_dependency,
) -> dict[str, Any]:
    """Update inventory/configuration metadata under administrator authorization."""
    tower = db.get(Tower, tower_id)
    if tower is None:
        raise HTTPException(status_code=404, detail="Tower inventory entry not found.")
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="No tower fields were provided.")
    if "region" in updates and updates["region"] is None:
        raise HTTPException(status_code=422, detail="Tower region cannot be null.")
    for key, value in updates.items():
        setattr(tower, key, value)
    audit(db, principal.username, "tower.updated", "tower", tower_id, updates)
    db.commit()
    return {"tower_id": tower_id, "enabled": tower.enabled}


@app.patch("/v1/users/{username}/disable")
def disable_user(
    username: str,
    principal: Annotated[CurrentPrincipal, admin_dependency],
    db: Session = db_dependency,
) -> dict[str, str]:
    """Disable an account immediately and invalidate its future token use."""
    if username == principal.username:
        raise HTTPException(status_code=409, detail="Administrators cannot disable themselves.")
    user = db.scalar(select(User).where(User.username == username).with_for_update())
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    user.enabled = False
    audit(db, principal.username, "user.disabled", "user", username)
    db.commit()
    return {"username": username, "status": "disabled"}


@app.get("/v1/towers")
def list_towers(
    principal: Annotated[CurrentPrincipal, engineer_dependency],
    db: Session = db_dependency,
) -> list[dict[str, Any]]:
    """List tower inventory without exposing network credentials."""
    towers = db.scalars(select(Tower).order_by(Tower.region, Tower.tower_id)).all()
    return [
        {
            "tower_id": tower.tower_id,
            "region": tower.region,
            "latitude": tower.latitude,
            "longitude": tower.longitude,
            "netconf_configured": bool(tower.netconf_host and tower.yang_profile),
            "enabled": tower.enabled,
            "hardware_status": tower.hardware_status,
        }
        for tower in towers
    ]


@app.post("/v1/ingest/pm")
async def ingest_pm(
    request: Request,
    feed_format: Literal["json", "csv", "xml"] = Query(alias="format"),
    actor: str = Depends(require_ingestion_identity),
    db: Session = db_dependency,
) -> dict[str, int]:
    """Parse a PM delivery and upsert KPI observations."""
    try:
        records = parse_feed(await read_bounded_body(request), "pm", feed_format)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not records or len(records) > 10000:
        raise HTTPException(status_code=422, detail="Feed must contain 1 to 10,000 records.")
    for record in records:
        try:
            tower_id = str(record["tower_id"])
            if not 1 <= len(tower_id) <= 64:
                raise ValueError("Invalid tower_id.")
            timestamp = parse_timestamp(record["timestamp"], "timestamp")
            tower_insert = _insert_for_database(db, Tower).values(
                tower_id=tower_id,
                region=str(record.get("region") or "Unknown")[:128],
                hardware_status=str(record.get("hardware_status") or "unknown")[:32],
            )
            db.execute(
                tower_insert.on_conflict_do_update(
                    index_elements=["tower_id"],
                    set_={"hardware_status": tower_insert.excluded.hardware_status},
                )
            )
            values = {
                "tower_id": tower_id,
                "timestamp": timestamp,
                "connected_users": int(record["connected_users"]),
                "prb_usage_pct": float(record["prb_usage_pct"]),
                "throughput_mbps": float(record["throughput_mbps"]),
                "latency_ms": float(record["latency_ms"]),
                "call_drop_rate_pct": float(record["call_drop_rate_pct"]),
                "handover_failure_rate_pct": float(record["handover_failure_rate_pct"]),
                "hardware_status": str(record.get("hardware_status") or "unknown")[:32],
                "source": "nms-pm",
            }
            if (
                values["connected_users"] < 0
                or not 0 <= values["prb_usage_pct"] <= 100
                or not 0 <= values["call_drop_rate_pct"] <= 100
                or not 0 <= values["handover_failure_rate_pct"] <= 100
                or any(not math.isfinite(float(values[key])) for key in (
                    "prb_usage_pct",
                    "throughput_mbps",
                    "latency_ms",
                    "call_drop_rate_pct",
                    "handover_failure_rate_pct",
                ))
                or min(
                    values["throughput_mbps"],
                    values["latency_ms"],
                    values["call_drop_rate_pct"],
                    values["handover_failure_rate_pct"],
                )
                < 0
            ):
                raise ValueError("PM KPIs are outside allowed numeric ranges.")
            statement = _insert_for_database(db, KpiMetric).values(**values)
            db.execute(
                statement.on_conflict_do_update(
                    index_elements=["tower_id", "timestamp"],
                    set_={
                        key: value
                        for key, value in values.items()
                        if key not in {"tower_id", "timestamp"}
                    },
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            db.rollback()
            raise HTTPException(
                status_code=422, detail=f"Invalid PM feed record: {exc}"
            ) from exc
    audit(db, actor, "pm.ingested", "feed", "pm", {"records": len(records), "format": feed_format})
    db.commit()
    return {"accepted": len(records)}


@app.post("/v1/ingest/fm")
async def ingest_fm(
    request: Request,
    feed_format: Literal["json", "csv", "xml"] = Query(alias="format"),
    actor: str = Depends(require_ingestion_identity),
    db: Session = db_dependency,
) -> dict[str, int]:
    """Persist fault-management alarms and preserve their source payloads."""
    try:
        records = parse_feed(await read_bounded_body(request), "fm", feed_format)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not records or len(records) > 10000:
        raise HTTPException(status_code=422, detail="Feed must contain 1 to 10,000 records.")
    accepted = 0
    for record in records:
        tower_id = str(record["tower_id"])
        if db.get(Tower, tower_id) is None:
            raise HTTPException(status_code=422, detail=f"Unknown tower_id {tower_id}.")
        occurred_at = parse_timestamp(record["timestamp"], "timestamp")
        external_id = str(record["external_id"])[:255] if record.get("external_id") else None
        event = FaultEvent(
            external_id=external_id,
            tower_id=tower_id,
            occurred_at=occurred_at,
            cleared_at=(
                parse_timestamp(record["cleared_at"], "cleared_at")
                if record.get("cleared_at")
                else None
            ),
            severity=str(record.get("severity") or "unknown")[:32],
            alarm_code=str(record.get("alarm_code") or "unspecified")[:128],
            description=str(record.get("description") or "")[:4000],
            raw_payload={
                key: str(record[key])[:255]
                for key in ("external_id", "alarm_code", "severity")
                if record.get(key) not in (None, "")
            },
            source="nms-fm",
        )
        if external_id:
            statement = _insert_for_database(db, FaultEvent).values(
                external_id=event.external_id,
                tower_id=event.tower_id,
                occurred_at=event.occurred_at,
                cleared_at=event.cleared_at,
                severity=event.severity,
                alarm_code=event.alarm_code,
                description=event.description,
                raw_payload=event.raw_payload,
                source=event.source,
            )
            db.execute(
                statement.on_conflict_do_update(
                    index_elements=["external_id"],
                    set_={
                        "cleared_at": func.coalesce(
                            statement.excluded.cleared_at,
                            FaultEvent.cleared_at,
                        ),
                        "severity": statement.excluded.severity,
                        "description": statement.excluded.description,
                    },
                )
            )
        else:
            db.add(event)
        accepted += 1
    audit(db, actor, "fm.ingested", "feed", "fm", {"records": accepted, "format": feed_format})
    db.commit()
    return {"accepted": accepted}


@app.get("/v1/kpis/latest")
def latest_kpis(
    principal: Annotated[CurrentPrincipal, engineer_dependency],
    limit: int = Query(default=500, ge=1, le=5000),
    db: Session = db_dependency,
) -> list[dict[str, Any]]:
    """Return each tower's newest KPI observation."""
    latest = (
        select(KpiMetric.tower_id, func.max(KpiMetric.timestamp).label("latest"))
        .group_by(KpiMetric.tower_id)
        .subquery()
    )
    rows = db.execute(
        select(KpiMetric, Tower.region, Tower.latitude, Tower.longitude)
        .join(latest, (latest.c.tower_id == KpiMetric.tower_id) & (latest.c.latest == KpiMetric.timestamp))
        .join(Tower, Tower.tower_id == KpiMetric.tower_id)
        .order_by(KpiMetric.timestamp.desc(), KpiMetric.tower_id)
        .limit(limit)
    ).all()
    return [
        {
            "timestamp": metric.timestamp.isoformat(),
            "tower_id": metric.tower_id,
            "region": region,
            "latitude": latitude,
            "longitude": longitude,
            "connected_users": metric.connected_users,
            "prb_usage_pct": metric.prb_usage_pct,
            "throughput_mbps": metric.throughput_mbps,
            "latency_ms": metric.latency_ms,
            "call_drop_rate_pct": metric.call_drop_rate_pct,
            "handover_failure_rate_pct": metric.handover_failure_rate_pct,
            "hardware_status": metric.hardware_status,
            "source": metric.source,
        }
        for metric, region, latitude, longitude in rows
    ]


@app.post("/v1/simulation/telemetry")
def persist_simulated_telemetry(
    payload: SimulatedTelemetryBatch,
    principal: Annotated[CurrentPrincipal, engineer_dependency],
    db: Session = db_dependency,
) -> dict[str, int]:
    """Persist explicitly synthetic KPI samples and track anomaly state transitions."""
    accepted = 0
    for record in payload.records:
        tower = db.get(Tower, record.tower_id)
        if tower is None:
            tower = Tower(
                tower_id=record.tower_id,
                region=record.region,
                latitude=record.latitude,
                longitude=record.longitude,
                hardware_status=record.hardware_status,
                enabled=True,
            )
            db.add(tower)
            db.flush()
        else:
            if tower.netconf_host or tower.yang_profile:
                raise HTTPException(
                    status_code=409,
                    detail="Simulation IDs cannot be associated with a configured network device.",
                )
            tower.region = record.region
            tower.latitude = record.latitude
            tower.longitude = record.longitude
            tower.hardware_status = record.hardware_status

        metric = SimulatedKpiHistory(
            tower_id=record.tower_id,
            timestamp=record.timestamp,
            connected_users=record.connected_users,
            prb_usage_pct=record.prb_usage_pct,
            throughput_mbps=record.throughput_mbps,
            latency_ms=record.latency_ms,
            call_drop_rate_pct=record.call_drop_rate_pct,
            hardware_status=record.hardware_status,
        )
        statement = _insert_for_database(db, SimulatedKpiHistory).values(
            tower_id=metric.tower_id,
            timestamp=metric.timestamp,
            connected_users=metric.connected_users,
            prb_usage_pct=metric.prb_usage_pct,
            throughput_mbps=metric.throughput_mbps,
            latency_ms=metric.latency_ms,
            call_drop_rate_pct=metric.call_drop_rate_pct,
            hardware_status=metric.hardware_status,
        )
        db.execute(
            statement.on_conflict_do_update(
                index_elements=["tower_id", "timestamp"],
                set_={
                    "connected_users": statement.excluded.connected_users,
                    "prb_usage_pct": statement.excluded.prb_usage_pct,
                    "throughput_mbps": statement.excluded.throughput_mbps,
                    "latency_ms": statement.excluded.latency_ms,
                    "call_drop_rate_pct": statement.excluded.call_drop_rate_pct,
                    "hardware_status": statement.excluded.hardware_status,
                },
            )
        )

        open_anomaly = db.scalar(
            select(SimulationAnomaly)
            .where(
                SimulationAnomaly.tower_id == record.tower_id,
                SimulationAnomaly.resolved_at.is_(None),
            )
            .order_by(SimulationAnomaly.detected_at.desc())
            .limit(1)
        )
        if record.hardware_status in {"Warning", "Critical"}:
            detail = (
                f"Traffic {record.prb_usage_pct:.1f}%, "
                f"drop rate {record.call_drop_rate_pct:.2f}%, "
                f"latency {record.latency_ms:.1f} ms."
            )
            if open_anomaly is None:
                db.add(
                    SimulationAnomaly(
                        tower_id=record.tower_id,
                        detected_at=record.timestamp,
                        severity=record.hardware_status.lower(),
                        detail=detail,
                    )
                )
            else:
                open_anomaly.severity = record.hardware_status.lower()
                open_anomaly.detail = detail
        elif open_anomaly is not None:
            open_anomaly.resolved_at = record.timestamp
        accepted += 1

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="Simulated telemetry could not be persisted."
        ) from exc
    return {"accepted": accepted}


@app.get("/v1/simulation/latest")
def latest_simulated_telemetry(
    principal: Annotated[CurrentPrincipal, engineer_dependency],
    db: Session = db_dependency,
) -> list[dict[str, Any]]:
    """Return the newest persisted reading for each synthetic cell."""
    latest = (
        select(
            SimulatedKpiHistory.tower_id,
            func.max(SimulatedKpiHistory.timestamp).label("latest"),
        )
        .group_by(SimulatedKpiHistory.tower_id)
        .subquery()
    )
    rows = db.execute(
        select(SimulatedKpiHistory, Tower.region, Tower.latitude, Tower.longitude)
        .join(
            latest,
            (latest.c.tower_id == SimulatedKpiHistory.tower_id)
            & (latest.c.latest == SimulatedKpiHistory.timestamp),
        )
        .join(Tower, Tower.tower_id == SimulatedKpiHistory.tower_id)
        .order_by(SimulatedKpiHistory.tower_id)
    ).all()
    return [
        {
            "tower_id": metric.tower_id,
            "region": region,
            "latitude": latitude,
            "longitude": longitude,
            "timestamp": metric.timestamp.isoformat(),
            "connected_users": metric.connected_users,
            "prb_usage_pct": metric.prb_usage_pct,
            "throughput_mbps": metric.throughput_mbps,
            "latency_ms": metric.latency_ms,
            "call_drop_rate_pct": metric.call_drop_rate_pct,
            "hardware_status": metric.hardware_status,
            "source": "illustrative-synthetic",
        }
        for metric, region, latitude, longitude in rows
    ]


@app.post("/v1/simulation/actions")
def execute_simulated_action(
    payload: SimulatedActionRequest,
    principal: Annotated[CurrentPrincipal, simulation_control_dependency],
    db: Session = db_dependency,
) -> dict[str, Any]:
    """Persist a synthetic control result without opening a device connection."""
    tower = db.scalar(
        select(Tower).where(Tower.tower_id == payload.cell_id).with_for_update()
    )
    if tower is None or not payload.cell_id.startswith("SIM-ADD-"):
        raise HTTPException(status_code=404, detail="Simulated cell was not found.")
    if tower.netconf_host or tower.yang_profile:
        raise HTTPException(
            status_code=409,
            detail="Simulation control is unavailable for configured network devices.",
        )
    latest = db.scalar(
        select(SimulatedKpiHistory)
        .where(
            SimulatedKpiHistory.tower_id == payload.cell_id,
        )
        .order_by(SimulatedKpiHistory.timestamp.desc())
        .limit(1)
        .with_for_update()
    )
    if latest is None:
        raise HTTPException(status_code=404, detail="Simulated KPI data was not found.")
    if latest.hardware_status == "Optimal":
        raise HTTPException(status_code=409, detail="Cell is already within normal thresholds.")

    parameters = {"offload_percent": 15.0}
    try:
        driver_result = SimulatedControlDriver().execute_simulated(
            payload.cell_id, "load_balancing", parameters
        )
    except NetworkDriverError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    now = datetime.now(timezone.utc)
    updated_metric = SimulatedKpiHistory(
        tower_id=payload.cell_id,
        timestamp=now,
        connected_users=latest.connected_users,
        prb_usage_pct=58.0,
        throughput_mbps=round(max(95.0, latest.throughput_mbps * 1.8), 1),
        latency_ms=62.0,
        call_drop_rate_pct=0.7,
        hardware_status="Optimal",
    )
    tower.hardware_status = "Optimal"
    open_anomalies = db.scalars(
        select(SimulationAnomaly)
        .where(
            SimulationAnomaly.tower_id == payload.cell_id,
            SimulationAnomaly.resolved_at.is_(None),
        )
        .with_for_update()
    ).all()
    for anomaly in open_anomalies:
        anomaly.resolved_at = now
    action = SimulatedControlAction(
        tower_id=payload.cell_id,
        actor=principal.username,
        action_type="load_balancing",
        parameters=parameters,
        status=driver_result["status"],
        execution_log=driver_result["execution_log"],
    )
    db.add_all([updated_metric, action])
    audit(
        db,
        principal.username,
        "simulation.control.executed",
        "simulated_cell",
        payload.cell_id,
        {"action_type": "load_balancing", "status": driver_result["status"]},
    )
    db.commit()
    return {
        "cell_id": payload.cell_id,
        "status": driver_result["status"],
        "execution_log": driver_result["execution_log"],
        "simulation_only": True,
        "telemetry": {
            "timestamp": now.isoformat(),
            "connected_users": updated_metric.connected_users,
            "prb_usage_pct": updated_metric.prb_usage_pct,
            "throughput_mbps": updated_metric.throughput_mbps,
            "latency_ms": updated_metric.latency_ms,
            "call_drop_rate_pct": updated_metric.call_drop_rate_pct,
            "hardware_status": updated_metric.hardware_status,
            "source": "illustrative-synthetic",
            "optimized": True,
        },
    }


@app.get("/v1/simulation/history")
def simulated_history(
    principal: Annotated[CurrentPrincipal, engineer_dependency],
    days: int = Query(default=7, ge=1, le=30),
    db: Session = db_dependency,
) -> dict[str, Any]:
    """Return hourly synthetic KPI trends and anomaly/control resolution history."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    if db.get_bind().dialect.name == "sqlite":
        bucket = func.strftime(
            "%Y-%m-%dT%H:00:00", SimulatedKpiHistory.timestamp
        )
    else:
        bucket = func.date_trunc("hour", SimulatedKpiHistory.timestamp)
    trend_rows = db.execute(
        select(
            bucket.label("bucket"),
            func.avg(SimulatedKpiHistory.prb_usage_pct).label("traffic_load_pct"),
            func.avg(SimulatedKpiHistory.call_drop_rate_pct).label("drop_rate_pct"),
            func.avg(SimulatedKpiHistory.latency_ms).label("latency_ms"),
            func.avg(SimulatedKpiHistory.throughput_mbps).label("throughput_mbps"),
            func.count(func.distinct(SimulatedKpiHistory.tower_id)).label("cell_count"),
            func.count(SimulatedKpiHistory.timestamp).label("sample_count"),
            func.sum(case((SimulatedKpiHistory.hardware_status == "Critical", 1), else_=0)).label(
                "critical_count"
            ),
            func.sum(case((SimulatedKpiHistory.hardware_status == "Warning", 1), else_=0)).label(
                "warning_count"
            ),
        )
        .where(
            SimulatedKpiHistory.timestamp >= cutoff,
        )
        .group_by(bucket)
        .order_by(bucket)
        .limit(744)
    ).all()
    anomalies = db.scalars(
        select(SimulationAnomaly)
        .where(SimulationAnomaly.detected_at >= cutoff)
        .order_by(SimulationAnomaly.detected_at.desc())
        .limit(5000)
    ).all()
    actions = db.scalars(
        select(SimulatedControlAction)
        .where(SimulatedControlAction.created_at >= cutoff)
        .order_by(SimulatedControlAction.created_at.desc())
        .limit(1000)
    ).all()
    return {
        "days": days,
        "trends": [
            {
                "timestamp": (
                    row.bucket.isoformat()
                    if isinstance(row.bucket, datetime)
                    else row.bucket
                ),
                "traffic_load_pct": float(row.traffic_load_pct or 0),
                "drop_rate_pct": float(row.drop_rate_pct or 0),
                "latency_ms": float(row.latency_ms or 0),
                "throughput_mbps": float(row.throughput_mbps or 0),
                "cell_count": int(row.cell_count or 0),
                "sample_count": int(row.sample_count or 0),
                "critical_count": int(row.critical_count or 0),
                "warning_count": int(row.warning_count or 0),
            }
            for row in trend_rows
            if row.bucket is not None
        ],
        "anomalies": [
            {
                "cell_id": anomaly.tower_id,
                "detected_at": anomaly.detected_at.isoformat(),
                "resolved_at": (
                    anomaly.resolved_at.isoformat() if anomaly.resolved_at else None
                ),
                "severity": anomaly.severity,
                "detail": anomaly.detail,
            }
            for anomaly in anomalies
        ],
        "actions": [
            {
                "cell_id": action.tower_id,
                "actor": action.actor,
                "action_type": action.action_type,
                "status": action.status,
                "execution_log": action.execution_log,
                "created_at": action.created_at.isoformat(),
            }
            for action in actions
        ],
    }


@app.get("/v1/analytics")
def analytics(
    principal: Annotated[CurrentPrincipal, engineer_dependency],
    hours: int = Query(default=48, ge=6, le=168),
    forecast_hours: int = Query(default=24, ge=1, le=48),
    db: Session = db_dependency,
) -> dict[str, Any]:
    """Train/reuse process-memory ML models from recent persisted KPI telemetry."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = db.execute(
        select(KpiMetric, Tower.region)
        .join(Tower, Tower.tower_id == KpiMetric.tower_id)
        .where(KpiMetric.timestamp >= cutoff)
        .order_by(KpiMetric.tower_id, KpiMetric.timestamp)
        .limit(50000)
    ).all()
    if len(rows) < 40:
        raise HTTPException(
            status_code=422,
            detail="At least 40 recent KPI observations are required for ML analytics.",
        )
    dataframe = pd.DataFrame(
        [
            {
                "Timestamp": metric.timestamp,
                "Tower_ID": metric.tower_id,
                "Region": region,
                "Connected_Users": metric.connected_users,
                "PRB_Usage_pct": metric.prb_usage_pct,
                "Throughput_Mbps": metric.throughput_mbps,
                "Latency_ms": metric.latency_ms,
                "Call_Drop_Rate_pct": metric.call_drop_rate_pct,
                "Handover_Failure_Rate_pct": metric.handover_failure_rate_pct,
                "Hardware_Status": metric.hardware_status,
            }
            for metric, region in rows
        ]
    )
    signature = hashlib.sha256(
        pd.util.hash_pandas_object(dataframe, index=False).values.tobytes()
    ).hexdigest()
    global _model_cache
    with _model_cache_lock:
        if _model_cache is None or _model_cache[0] != signature:
            try:
                _model_cache = (signature, NetworkMLEngine.train(dataframe))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        model = _model_cache[1]
    scored = model.detect_anomalies(dataframe)
    latest_timestamp = dataframe["Timestamp"].max()
    anomalies = scored[
        (scored["Timestamp"] == latest_timestamp) & scored["Is_Anomaly"]
    ].sort_values("Anomaly_Score", ascending=False)
    forecast = model.forecast(dataframe, hours=forecast_hours)
    return {
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "training_observations": len(dataframe),
        "latest_anomalies": [
            {
                "tower_id": row.Tower_ID,
                "region": row.Region,
                "timestamp": row.Timestamp.isoformat(),
                "anomaly_score": float(row.Anomaly_Score),
                "root_cause": row.Root_Cause,
                "prb_usage_pct": float(row.PRB_Usage_pct),
                "latency_ms": float(row.Latency_ms),
                "call_drop_rate_pct": float(row.Call_Drop_Rate_pct),
            }
            for row in anomalies.itertuples(index=False)
        ],
        "forecast": [
            {
                "timestamp": row.Timestamp.isoformat(),
                "predicted_prb_usage_pct": float(row.Predicted_PRB_Usage_pct),
                "predicted_throughput_mbps": float(row.Predicted_Throughput_Mbps),
                "predicted_connected_users": float(row.Predicted_Connected_Users),
            }
            for row in forecast.itertuples(index=False)
        ],
    }


@app.get("/v1/faults")
def list_faults(
    principal: Annotated[CurrentPrincipal, engineer_dependency],
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = db_dependency,
) -> list[dict[str, Any]]:
    """List recent alarm and fault events."""
    rows = db.scalars(
        select(FaultEvent).order_by(FaultEvent.occurred_at.desc()).limit(limit)
    ).all()
    return [
        {
            "id": str(row.id),
            "tower_id": row.tower_id,
            "occurred_at": row.occurred_at.isoformat(),
            "cleared_at": row.cleared_at.isoformat() if row.cleared_at else None,
            "severity": row.severity,
            "alarm_code": row.alarm_code,
            "description": row.description,
        }
        for row in rows
    ]


@app.post("/v1/actions", status_code=status.HTTP_201_CREATED)
def create_action(
    payload: ActionCreateRequest,
    principal: Annotated[CurrentPrincipal, engineer_dependency],
    db: Session = db_dependency,
) -> dict[str, Any]:
    """Submit a bounded configuration request into the two-person workflow."""
    tower = db.get(Tower, payload.tower_id)
    if tower is None or not tower.enabled:
        raise HTTPException(status_code=404, detail="Enabled tower was not found.")
    if payload.action_type == "load_balancing":
        neighbors = payload.parameters["neighbor_cell_ids"]
        if payload.tower_id in neighbors:
            raise HTTPException(
                status_code=422,
                detail="Load-balancing neighbors cannot include the target tower.",
            )
        enabled_neighbors = set(
            db.scalars(
                select(Tower.tower_id).where(
                    Tower.tower_id.in_(neighbors),
                    Tower.enabled.is_(True),
                )
            ).all()
        )
        if enabled_neighbors != set(neighbors):
            raise HTTPException(
                status_code=422,
                detail="Every load-balancing neighbor must be an enabled inventory tower.",
            )
    action = OptimizationAction(
        tower_id=payload.tower_id,
        action_type=payload.action_type,
        parameters=payload.parameters,
        status="pending_approval",
        created_by=principal.username,
    )
    db.add(action)
    db.flush()
    audit(db, principal.username, "action.requested", "optimization_action", str(action.id))
    db.commit()
    return {"id": str(action.id), "status": action.status}


@app.post("/v1/actions/{action_id}/reject")
def reject_action(
    action_id: UUID,
    principal: Annotated[CurrentPrincipal, supervisor_dependency],
    db: Session = db_dependency,
) -> dict[str, str]:
    """Reject an unapproved request independently of its originator."""
    action = _get_action(db, action_id, lock=True)
    if action.status != "pending_approval":
        raise HTTPException(status_code=409, detail="Action is not awaiting approval.")
    if action.created_by == principal.username:
        raise HTTPException(status_code=403, detail="The request originator cannot reject it.")
    action.status = "rejected"
    action.approved_by = principal.username
    audit(db, principal.username, "action.rejected", "optimization_action", str(action.id))
    db.commit()
    return {"id": str(action.id), "status": action.status}


@app.get("/v1/actions")
def list_actions(
    principal: Annotated[CurrentPrincipal, engineer_dependency],
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = db_dependency,
) -> list[dict[str, Any]]:
    """List recent optimization requests and their authorization/execution state."""
    actions = db.scalars(
        select(OptimizationAction)
        .order_by(OptimizationAction.created_at.desc())
        .limit(limit)
    ).all()
    return [
        {
            "id": str(action.id),
            "tower_id": action.tower_id,
            "action_type": action.action_type,
            "parameters": action.parameters,
            "status": action.status,
            "created_by": action.created_by,
            "approved_by": action.approved_by,
            "executed_by": action.executed_by,
            "failure_detail": action.failure_detail,
            "created_at": action.created_at.isoformat(),
        }
        for action in actions
    ]


@app.post("/v1/actions/{action_id}/approve")
def approve_action(
    action_id: UUID,
    principal: Annotated[CurrentPrincipal, supervisor_dependency],
    db: Session = db_dependency,
) -> dict[str, str]:
    """Approve a request only when the approver is not its originator."""
    action = _get_action(db, action_id, lock=True)
    if action.status != "pending_approval":
        raise HTTPException(status_code=409, detail="Action is not awaiting approval.")
    if action.created_by == principal.username:
        raise HTTPException(status_code=403, detail="The request originator cannot approve it.")
    action.status = "approved"
    action.approved_by = principal.username
    audit(db, principal.username, "action.approved", "optimization_action", str(action.id))
    db.commit()
    return {"id": str(action.id), "status": action.status}


@app.post("/v1/actions/{action_id}/execute")
def execute_action(
    action_id: UUID,
    principal: Annotated[CurrentPrincipal, admin_dependency],
    db: Session = db_dependency,
) -> dict[str, Any]:
    """Push a confirmed NETCONF candidate only after independent approval."""
    action = _get_action(db, action_id, lock=True)
    if action.status != "approved":
        raise HTTPException(status_code=409, detail="Action must be independently approved first.")
    if principal.username in {action.created_by, action.approved_by}:
        raise HTTPException(
            status_code=403, detail="Request, approval, and execution must use distinct accounts."
        )
    tower = db.get(Tower, action.tower_id)
    if tower is None:
        raise HTTPException(status_code=404, detail="Tower inventory entry not found.")
    tower = db.scalar(
        select(Tower).where(Tower.tower_id == action.tower_id).with_for_update()
    )
    if tower is None:
        raise HTTPException(status_code=404, detail="Tower inventory entry not found.")
    pending_for_tower = db.scalar(
        select(OptimizationAction.id).where(
            OptimizationAction.tower_id == action.tower_id,
            OptimizationAction.status.in_(["executing", "pending_confirmation"]),
        )
    )
    if pending_for_tower:
        raise HTTPException(status_code=409, detail="Tower already has a pending confirmed commit.")

    action.status = "executing"
    action.executed_by = principal.username
    audit(db, principal.username, "action.execution_started", "optimization_action", str(action.id))
    db.commit()
    try:
        result = NetconfDriver().execute_confirmed(tower, action)
    except NetworkDriverError as exc:
        action.status = "failed"
        action.failure_detail = str(exc)[:2000]
        audit(
            db,
            principal.username,
            "action.execution_failed",
            "optimization_action",
            str(action.id),
            {"error": action.failure_detail},
        )
        db.commit()
        raise HTTPException(status_code=502, detail="Network action failed; see audit trail.") from exc
    action.status = "pending_confirmation"
    action.netconf_message_id = result["message_id"]
    action.confirmation_deadline = result["confirmation_deadline"]
    audit(
        db,
        principal.username,
        "action.confirmed_commit_pending",
        "optimization_action",
        str(action.id),
        {"message_id": action.netconf_message_id},
    )
    db.commit()
    return {"id": str(action.id), **result}


@app.post("/v1/actions/{action_id}/confirm")
def confirm_action(
    action_id: UUID,
    principal: Annotated[CurrentPrincipal, supervisor_dependency],
    db: Session = db_dependency,
) -> dict[str, str]:
    """Confirm a pending NETCONF commit following an independent supervisor check."""
    action = _get_action(db, action_id, lock=True)
    if action.status != "pending_confirmation":
        raise HTTPException(status_code=409, detail="Action has no pending confirmed commit.")
    if principal.username in {action.created_by, action.approved_by, action.executed_by}:
        raise HTTPException(status_code=403, detail="A separate supervisor must confirm the action.")
    if action.confirmation_deadline and action.confirmation_deadline <= datetime.now(timezone.utc):
        action.status = "reverted"
        audit(db, principal.username, "action.confirmation_expired", "optimization_action", str(action.id))
        db.commit()
        raise HTTPException(status_code=409, detail="Confirmation window expired; device should revert.")
    tower = db.get(Tower, action.tower_id)
    if tower is None:
        raise HTTPException(status_code=404, detail="Tower inventory entry not found.")
    try:
        NetconfDriver().confirm_commit(tower, action)
    except NetworkDriverError as exc:
        action.status = "confirmation_failed"
        action.failure_detail = str(exc)[:2000]
        audit(
            db,
            principal.username,
            "action.confirmation_failed",
            "optimization_action",
            str(action.id),
            {"error": action.failure_detail},
        )
        db.commit()
        raise HTTPException(
            status_code=502,
            detail="Commit confirmation failed; device should auto-revert at timeout.",
        ) from exc
    action.status = "confirmed"
    audit(db, principal.username, "action.confirmed", "optimization_action", str(action.id))
    db.commit()
    return {"id": str(action.id), "status": action.status}


@app.get("/v1/audit")
def list_audit(
    principal: Annotated[CurrentPrincipal, admin_dependency],
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = db_dependency,
) -> list[dict[str, Any]]:
    """Return the append-only operational audit trail to administrators."""
    rows = db.scalars(
        select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    ).all()
    return [
        {
            "id": row.id,
            "actor": row.actor,
            "event_type": row.event_type,
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "details": row.details,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]
