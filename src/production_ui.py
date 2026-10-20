"""Streamlit client for the authenticated SON operations API."""

from __future__ import annotations

from datetime import datetime, timezone
import html
import json
import os
from io import BytesIO
from typing import Any

import folium
import pandas as pd
import requests
import streamlit as st
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from streamlit_folium import st_folium

API_URL = os.environ.get("SON_API_URL", "http://localhost:8000").rstrip("/")
ROLE_ENGINEER = "network_engineer"
ROLE_SUPERVISOR = "noc_supervisor"
ROLE_ADMIN = "system_admin"

ADDIS_DEMO_CELLS = (
    (9.019, 38.752, 64.0, 1.1, 58.0, 112.0, 540, "Optimal"),
    (9.034, 38.761, 78.0, 2.2, 83.0, 76.0, 720, "Optimal"),
    (8.997, 38.729, 93.0, 5.8, 136.0, 42.0, 810, "Degraded"),
    (9.051, 38.783, 52.0, 0.8, 54.0, 138.0, 430, "Optimal"),
    (9.008, 38.792, 86.0, 3.7, 108.0, 61.0, 650, "Degraded"),
    (9.067, 38.735, 42.0, 0.6, 49.0, 154.0, 380, "Optimal"),
    (8.981, 38.767, 97.0, 7.1, 149.0, 29.0, 920, "Outage"),
    (9.041, 38.704, 69.0, 1.5, 72.0, 94.0, 590, "Optimal"),
)


