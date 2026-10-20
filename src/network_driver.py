"""Guarded NETCONF transport with server-owned, vendor-specific YANG templates."""

from __future__ import annotations

import ipaddress
import hashlib
import hmac
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from xml.sax.saxutils import escape

from ncclient import manager
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from pydantic import SecretStr

from src.dbmodels import OptimizationAction, Tower
from src.settings import settings

TOWER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


class NetworkDriverError(RuntimeError):
    """Raised when a network change cannot be validated or delivered."""


class NetconfDriver:
    """Execute only approved actions through configured, host-key-verified NETCONF."""

    def _validate_target(self, tower: Tower) -> str:
        configured_password = settings.netconf_password
        password = (
            configured_password.get_secret_value()
            if isinstance(configured_password, SecretStr)
            else configured_password
        )
        if not settings.son_netconf_enabled:
            raise NetworkDriverError("Live NETCONF is disabled by SON_NETCONF_ENABLED.")
        if not settings.netconf_username or not password:
            raise NetworkDriverError("NETCONF service credentials are not configured.")
        if not settings.netconf_known_hosts:
            raise NetworkDriverError("NETCONF_KNOWN_HOSTS is required for host-key verification.")
        if not tower.netconf_host or not tower.yang_profile:
            raise NetworkDriverError("Tower requires a NETCONF host and YANG profile.")
        if not settings.allowed_device_networks:
            raise NetworkDriverError("SON_ALLOWED_DEVICE_CIDRS must define device subnets.")
        try:
            address = ipaddress.ip_address(tower.netconf_host)
            networks = [
                ipaddress.ip_network(network, strict=False)
                for network in settings.allowed_device_networks
            ]
        except ValueError as exc:
            raise NetworkDriverError("Device host and allowlist must be valid IP addresses/networks.") from exc
        if not any(address in network for network in networks):
            raise NetworkDriverError("Device address is outside the configured NETCONF allowlist.")
        if not tower.enabled:
            raise NetworkDriverError("Tower is disabled in the network inventory.")
        return str(address)

    def _render_config(
        self, tower: Tower, action: OptimizationAction
    ) -> str:
        if not TOWER_ID_PATTERN.fullmatch(tower.tower_id):
            raise NetworkDriverError("Tower identifier contains unsupported characters.")
        template_name = f"{tower.yang_profile}:{action.action_type}"
        try:
            template = settings.netconf_action_templates.get(template_name)
        except ValueError as exc:
            raise NetworkDriverError("NETCONF YANG template configuration is invalid.") from exc
        if template is None:
            raise NetworkDriverError(
                f"No server-side YANG template is configured for {template_name}."
            )
        required_placeholders = (
            {"__TOWER_ID__", "__RET_TILT_DEGREES__"}
            if action.action_type == "ret_adjustment"
            else {
                "__TOWER_ID__",
                "__NEIGHBOR_CELL_IDS__",
                "__OFFLOAD_PERCENT__",
            }
        )
        if not required_placeholders.issubset(set(re.findall(r"__[A-Z0-9_]+__", template))):
            raise NetworkDriverError(
                "Configured YANG template must include all required action placeholders."
            )
        if "<!DOCTYPE" in template.upper() or "<!ENTITY" in template.upper():
            raise NetworkDriverError("DTD and entity declarations are not permitted in YANG templates.")
        values: dict[str, str]
        if action.action_type == "ret_adjustment":
            allowed = {"tilt_degrees"}
            if set(action.parameters) != allowed:
                raise NetworkDriverError("RET action requires only tilt_degrees.")
            try:
                tilt = float(action.parameters["tilt_degrees"])
            except (TypeError, ValueError) as exc:
                raise NetworkDriverError("tilt_degrees must be numeric.") from exc
            if not math.isfinite(tilt) or not 0 <= tilt <= 15:
                raise NetworkDriverError("tilt_degrees must be between 0 and 15 degrees.")
            values = {"__TOWER_ID__": escape(tower.tower_id), "__RET_TILT_DEGREES__": f"{tilt:g}"}
        elif action.action_type == "load_balancing":
            allowed = {"neighbor_cell_ids", "offload_percent"}
            if set(action.parameters) != allowed:
                raise NetworkDriverError(
                    "Load-balancing action requires neighbor_cell_ids and offload_percent."
                )
            neighbors = action.parameters["neighbor_cell_ids"]
            if (
                not isinstance(neighbors, list)
                or not 1 <= len(neighbors) <= 8
                or len(set(neighbors)) != len(neighbors)
                or any(
                    not isinstance(neighbor, str)
                    or not TOWER_ID_PATTERN.fullmatch(neighbor)
                    for neighbor in neighbors
                )
            ):
                raise NetworkDriverError("neighbor_cell_ids must contain 1-8 valid cell IDs.")
            offload = float(action.parameters["offload_percent"])
            if not math.isfinite(offload) or not 1 <= offload <= 40:
                raise NetworkDriverError("offload_percent must be between 1 and 40.")
            values = {
                "__TOWER_ID__": escape(tower.tower_id),
                "__NEIGHBOR_CELL_IDS__": ",".join(escape(value) for value in neighbors),
                "__OFFLOAD_PERCENT__": f"{offload:g}",
            }
        else:
            raise NetworkDriverError("Unsupported NETCONF action type.")
        rendered = template
        for placeholder, value in values.items():
            rendered = rendered.replace(placeholder, value)
        if re.search(r"__[A-Z0-9_]+__", rendered):
            raise NetworkDriverError("Configured YANG template contains unsupported placeholders.")
        try:
            root = ET.fromstring(rendered)
        except (ET.ParseError, DefusedXmlException) as exc:
            raise NetworkDriverError(f"Configured YANG template is invalid XML: {exc}") from exc
        if root.tag.rsplit("}", 1)[-1] != "config":
            raise NetworkDriverError("NETCONF YANG edit template must have a <config> root element.")
        return rendered

    def execute_confirmed(
        self, tower: Tower, action: OptimizationAction
    ) -> dict[str, Any]:
        """Apply candidate configuration as a confirmed commit that auto-reverts."""
        host = self._validate_target(tower)
        config = self._render_config(tower, action)
        persist_token = self._persist_token(action.id)
        try:
            with manager.connect_ssh(
                host=host,
                port=tower.netconf_port,
                username=settings.netconf_username,
                password=settings.netconf_password.get_secret_value(),
                hostkey_verify=True,
                known_hosts=settings.netconf_known_hosts,
                allow_agent=False,
                look_for_keys=False,
                timeout=settings.netconf_timeout_seconds,
                device_params={"name": "default"},
            ) as session:
                session.lock(target="candidate")
                try:
                    reply = session.edit_config(target="candidate", config=config)
                    session.validate(source="candidate")
                    session.commit(
                        confirmed=True,
                        timeout=settings.confirmed_commit_timeout_seconds,
                        persist=persist_token,
                    )
                    confirmation_deadline = datetime.now(timezone.utc) + timedelta(
                        seconds=settings.confirmed_commit_timeout_seconds
                    )
                    message_id = str(
                        getattr(getattr(reply, "xml", None), "attrib", {}).get(
                            "message-id", action.id
                        )
                    )
                except Exception:
                    session.discard_changes()
                    raise
                finally:
                    session.unlock(target="candidate")
            return {
                "message_id": message_id,
                "confirmation_timeout_seconds": settings.confirmed_commit_timeout_seconds,
                "confirmation_deadline": confirmation_deadline,
                "status": "pending_confirmation",
            }
        except NetworkDriverError:
            raise
        except Exception as exc:
            raise NetworkDriverError(f"NETCONF confirmed-commit failed: {exc}") from exc

    def confirm_commit(self, tower: Tower, action: OptimizationAction) -> None:
        """Confirm a previously approved, pending confirmed commit."""
        host = self._validate_target(tower)
        persist_token = self._persist_token(action.id)
        try:
            with manager.connect_ssh(
                host=host,
                port=tower.netconf_port,
                username=settings.netconf_username,
                password=settings.netconf_password.get_secret_value(),
                hostkey_verify=True,
                known_hosts=settings.netconf_known_hosts,
                allow_agent=False,
                look_for_keys=False,
                timeout=settings.netconf_timeout_seconds,
                device_params={"name": "default"},
            ) as session:
                session.commit(persist_id=persist_token)
        except Exception as exc:
            raise NetworkDriverError(f"NETCONF commit confirmation failed: {exc}") from exc

    @staticmethod
    def _persist_token(action_id: Any) -> str:
        secret = settings.jwt_secret.get_secret_value().encode("utf-8")
        message = f"netconf-confirmed-commit:{action_id}".encode("utf-8")
        return hmac.new(secret, message, hashlib.sha256).hexdigest()
