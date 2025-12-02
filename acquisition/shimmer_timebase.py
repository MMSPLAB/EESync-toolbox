# acquisition/shimmer_timebase.py
# Convert Shimmer 16-bit ticks (32,768 Hz) into seconds, handling rollovers thread-safely.

from __future__ import annotations

from threading import Lock
from typing import TypedDict, Optional, Dict
from pyshimmer import EChannelType
from utils.logger import get_logger

"""
Maintain a device-aligned timebase. Anchor on first observed tick, detect
16-bit counter rollovers, and expose a thread-safe conversion to seconds.
"""

logger = get_logger(__name__)

# --- Shimmer timestamp characteristics (constants) ---
_TICK_RATE_HZ: float = 32768.0     # Device tick frequency
_COUNTER_MOD: int = 65536          # 16-bit counter modulus


# --- Per-device state (protected by _STATE_LOCK) ---
class _StateEntry(TypedDict):
    start: Optional[int]  # First observed tick (anchor); None before first packet
    last: Optional[int]   # Last raw tick observed (for rollover detection)
    offset: int           # Accumulated ticks added at each rollover

_STATE: Dict[str, _StateEntry] = {}   # key -> {"start": int|None, "last": int|None, "offset": int}
_STATE_LOCK = Lock()                  # Ensure thread-safe access


# ====== PUBLIC API ======
def reset(key: str = "default") -> None:
    """Reset timebase for a device key (anchor cleared, offset=0)."""
    with _STATE_LOCK:
        _STATE[key] = {"start": None, "last": None, "offset": 0}
    logger.debug("Shimmer timebase reset for key=%s.", key)


def device_time_s(pkt, key: str = "default") -> float:
    """Convert packet tick counter to seconds (thread-safe, rollover-aware)."""
    # Extract raw 16-bit tick from packet (raises if missing)
    ts_raw = int(pkt[EChannelType.TIMESTAMP])

    with _STATE_LOCK:
        st = _STATE.setdefault(key, {"start": None, "last": None, "offset": 0})
        start = st["start"]
        last = st["last"]
        offset = st["offset"]

        # Anchor on first packet
        if start is None:
            st["start"] = ts_raw
            st["last"] = ts_raw
            st["offset"] = 0
            start = ts_raw
            last = ts_raw
            offset = 0
            logger.info("Shimmer timebase anchored for key=%s (start_ts=%d).", key, ts_raw)
        else:
            # Detect 16-bit rollover: current raw tick wrapped below last seen
            if last is not None and ts_raw < last:
                st["offset"] = offset + _COUNTER_MOD
                offset = st["offset"]
            # Update last seen raw tick
            st["last"] = ts_raw

        # Total ticks since anchor = accumulated offset + (current - start)
        total_ticks = offset + (ts_raw - start)

    return float(total_ticks) / _TICK_RATE_HZ