# visualization/plot_sink.py
# Live Matplotlib sink with minimal structure: fixed X window, autoscale Y, persistent markers.

from __future__ import annotations

import queue
import threading
import math
import time
from collections import deque
from typing import Dict, Tuple, List, Sequence, Optional

import matplotlib.pyplot as plt
import matplotlib.patheffects as pe
from utils.config import CONFIG
from processing.sync_controller import sync_manager as SYNC

from utils.logger import get_logger
logger = get_logger(__name__)

# ====== LOCAL PALETTE ======
# Color sequence used for events, spikes, and series lines.
COLORS: Sequence[str] = [
    "#289df1", "#ff9b44", "#3ae73a", "#ff4747", "#c187f6",
    "#a96e62", "#ff7ad7", "#979797", "#f5f563", "#53eeff",
]

# ====== INTERNAL TYPES ======
# --- Packet format (SyncManager → sinks) ---
# Tuples are tagged by type in the first field ("tag").
# Supported tags and payloads:
#   ("sample", k, t_q, device, ((ch, val), ...))
#   ("event",  k, t_q, label,  source, current_event_after)
#   ("spike",  k, t_q, label,  source)
#
# Fields:
#   tag: str        # Packet kind: "sample" | "event" | "spike".
#   k: int          # Discrete frame index on the fixed delta grid.
#   t_q: float      # Quantized time in seconds; invariant t_q = k * delta.
#   device: str     # Source device name (for "sample" packets).
#   (ch, val):      # Tuple: channel name (str) and numeric value (float).
#   label: str      # Event/spike label (semantic category).
#   source: str     # Origin of the trigger (e.g., "keyboard", "api").
#   current_event_after: str
#                   # Sticky event state after applying the event.
#
# Notes:
# - Samples may contain multiple (ch, val) pairs for the same device tick.
# - Use k for frame alignment, gap detection, decimation, and dedup.
# - Use t_q for plotting/time labels; both increase monotonically.

Pkt = Tuple
SeriesKey = str  # "device_channel"

