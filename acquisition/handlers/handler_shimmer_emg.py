# acquisition/handlers/handler_shimmer_emg.py
# EMG handler for Shimmer: two channels, convert counts→µV, filter, emit pairs (None for gaps).

from __future__ import annotations
from threading import Event
from typing import Callable, Optional, List, Tuple, Union, Dict
import math

from pyshimmer import DataPacket, EChannelType

from utils.logger import get_logger
from acquisition.shimmer_timebase import device_time_s
from processing.rt_filter import StreamingSOS, design_sos
from utils.config import CONFIG

"""
Factory-based EMG handler.
Converts counts→µV, filters via StreamingSOS, emits (name, value|None) pairs.
"""

logger = get_logger(__name__)

ValueT = Optional[Union[float, int]]

# ====== HELPERS ======
def _present_channels(pkt: DataPacket) -> List[str]:
    """Return available EChannelType names found in this packet."""
    out: List[str] = []
    for name in dir(EChannelType):
        if not name.isupper():
            continue  # Skip non-enum attributes
        try:
            ch = getattr(EChannelType, name)         # Resolve enum member
            _ = pkt[ch]                               # Probe access; raises if absent
            out.append(name)                          # Keep only present channels
        except Exception:
            pass                                       # Best-effort listing
    return out


# ====== FACTORY ======
def build_emg_handler(
    *,
    handler_cfg: dict,
    timebase_key: str,
    stop_event: Event,
    # Explicit per-channel flags (mirror PPG/GSR style)
    want_emg1_raw: bool,
    want_emg2_raw: bool,
    want_emg1_flt: bool,
    want_emg2_flt: bool,
) -> Callable[[DataPacket], List[Tuple[str, ValueT]]]:
    """
    Return EMG handler emitting (channel, value|None) pairs.

    Two physical channels max. Flags control RAW/filtered per channel.
    """

    # --- Instance parameters (fallbacks keep config user-friendly) ---
    VREF_EXG = float(handler_cfg.get("VREF_EXG", 2.42))   # ADS1292R Vref (V)
    EXG_GAIN = float(handler_cfg.get("EXG_GAIN", 12.0))   # PGA gain
    FS_HZ    = float(handler_cfg.get("FS_HZ", 512.0))     # Effective fs (Hz)

    # --- Resolve first two EMG enums from config (fail-fast, no discovery) ---
    names_cfg: List[str] = list(handler_cfg.get("EMG_CHANNELS", []))  # Ordered
    if not names_cfg:
        raise ValueError(f"[EMG:{timebase_key}] EMG_CHANNELS missing in config")
    names_cfg = names_cfg[:2]  # Only CH1..CH2 supported

    emg_enums: Dict[int, EChannelType] = {}
    for i, nm in enumerate(names_cfg, start=1):
        try:
            emg_enums[i] = getattr(EChannelType, str(nm))  # Resolve enum once
        except AttributeError:
            raise ValueError(f"[EMG:{timebase_key}] Invalid EMG channel enum: {nm}")
    n_phys = min(2, len(emg_enums))  # Guard even if list longer

    # --- Filter spec (per-device block) for logical 'emg_uV' ---
    try:
        spec = dict(CONFIG.get("devices", {}).get("shimmer", {}).get("FILTERS", {}).get("emg_uV", {}))
    except Exception:
        spec = {}

    # --- Build stateless SOS chain and per-instance streaming filter ---
    pipes: Dict[int, StreamingSOS] = {}
    for i in range(1, n_phys + 1):
        sos = design_sos(sensor_key=f"{timebase_key}:emg{i}_uV", fs_hz=FS_HZ, spec=spec)
        pipes[i] = StreamingSOS(sos, context=f"{timebase_key}:emg{i}_uV")  # Independent state

    # --- Telemetry window/state ---
    TELEMETRY_WINDOW_S = float(CONFIG.get("telemetry", {}).get("WINDOW_S", 10.0))
    _invalid_count: Dict[int, int] = {i: 0 for i in range(1, 3)}
    _last_telem_t0: Optional[float] = None
    _warned_missing: Dict[int, bool] = {i: False for i in range(1, 3)}

    def _telemetry_update(t_s: float, ch_i: int, invalid: bool) -> None:
        """Aggregate invalids per channel; emit every TELEMETRY_WINDOW_S seconds."""
        nonlocal _last_telem_t0
        if _last_telem_t0 is None:
            _last_telem_t0 = t_s
        if invalid:
            _invalid_count[ch_i] += 1
        elapsed = t_s - _last_telem_t0
        if elapsed >= TELEMETRY_WINDOW_S:
            for j in range(1, 3):
                cnt = _invalid_count[j]
                if cnt > 0:
                    logger.warning(
                        "Telemetry window: sensor=EMG[%s].ch%d invalid_samples=%d window=%.1fs",
                        timebase_key, j, cnt, elapsed
                    )
                    _invalid_count[j] = 0
            _last_telem_t0 = t_s

    def _counts_to_uV(enum_name: str, counts: int) -> float:
        """Convert ADS1292R signed counts to microvolts (24/16-bit aware)."""
        fs_code = 32767.0 if enum_name.endswith("16BIT") else 8388607.0  # ADC range
        return (float(counts) / fs_code) * (VREF_EXG / EXG_GAIN) * 1e6    # µV

    # --- Channel processing helpers (reduce duplication) ---
    def _process_ch(i: int, raw_flag: bool, flt_flag: bool, pkt: DataPacket, t_s: float,
                    out: List[Tuple[str, ValueT]]) -> None:
        """Read counts, convert to µV, push RAW/filtered; log gaps and missing."""
        enum = emg_enums.get(i)
        if enum is None:
            return  # Channel not configured
        enum_name = getattr(enum, "name", str(enum))
        try:
            counts = int(pkt[enum])                       # Signed 24/16-bit
            uV = _counts_to_uV(enum_name, counts)         # Counts→µV

            if raw_flag:
                out.append((f"RAW_emg{i}_uV", float(uV)))  # RAW

            if flt_flag:
                v_f = pipes[i].apply(float(uV))           # Filter chain
                ok = isinstance(v_f, (float, int)) and math.isfinite(float(v_f))
                out.append((f"emg{i}_uV", float(v_f) if ok else None))
                if not ok:
                    _telemetry_update(t_s, i, invalid=True)

        except Exception:
            if not _warned_missing[i]:
                # Log once with a short preview of present channels
                try:
                    avail = _present_channels(pkt)[:20]   # Avoid huge logs
                    logger.warning("[EMG:%s] MISSING %s (ch%d) — available=%s",
                                   timebase_key, enum_name, i, avail)
                except Exception:
                    logger.warning("[EMG:%s] MISSING %s (ch%d)", timebase_key, enum_name, i)
                _warned_missing[i] = True
            _telemetry_update(t_s, i, invalid=True)

    # --- Handler closure ---
    def handler(pkt: DataPacket) -> List[Tuple[str, ValueT]]:
        """Process one packet and return enabled channel/value pairs."""
        if stop_event.is_set():
            return []  # Early exit on stop

        t_s = device_time_s(pkt, key=timebase_key)        # Device-relative seconds
        out: List[Tuple[str, ValueT]] = []

        _process_ch(1, want_emg1_raw, want_emg1_flt, pkt, t_s, out)  # Channel 1
        _process_ch(2, want_emg2_raw, want_emg2_flt, pkt, t_s, out)  # Channel 2

        return out

    logger.info(
        "[EMG:%s] Handler ready (chs=%s, fs=%.1f Hz, Vref=%.3f V, gain=%.1f, flags={raw1:%s,raw2:%s,flt1:%s,flt2:%s})",
        timebase_key,
        [getattr(emg_enums[i], "name", "?") for i in range(1, n_phys + 1)],
        FS_HZ, VREF_EXG, EXG_GAIN,
        want_emg1_raw, want_emg2_raw, want_emg1_flt, want_emg2_flt,
    )
    return handler