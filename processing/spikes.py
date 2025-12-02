# processing/spikes.py
# Non-sticky spike bus: one-shot notifications with keymap triggers (thread-safe).

from __future__ import annotations
from typing import Callable, Dict, Optional, List, Tuple
import threading
import time

from utils.logger import get_logger
from utils.config import CONFIG

# ====== CONFIG & LOGGER ======
logger = get_logger(__name__)

# ====== TYPE DEFINITIONS ======
# Subscriber signature: (timestamp_s, new_spike, source)
Subscriber = Callable[[float, str, str], None]


# ====== SPIKE BUS ======
class SpikeBus:
    """One-shot spike notifications with thread-safe broadcasting.

    Spikes are instantaneous markers: no sticky state is stored or exposed.
    Triggers can come from a keymap (keyboard) or from API calls at runtime.
    """

    def __init__(self, keymap: Dict[str, str], enabled: Optional[bool] = None):
        """Create a non-sticky bus with an optional enable flag.

        If 'enabled' is None, falls back to CONFIG['spikes']['ENABLE_SPIKE_TRIGGERS'].
        The provided keymap is copied defensively to isolate internal state.
        """
        # Resolve enable flag with a safe default (True if missing in config).
        if enabled is None:
            _spikes_cfg = dict(CONFIG.get("spikes", {}))  # safe default layer
            self._enabled = bool(_spikes_cfg.get("ENABLE_SPIKE_TRIGGERS", True))
        else:
            self._enabled = bool(enabled)

        # Defensive copy prevents external mutations from affecting the bus.
        self._keymap = dict(keymap)

        # Log available spike labels once at startup (compact summary).
        try:
            labels = sorted({str(v) for v in self._keymap.values()})
            logger.info("SpikeBus triggers: %s", ", ".join(labels) if labels else "none")
        except Exception:
            pass  # Keep construction robust if keymap is malformed

        # Subscribers container and re-entrant lock for thread safety.
        self._subs: List[Subscriber] = []
        self._lock = threading.Lock()

        logger.info("SpikeBus ready: enabled=%s", self._enabled)

    # --- Query (parity only; spikes have no sticky state) ---
    def current(self) -> Tuple[str, float]:
        """Return a sentinel non-sticky state ('NONE', 0.0)."""
        # Provided for API parity with EventBus; not used to carry state.
        return "NONE", 0.0

    # --- Subscription ---
    def subscribe(self, fn: Subscriber) -> None:
        """Register a subscriber for spike notifications."""
        with self._lock:
            self._subs.append(fn)
            logger.info("SpikeBus: subscriber added (n=%d)", len(self._subs))  # Count helps debugging wiring

    # --- Triggers (keyboard/API) ---
    def set_by_key(self, key: str, source: str = "keyboard") -> None:
        """Fire a spike mapped from `key` (no toggle, non-sticky)."""
        if not self._enabled:
            logger.warning("SpikeBus: ignored key='%s' (triggers disabled)", key)
            return
        name = self._keymap.get(key)
        if name is None:
            logger.warning("SpikeBus: unmapped spike key='%s'", key)
            return
        # Mirror EventBus: compute target and delegate to the common setter.
        with self._lock:
            target = name  # no toggle for spikes
        self.set_spike(target, source=source)

    def set_spike(self, name: str, source: str = "api") -> None:
        """Emit a spike 'now' with monotonic timestamp (non-sticky)."""
        if not self._enabled:
            logger.warning("SpikeBus: ignored spike='%s' (triggers disabled)", name)
            return
        now = time.monotonic()
        # Snapshot subscribers under lock; notify out of the lock (like EventBus).
        with self._lock:
            subs = list(self._subs)
        for fn in subs:
            try:
                fn(now, name, source)
            except Exception as e:
                logger.error("SpikeBus: subscriber failed: %s", e)  # Protect bus; report once per failure

    def announce_at(
        self,
        ts_s: float,
        new_spike: str,
        source: str = "sync",
    ) -> None:
        """Notify a spike at an explicit timestamp (non-sticky).

        Use when an externally-quantized timestamp is available and must
        be preserved. No sticky state is updated (there is none).
        """
        # Snapshot subscribers; no shared state mutation occurs here.
        with self._lock:
            subs = list(self._subs)

        # Notify out of the lock for safety and responsiveness.
        for fn in subs:
            try:
                fn(float(ts_s), new_spike, source)
            except Exception as e:
                logger.error("SpikeBus: subscriber failed: %s", e)


# ====== SINGLETON ======
# Convenience singleton for imports: from processing.spikes import spike_bus
spike_bus = SpikeBus(dict(CONFIG.get("spikes", {}).get("SPIKE_KEYMAP", {})))
