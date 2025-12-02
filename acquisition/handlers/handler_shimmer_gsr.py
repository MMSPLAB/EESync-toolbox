# acquisition/handlers/handler_shimmer_gsr.py
# GSR handler for Shimmer: decode RAW→µS, apply streaming SOS (optional), emit pairs with None for gaps.

from __future__ import annotations
from threading import Event
from typing import Callable, Optional, List, Tuple, Union
import math

from pyshimmer import DataPacket, EChannelType

from utils.logger import get_logger
from acquisition.shimmer_timebase import device_time_s
from processing.rt_filter import StreamingSOS, design_sos
from utils.config import CONFIG

"""
Factory-based GSR handler. Decodes Shimmer GSR RAW to µS, applies optional
StreamingSOS filters, and returns (channel, value|None) pairs.
"""

logger = get_logger(__name__)

ValueT = Optional[Union[float, int]]

# Map of range bitfield to feedback resistor (Ohm)
RFEEDBACK_MAP = {
    0: 40_200,
    1: 287_000,
    2: 1_000_000,
    3: 3_300_000,
}


# ====== HELPERS ======
def _present_channels(pkt: DataPacket) -> List[str]:
    """Return available channel names in this packet (best-effort)."""
    out: List[str] = []
    for name in dir(EChannelType):
        if not name.isupper():
            continue
        try:
            ch = getattr(EChannelType, name)
            _ = pkt[ch]  # Access to test presence
            out.append(name)
        except Exception:
            pass
    return out


# ====== FACTORY ======
def build_gsr_handler(
    *,
    handler_cfg: dict,
    timebase_key: str,
    stop_event: Event,
    want_raw: bool,
    want_filtered: bool,
) -> Callable[[DataPacket], List[Tuple[str, ValueT]]]:
    """
    Return a per-instance GSR handler that yields (channel, value|None) pairs.

    The handler:
    - Decodes Shimmer GSR RAW to µS (None if invalid).
    - Filters per CONFIG.devices.shimmer.FILTERS['gsr_uS'] using StreamingSOS.
    - Emits RAW and/or filtered channels per flags.
    """

    # --- Electrical and fs parameters (instance-scoped) ---
    VREF_GSR = float(handler_cfg.get("VREF_GSR", 3.0))     # ADC reference (V)
    V_BIAS   = float(handler_cfg.get("V_BIAS", 0.5))       # Mid-bias (V)
    FS_HZ    = float(handler_cfg.get("FS_HZ", 128.0))      # Sampling rate

    # --- Telemetry params (global) ---
    TELEMETRY_WINDOW_S = float(CONFIG.get("telemetry", {}).get("WINDOW_S", 10.0))

    # --- Filter spec (per-device block) ---
    try:
        shimmer_block = CONFIG.get("devices", {}).get("shimmer", {})
        filters_block = shimmer_block.get("FILTERS", {})
        gsr_spec = dict(filters_block.get("gsr_uS", {}))  # local copy
    except Exception:
        gsr_spec = {}

    # --- Build stateless SOS chain and per-instance streaming filter ---
    # Use a full sensor key "device:channel" for clearer logs and cache scoping.
    sos_chain = design_sos(sensor_key=f"{timebase_key}:gsr_uS", fs_hz=FS_HZ, spec=gsr_spec)
    pipe = StreamingSOS(sos_chain, context=f"{timebase_key}:gsr_uS")  # Context for runtime logs

    # --- Telemetry state (invalid sample aggregation) ---
    _invalid_count = 0
    _last_telem_t0: Optional[float] = None
    _warned_missing: bool = False

    def _telemetry_update(t_s: float, invalid: bool) -> None:
        """Aggregate invalid samples and emit every TELEMETRY_WINDOW_S seconds."""
        nonlocal _invalid_count, _last_telem_t0
        if _last_telem_t0 is None:
            _last_telem_t0 = t_s
        if invalid:
            _invalid_count += 1
        elapsed = t_s - _last_telem_t0
        if elapsed >= TELEMETRY_WINDOW_S:
            if _invalid_count > 0:
                logger.warning(
                    "Telemetry window: sensor=GSR[%s] invalid_samples=%d window=%.1fs",
                    timebase_key, _invalid_count, elapsed
                )
                _invalid_count = 0
            _last_telem_t0 = t_s

    def handler(pkt: DataPacket) -> List[Tuple[str, ValueT]]:
        """Process one packet and return the desired channel pairs."""
        if stop_event.is_set():
            return []

        # Device-relative time (for telemetry only)
        t_s = device_time_s(pkt, key=timebase_key)

        # Read packed GSR RAW (2 MSB = range, 14 LSB = ADC)
        try:
            gsr_raw = int(pkt[EChannelType.GSR_RAW])
        except Exception:
            _telemetry_update(t_s, invalid=True)
            nonlocal _warned_missing
            if not _warned_missing:
                try:
                    avail = _present_channels(pkt)
                    logger.warning("[GSR:%s] MISSING GSR_RAW — available=%s", timebase_key, avail)
                except Exception:
                    pass
                _warned_missing = True
            return []

        # --- Decode range and ADC ---
        range_code = (gsr_raw >> 14) & 0x03                  # Extract range code (0..3)
        adc        = gsr_raw & 0x3FFF                        # 14-bit ADC (0..16383)
        r_feedback = RFEEDBACK_MAP.get(range_code, RFEEDBACK_MAP[0])  # Fallback to range 0
        vin = (adc / 16383.0) * VREF_GSR                     # Convert ADC to input voltage

        # --- Convert to µS with bias guard ---
        invalid = False
        gsr_uS: Optional[float]
        if 0.0 < vin < V_BIAS:                               # Valid only below bias
            r_skin = r_feedback * (V_BIAS / vin - 1.0)       # Skin resistance estimation
            gsr_uS = 1e6 / max(r_skin, 1e-12)                # Conductance in µS
        else:
            gsr_uS = None                                    # Mark invalid sample
            invalid = True

        out: List[Tuple[str, Optional[float]]] = []

        # --- RAW stream (µS) if requested ---
        if want_raw:
            out.append((f"RAW_gsr_uS", gsr_uS))

        # --- Filtered stream if requested ---
        if want_filtered:
            if gsr_uS is None:
                out.append((f"gsr_uS", None))        # Preserve gap on invalid
                invalid = True
            else:
                v_f = pipe.apply(float(gsr_uS))              # Apply filter on valid only
                # Sanitize to None if not finite or unexpected type
                if not (isinstance(v_f, (float, int)) and math.isfinite(float(v_f))):
                    out.append((f"gsr_uS", None))
                    invalid = True
                else:
                    out.append((f"gsr_uS", float(v_f)))

        _telemetry_update(t_s, invalid)
        return out
    
    logger.info(
        "[GSR:%s] Handler ready (fs=%.1f Hz, raw=%s, filtered=%s)",
        timebase_key, FS_HZ, want_raw, want_filtered
    )

    return handler