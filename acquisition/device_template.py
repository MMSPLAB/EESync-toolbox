# acquisition/device_template.py
# Skeleton device module: structure-only manager + idle worker, no emissions.

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional, Tuple, Sequence

from utils.logger import get_logger
from processing.sync_controller import sync_manager as SYNC

logger = get_logger(__name__)


# ====== WORKER THREAD (IDLE SKELETON) ======
class _TemplateThread(threading.Thread):
    """Idle worker loop to show where acquisition and enqueue should go.

    This thread does not emit any packets. It documents the expected places
    for: device read, channel mapping, and SYNC.enqueue_packet(...).

    Marker APIs:
      - SYNC.set_event(label, source)        # sticky event change
      - SYNC.trigger_spike(label, source)    # instantaneous spike
    """

    def __init__(
        self,
        device_name: str,
        fs_hz: float,
        enabled_channels: Sequence[str],
        stop_evt: Optional[threading.Event] = None,
    ) -> None:
        """Configure device identity and cadence; no I/O is performed."""
        super().__init__(name=f"DeviceTemplate[{device_name}]", daemon=True)
        self.device_name = device_name  # Device identity used in outputs
        self.fs_hz = max(float(fs_hz), 0.0)  # Emission cadence (if used later)
        self.enabled_channels = tuple(enabled_channels)  # e.g., ("tpl_ch1", "tpl_ch2")
        self._stop_evt = stop_evt or threading.Event()
        self._period = 1.0 / self.fs_hz if self.fs_hz > 0.0 else 0.0  # Loop period
        self._next_tick = time.monotonic()  # Next loop deadline

    def run(self) -> None:
        """Run idle loop; replace comments with real acquisition later."""
        logger.info(
            "device_template '%s': thread started (no emission; skeleton only)",
            self.device_name,
        )

        while not self._stop_evt.is_set():
            # --- Pace loop if a nominal FS is provided (optional) ---
            if self._period > 0.0:
                now = time.monotonic()
                sleep_s = self._next_tick - now
                if sleep_s > 0.0:
                    time.sleep(min(sleep_s, 0.05))
                    continue
                self._next_tick += self._period
            else:
                # No cadence configured; avoid busy spin
                time.sleep(0.05)

            # --- PLACEHOLDER: read from the physical/virtual device -----------
            # Example shape of a single-sample read (replace with real data):
            #   device_ts: float = time.monotonic()  # Device-local stable clock
            #   values: Dict[str, float] = {
            #       "tpl_ch1": <value1>,  # Use your actual channel names
            #       "tpl_ch2": <value2>,
            #   }

            # --- PLACEHOLDER: build packet for enabled channels ---------------
            # pairs: Tuple[Tuple[str, float], ...] = tuple(
            #     (ch, values[ch]) for ch in self.enabled_channels if ch in values
            # )
            # if pairs:
            #     SYNC.enqueue_packet(
            #         device_ts=device_ts,                 # float seconds
            #         device_name=self.device_name,        # e.g., "tpl_1"
            #         channel_pairs=pairs,                 # (("tpl_ch1", v1), ...)
            #     )

            # --- OPTIONAL: emit markers ---
            # Sticky event change (persists until changed again):
            # SYNC.set_event(label="TASK_1", source=self.device_name)
            #
            # Instantaneous spike (one-shot marker):
            # SYNC.trigger_spike(label="SPIKE_A", source=self.device_name)

            # This skeleton intentionally does nothing.

    def stop(self) -> None:
        """Request cooperative shutdown for the worker thread."""
        self._stop_evt.set()


# ====== PUBLIC MANAGER ======
class DeviceTemplateManager:
    """Minimal manager exposing start/stop; no emissions.

    Future contributors should:
      - Open/close device resources in start/stop.
      - Create the worker with proper params and enable emission points.
    """

    def __init__(self, instance_cfg: Dict[str, Any]) -> None:
        """Store instance configuration snapshot for later use."""
        self.cfg = instance_cfg  # Persisted reference to config node
        self.name = str(self.cfg.get("DEVICE_NAME", "device_template")).strip()
        self._thr: Optional[_TemplateThread] = None

    def start_stream(self) -> None:
        """Start worker thread if not already running; no I/O for now."""
        if self._thr is not None:
            return  # Already running

        fs = float(self.cfg.get("FS", 100.0))  # Nominal cadence (optional)
        channels_cfg = self.cfg.get("CHANNELS", {})
        enabled_channels = [ch for ch, on in channels_cfg.items() if bool(on)]

        # Keep a log trace of resolved runtime parameters.
        logger.info(
            "device_template '%s': start (fs=%.3f Hz, enabled_channels=%s)",
            self.name,
            fs,
            enabled_channels,
        )

        # NOTE: Open device handles here in real implementations.

        self._thr = _TemplateThread(
            device_name=self.name,
            fs_hz=fs,
            enabled_channels=enabled_channels,
        )
        self._thr.start()

    def stop(self) -> None:
        """Stop worker and release resources; best-effort semantics."""
        thr = self._thr
        if thr is None:
            return  # Not running
        try:
            thr.stop()  # Cooperative stop
        finally:
            self._thr = None
            # NOTE: Close device handles here in real implementations.
            logger.info("device_template '%s': stopped", self.name)