def api_request(
    method: str,
    path: str,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    """Call the backend with bounded timeouts and a user-safe error message."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        response = requests.request(
            method,
            f"{API_URL}{path}",
            headers=headers,
            json=payload,
            params=params,
            timeout=(3.05, 30),
        )
        response.raise_for_status()
        return response.json() if response.content else {}
    except requests.RequestException as exc:
        detail = "The operations service is unavailable."
        if exc.response is not None:
            try:
                detail = exc.response.json().get("detail", detail)
            except (ValueError, AttributeError):
                detail = f"Operations service returned HTTP {exc.response.status_code}."
        st.error(str(detail))
        return None


def login_panel() -> None:
    """Authenticate a named user and keep the short-lived token session-scoped."""
    st.title("Ethio Telecom SON Operations")
    st.caption("Sign in using your network operations account.")
    with st.form("login"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        try:
            response = requests.post(
                f"{API_URL}/v1/auth/login",
                json={"username": username, "password": password},
                timeout=(3.05, 15),
            )
            response.raise_for_status()
            auth = response.json()
            st.session_state["son_token"] = auth["access_token"]
            st.session_state["son_role"] = auth["role"]
            st.session_state["son_username"] = username
            st.rerun()
        except requests.RequestException as exc:
            st.error("Sign-in failed. Check service availability and credentials.")
        except (KeyError, ValueError):
            st.error("Authentication service returned an invalid response.")


def _illustrative_addis_cells() -> list[dict[str, Any]]:
    """Create labeled, temporary sample cells without writing them to inventory."""
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return [
        {
            "tower_id": f"SAMPLE-ADD-{index:03d}",
            "region": "Addis Ababa (illustrative)",
            "latitude": latitude,
            "longitude": longitude,
            "prb_usage_pct": traffic,
            "call_drop_rate_pct": drop_rate,
            "latency_ms": latency,
            "throughput_mbps": throughput,
            "connected_users": users,
            "hardware_status": status,
            "timestamp": observed_at,
            "source": "illustrative-synthetic",
        }
        for index, (
            latitude,
            longitude,
            traffic,
            drop_rate,
            latency,
            throughput,
            users,
            status,
        ) in enumerate(ADDIS_DEMO_CELLS, start=1)
    ]


def _tower_map(rows: list[dict[str, Any]]) -> None:
    located = [
        row
        for row in rows
        if row.get("latitude") is not None and row.get("longitude") is not None
    ]
    if not located:
        st.info("Tower inventory has no mapped coordinates.")
        return
    latitudes = [float(row["latitude"]) for row in located]
    longitudes = [float(row["longitude"]) for row in located]
    zoom = (
        11
        if max(latitudes) - min(latitudes) < 0.5
        and max(longitudes) - min(longitudes) < 0.5
        else 6
    )
    map_view = folium.Map(
        location=[sum(latitudes) / len(latitudes), sum(longitudes) / len(longitudes)],
        zoom_start=zoom,
        tiles="OpenStreetMap",
    )
    for row in located:
        prb = float(row["prb_usage_pct"])
        status = str(row.get("hardware_status") or "unknown").lower()
        critical = (
            status == "outage"
            or prb >= 90
            or float(row["call_drop_rate_pct"]) > 5
            or float(row["latency_ms"]) > 120
        )
        degraded = (
            status in {"degraded", "warning"}
            or prb >= 80
            or float(row["call_drop_rate_pct"]) > 3
        )
        color = "#d62828" if critical else "#f2b705" if degraded else "#00813d"
        health = "Critical" if critical else "Warning" if degraded else "Normal"
        source_note = (
            "<br><b>Illustrative synthetic sample — not live network data</b>"
            if row.get("source") == "illustrative-synthetic"
            else ""
        )
        popup = folium.Popup(
            (
                f"<b>{html.escape(row['tower_id'])} / {html.escape(row['region'])}</b><br>"
                f"Status: {health} ({html.escape(status)})<br>"
                f"Latitude: {float(row['latitude']):.5f} · "
                f"Longitude: {float(row['longitude']):.5f}<br>"
                f"Traffic load: {prb:.1f}% · latency {float(row['latency_ms']):.1f} ms<br>"
                f"Drop rate: {float(row['call_drop_rate_pct']):.2f}% · "
                f"throughput {float(row['throughput_mbps']):.1f} Mbps · "
                f"users {int(row['connected_users']):,}<br>"
                f"Updated: {html.escape(str(row['timestamp']))}"
                f"{source_note}"
            ),
            max_width=320,
        )
        folium.CircleMarker(
            [row["latitude"], row["longitude"]],
            radius=7,
            color=color,
            fill=True,
            fill_color=color,
            fill_opacity=0.85,
            tooltip=html.escape(row["tower_id"]),
            popup=popup,
        ).add_to(map_view)
    st_folium(map_view, width=None, height=520, returned_objects=[])


def _health_summary(dataframe: pd.DataFrame) -> tuple[pd.Series, pd.Series, float]:
    """Return per-cell critical/warning flags and a transparent health score."""
    status = dataframe["hardware_status"].fillna("unknown").astype(str).str.lower()
    critical = (
        status.eq("outage")
        | dataframe["prb_usage_pct"].ge(90)
        | dataframe["call_drop_rate_pct"].gt(5)
        | dataframe["latency_ms"].gt(120)
    )
    warning = (
        status.isin(["degraded", "warning"])
        | dataframe["prb_usage_pct"].ge(80)
        | dataframe["call_drop_rate_pct"].gt(3)
        | dataframe["latency_ms"].gt(100)
    ) & ~critical
    if dataframe.empty:
        return critical, warning, 0.0
    index = max(
        0.0,
        100.0
        * (
            len(dataframe)
            - 1.0 * int(critical.sum())
            - 0.4 * int(warning.sum())
        )
        / len(dataframe),
    )
    return critical, warning, index


def _build_pdf_report(dataframe: pd.DataFrame) -> bytes:
    """Build a compact PDF snapshot from the displayed latest KPI records."""
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=landscape(letter),
        rightMargin=0.45 * inch,
        leftMargin=0.45 * inch,
        topMargin=0.45 * inch,
        bottomMargin=0.45 * inch,
        title="Ethio Telecom SON Operations KPI Report",
    )
    styles = getSampleStyleSheet()
    critical, warning, score = _health_summary(dataframe)
    elements = [
        Paragraph("Ethio Telecom SON Operations — KPI Report", styles["Title"]),
        Paragraph(
            "Generated "
            + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            + " · Latest available KPI per reporting cell",
            styles["Normal"],
        ),
        Spacer(1, 0.18 * inch),
        Paragraph(
            f"Network Health Index: {score:.1f}/100 · "
            f"Reporting cells: {len(dataframe):,} · "
            f"Critical: {int(critical.sum()):,} · "
            f"Warning: {int(warning.sum()):,}",
            styles["Heading2"],
        ),
        Spacer(1, 0.12 * inch),
    ]
    columns = [
        ("tower_id", "Cell ID"),
        ("region", "Region"),
        ("hardware_status", "Status"),
        ("prb_usage_pct", "Traffic %"),
        ("call_drop_rate_pct", "Drop %"),
        ("latency_ms", "Latency ms"),
        ("throughput_mbps", "Throughput Mbps"),
        ("timestamp", "Observed at"),
    ]
    table_data = [[label for _, label in columns]]
    for row in dataframe.to_dict(orient="records"):
        values = []
        for key, _ in columns:
            value = row.get(key, "")
            if key in {"prb_usage_pct", "call_drop_rate_pct", "latency_ms", "throughput_mbps"}:
                value = f"{float(value):.2f}"
            elif key == "timestamp":
                value = str(value)[:19].replace("T", " ")
            values.append(Paragraph(html.escape(str(value)), styles["BodyText"]))
        table_data.append(values)
    table = Table(table_data, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#16324F")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 7),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#B8C4CE")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F1F5F8")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    elements.append(table)
    document.build(elements)
    return output.getvalue()


def _alert_simulator(dataframe: pd.DataFrame) -> None:
    """Render an explicit simulation; this function makes no external delivery."""
    st.subheader("Alert simulator")
    st.caption(
        "Preview only: no Telegram message, email, or webhook is sent from this dashboard."
    )
    if dataframe.empty:
        st.info("No KPI data is available to evaluate for alerts.")
        return
    flagged = dataframe[
        (dataframe["call_drop_rate_pct"] > 5)
        | (dataframe["latency_ms"] > 120)
        | (dataframe["prb_usage_pct"] >= 90)
        | dataframe["hardware_status"].astype(str).str.lower().eq("outage")
    ].copy()
    if flagged.empty:
        st.success("No critical KPI threshold is currently breached.")
        return
    channel = st.selectbox("Simulated delivery format", ["Telegram", "Webhook", "Email"])
    if st.button("Preview critical alerts", type="secondary"):
        previews: list[str] = []
        for row in flagged.to_dict(orient="records"):
            event = {
                "event": "son.network.critical",
                "severity": "critical",
                "cell_id": str(row["tower_id"]),
                "region": str(row["region"]),
                "drop_rate_pct": float(row["call_drop_rate_pct"]),
                "latency_ms": float(row["latency_ms"]),
                "traffic_load_pct": float(row["prb_usage_pct"]),
                "observed_at": str(row["timestamp"]),
            }
            telegram_message = (
                f"🚨 SON CRITICAL — {event['cell_id']} ({event['region']}) | "
                f"drop {event['drop_rate_pct']:.2f}% | "
                f"latency {event['latency_ms']:.1f} ms | "
                f"traffic {event['traffic_load_pct']:.1f}% | "
                f"observed {event['observed_at']}"
            )
            if channel == "Webhook":
                previews.append(json.dumps(event, indent=2))
            elif channel == "Email":
                previews.append(
                    f"Subject: [SON CRITICAL] {event['cell_id']} — high network KPI\n\n"
                    f"Cell: {event['cell_id']} ({event['region']})\n"
                    f"Drop rate: {event['drop_rate_pct']:.2f}%\n"
                    f"Latency: {event['latency_ms']:.1f} ms\n"
                    f"Traffic load: {event['traffic_load_pct']:.1f}%\n"
                    f"Observed at: {event['observed_at']}"
                )
            else:
                previews.append(telegram_message)
        st.session_state["son_alert_preview"] = {
            "channel": channel,
            "messages": previews,
        }
    preview = st.session_state.get("son_alert_preview", {})
    if preview.get("channel") == channel:
        for message in preview.get("messages", []):
            st.code(message, language="json" if channel == "Webhook" else None)


def _action_controls(
    token: str, role: str, username: str, towers: list[dict[str, Any]], actions: list[dict[str, Any]]
) -> None:
    st.subheader("Change request workflow")
    st.caption(
        "Request → independent NOC approval → independent administrator execution "
        "→ independent NOC confirmed-commit verification."
    )
    if role in {ROLE_ENGINEER, ROLE_SUPERVISOR, ROLE_ADMIN} and towers:
        enabled_ids = [tower["tower_id"] for tower in towers if tower["enabled"]]
        if enabled_ids:
            with st.form("new-son-action"):
                tower_id = st.selectbox("Tower", enabled_ids)
                action_type = st.selectbox(
                    "YANG action family", ["ret_adjustment", "load_balancing"]
                )
                if action_type == "ret_adjustment":
                    params: dict[str, Any] = {
                        "tilt_degrees": st.number_input(
                            "Target RET tilt (degrees)", min_value=0.0, max_value=15.0, step=0.5
                        )
                    }
                else:
                    neighbor_ids = st.text_input(
                        "Neighbor cell IDs", help="Comma-separated tower IDs from inventory."
                    )
                    params = {
                        "neighbor_cell_ids": [
                            value.strip()
                            for value in neighbor_ids.split(",")
                            if value.strip()
                        ],
                        "offload_percent": st.number_input(
                            "Traffic offload (%)", min_value=1.0, max_value=40.0, step=1.0
                        ),
                    }
                submitted = st.form_submit_button("Request configuration change")
            if submitted:
                result = api_request(
                    "POST",
                    "/v1/actions",
                    token,
                    {"tower_id": tower_id, "action_type": action_type, "parameters": params},
                )
                if result:
                    st.success(f"Action {result['id']} queued for NOC approval.")

    for action in actions:
        with st.expander(
            f"{action['id']} · {action['tower_id']} · {action['action_type']} · {action['status']}"
        ):
            st.json(
                {
                    "parameters": action["parameters"],
                    "created_by": action["created_by"],
                    "approved_by": action["approved_by"],
                    "executed_by": action["executed_by"],
                    "failure_detail": action["failure_detail"],
                }
            )
            action_id = action["id"]
            if role == ROLE_SUPERVISOR and action["status"] == "pending_approval":
                if st.button("Approve request", key=f"approve-{action_id}"):
                    result = api_request(
                        "POST", f"/v1/actions/{action_id}/approve", token
                    )
                    if result:
                        st.success("Approved. A separate system administrator may execute.")
                        st.rerun()
            if role == ROLE_ADMIN and action["status"] == "approved":
                if st.button("Execute approved NETCONF commit", key=f"execute-{action_id}"):
                    result = api_request(
                        "POST", f"/v1/actions/{action_id}/execute", token
                    )
                    if result:
                        st.warning(
                            "Confirmed commit is pending independent NOC confirmation; "
                            "device rollback remains armed."
                        )
                        st.rerun()
            if role == ROLE_SUPERVISOR and action["status"] == "pending_confirmation":
                if st.button("Confirm verified device change", key=f"confirm-{action_id}"):
                    result = api_request(
                        "POST", f"/v1/actions/{action_id}/confirm", token
                    )
                    if result:
                        st.success("NETCONF commit confirmed.")
                        st.rerun()


def main() -> None:
    """Render the database-backed operations UI for the authenticated user."""
    st.set_page_config(
        page_title="Ethio Telecom SON Operations",
        page_icon="📡",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    token = st.session_state.get("son_token")
    if not token:
        login_panel()
        return

    username = st.session_state["son_username"]
    role = st.session_state["son_role"]
    with st.sidebar:
        st.markdown("### Network Operations")
        st.write(username)
        st.caption(role.replace("_", " ").title())
        if st.button("Refresh telemetry"):
            st.rerun()
        if st.button("Sign out"):
            for key in ("son_token", "son_role", "son_username"):
                st.session_state.pop(key, None)
            st.rerun()

    st.title("Ethio Telecom Self-Optimizing Network")
    st.caption("Operational telemetry and audited configuration workflows")
    try:
        health = requests.get(f"{API_URL}/health/ready", timeout=(2, 3))
        health.raise_for_status()
        st.success("Operations API and database are ready.")
    except requests.RequestException:
        st.error("Operations API is not ready; refresh when connectivity returns.")
        return

    towers = api_request("GET", "/v1/towers", token)
    latest = api_request("GET", "/v1/kpis/latest", token, params={"limit": 5000})
    faults = api_request("GET", "/v1/faults", token, params={"limit": 300})
    actions = api_request("GET", "/v1/actions", token, params={"limit": 300})
    if any(value is None for value in (towers, latest, faults, actions)):
        return
    if not all(isinstance(value, list) for value in (towers, latest, faults, actions)):
        st.error("The API returned an unexpected response.")
        return
    dataframe = pd.DataFrame(latest)
    if dataframe.empty:
        st.info("No KPI observations received yet. Configure an NMS PM feed.")
    if not dataframe.empty and dataframe["source"].eq("synthetic-demo").all():
        st.info(
            "Showing synthetic demo telemetry. Connect an NMS PM feed before "
            "using this dashboard for operational decisions."
        )

    tabs = st.tabs(
        [
            "Overview",
            "GIS Map",
            "AI Anomaly Detection",
            "Reports & Exports",
            "Fault Management",
            "SON Actions",
        ]
    )
    with tabs[0]:
        if not dataframe.empty:
            critical, warning, health_index = _health_summary(dataframe)
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Network Health Index", f"{health_index:.1f}/100")
            col2.metric("Cells reporting", f"{len(dataframe):,}")
            col3.metric("Critical / Warning", f"{int(critical.sum()):,} / {int(warning.sum()):,}")
            col4.metric("Mean traffic load", f"{dataframe['prb_usage_pct'].mean():.1f}%")
            if critical.any():
                st.error(
                    f"Critical network condition: {int(critical.sum())} cell(s) exceed "
                    "one or more operational thresholds."
                )
            elif warning.any():
                st.warning(f"{int(warning.sum())} cell(s) require attention.")
            else:
                st.success("No critical or warning conditions in the latest KPI snapshot.")
            st.markdown("#### Live KPI snapshot")
            st.dataframe(dataframe, width="stretch", hide_index=True)
        _alert_simulator(dataframe)
    with tabs[1]:
        st.markdown("#### Interactive cell tower health map")
        st.caption(
            "Marker colors: green = normal, yellow = warning, red = critical. "
            "Map points come from tower inventory coordinates and latest reported KPIs."
        )
        include_samples = st.checkbox(
            "Include illustrative Addis Ababa sample cells (synthetic, not live)",
            value=not any(
                row.get("latitude") is not None and row.get("longitude") is not None
                for row in latest
            ),
        )
        map_rows = list(latest)
        if include_samples:
            st.warning(
                "Illustrative sample sites are synthetic and do not represent actual "
                "tower locations or live network measurements."
            )
            map_rows.extend(_illustrative_addis_cells())
        if map_rows:
            _tower_map(map_rows)
        else:
            inventory_coordinates = [
                tower
                for tower in towers
                if tower.get("latitude") is not None and tower.get("longitude") is not None
            ]
            if inventory_coordinates:
                st.info("No KPI readings yet; mapped inventory locations have no live status.")
                inventory_map = folium.Map(location=[9.03, 38.74], zoom_start=11)
                for tower in inventory_coordinates:
                    folium.Marker(
                        [tower["latitude"], tower["longitude"]],
                        tooltip=html.escape(tower["tower_id"]),
                        popup=html.escape(f"{tower['tower_id']} · {tower['region']} · No KPI data"),
                        icon=folium.Icon(color="gray", icon="tower", prefix="fa"),
                    ).add_to(inventory_map)
                st_folium(inventory_map, width=None, height=520, returned_objects=[])
            else:
                st.info("No tower coordinates are available in inventory.")
    with tabs[2]:
        if st.button("Run anomaly detection and 24-hour forecast"):
            result = api_request("GET", "/v1/analytics", token)
            if result:
                st.session_state["son_analytics"] = result
        analytics = st.session_state.get("son_analytics")
        if analytics:
            anomalies = pd.DataFrame(analytics["latest_anomalies"])
            if anomalies.empty:
                st.success("AI analysis completed: no anomalies detected in the latest readings.")
            else:
                st.error(f"AI anomaly engine detected {len(anomalies)} anomalous cell(s).")
                st.dataframe(anomalies, width="stretch", hide_index=True)
            st.caption(
                f"Backend Isolation Forest trained at {analytics['trained_at']} using "
                f"{analytics['training_observations']:,} persisted observations."
            )
            st.markdown("#### 24-hour network forecast")
            forecast = pd.DataFrame(analytics["forecast"])
            if not forecast.empty:
                st.line_chart(
                    forecast.set_index("timestamp")["predicted_prb_usage_pct"],
                    y_label="Predicted mean PRB utilization (%)",
                )
                st.dataframe(forecast, width="stretch", hide_index=True)
        else:
            st.info("Run analytics once sufficient recent KPI history is available.")
    with tabs[3]:
        st.subheader("Operational KPI report")
        st.caption("Exports use the latest available KPI observation for each reporting cell.")
        if dataframe.empty:
            st.info("Reports are unavailable until KPI observations are received.")
        else:
            csv_data = dataframe.to_csv(index=False).encode("utf-8")
            st.download_button(
                "Download KPI summary (CSV)",
                data=csv_data,
                file_name="ethio_telecom_son_kpi_summary.csv",
                mime="text/csv",
            )
            st.download_button(
                "Download KPI report (PDF)",
                data=_build_pdf_report(dataframe),
                file_name="ethio_telecom_son_kpi_report.pdf",
                mime="application/pdf",
            )
            st.dataframe(dataframe, width="stretch", hide_index=True)
    with tabs[4]:
        st.dataframe(pd.DataFrame(faults), width="stretch", hide_index=True)
    with tabs[5]:
        _action_controls(token, role, username, towers, actions)
    if role == ROLE_ADMIN:
        with st.expander("System administration"):
            st.markdown("#### Create user")
            with st.form("create-user"):
                new_username = st.text_input("New username")
                new_password = st.text_input("Temporary password", type="password")
                new_role = st.selectbox(
                    "Role", [ROLE_ENGINEER, ROLE_SUPERVISOR, ROLE_ADMIN]
                )
                create_submitted = st.form_submit_button("Create user")
            if create_submitted:
                result = api_request(
                    "POST",
                    "/v1/users",
                    token,
                    {
                        "username": new_username,
                        "password": new_password,
                        "role": new_role,
                    },
                )
                if result:
                    st.success(f"Created user {result['username']}.")
            st.markdown("#### Add tower inventory entry")
            with st.form("create-tower"):
                tower_id = st.text_input("Tower ID")
                region = st.text_input("Region")
                latitude = st.number_input("Latitude", min_value=-90.0, max_value=90.0)
                longitude = st.number_input("Longitude", min_value=-180.0, max_value=180.0)
                netconf_host = st.text_input("NETCONF device IP (optional)")
                yang_profile = st.text_input("YANG profile name (optional)")
                tower_submitted = st.form_submit_button("Add tower")
            if tower_submitted:
                body: dict[str, Any] = {
                    "tower_id": tower_id,
                    "region": region,
                    "latitude": latitude,
                    "longitude": longitude,
                }
                if netconf_host:
                    body["netconf_host"] = netconf_host
                    body["yang_profile"] = yang_profile
                result = api_request("POST", "/v1/towers", token, body)
                if result:
                    st.success(f"Added tower {result['tower_id']}.")
            if st.button("Refresh audit trail"):
                audit_rows = api_request("GET", "/v1/audit", token)
                if audit_rows is not None:
                    st.session_state["son_audit"] = audit_rows
            if st.session_state.get("son_audit") is not None:
                st.dataframe(
                    pd.DataFrame(st.session_state["son_audit"]),
                    width="stretch",
                    hide_index=True,
                )
