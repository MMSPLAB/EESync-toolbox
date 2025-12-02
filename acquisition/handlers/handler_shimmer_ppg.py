# acquisition/handlers/handler_shimmer_ppg.py
# PPG handler for Shimmer: read ADC, convert to mV (optional invert), filter, emit pairs (None for gaps).

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
Factory-based PPG handler. Reads a configured Shimmer ADC channel, converts to
mV (optionally inverted), filters via StreamingSOS, and emits (channel, value|None).
"""

logger = get_logger(__name__)

ValueT = Optional[Union[float, int]]


# ====== HELPERS ======
def _present_channels(pkt: DataPacket) -> List[str]:
    """Return available channel names in this packet (best-effort)."""
    out: List[str] = []
    for name in dir(EChannelType):
        if not name.isupper():
            continue
        try:
            ch = getattr(EChannelType, name)
            _ = pkt[ch]
            out.append(name)
        except Exception:
            pass
    return out


# ====== FACTORY ======
def build_ppg_handler(
    *,
    handler_cfg: dict,
    timebase_key: str,
    stop_event: Event,
    want_raw: bool,
    want_filtered: bool,
) -> Callable[[DataPacket], List[Tuple[str, ValueT]]]:
    """
    Return a per-instance PPG handler that yields (channel, value|None) pairs.

    RAW emits millivolts under 'RAW_ppg_mV' (no filtering).
    Filtered emits millivolts under 'ppg_mV' using a StreamingSOS chain.
    """

    # --- Instance parameters (fallback defaults keep config user-friendly) ---
    VREF_PPG = float(handler_cfg.get("VREF_PPG", 3.0))     # ADC reference (V)
    FS_HZ    = float(handler_cfg.get("FS_HZ", 128.0))      # Sampling rate
    PPG_INV  = bool(handler_cfg.get("PPG_INVERT", True))   # Invert polarity

    PPG_CHANNEL = str(handler_cfg.get("PPG_CHANNEL", "INTERNAL_ADC_13"))
    try:
        ppg_ch_enum = getattr(EChannelType, PPG_CHANNEL)
    except AttributeError:
        raise ValueError(f"Invalid PPG channel in instance '{timebase_key}': {PPG_CHANNEL}")

    # --- Telemetry window (global; keep a safe default) ---
    TELEMETRY_WINDOW_S = float(CONFIG.get("telemetry", {}).get("WINDOW_S", 10.0))

    # --- Filter spec (per-device block) for the logical channel 'ppg_mV' ---
    try:
        spec = dict(CONFIG.get("devices", {}).get("shimmer", {}).get("FILTERS", {}).get("ppg_mV", {}))
    except Exception:
        spec = {}

    # --- Build stateless SOS chain and per-instance streaming filter ---
    # Use a full sensor key "device:channel" for clearer logs and cache scoping.
    sos_chain = design_sos(sensor_key=f"{timebase_key}:ppg_mV", fs_hz=FS_HZ, spec=spec)
    pipe = StreamingSOS(sos_chain, context=f"{timebase_key}:ppg_mV")  # Context for runtime logs

    # --- Telemetry/debug state ---
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
                    "Telemetry window: sensor=PPG[%s] invalid_samples=%d window=%.1fs",
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

        # Read ADC from configured channel
        try:
            adc = int(pkt[ppg_ch_enum])                      # 14-bit ADC 0..16383
        except Exception:
            _telemetry_update(t_s, invalid=True)
            nonlocal _warned_missing
            if not _warned_missing:
                try:
                    avail = _present_channels(pkt)
                    logger.warning("[PPG:%s] MISSING %s — available=%s", timebase_key, PPG_CHANNEL, avail)
                except Exception:
                    pass
                _warned_missing = True
            return []

        # Convert ADC → mV (14-bit range)
        v_mv = (adc / 16383.0) * VREF_PPG * 1000.0
        if PPG_INV:
            v_mv = -v_mv

        out: List[Tuple[str, Optional[float]]] = []
        invalid = False

        # RAW stream (mV) if requested
        if want_raw:
            out.append((f"RAW_ppg_mV", float(v_mv)))

        # Filtered stream (mV) if requested
        if want_filtered:
            v_f = pipe.apply(float(v_mv))
            # Sanitize to None if not finite or unexpected type
            if not (isinstance(v_f, (float, int)) and math.isfinite(float(v_f))):
                export_val: Optional[float] = None
                invalid = True
            else:
                export_val = float(v_f)
            out.append((f"ppg_mV", export_val))

        _telemetry_update(t_s, invalid)
        return out

    logger.info(
        "[PPG:%s] Handler ready (ch=%s, fs=%.1f Hz, invert=%s, raw=%s, filtered=%s)",
        timebase_key, PPG_CHANNEL, FS_HZ, PPG_INV, want_raw, want_filtered
    )

    return handler