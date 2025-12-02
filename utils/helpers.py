# utils/helpers.py
# Small config-driven helpers + runtime control helpers used in main.

from __future__ import annotations

from typing import List, Dict, Any
import threading
import signal

from utils.logger import get_logger
logger = get_logger(__name__)


# ====== PUBLIC API ======

def compute_fs_max_from_config(config: Dict[str, Any]) -> float:
    """Return max FS across enabled instances; fallback to 250.0 Hz."""
    # Count enabled instances and track malformed FS values for transparency.
    fs_values: List[float] = []
    enabled_seen = 0  # Enabled instances encountered
    discarded = 0     # Non-numeric or missing FS entries discarded

    devices = config.get("devices", {})
    for _typ, block in devices.items():
        for inst in block.get("INSTANCES", []):
            if not inst.get("ENABLED", False) or not inst.get("EXPORT_ENABLE", True):
                continue  # Skip disabled instances
            enabled_seen += 1  # Track enabled instance seen
            try:
                fs_values.append(float(inst["FS"]))  # FS must be numeric
            except Exception:
                discarded += 1  # Record malformed or missing FS

    if fs_values:
        fs_max = max(fs_values)
        # Log compact summary: derived fs_max and counts for context.
        logger.info(
            "Helpers: fs_max=%.3f Hz from %d enabled instance(s), discarded=%d",
            fs_max, enabled_seen, discarded,
        )
        return fs_max

    # No valid FS → use safe default and warn once.
    default_fs = 250.0
    logger.warning(
        "Helpers: no valid FS found across %d enabled instance(s); using default %.1f Hz",
        enabled_seen, default_fs,
    )
    return default_fs


def collect_known_channels_from_config(config: Dict[str, Any]) -> List[str]:
    """Build 'dev:ch' list from CONFIG for enabled/exportable instances only."""
    # Count export-enabled instances, empty-channel cases, and duplicates.
    devices = config.get("devices", {})
    cols: List[str] = []
    seen = set()  # Preserve order while deduplicating

    export_enabled_instances = 0         # Instances with EXPORT_ENABLE=True
    instances_with_no_channels = 0       # Export-enabled but no enabled channels
    duplicates = 0                       # Duplicated "dev:ch" pairs deduplicated

    for _typ, block in devices.items():
        for inst in block.get("INSTANCES", []):
            if not inst.get("ENABLED", False):
                continue  # Instance disabled globally
            if not inst.get("EXPORT_ENABLE", False):
                continue  # Skip non-exportable instances

            export_enabled_instances += 1  # Count export-enabled instance

            dev = str(inst.get("DEVICE_NAME", "")).strip()
            if not dev:
                continue  # Device name required to build "dev:ch"

            chs = inst.get("CHANNELS", {})
            # Normalize channels to an enabled list (dict of {name: bool} or list[str]).
            if isinstance(chs, dict):
                enabled: List[str] = [k for k, v in chs.items() if v]
            elif isinstance(chs, list):
                enabled = [str(k) for k in chs]
            else:
                enabled = []

            if not enabled:
                instances_with_no_channels += 1  # Export-enabled but empty channel set
                continue

            for ch in enabled:
                key = f"{dev}:{ch}"
                if key in seen:
                    duplicates += 1  # Track duplicates we will ignore
                else:
                    cols.append(key)
                    seen.add(key)

    # Log compact summary and any noteworthy conditions.
    logger.info(
        "Helpers: exportable columns=%d from %d export-enabled instance(s)",
        len(cols), export_enabled_instances,
    )
    if instances_with_no_channels > 0:
        logger.warning(
            "Helpers: %d export-enabled instance(s) with no channels enabled",
            instances_with_no_channels,
        )
    if duplicates > 0:
        logger.warning(
            "Helpers: %d duplicate channel entry(ies) deduplicated",
            duplicates,
        )

    return cols

def iter_enabled_instances(config: Dict[str, Any]):
    """Yield (typ, inst) for enabled instances from CONFIG.devices."""
    devices = config.get("devices", {})
    for typ, block in devices.items():
        for inst in block.get("INSTANCES", []):
            if inst.get("ENABLED", False):
                yield typ, inst



# ====== RUNTIME HELPERS ======

# --- Shared stop flag exposed to the whole app ---
STOP_EVT = threading.Event()  # Set by signal handlers to request shutdown


def _term_handler(_signum, _frame):
    """Handle SIGINT/SIGTERM: close UI if present and set STOP_EVT."""
    try:
        # Lazy import to avoid hard dependency if UI is disabled
        import matplotlib.pyplot as plt  # noqa: WPS433
        try:
            plt.close('all')  # Ask Matplotlib/Tk to exit mainloop if active
        except Exception:
            pass
    except Exception:
        # Matplotlib not available or not needed; ignore
        pass
    STOP_EVT.set()  # Signal cooperative shutdown to waiters


def setup_signal_handlers() -> None:
    """Register SIGINT/SIGTERM to trigger a graceful shutdown via STOP_EVT."""
    signal.signal(signal.SIGINT, _term_handler)  # Ctrl-C
    try:
        signal.signal(signal.SIGTERM, _term_handler)  # kill <pid>
    except Exception:
        # Windows may not support SIGTERM consistently; safe to ignore
        pass


def wait_for_producers(producers) -> None:
    """Cooperative wait for producers; interruptible by STOP_EVT.

    Never block indefinitely on join(); use short timeouts and poll STOP_EVT.
    Exits when all joinable producers have finished or STOP_EVT is set.
    """
    if not producers:
        STOP_EVT.wait()  # Pure idle wait, interruptible
        return

    while True:
        all_done = True  # Assume done until proven otherwise

        for p in producers:
            j = getattr(p, "join", None)
            is_alive = getattr(p, "is_alive", None)

            if callable(j):
                try:
                    j(timeout=0.1)  # Short join to allow signal processing
                except TypeError:
                    # join() without timeout; rely on is_alive
                    pass

                try:
                    if callable(is_alive) and is_alive():
                        all_done = False
                except Exception:
                    # Conservative: assume the producer is still active
                    all_done = False
            else:
                # Not joinable: treat as potentially active
                all_done = False

        if all_done:
            return

        if STOP_EVT.wait(0.2):  # Periodic check + small sleep
            return