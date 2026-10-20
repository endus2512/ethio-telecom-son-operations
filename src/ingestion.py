"""Parsers for normalized JSON, CSV, and XML PM/FM feed deliveries."""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

TOWER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


def parse_feed(payload: bytes, feed_kind: str, feed_format: str) -> list[dict[str, Any]]:
    """Decode a bounded feed document into normalized record dictionaries."""
    if len(payload) > 20 * 1024 * 1024:
        raise ValueError("Feed exceeds the 20 MiB ingestion limit.")
    if feed_kind not in {"pm", "fm"}:
        raise ValueError("feed_kind must be 'pm' or 'fm'.")
    try:
        if feed_format == "json":
            decoded = json.loads(payload)
            if isinstance(decoded, list):
                records = decoded
            elif isinstance(decoded, dict):
                records = decoded.get("records")
            else:
                raise ValueError(
                    "JSON feed must be an array or an object with a records array."
                )
            if not isinstance(records, list):
                raise ValueError("JSON feed must be an array or an object with a records array.")
            return [_normalize_record(record, feed_kind) for record in records]
        if feed_format == "csv":
            text = payload.decode("utf-8-sig")
            return [
                _normalize_record(record, feed_kind)
                for record in csv.DictReader(io.StringIO(text))
            ]
        if feed_format == "xml":
            root = ET.fromstring(payload)
            record_elements = [
                element
                for element in root.iter()
                if _local_name(element.tag).lower()
                in {"measurement", "pmrecord", "fmrecord", "alarm", "record"}
            ]
            records = [
                {
                    _local_name(child.tag): child.text or ""
                    for child in element
                    if len(child) == 0
                }
                for element in record_elements
            ]
            if not records:
                raise ValueError("XML feed has no recognized measurement or alarm records.")
            return [_normalize_record(record, feed_kind) for record in records]
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ET.ParseError,
        DefusedXmlException,
    ) as exc:
        raise ValueError(f"Unable to parse {feed_format} {feed_kind.upper()} feed: {exc}") from exc
    raise ValueError("feed_format must be one of: json, csv, xml.")


def _normalize_record(record: Any, feed_kind: str) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("Every feed record must be a JSON/XML object.")
    normalized = {_normalize_key(str(key)): value for key, value in record.items()}
    aliases = {
        "tower_id": ("tower_id", "cell_id", "ne_id", "managed_element", "object"),
        "timestamp": ("timestamp", "time", "end_time", "event_time", "occurred_at"),
    }
    for target, sources in aliases.items():
        for source in sources:
            if source in normalized and normalized[source] not in (None, ""):
                normalized[target] = normalized[source]
                break
    if not normalized.get("tower_id") or not normalized.get("timestamp"):
        raise ValueError("Each feed record requires a tower_id and timestamp.")
    if not TOWER_ID_PATTERN.fullmatch(str(normalized["tower_id"])):
        raise ValueError("tower_id contains unsupported characters.")
    if feed_kind == "pm":
        _copy_alias(
            normalized,
            "prb_usage_pct",
            "prb_usage_pct",
            "prb_utilization",
            "dl_prb_usage",
            "prb",
        )
        _copy_alias(normalized, "connected_users", "connected_users", "rrc_users", "users")
        _copy_alias(normalized, "throughput_mbps", "throughput_mbps", "dl_throughput_mbps")
        _copy_alias(normalized, "latency_ms", "latency_ms", "average_latency_ms")
        _copy_alias(
            normalized,
            "call_drop_rate_pct",
            "call_drop_rate_pct",
            "cdr_pct",
            "call_drop_rate",
        )
        _copy_alias(
            normalized,
            "handover_failure_rate_pct",
            "handover_failure_rate_pct",
            "hofr_pct",
            "handover_failure_rate",
        )
        _copy_alias(normalized, "hardware_status", "hardware_status", "operational_state")
        _copy_alias(normalized, "region", "region", "site_region")
    else:
        _copy_alias(normalized, "alarm_code", "alarm_code", "code", "specific_problem")
        _copy_alias(normalized, "severity", "severity", "perceived_severity")
        _copy_alias(normalized, "description", "description", "additional_text", "problem_text")
        _copy_alias(normalized, "external_id", "external_id", "alarm_id", "event_id")
        _copy_alias(normalized, "cleared_at", "cleared_at", "clear_time")
        required_fields = ("alarm_code", "severity", "description")
        missing_fields = [
            field
            for field in required_fields
            if not isinstance(normalized.get(field), (str, int, float))
            or isinstance(normalized.get(field), bool)
            or not str(normalized[field]).strip()
        ]
        if missing_fields:
            raise ValueError(
                "Each FM record requires non-empty "
                + ", ".join(missing_fields)
                + "."
            )
    return normalized


def _normalize_key(value: str) -> str:
    value = _local_name(value).strip()
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1].split(":")[-1]


def _copy_alias(record: dict[str, Any], target: str, *candidates: str) -> None:
    for candidate in candidates:
        if candidate in record and record[candidate] not in (None, ""):
            record[target] = record[candidate]
            return
