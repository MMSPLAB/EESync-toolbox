# processing/rt_filter.py
# Streaming SOS filtering utilities: design (cached) and per-instance stateful apply.

from __future__ import annotations

from functools import lru_cache
from typing import List, Tuple, cast

import numpy as np
import scipy.signal as signal

from utils.logger import get_logger

logger = get_logger(__name__)


# ====== PUBLIC TYPES ======
SOSArray = np.ndarray  # shape: (n_sections, 6), SciPy SOS (Second-Order Sections) format


# ====== STREAMING FILTER (STATEFUL) ======
class StreamingSOS:
    """Stateful streaming SOS filter chain for realtime single-sample processing."""
    def __init__(self, sos_chain: List[SOSArray], context: str | None = None):
        """Build with a list of SOS stages; empty list means identity."""
        self._sos_chain: List[SOSArray] = list(sos_chain)
        self._zi_chain: List[np.ndarray] = [
            signal.sosfilt_zi(sos) * 0.0 for sos in self._sos_chain
        ]
        self._ctx = str(context) if context else ""  # Optional 'dev:ch' tag
        # Log concise init with stage count and optional context tag.
        logger.info(
            "StreamingSOS init: stages=%d%s",
            len(self._sos_chain),
            (f", ctx={self._ctx}" if self._ctx else "")
        )

    def reset(self) -> None:
        """Reset internal states (zi) to zero without changing the topology."""
        self._zi_chain = [signal.sosfilt_zi(sos) * 0.0 for sos in self._sos_chain]
        if self._ctx:
            logger.info("StreamingSOS state reset (ctx=%s)", self._ctx)
        else:
            logger.info("StreamingSOS state reset")

    def apply(self, x: float) -> float:
        """Filter a single sample through the chain; NaN passes through unchanged."""
        if not self._sos_chain:
            return x
        if isinstance(x, float) and np.isnan(x):
            return x
        y = float(x)
        try:
            for i, sos in enumerate(self._sos_chain):
                y_arr, zi_next = signal.sosfilt(sos, [y], zi=self._zi_chain[i])
                y = float(y_arr[0])
                self._zi_chain[i] = zi_next
            return y
        except Exception as e:
            # Fail-safe: surface error and pass-through the raw sample.
            if self._ctx:
                logger.error("StreamingSOS apply failed (ctx=%s): %s", self._ctx, e)
            else:
                logger.error("StreamingSOS apply failed: %s", e)
            return x


# ====== SOS DESIGN (STATELESS, CACHED) ======
# --- Internal: normalize spec dict into primitives (with defaults) ---
def _parse_spec(
    fs_hz: float,
    spec: dict,
) -> Tuple[bool, int, float, float, int, float]:
    """Extract and validate primitives: (bp_enable, bp_order, low, high, notch, notch_q)."""
    # Pull with defaults; upstream should supply correct types via CONFIG.
    bp_enable = bool(spec.get("BANDPASS_ENABLE", False))
    bp_order = int(spec.get("BANDPASS_ORDER", 4))
    low_hz = float(spec.get("LOW_HZ", 0.1))
    high_hz = float(spec.get("HIGH_HZ", 10.0))
    notch = int(spec.get("NOTCH", 0))          # 0 disables, 50 or 60 enables
    notch_q = float(spec.get("NOTCH_Q", 30.0))

    # Validate band edges; disable BP if invalid to avoid unstable designs.
    nyq = fs_hz / 2.0
    bp_valid = (bp_enable and 0.0 < low_hz < high_hz < nyq and bp_order >= 1)
    if bp_enable and not bp_valid:
        logger.warning(
            "Band-pass invalid (low=%.3f, high=%.3f, order=%d, nyq=%.3f) — disabled.",
            low_hz, high_hz, bp_order, nyq
        )
        bp_enable = False

    # Validate notch: only 50 or 60 are accepted; 0 disables.
    if notch not in (0, 50, 60):
        logger.warning("Notch=%s unsupported (use 0, 50 or 60) — disabled.", notch)
        notch = 0

    # Validate Q; guard against non-positive values.
    if notch != 0 and notch_q <= 0.0:
        logger.warning("NOTCH_Q=%.3f invalid — notch disabled.", notch_q)
        notch = 0

    return bp_enable, bp_order, low_hz, high_hz, notch, notch_q


@lru_cache(maxsize=128)
def _design_sos_cached(
    sensor_key: str,
    fs_hz: float,
    bp_enable: bool,
    bp_order: int,
    low_hz: float,
    high_hz: float,
    notch: int,
    notch_q: float,
) -> Tuple[SOSArray, ...]:
    """Cached SOS designer. Keyed only by primitives for deterministic reuse.

    Returns a tuple of SOS arrays, one per stage (e.g., (notch_sos, bp_sos)).
    Empty tuple means identity (no filtering).
    """
    stages: List[SOSArray] = []

    # Optional notch
    if notch in (50, 60):
        try:
            b, a = signal.iirnotch(w0=float(notch), Q=notch_q, fs=fs_hz)
            sos_notch = cast(SOSArray, signal.tf2sos(b, a))   # type: ignore[assignment]
            stages.append(sos_notch)
        except Exception as e:
            logger.error("design_sos: notch failed (sensor=%s): %s", sensor_key, e)

    # Optional band-pass
    if bp_enable:
        try:
            sos_bp = cast(SOSArray, signal.butter(            # type: ignore[assignment]
                N=bp_order, Wn=[low_hz, high_hz], btype="bandpass", output="sos", fs=fs_hz,
            ))
            stages.append(sos_bp)
        except Exception as e:
            logger.error("design_sos: bandpass failed (sensor=%s): %s", sensor_key, e)

    # Return immutable tuple to satisfy cache requirements.
    return tuple(stages)


def design_sos(sensor_key: str, fs_hz: float, spec: dict) -> List[SOSArray]:
    """Design a list of SOS stages for a sensor using a spec dict.

    The returned list is stateless and can be shared; use StreamingSOS
    per instance to hold per-device filter states (zi).
    """
    # Normalize and validate the spec to primitives suitable for caching.
    bp_enable, bp_order, low_hz, high_hz, notch, notch_q = _parse_spec(fs_hz, spec)

    # Delegate to the cached designer; convert tuple → list for callers.
    sos_tuple = _design_sos_cached(
        sensor_key,
        float(fs_hz),
        bool(bp_enable),
        int(bp_order),
        float(low_hz),
        float(high_hz),
        int(notch),
        float(notch_q),
    )

    # Build human-readable summary before returning.
    try:
        parts: List[str] = []
        if notch in (50, 60):
            parts.append(f"notch={notch}Hz(Q={notch_q:.1f})")
        if bp_enable:
            parts.append(f"bp=on[{low_hz:.2f}-{high_hz:.2f} Hz, ord={bp_order}]")
        summary = ", ".join(parts) if parts else "identity"
        logger.info("design_sos: sensor=%s → %s", sensor_key, summary)
    except Exception:
        pass

    return [s for s in sos_tuple]
