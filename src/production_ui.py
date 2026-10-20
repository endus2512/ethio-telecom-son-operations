"""Streamlit client for the authenticated SON operations API."""

from __future__ import annotations

import html
import os
from typing import Any

import folium
import pandas as pd
import requests
import streamlit as st
from streamlit_folium import st_folium

API_URL = os.environ.get("SON_API_URL", "http://localhost:8000").rstrip("/")
ROLE_ENGINEER = "network_engineer"
ROLE_SUPERVISOR = "noc_supervisor"
ROLE_ADMIN = "system_admin"


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


def _tower_map(rows: list[dict[str, Any]]) -> None:
    located = [
        row
        for row in rows
        if row.get("latitude") is not None and row.get("longitude") is not None
    ]
    if not located:
        st.info("Tower inventory has no mapped coordinates.")
        return
    map_view = folium.Map(location=[9.15, 39.2], zoom_start=6, tiles="OpenStreetMap")
    for row in located:
        prb = float(row["prb_usage_pct"])
        critical = (
            row["hardware_status"].lower() == "outage"
            or prb >= 90
            or row["call_drop_rate_pct"] > 5
        )
        degraded = (
            row["hardware_status"].lower() == "degraded"
            or prb >= 80
            or row["latency_ms"] > 120
        )
        color = "#d62828" if critical else "#f2b705" if degraded else "#00813d"
        popup = folium.Popup(
            (
                f"<b>{html.escape(row['tower_id'])} / {html.escape(row['region'])}</b><br>"
                f"PRB {prb:.1f}% · latency {row['latency_ms']:.1f} ms<br>"
                f"Call drops {row['call_drop_rate_pct']:.2f}% · "
                f"users {row['connected_users']:,}"
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

    towers = api_request("GET", "/v1/towers", token) or []
    latest = api_request("GET", "/v1/kpis/latest", token, params={"limit": 5000}) or []
    faults = api_request("GET", "/v1/faults", token, params={"limit": 300}) or []
    actions = api_request("GET", "/v1/actions", token, params={"limit": 300}) or []
    if not all(isinstance(value, list) for value in (towers, latest, faults, actions)):
        st.error("The API returned an unexpected response.")
        return
    dataframe = pd.DataFrame(latest)
    if dataframe.empty:
        st.info("No KPI observations received yet. Configure an NMS PM feed.")
    else:
        if dataframe["source"].eq("synthetic-demo").all():
            st.info(
                "Showing synthetic demo telemetry. Connect an NMS PM feed before "
                "using this dashboard for operational decisions."
            )
        critical = (
            (dataframe["prb_usage_pct"] >= 90)
            | (dataframe["call_drop_rate_pct"] > 5)
            | (dataframe["hardware_status"].str.lower() == "outage")
        )
        degraded = (
            (dataframe["prb_usage_pct"] >= 80)
            | (dataframe["latency_ms"] > 120)
            | (dataframe["hardware_status"].str.lower() == "degraded")
        ) & ~critical
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Cells reporting", f"{len(dataframe):,}")
        col2.metric("Critical", f"{int(critical.sum()):,}")
        col3.metric("Degraded", f"{int(degraded.sum()):,}")
        col4.metric("Mean PRB", f"{dataframe['prb_usage_pct'].mean():.1f}%")

    tabs = st.tabs(["Network Overview", "AI Analytics", "Fault Management", "SON Actions"])
    with tabs[0]:
        _tower_map(latest)
        if not dataframe.empty:
            st.dataframe(dataframe, width="stretch", hide_index=True)
    with tabs[1]:
        if st.button("Run anomaly detection and 24-hour forecast"):
            result = api_request("GET", "/v1/analytics", token)
            if result:
                st.session_state["son_analytics"] = result
        analytics = st.session_state.get("son_analytics")
        if analytics:
            st.caption(
                f"Model trained in backend memory at {analytics['trained_at']} "
                f"from {analytics['training_observations']:,} persisted observations."
            )
            st.markdown("#### Latest anomaly diagnostics")
            st.dataframe(
                pd.DataFrame(analytics["latest_anomalies"]),
                width="stretch",
                hide_index=True,
            )
            st.markdown("#### 24-hour congestion forecast")
            forecast = pd.DataFrame(analytics["forecast"])
            if not forecast.empty:
                st.line_chart(
                    forecast.set_index("timestamp")["predicted_prb_usage_pct"],
                    y_label="Mean PRB utilization (%)",
                )
                st.dataframe(forecast, width="stretch", hide_index=True)
        else:
            st.info("Run analytics after the PM feed has collected sufficient KPI history.")
    with tabs[2]:
        st.dataframe(pd.DataFrame(faults), width="stretch", hide_index=True)
    with tabs[3]:
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
