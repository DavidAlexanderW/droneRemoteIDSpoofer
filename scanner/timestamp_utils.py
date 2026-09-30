#!/usr/bin/env python3
"""
Timestamp resolution and validation utilities for drone Remote ID packets.
Eliminates duplicated validation bounds across scanner modules and avoids
hardcoded epoch limits with dynamic bounds based on current time.
"""

import base64
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Base minimum valid timestamp: January 1, 2026 00:00:00 UTC
DEFAULT_MIN_VALID_TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
# Default max allowed forward clock skew: 1 year (365 days)
DEFAULT_MAX_CLOCK_SKEW_S = 31536000.0


def parse_iso_timestamp(iso_str: str) -> Optional[float]:
    """Parses an ISO 8601 timestamp string into a UTC epoch float."""
    if not iso_str or not isinstance(iso_str, str):
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return None


def is_valid_timestamp(
    ts: Optional[float],
    min_valid: Optional[float] = None,
    max_skew_s: float = DEFAULT_MAX_CLOCK_SKEW_S,
    ref_now: Optional[float] = None,
) -> bool:
    """Checks whether a numeric timestamp falls within acceptable dynamic bounds."""
    if ts is None:
        return False
    try:
        val = float(ts)
    except (ValueError, TypeError):
        return False

    min_bound = min_valid if min_valid is not None else DEFAULT_MIN_VALID_TIMESTAMP
    now = ref_now if ref_now is not None else time.time()
    max_bound = now + max_skew_s

    return min_bound <= val <= max_bound


def resolve_reception_timestamp(
    packet: Dict[str, Any],
    fallback_now: Optional[float] = None,
    min_valid: Optional[float] = None,
    max_skew_s: float = DEFAULT_MAX_CLOCK_SKEW_S,
) -> float:
    """
    Resolves the true physical reception timestamp for a packet.

    Priority hierarchy:
    1. Direct numeric reception_timestamp / timestamp / timestamp_epoch
    2. Parsed ISO 8601 string from timestamp_iso
    3. Recovered system_timestamp_epoch from inner ASTM System messages
    4. Derived timestamp from encounter_id format (e.g., ENC-YYYYMMDD-HHMMSS-...)
    5. fallback_now (defaults to time.time())
    """
    now = fallback_now if fallback_now is not None else time.time()
    min_bound = min_valid if min_valid is not None else DEFAULT_MIN_VALID_TIMESTAMP
    max_bound = now + max_skew_s

    # 1. Primary explicit timestamp fields
    raw_ts = (
        packet.get("reception_timestamp")
        if packet.get("reception_timestamp") is not None
        else (packet.get("timestamp") if packet.get("timestamp") is not None else packet.get("timestamp_epoch"))
    )

    if raw_ts is not None:
        try:
            val = float(raw_ts)
            if min_bound <= val <= max_bound:
                return val
        except (ValueError, TypeError):
            pass

    # 2. ISO 8601 timestamp string fallback
    iso_str = packet.get("timestamp_iso")
    if iso_str:
        val = parse_iso_timestamp(iso_str)
        if val is not None and min_bound <= val <= max_bound:
            return val

    # 3. Inner System message epoch recovery
    msgs = packet.get("messages") or []
    if isinstance(msgs, list):
        for m in msgs:
            if isinstance(m, dict):
                sys_ep = m.get("system_timestamp_epoch")
                if sys_ep is not None:
                    try:
                        val = float(sys_ep)
                        if min_bound <= val <= max_bound:
                            return val
                    except (ValueError, TypeError):
                        pass

    # 4. Parse Base64 messages if messages list was empty
    b64_list = packet.get("messages_b64")
    if b64_list and isinstance(b64_list, list):
        try:
            from scanner.parser import decode_astm_message, parse_astm_payload
            raw_blocks = [base64.b64decode(b) for b in b64_list if isinstance(b, str)]
            if raw_blocks:
                parsed_msgs = []
                if parse_astm_payload:
                    p, _ = parse_astm_payload(b"".join(raw_blocks))
                    if p:
                        parsed_msgs.extend(p)
                elif decode_astm_message:
                    for b in raw_blocks:
                        dm = decode_astm_message(b)
                        if dm:
                            parsed_msgs.append(dm)
                for m in parsed_msgs:
                    if isinstance(m, dict):
                        sys_ep = m.get("system_timestamp_epoch")
                        if sys_ep is not None:
                            try:
                                val = float(sys_ep)
                                if min_bound <= val <= max_bound:
                                    return val
                            except (ValueError, TypeError):
                                pass
        except Exception:
            pass

    # 5. Extract timestamp from encounter_id format (ENC-YYYYMMDD-HHMMSS-...)
    enc_id = packet.get("encounter_id", "")
    if isinstance(enc_id, str) and enc_id.startswith("ENC-"):
        parts = enc_id.split("-")
        if len(parts) >= 3 and len(parts[1]) == 8 and len(parts[2]) == 6:
            try:
                dt = datetime.strptime(f"{parts[1]}_{parts[2]}", "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
                val = dt.timestamp()
                if min_bound <= val <= max_bound:
                    return val
            except Exception:
                pass

    return now