class PlotSink:
    """Render multi-subplot live data with persistent event/spike markers.

    One subplot per (device_channel). X shows last window_sec. Y autoscale only.
    Markers are drawn once per arrival and kept; optional pruning prevents growth.
    """

    # ====== CTOR ======
    def __init__(
        self,
        delta: float | None = None,
        window_sec: float | None = None,
        update_hz: float | None = None,
        init_event: str = "UNDEFINED",
    ) -> None:
        """Init sink using CONFIG-driven defaults; all args optional.

        If any arg is None, value is read from CONFIG with safe fallbacks.
        Keymaps are loaded from CONFIG and keys are normalized to lowercase.
        """
        # --- Build device whitelist from CONFIG ---
        self._plot_devices = self._build_plot_device_whitelist()

        # --- Read UI defaults from CONFIG (safe fallbacks) ---
        ui_cfg = dict(CONFIG.get("ui", {}))                  # may include WINDOW_SEC/UPDATE_HZ/DELTA
        sync_cfg = dict(CONFIG.get("sync", {}))              # alternative place for DELTA if used

        # Resolve delta from args → ui.DELTA → sync.DELTA → 0.004
        d_val = delta if delta is not None else ui_cfg.get("DELTA", sync_cfg.get("DELTA", 0.004))

        ws_val = window_sec if window_sec is not None else ui_cfg.get("WINDOW_SEC", 20.0)
        hz_val = update_hz if update_hz is not None else ui_cfg.get("UPDATE_HZ", 24.0)

        # --- Pruning and visuals (shared policy from config) ---
        self._pruning_margin: float = float(ui_cfg.get("PRUNING_MARGIN", 1.20))  # time margin factor
        self._markers_max: int = int(ui_cfg.get("PRUNE_MARKERS_MAX", 10))        # hard cap per-axis, per-kind

        # Autoscale is now performed every frame (config removed for simplicity).
        self._aa_lines: bool = bool(ui_cfg.get("AA_LINES", False))               # line antialiasing toggle
        self._line_width: float = float(ui_cfg.get("SIGNAL_LINE_WIDTH", ui_cfg.get("LINE_WIDTH", 1.5)))  # signal trace thickness
        marker_width = float(ui_cfg.get("MARKER_LINE_WIDTH", 2.5))
        self._marker_line_width: float = marker_width                            # primary colored marker width
        self._marker_outer_width: float = marker_width * 2.0                     # glow stroke width
        self._marker_inner_width: float = marker_width / 3.0 if marker_width else 0.5  # inner edge width

        # --- FPS overlay (real measured FPS) ---
        self._show_fps: bool = bool(ui_cfg.get("SHOW_FPS", True))         # Toggle via CONFIG
        self._fps_text = None                                             # Matplotlib text handle
        self._fps_last_wall: float = time.perf_counter()                  # Last wall-clock sample
        self._fps_frames: int = 0                                         # Frames since last sample
        self._fps_ema: Optional[float] = None                             # Smoothed FPS value
        self._fps_ema_alpha: float = float(ui_cfg.get("FPS_EMA_ALPHA", 0.30))  # EMA smoothing

        self._on_event = getattr(SYNC, "set_event", None)
        self._on_spike = getattr(SYNC, "trigger_spike", None)

        self.delta = float(d_val)                            # Time quantum (s)
        self.window_sec = float(ws_val)                      # Visible X window (s)
        self.update_hz = float(hz_val)                       # Redraw frequency (Hz)

        # Keep a configurable margin over the visible window to avoid churn at edges.
        self._buflen = max(1, int(math.ceil((self.window_sec / self.delta) * self._pruning_margin)))

        # Intake queue and core buffers
        self.queue: "queue.Queue[Pkt]" = queue.Queue(maxsize=4096)
        self._tbuf, self._vbuf = {}, {}
        self._series_colors = {}

        # Marker buffers (state + pruning basis)
        self._events = deque(maxlen=4096)
        self._spikes = deque(maxlen=4096)

        # Matplotlib objects and overlays
        self._fig = None
        self._axes, self._lines = {}, {}
        self._time_text = None
        self._event_text = None

        # --- Keymaps from CONFIG (lowercase keys) ---
        self._event_keymap = {str(k).lower(): str(v) for k, v in CONFIG.get("events", {}).get("EVENT_KEYMAP", {}).items()}
        self._spike_keymap = {str(k).lower(): str(v) for k, v in CONFIG.get("spikes", {}).get("SPIKE_KEYMAP", {}).items()}

        # Callbacks and current event
        self._current_event = next(iter(self._event_keymap.values()), init_event)

        # Palette per label (order by insertion of CONFIG values)
        self._event_label_color, self._spike_label_color = {}, {}
        for i, lbl in enumerate(list(self._event_keymap.values())):
            self._event_label_color[str(lbl)] = COLORS[i % len(COLORS)]
        for i, lbl in enumerate(list(self._spike_keymap.values())):
            self._spike_label_color[str(lbl)] = COLORS[i % len(COLORS)]

        # Newly arrived markers processed once per timer tick
        self._new_events: List[Tuple[float, str]] = []      # (t, label)
        self._new_spikes: List[Tuple[float, str]] = []      # (t, label)

        # Per-kind → per-axis → deque[(t, label, artist)]
        self._markers: Dict[str, Dict[SeriesKey, deque]] = {"event": {}, "spike": {}}

        # Timer/close + persistent marker storage
        self._closed_evt = threading.Event()
        self._timer = None

        # --- Keyboard binding & debounce state ---
        self._keys_bound: bool = False               # Avoid multiple mpl_connect
        self._last_event_k: Optional[int] = None     # One event per tick (k)
        self._debounce_ms: int = 120                 # Soft debounce (keyboard auto-repeat)
        self._last_event_wall_ms: float = 0.0        # Last event wall-clock ms

        # --- Global antialiasing policy (cheap toggle) ---
        # Disable line AA when requested to improve interactive performance.
        try:
            import matplotlib as mpl
            mpl.rcParams["lines.antialiased"] = bool(self._aa_lines)
        except Exception:
            pass  # Safe fallback if rcParams are locked by backend

        # Log key runtime settings for traceability
        logger.info(
            "PlotSink init: delta=%.6f s, window=%.2f s, update=%.1f Hz, "
            "pruning_margin=%.2f, markers_max=%d, aa_lines=%s",
            self.delta, self.window_sec, self.update_hz,
            self._pruning_margin, self._markers_max, str(self._aa_lines),
        )

    # --- Device whitelist from CONFIG ---
    def _build_plot_device_whitelist(self) -> frozenset[str]:
        """Collect device instance names with ENABLED=True and PLOT_ENABLE=True.

        Summary: Return a frozenset of device names to plot.
        Body: Instances missing PLOT_ENABLE default to True. Names are stripped.
        """
        names: set[str] = set()
        devices = CONFIG.get("devices", {})
        for _, block in devices.items():
            for inst in block.get("INSTANCES", []):
                if not inst.get("ENABLED", False):
                    continue  # Instance disabled
                if not inst.get("PLOT_ENABLE", True):
                    continue  # Plot disabled for this instance
                name = str(inst.get("DEVICE_NAME", "")).strip()
                if name:
                    names.add(name)
        return frozenset(names)

    # ====== PUBLIC API ======
    def run(self) -> None:
        """Build figure and enter Matplotlib main loop (blocking)."""
        try:
            self._fig = plt.figure()  # Create figure early for timer
            self._unbind_default_keys()
            self._connect_key_handler()
            self._start_timer()
            logger.info("PlotSink started")  # Lifecycle start
            plt.show()
        except Exception as e:
            # Unhandled GUI/backend failure should be visible in logs.
            logger.error("PlotSink.run failed: %s", e)
            raise
        finally:
            self._stop_timer()
            self._closed_evt.set()
            logger.info("PlotSink stopped")  # Lifecycle stop     

    def _unbind_default_keys(self) -> None:
        """Disable default Matplotlib bindings that collide with typing."""
        to_unbind = {
            "keymap.quit": ["q"],
            "keymap.save": ["s"],
            "keymap.fullscreen": ["f"],
            "keymap.home": ["h", "r"],
            "keymap.back": ["c"],
            "keymap.forward": ["v"],
            "keymap.pan": ["p"],
            "keymap.zoom": ["o"],
            "keymap.grid": ["g", "G"],
            "keymap.yscale": ["l"],
            "keymap.xscale": ["k"],
            "keymap.tight_layout": ["t"],
            "keymap.all_axes": ["a"],
        }
        for rc_key, keys in to_unbind.items():
            if rc_key not in plt.rcParams:
                continue
            km = plt.rcParams.get(rc_key, [])
            if isinstance(km, (list, tuple)):
                plt.rcParams[rc_key] = [k for k in km if k not in keys]

    def _connect_key_handler(self) -> None:
        """Bind keyboard handler once; debounced per quantized tick."""
        assert self._fig is not None
        if self._keys_bound:
            return  # Prevent multiple bindings after layout rebuilds

        def _on_key(evt):
            k = (evt.key or "").lower()                   # Normalize key string

            # Close on Alt+Q
            if k == "alt+q":
                try:
                    self._stop_timer()
                    plt.close(self._fig)
                finally:
                    return

            # Compute current quantized tick to enforce "one event per k"
            try:
                t_now = SYNC._host_rel_now()
                k_now, _ = SYNC._quantize(t_now)
            except Exception:
                k_now = None  # Fallback: allow event if quantization unavailable

            # Soft debounce on wall time (avoid OS auto-repeat floods)
            try:
                import time as _time
                now_ms = _time.time() * 1000.0
            except Exception:
                now_ms = 0.0

            # EVENTS
            if k in self._event_keymap and self._on_event:
                # One event per tick guard
                if (k_now is not None) and (self._last_event_k == k_now):
                    return
                # Time debounce
                if (now_ms - self._last_event_wall_ms) < self._debounce_ms:
                    return

                try:
                    label = self._event_keymap[k]
                    self._on_event(label, "keyboard")      # Dispatch to SYNC
                    # Update HUD immediately (no local marker here)
                    self._current_event = str(label)
                    if self._event_text is not None:
                        self._event_text.set_text(f"Event = {self._current_event}")
                        self._event_text.set_color(
                            self._event_label_color.get(str(label).upper(), COLORS[0])
                        )
                    # Update debounce state
                    self._last_event_k = k_now
                    self._last_event_wall_ms = now_ms
                finally:
                    return

            # SPIKES
            if k in self._spike_keymap and self._on_spike:
                # Optional: share same debounce with events to keep UI snappy
                if (k_now is not None) and (self._last_event_k == k_now):
                    return
                if (now_ms - self._last_event_wall_ms) < self._debounce_ms:
                    return
                try:
                    self._on_spike(self._spike_keymap[k], "keyboard")
                    self._last_event_k = k_now
                    self._last_event_wall_ms = now_ms
                finally:
                    return

        self._fig.canvas.mpl_connect("key_press_event", _on_key)
        self._keys_bound = True


    # ====== TIMER ======
    def _start_timer(self) -> None:
        """Start periodic redraw using Matplotlib timer."""
        assert self._fig is not None
        interval_ms = int(max(1.0, 1000.0 / self.update_hz))  # Convert Hz to ms
        self._timer = self._fig.canvas.new_timer(interval=interval_ms)
        self._timer.add_callback(self._on_timer)
        self._timer.start()
        logger.info("PlotSink timer started: interval_ms=%d", interval_ms)  # Redraw cadence

    def _stop_timer(self) -> None:
        """Stop the redraw timer if active."""
        if self._timer is not None:
            try:
                self._timer.stop()
                logger.info("PlotSink timer stopped")  # Timer lifecycle
            finally:
                self._timer = None

    # ====== CORE LOOP ======
    def _on_timer(self) -> None:
        """Redraw callback: drain packets, update axes, place/prune markers."""
        if self._fig is None:
            return  # Abort if figure is not ready

        # --- Drain packets (minimal, inline policies) ---
        last_t: Optional[float] = None           # Last processed timestamp
        new_series = False                       # Flag: any new series discovered?

        # Consume all queued packets without blocking
        while True:
            try:
                pkt = self.queue.get_nowait()    # Non-blocking fetch of next packet
            except Exception:
                break                            # Queue empty -> stop draining

            if not isinstance(pkt, tuple) or not pkt:
                continue                         # Skip malformed/empty packets (go to next while iteration)

            tag = pkt[0]                         # First field indicates packet type

            if tag == "sample":
                _, k, t_q, dev, pairs = pkt      # Unpack sample payload
                
                # Apply per-instance whitelist strictly: empty set means plot none
                if dev not in self._plot_devices:
                    continue  # Skip entire sample from this device

                t = float(t_q)
                for ch_name, ch_val in pairs:    # Iterate channel/value pairs
                    key = f"{dev}_{ch_name}"     # Unique series key per device+ch

                    if key not in self._tbuf:
                        logger.info("New series: %s (device=%s)", key, dev)  # First sighting
                        # Create buffers and assign a color for a new series
                        self._tbuf[key] = deque(maxlen=self._buflen)  # Time buf
                        self._vbuf[key] = deque(maxlen=self._buflen)  # Value buf
                        idx = len(self._series_colors)                 # Order idx
                        self._series_colors[key] = COLORS[idx % len(COLORS)]
                        new_series = True                              # A new series/channel has been found

                    self._tbuf[key].append(t)     # Append sample time
                    self._vbuf[key].append(float(ch_val))  # Append sample value

                last_t = t              # Track the latest timestamp seen

            elif tag == "event":
                _, k, t_q, label, source, cur_after = pkt
                t = float(t_q)
                lbl = str(label)
                self._events.append((t, lbl))            # Keep for pruning
                self._new_events.append((t, lbl))        # Will draw once
                self._current_event = str(cur_after)
                last_t = float(t_q)

            elif tag == "spike":
                _, k, t_q, label, source = pkt
                t = float(t_q)
                lbl = str(label)
                self._spikes.append((t, lbl))
                self._new_spikes.append((t, lbl))        # Will draw once
                last_t = float(t_q)
        
        # --- Ensure axes exist and are up to date ---
        keys = sorted(self._tbuf.keys())  # Sorted for stable layout
        if new_series:
            # Rebuild layout
            self._fig.clf()  # Clear figure

            # Clock
            self._time_text = self._fig.text(0.99, 0.99, "t = 0.000000 s",
                                             fontsize=12,ha="right", va="top")
            # FPS text (top-left). Shows real measured FPS updated in _on_timer.
            if self._show_fps:
                self._fps_text = self._fig.text(
                    0.01, 0.99, "FPS = --", ha="left", va="top", fontsize=11, color="gray"
                )
            else:
                self._fps_text = None
            # Hint
            self._fig.text(0.99, 0.01, "Alt+Q to stop", ha="right", va="bottom",
                           fontsize=11, color="gray")
            # Event
            self._event_text = self._fig.text(
                0.01, 0.01, f"Event = {self._current_event}",
                ha="left", va="bottom", fontsize=12, fontweight="bold",
                color=self._event_label_color.get(self._current_event, COLORS[0]))
            self._axes.clear()   # Drop old axis references
            self._lines.clear()  # Drop old line references
            n = max(1, len(keys))  # Ensure at least one row

            for i, key in enumerate(keys, start=1):
                ax = self._fig.add_subplot(n, 1, i)  # One subplot per series
                self._fig.subplots_adjust(left=0.1, right=0.95, top=0.93, bottom=0.12, hspace=0.22)
                ax.set_xlabel("" if i < n else "t [s]")  # X label only on last
                ax.tick_params(labelbottom=False if i < n else "t [s]") # X ticks only on last
                c = self._series_colors.get(key, COLORS[0])
                (line,) = ax.plot([], [], lw=self._line_width, label=key, color=c, antialiased=self._aa_lines)
                ax.legend(loc="upper left", fontsize=7, frameon=True)  # Show series name
                self._axes[key] = ax
                self._lines[key] = line
                # Ensure per-axis marker deques (event/spike)
                self._markers["event"].setdefault(key, deque(maxlen=self._markers_max))
                self._markers["spike"].setdefault(key, deque(maxlen=self._markers_max))

            # Re-bind keys after clf (clearing removes callbacks)
            self._connect_key_handler()

        # --- Update data and x/ylim (CLOCK-DRIVEN, SINGLE GLOBAL WINDOW) ---
        try:
            # Get host-relative "now" and quantize to the same delta grid.
            t_now = SYNC._host_rel_now()                  # Host-relative seconds
            _, t_right = SYNC._quantize(t_now)            # Quantized right edge
            t_right = float(t_right)                      # Ensure plain float
        except RuntimeError as e:
            logger.error("PlotSink: sync session not started: %s", e)
            return
        except Exception as e:
            logger.error("PlotSink: failed to read sync clock: %s", e)
            return

        # Compute the global left edge from the quantized "now".
        t_left = max(0.0, t_right - self.window_sec)      # Shared left edge

        # Push only in-window data to lines; autoscale Y every frame.
        keys = sorted(self._tbuf.keys())
        for key in keys:
            ax, line = self._axes.get(key), self._lines.get(key)
            if ax is None or line is None:
                continue

            # --- Slice buffers to the visible window [t_left, t_right] ---
            tb = self._tbuf[key]                          # Deque of times
            vb = self._vbuf[key]                          # Deque of values
            if tb and vb:
                # Simple list-based slice for clarity; fast enough for live plot.
                # If needed, this can be optimized with index caching.
                t_win: List[float] = []
                v_win: List[float] = []
                for tt, vv in zip(tb, vb):
                    if tt < t_left or tt > t_right:
                        continue                           # Skip out-of-window samples
                    t_win.append(tt)                       # Keep time in window
                    v_win.append(vv)                       # Keep value in window
            else:
                t_win, v_win = [], []

            # Update the line with in-window samples only
            line.set_data(t_win, v_win)

            # Apply the SAME xlim to every subplot; minimal positive width guard.
            ax.set_xlim(t_left, t_right if t_right > t_left else t_left + self.delta)

            # Autoscale Y every frame (no rate limiting, no stale points influence)
            ax.relim()                                     # Recompute Y limits from current line data
            ax.autoscale_view(scalex=False, scaley=True)   # Autoscale Y only

        # --- Place newly arrived markers once (persistent axvline) ---
        if keys and (self._new_events or self._new_spikes):
            for key in keys:
                ax = self._axes.get(key)
                if ax is None:
                    continue

                # Events: dashed colored with white underlay
                if self._new_events:
                    for t, lbl in self._new_events:
                        col = self._event_label_color.get(lbl, COLORS[0])
                        ln = ax.axvline(t, linewidth=self._marker_line_width, color=col, zorder=3, antialiased=self._aa_lines)
                        ln.set_path_effects([
                            pe.Stroke(linewidth=self._marker_outer_width, foreground="white"),   # outer glow
                            pe.Normal(),                                    # draw the colored line
                            pe.Stroke(linewidth=self._marker_inner_width, foreground="black"),  # inner edge
                        ])
                        self._markers["event"][key].append((t, lbl, ln))    # Append to deque

                # Spikes: solid colored with white underlay
                if self._new_spikes:
                    for t, lbl in self._new_spikes:
                        col = self._spike_label_color.get(lbl, COLORS[0])
                        ln = ax.axvline(t, linewidth=self._marker_line_width, color=col, linestyle="--", zorder=3, antialiased=self._aa_lines)
                        ln.set_path_effects([
                            pe.Stroke(linewidth=self._marker_outer_width, foreground="white"),   # outer glow
                            pe.Normal(),                                    # draw the colored line
                            pe.Stroke(linewidth=self._marker_inner_width, foreground="black"),  # inner edge
                        ])
                        self._markers["spike"][key].append((t, lbl, ln))    # Append to deque

            # Clear per-tick arrival buffers
            self._new_events.clear()
            self._new_spikes.clear()

        # --- Optional pruning: pop-left old markers to cap memory (O(r) per tick) ---
        if keys:
            ref_ax = self._axes[keys[-1]]
            _, x2 = ref_ax.get_xlim()
            cutoff = max(0.0, x2 - self.window_sec * self._pruning_margin)

            for kind in ("event", "spike"):
                for key in keys:
                    dq = self._markers[kind].get(key)
                    if not dq:
                        continue
                    # Pop-left all markers left of cutoff
                    while dq and (dq[0][0] is None or dq[0][0] < cutoff):
                        _, _, artist = dq[0]
                        try:
                            artist.remove()  # Drop old single Line2D
                        except Exception:
                            pass
                        dq.popleft()

        # --- Overlays update ---
        if last_t is not None and self._time_text is not None:
            self._time_text.set_text(f"t = {last_t:.2f} s")
        if self._event_text is not None:
            self._event_text.set_text(f"Event = {self._current_event}")
            self._event_text.set_color(self._event_label_color.get(self._current_event, COLORS[0]))

        # --- FPS measurement (real redraw rate) ---
        if self._show_fps:
            now = time.perf_counter()                       # High-res wall clock
            self._fps_frames += 1                           # Count this callback
            dt = now - self._fps_last_wall                  # Elapsed since last sample
            if dt >= 0.5:                                   # Update every ~0.5 s
                inst_fps = self._fps_frames / max(dt, 1e-9) # Instantaneous FPS
                self._fps_last_wall = now                   # Reset window
                self._fps_frames = 0                        # Reset counter
                # Exponential moving average for stable display
                if self._fps_ema is None:
                    self._fps_ema = inst_fps
                else:
                    a = self._fps_ema_alpha
                    self._fps_ema = a * inst_fps + (1.0 - a) * self._fps_ema
                # Push text if overlay is present
                if self._fps_text is not None:
                    self._fps_text.set_text(f"FPS = {self._fps_ema:.1f}")

        # --- Request redraw (non-blocking) ---
        try:
            self._fig.canvas.draw_idle()
        except Exception as e:
            # Backend/GUI issue; log once per failure occurrence.
            logger.error("PlotSink: canvas draw failed: %s", e)
