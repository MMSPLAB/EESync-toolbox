# acquisition/event_demo.py
# Randomized event marker generator for demo sync integration.

from __future__ import annotations

from utils.logger import get_logger
import random  # Randomized cadence for marker emission
import threading
from typing import Iterable, Optional, Tuple

from utils.config import CONFIG
from processing.sync_controller import sync_manager as SYNC

logger = get_logger(__name__)

# ====== RUNNER ======
class _PeriodicEventRunner:
    """Run a background thread that periodically sets sticky events.

    Emits SYNC.set_event(label, source). Stops cooperatively via stop().
    """

    def __init__(self, name: str, delay_range_sec: Tuple[float, float], labels: Iterable[str]) -> None:
        """Init runner with source name, delay range, and label pool."""
        self._name = str(name)                         # Source for controller
        self._delay_range = tuple(delay_range_sec)     # Emit cadence range (s)
        self._labels = list(labels) or ["TASK"]        # Available labels
        self._stop = threading.Event()                 # Cooperative stop flag
        self._thr = threading.Thread(            # Daemon thread drives emission loop
            target=self._loop, name=f"EventDemo:{self._name}", daemon=True
        )

    def start(self) -> "_PeriodicEventRunner":
        """Start the background thread."""
        logger.info("Starting event demo emitter '%s'", self._name)
        self._thr.start()
        return self

    def stop(self) -> None:
        """Signal stop and wait briefly for thread exit."""
        self._stop.set()
        try:
            self._thr.join(timeout=1.0)
        except Exception:
            pass
        logger.info("Stopped event demo emitter '%s'", self._name)

    # --- Thread loop ---
    def _loop(self) -> None:
        """Periodic emit loop that picks random labels with random delays."""
        # Startup grace period to let other components initialize
        if self._stop.wait(3.0):
            return
        # Initial stagger to reduce same-bucket collisions across instances
        self._stop.wait((hash(self._name) % 100) / 100.0)  # ≤ 0.99 s
        while not self._stop.is_set():
            try:
                label = random.choice(self._labels)       # Pick label
                SYNC.set_event(label, self._name)         # Sticky trigger
            except Exception:
                pass                                      # Keep running
            self._stop.wait(self._next_delay())           # Cadence sleep

    def _next_delay(self) -> float:
        """Pick the next delay (s) within the configured range."""
        lo, hi = self._delay_range
        if hi <= lo:
            return max(lo, 0.0)              # Degenerate range collapses to single delay
        return max(random.uniform(lo, hi), 0.0)


def _sanitize_delay_range(delay_range) -> Optional[Tuple[float, float]]:
    """Return a validated (lo, hi) tuple in seconds or None."""
    if delay_range is None:
        return None
    try:
        lo, hi = delay_range                  # Expect simple iterable with two values
    except (TypeError, ValueError):
        return None
    try:
        lo_f = float(lo)
        hi_f = float(hi)
    except (TypeError, ValueError):
        return None
    if hi_f < lo_f:
        lo_f, hi_f = hi_f, lo_f
    lo_f = max(lo_f, 0.0)
    hi_f = max(hi_f, 0.0)
    return (lo_f, hi_f)


def _lookup_config_delay_range(name: str) -> Optional[Tuple[float, float]]:
    """Fetch delay range for the given generator name from CONFIG."""
    generators = CONFIG.get("marker_generators", {}).get("event_demo", {}).get("INSTANCES", [])
    for inst in generators:                   # Locate the matching generator entry
        if str(inst.get("GENERATOR_NAME", "")).strip() == name:
            return _sanitize_delay_range(inst.get("DELAY_RANGE_SEC"))
    return None


# ====== PUBLIC API ======
def start_event_demo(
    name: str,
    interval_s: Optional[float] = None,
    labels: Optional[Iterable[str]] = None,
    delay_range_sec: Optional[Tuple[float, float]] = None,
):
    """Start a single event demo runner and return it.

    If labels is None, use EVENT_KEYMAP values excluding 'REST'.
    """
    if labels is None:
        vals = (CONFIG.get("events", {}).get("EVENT_KEYMAP", {}) or {}).values()
        labels = [str(v) for v in vals if str(v).upper() != "REST"] or ["TASK"]  # Skip REST
    delay_range = (                            # Resolve precedence for delay configuration
        _sanitize_delay_range(delay_range_sec)
        or _lookup_config_delay_range(name)
        or _sanitize_delay_range((interval_s, interval_s))
        or (1.0, 1.0)
    )
    return _PeriodicEventRunner(name=name, delay_range_sec=delay_range, labels=labels).start()
