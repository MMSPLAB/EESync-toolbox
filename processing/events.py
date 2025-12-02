# processing/events.py
# Sticky event state + subscriber broadcast (thread-safe). Keyboard/API triggers.

from __future__ import annotations
from typing import Callable, Dict, Optional, List, Tuple
import threading
import time

from utils.logger import get_logger
from utils.config import CONFIG

# ====== CONFIG & LOGGER ======
logger = get_logger(__name__)

# ====== TYPE DEFINITIONS ======
# Subscriber signature: (timestamp_s, new_event, prev_event, source)
Subscriber = Callable[[float, str, str, str], None]


# ====== EVENT BUS ======
class EventBus:
    """Sticky event state with thread-safe notifications.

    Keeps a current event name (sticky) and notifies subscribers on changes.
    Triggers can be driven by a keymap (keyboard) or programmatic API calls.
    """

    def __init__(self, keymap: Dict[str, str], default_name: Optional[str] = None):
        """Build the bus with a keymap and an optional default event.

        If no default is provided, the first mapped value is used, else 'REST'.
        Triggers can be globally enabled/disabled via CONFIG['events'] flags.
        """

        self._enabled = bool(CONFIG.get("events", {}).get("ENABLE_EVENT_TRIGGERS", False))

        # Make a defensive copy of the provided keymap.
        self._keymap = dict(keymap)

        # Pick default: user-provided > first value from keymap > 'REST'.
        if default_name is not None:
            self._default = default_name
        else:
            self._default = list(keymap.values())[0] if keymap else "REST"

        # Log available event labels once (compact, ordered set).
        try:
            labels = list(dict.fromkeys(self._keymap.values()))  # Preserve order
            logger.info("EventBus triggers: %s", ", ".join(labels) if labels else "none")
        except Exception:
            pass


        # Sticky state: name and last change timestamp (monotonic seconds).
        self._cur_name: str = self._default
        self._cur_changed_ts: float = time.monotonic()

        # Subscribers and lock for thread-safe access.
        self._subs: List[Subscriber] = []
        self._lock = threading.Lock()

        logger.info("EventBus ready: default=%s enabled=%s", self._default, self._enabled)

    # --- Query ---
    def current(self) -> Tuple[str, float]:
        """Return the current sticky event and its last change timestamp."""
        # Guarded read to keep (name, ts) consistent under concurrent writers.
        with self._lock:
            return self._cur_name, self._cur_changed_ts

    # --- Subscription ---
    def subscribe(self, fn: Subscriber) -> None:
        """Register a subscriber for change notifications."""
        with self._lock:
            self._subs.append(fn)
            logger.info("EventBus: subscriber added (n=%d)", len(self._subs))

    # --- Triggers ---
    def set_by_key(self, key: str, source: str = "keyboard") -> None:
        """Toggle event using a key: same key twice returns to default."""
        # If triggers are disabled, ignore all requests.
        if not self._enabled:
            logger.warning("EventBus: ignored key='%s' (triggers disabled)", key)
            return

        # Look up the event name for the pressed key.
        name = self._keymap.get(key)
        if name is None:
            logger.warning("EventBus: unmapped event key='%s'", key)
            return

        # Compute the target: pressing the current event toggles to default.
        with self._lock:
            target = self._default if name == self._cur_name else name

        # Delegate to the common setter (single change path).
        self.set_event(target, source=source)

    def set_event(self, name: str, source: str = "api") -> None:
        """Set a new event and notify subscribers if it actually changed."""
        # Respect the global enable flag (keyboard/API unified gate).
        if not self._enabled:
            logger.warning("EventBus: ignored event='%s' (triggers disabled)", name)
            return

        # Capture 'now' with a monotonic clock to avoid wall-clock jumps.
        now = time.monotonic()

        # Update sticky state under lock and snapshot subscribers.
        with self._lock:
            prev = self._cur_name
            if name == prev:
                return  # No-op if the event is unchanged
            self._cur_name = name
            self._cur_changed_ts = now
            subs = list(self._subs)  # Copy for out-of-lock notifications

        # Notify out of the lock to avoid deadlocks/long critical sections.
        for fn in subs:
            try:
                fn(now, name, prev, source)
            except Exception as e:
                logger.error("EventBus: subscriber failed: %s", e)

    def announce_change_at(
        self,
        ts_s: float,
        new_event: str,
        prev_event: str,
        source: str = "sync",
    ) -> None:
        """Notify a change at a specific timestamp without changing sticky state.

        Use when a perfect, externally-quantized timestamp is available.
        The sticky state (current event) remains untouched by this call.
        """
        # Snapshot subscribers; no state mutation is performed here.
        with self._lock:
            subs = list(self._subs)

        # Notify out of the lock for safety.
        for fn in subs:
            try:
                fn(ts_s, new_event, prev_event, source)
            except Exception as e:
                logger.error("EventBus: subscriber failed: %s", e)


# ====== SINGLETON ======
# Convenience singleton for imports: from processing.events import event_bus
event_bus = EventBus(CONFIG.get("events", {}).get("EVENT_KEYMAP", {}))