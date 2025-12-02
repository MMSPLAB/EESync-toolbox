# processing/sync_controller.py
# Sync manager: host timebase, device anchor mapping, delta-quantization, packet fan-out.

from __future__ import annotations

import threading
import queue
import time
import math
from dataclasses import dataclass
from typing import Dict, Iterable, Tuple, List, Optional, Union
from utils.config import CONFIG  # Read default event from events.EVENT_KEYMAP

from utils.logger import get_logger
logger = get_logger(__name__)

# ====== DATA MODEL ======

@dataclass
class DeviceAnchor:
    """Per-device anchor: device origin ts, host origin ts, epoch count, drift."""
    dev_ts0: float            # First device ts seen (or after reset/backward jump)
    host_t0: float            # Host-relative time at the moment of anchoring
    epoch: int = 0            # Count of detected device clock resets/backward jumps
    scale: float = 1.0        # Drift scale (1.0 = offset-only mapping for now)


NumberOrNone = Optional[Union[float, int]]  

# ====== SYNC MANAGER ======

class SyncManager:
    """Queue-backed synchronizer for samples + keyboard-triggered events/spikes.

    Sample packet (producer → sync):
      (device_ts: float, device_name: str, channel_pairs: Tuple[(str, float|None), ...])  # accept None

    Sink packet (sync → sinks), tagged:
      ("sample", k, t_q, device, ((ch,val), ...))
      ("event",  k, t_q, label, source, current_event_after)
      ("spike",  k, t_q, label, source)
    """

    # --- Lifecycle / construction ---
    def __init__(self, *, max_queue: int = 0) -> None:
        """Init queues and state. max_queue=0 → unbounded; >0 → drop-oldest policy."""
        # Use bounded queue only if requested; 0 means unbounded queue
        self._max_queue: int = int(max_queue if max_queue >= 0 else 0)
        self._q: "queue.Queue[tuple | None]" = queue.Queue(
            maxsize=self._max_queue or 0
        )

        self._consumer: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._started = False

        # Session timing
        self._session_t0: float | None = None
        self._delta: float | None = None

        # Default decimals for t_q formatting
        self._tq_decimals: int = 6

        # Per-device anchors
        self._anchors: Dict[str, DeviceAnchor] = {}

        # Logical current/default event labels
        self._default_event: str = ""   # Set at session start from CONFIG
        self._current_event: str = ""   # Sticky event (starts at default)

        # Optional sinks to forward quantized packets to (plot, export, etc.)
        self._sinks: List["queue.Queue"] = []

        # Plot-specific sinks and decimation state
        self._plot_sinks: List["queue.Queue"] = []         # Queues receiving decimated data
        self._plot_decimate_dt: float | None = None         # Bin width in seconds (None = disabled)
        self._plot_last_bin: Dict[str, int] = {}            # Per-series last bin index for keep-one

    # ====== SINK MANAGEMENT ======
    def add_sink_queue(self, q: "queue.Queue") -> None:
        """Register a sink queue to receive quantized packets."""
        if q not in self._sinks:
            self._sinks.append(q)
            logger.info("Sync: sink registered (full-rate)")
    
    def add_plot_sink_queue(self, q: "queue.Queue") -> None:
        """Register a sink queue for plotting; samples will be decimated."""
        if q not in self._plot_sinks:
            self._plot_sinks.append(q)
            logger.info("Sync: plot sink registered (decimated)")


    def remove_sink_queue(self, q: "queue.Queue") -> None:
        """Unregister a sink queue."""
        try:
            self._sinks.remove(q)
        except ValueError:
            pass

    # ====== LIFECYCLE ======
    def start_session(self, delta: float) -> None:
        """Start consumer thread with fixed delta; set host timebase origin."""
        if self._started:
            return
        if not (isinstance(delta, (int, float)) and delta > 0.0):
            raise ValueError("start_session(delta): delta must be > 0")

        # Read default event from CONFIG: first value in EVENT_KEYMAP sequence
        ev_map = CONFIG.get("events", {}).get("EVENT_KEYMAP", {})
        try:
            default_event = next(iter(ev_map.values()))  # Ordered by definition
        except StopIteration:
            default_event = ""  # Fallback to empty if config is missing

        self._default_event = str(default_event)
        self._current_event = self._default_event

        # Set timing baseline and clear per-session state
        self._delta = float(delta)
        self._session_t0 = time.monotonic()
        self._anchors.clear()

        # Compute decimals for t_q formatting
        self._tq_decimals = self._decimals_from_delta(self._delta)

        # Plot decimation: read target Hz from config; disabled if <= 0.
        plot_hz = float(CONFIG.get("ui", {}).get("PLOT_DECIMATE_HZ", 0.0))
        self._plot_decimate_dt = (1.0 / plot_hz) if plot_hz > 0.0 else None
        self._plot_last_bin.clear()  # Reset per-series bin tracker

        # Log session parameters for traceability.
        logger.info(
            "Sync: session started (delta=%.6f s, t_dec=%.6f s, default_event='%s')",
            self._delta,
            (self._plot_decimate_dt if self._plot_decimate_dt is not None else 0.0),
            self._default_event,
        )

        # Reset and start consumer
        self._stop_evt.clear()
        self._consumer = threading.Thread(
            target=self._consume_loop, name="SyncConsumer", daemon=True
        )
        self._consumer.start()
        self._started = True

    def stop_session(self) -> None:
        """Signal stop and wait for consumer to exit. Reset per-session state."""
        if not self._started:
            return

        # Signal thread to stop and push poison pill
        self._stop_evt.set()
        try:
            self._q.put_nowait(None)  # Poison pill; do not use drop-oldest here
        except Exception:
            pass

        # Join consumer if alive
        if self._consumer and self._consumer.is_alive():
            self._consumer.join(timeout=2.0)
        self._consumer = None
        self._started = False

        # Reset session state; sinks are cleared as in the original behavior
        self._session_t0 = None
        self._delta = None
        self._anchors.clear()
        self._sinks.clear()

        self._plot_sinks.clear()
        self._plot_decimate_dt = None
        self._plot_last_bin.clear()

        logger.info("Sync: session stopped")


    # ====== PRODUCER-FACING API (SAMPLES) ======
    def enqueue_packet(
        self,
        device_ts: float,
        device_name: str,
        channel_pairs: Iterable[Tuple[str, NumberOrNone]],
    ) -> None:
        """Enqueue a single device packet with (ts, name, channel pairs).

        Drop-oldest policy applies only when max_queue > 0 and the queue is full.
        """
        pkt = (float(device_ts), str(device_name), tuple(channel_pairs))

        # Implement drop-oldest when bounded queue is full (non-blocking).
        if self._max_queue > 0:
            try:
                self._q.put_nowait(pkt)  # Fast path
                return
            except queue.Full:
                # Queue is full: drop oldest then retry (non-blocking).
                logger.warning("Sync: queue full, dropping oldest packet")  # Visible backpressure
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(pkt)
            except queue.Full:
                # If still full due to races, drop the new packet silently
                pass
        else:
            # Unbounded queue: always succeeds without blocking
            self._q.put_nowait(pkt)

    # ====== CONTROL: CURRENT EVENT ======
    def get_current_event(self) -> str:
        """Return current sticky event label."""
        return self._current_event

    # ====== KEYBOARD TRIGGERS (EVENT/SPIKE) ======
    def set_event(self, label: str, source: str) -> Tuple[str, int, float, str, str, str]:
        """Sticky event: toggle to label, or back to default if pressing same label.

        Quantize timestamp using host monotonic now; source is required.
        Returns the emitted payload for convenience.
        """
        if not source:
            raise ValueError("source must be a non-empty string")

        t_now = self._host_rel_now()
        k, t_q = self._quantize(t_now)

        target = str(label)
        # Toggle rule: pressing same non-default label returns to default
        if target == self._current_event:
            if self._current_event != self._default_event:
                self._current_event = self._default_event
            else:
                # Already default and pressing default key → no-op; emit anyway?
                # Keep behavior minimal: emit current state to ensure sinks consistency
                pass
        else:
            self._current_event = target

        payload = ("event", k, t_q, self._current_event, str(source), self._current_event)
        self._emit_to_sinks(payload)
        return payload  # Useful in tests or for immediate feedback

    def trigger_spike(self, label: str, source: str) -> Tuple[str, int, float, str, str]:
        """Instantaneous spike at quantized 'now'; source is required."""
        if not source:
            raise ValueError("source must be a non-empty string")
        t_now = self._host_rel_now()
        k, t_q = self._quantize(t_now)
        payload = ("spike", k, t_q, str(label), str(source))
        self._emit_to_sinks(payload)
        return payload

    # ====== INTERNAL: time helpers ======
    def _host_rel_now(self) -> float:
        """Return host-relative time since session start."""
        if self._session_t0 is None:
            raise RuntimeError("Session not started")
        return time.monotonic() - self._session_t0

    def _map_to_host(self, dev: str, device_ts: float) -> float:
        """Map a device timestamp to host-relative time using offset-only anchor."""
        if self._session_t0 is None:
            raise RuntimeError("Session not started")

        # Initialize anchor for device if first time seen
        anchor = self._anchors.get(dev)
        if anchor is None:
            anchor = DeviceAnchor(dev_ts0=float(device_ts), host_t0=self._host_rel_now())
            self._anchors[dev] = anchor
            logger.info("Sync: anchor created for device '%s'", dev)  # First sighting
        else:
            # Detect backward jump/reset and advance epoch with re-anchor
            if device_ts + 1e-12 < anchor.dev_ts0:
                anchor.dev_ts0 = float(device_ts)          # Re-anchor on new device ts
                anchor.host_t0 = self._host_rel_now()      # Host time at re-anchor
                anchor.epoch += 1                          # Bump epoch
                logger.warning("Sync: device '%s' clock jump detected (epoch=%d)", dev, anchor.epoch)

        # Apply scale (drift) and clamp to non-negative
        t_host_est = anchor.scale * (float(device_ts) - anchor.dev_ts0) + anchor.host_t0
        return t_host_est if t_host_est >= 0.0 else 0.0

    def _quantize(self, t_host_est: float) -> Tuple[int, float]:
        """Quantize to the fixed grid delta (round half-up), then floor to n decimals."""
        if self._delta is None:
            raise RuntimeError("Delta not set")
        delta = self._delta

        # Round-half-up to nearest grid index
        k = int((t_host_est / delta) + 0.5)
        t_q = k * delta  # Exact grid time before formatting/floor

        # Floor to configured decimals derived from delta (computed at session start)
        t_q = self._floor_to_decimals(t_q, self._tq_decimals)
        return k, t_q

    def _emit_to_sinks(self, payload: tuple) -> None:
        """Forward payload to sinks (full-rate) and plot sinks (decimated)."""
        # Full-rate sinks (export, logging, etc.)
        for s in self._sinks:
            try:
                s.put_nowait(payload)
            except Exception:
                pass  # Best-effort only

        # Plot sinks (decimated samples; markers are forwarded as-is)
        if self._plot_sinks:
            self._emit_to_plot_sinks(payload)

    # ====== CONSUMER LOOP (SAMPLES) ======
    def _consume_loop(self) -> None:
        """Drain queue, map+quantize sample timestamps, forward to sinks."""
        while not self._stop_evt.is_set():
            try:
                pkt = self._q.get(timeout=0.2)  # Responsive to stop requests
            except queue.Empty:
                continue
            if pkt is None:
                break
            try:
                self._handle_sample_packet(pkt)
            except Exception as e:
                # Best-effort: skip malformed packet without stopping the loop
                logger.error("Sync: failed to handle packet: %s", e)
                pass

    def _handle_sample_packet(self, pkt: tuple) -> None:
        """Validate and process a sample packet, then forward tagged payload."""
        if not isinstance(pkt, tuple) or len(pkt) != 3:
            raise ValueError("Packet must be tuple(device_ts, device_name, channel_pairs)")
        device_ts, device_name, pairs = pkt

        if not isinstance(device_ts, (float, int)):
            raise TypeError("device_ts must be float seconds")
        if not isinstance(device_name, str):
            raise TypeError("device_name must be str")
        if not isinstance(pairs, (tuple, list)):
            raise TypeError("channel_pairs must be tuple/list of (name, value)")

        # Map device ts to host-relative time and quantize
        t_host_est = self._map_to_host(device_name, float(device_ts))
        k, t_q = self._quantize(t_host_est)

        # Forward to sinks (tagged) with normalized pairs
        payload = ("sample", k, t_q, device_name, tuple(pairs))
        self._emit_to_sinks(payload)

    def _decimals_from_delta(self, delta: float) -> int:
        """
        Compute decimal digits for visual/serialization based on delta.
        Clamp to [0, 9]; add +2 safety digits beyond the theoretical need.
        """
        if not (isinstance(delta, (int, float)) and delta > 0.0):
            return 6
        d = -math.log10(delta)
        return int(max(0, min(9, math.ceil(d) + 2)))

    def _floor_to_decimals(self, x: float, decimals: int) -> float:
        """
        Floor a non-negative float to a fixed number of decimals.
        Assumes x >= 0.0 (true for our timebase); avoids rounding-up drift.
        """
        if decimals <= 0:
            return float(math.floor(x))
        p = 10.0 ** decimals
        return math.floor(x * p) / p
    
    def _emit_to_plot_sinks(self, payload: tuple) -> None:
        """Decimate sample packets for plot sinks; pass events/spikes unchanged."""
        tag = payload[0] if payload else None

        # Fast path: markers are forwarded unchanged
        if tag in ("event", "spike") or self._plot_decimate_dt is None:
            for s in self._plot_sinks:
                try:
                    s.put_nowait(payload)
                except Exception:
                    pass
            return

        # Sample decimation (keep-one per time bin, per device_channel)
        if tag != "sample":
            return  # Unknown tag; ignore for plot

        _, k, t_q, dev, pairs = payload
        dt = self._plot_decimate_dt  # type: ignore[assignment]
        if not isinstance(dt, (float, int)) or dt <= 0.0:
            # Safety: if misconfigured, pass-through
            for s in self._plot_sinks:
                try:
                    s.put_nowait(payload)
                except Exception:
                    pass
            return

        # Compute bin index from quantized time
        bin_idx = int((float(t_q) / float(dt)))  # Stable integer binning

        # Filter channel pairs by per-series last-bin state
        filtered: List[tuple] = []
        for ch_name, ch_val in pairs:
            key = f"{dev}_{ch_name}"                    # Per-series unique key
            last = self._plot_last_bin.get(key, None)   # Last emitted bin
            if last != bin_idx:
                filtered.append((ch_name, ch_val))      # First sample in this bin
                self._plot_last_bin[key] = bin_idx      # Update bin tracker

        # If no channels survived, skip emitting any sample payload
        if not filtered:
            return

        # Emit a reduced sample payload containing only first-in-bin channels
        decimated = ("sample", k, float(t_q), str(dev), tuple(filtered))
        for s in self._plot_sinks:
            try:
                s.put_nowait(decimated)
            except Exception:
                pass



# ====== SINGLETON EXPORT ======
sync_manager: SyncManager = SyncManager()