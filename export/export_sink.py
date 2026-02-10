# export/export_sink.py
# CSV exporter sink: wide synced CSV (k,t_q,channels...,spike,event) + markers sidecar.

from __future__ import annotations

import os
import threading
import time
import queue
import math  # For ceil on lookahead-sec → steps
from typing import Dict, List, Optional, Iterable, Any
from utils.logger import get_logger

logger = get_logger(__name__)
from utils.config import CONFIG

# ====== TYPES ======
# Packet tags from SyncManager:
#   ("sample", k, t_q, device, ((ch,val), ...))
#   ("event",  k, t_q, label, source, current_event_after)
#   ("spike",  k, t_q, label, source)

class ExportSink:
    """Consume sync packets and write two CSVs: synced wide + markers sidecar.

    Synced CSV columns (in order): k, t_q, [dev:ch...], spike, event.
    Markers CSV columns: k, t_q, event, spike, source.

    Fixed lookahead L (in steps) for late handling; late ≤ commit → drop late.
    Flush triggers: every T seconds or after N committed rows.
    """

    # --- Construction / lifecycle ---
    def __init__(
        self,
        *,
        delta: float,
        known_channels: Optional[Iterable[str]] = None,
        lookahead_steps: Optional[int] = None,
        flush_period_sec: Optional[float] = None,
        flush_rows_threshold: Optional[int] = None,
        ts_str: Optional[str] = None,
        # (Optional) new: allow passing lookahead in seconds and idle watermark
        lookahead_sec: Optional[float] = None,
        idle_watermark_sec: Optional[float] = None,
    ) -> None:
        # Runtime queues/state
        self.q: "queue.Queue[tuple]" = queue.Queue(maxsize=0)   # Unbounded queue
        self._stop = threading.Event()
        self._thr: Optional[threading.Thread] = None

        # Timing / grid
        self._delta = float(delta)                               # Fixed time step
        self._k_seen_max: int = -1                               # Max k observed

        self._print_k = bool(CONFIG.get("export", {}).get("PRINT_K", True))

        # --- Config block (export.*) ---
        exp_cfg = CONFIG.get("export", {})

        # --- Lookahead: prefer seconds, else steps ---
        # If caller passes lookahead_steps, it wins. Else try LOOKAHEAD_SEC, else LOOKAHEAD_STEPS.
        cfg_lookahead_steps = exp_cfg.get("LOOKAHEAD_STEPS", None)       # May be None
        cfg_lookahead_sec = exp_cfg.get("LOOKAHEAD_SEC", None)           # May be None

        if lookahead_steps is not None:
            # Caller explicit steps override all
            self._L: int = int(lookahead_steps)                 # Fixed lookahead in steps
        elif lookahead_sec is not None:
            # Caller explicit seconds → convert using fs_max = 1/delta
            fs_max = 1.0 / self._delta                          # Samples per second
            self._L = int(math.ceil(float(lookahead_sec) * fs_max))
        elif cfg_lookahead_sec is not None:
            # Config seconds → convert to steps
            fs_max = 1.0 / self._delta
            self._L = int(math.ceil(float(cfg_lookahead_sec) * fs_max))
        else:
            # Fallback to steps from config or default 3
            self._L = int(cfg_lookahead_steps if cfg_lookahead_steps is not None else 3)

        # --- Flush cadence (time-driven primary, rows as backstop) ---
        self._flush_period: float = float(
            flush_period_sec
            if flush_period_sec is not None
            else exp_cfg.get("FLUSH_PERIOD_SEC", 0.25)
        )

        # If threshold is non-positive or missing, derive from fs_max and period, clamped.
        # This lets config use FLUSH_ROWS: 0 to mean "auto".
        if flush_rows_threshold is not None:
            raw_rows = int(flush_rows_threshold)                 # External override
            use_auto = raw_rows <= 0                             # 0 or negative → auto
        else:
            cfg_rows = exp_cfg.get("FLUSH_ROWS", None)           # Read from config
            if cfg_rows is None:
                use_auto = True                                  # Missing → auto
                raw_rows = 0
            else:
                raw_rows = int(cfg_rows)
                use_auto = raw_rows <= 0                         # 0 or negative → auto

        if use_auto:
            fs_max = 1.0 / self._delta                           # Samples per second
            est_rows = int(round(fs_max * self._flush_period))   # Rows per period
            # Clamp to avoid too-frequent or too-rare flushes
            self._flush_rows_threshold = int(min(2048, max(64, est_rows)))
        else:
            self._flush_rows_threshold = raw_rows                # Fixed positive threshold


        # --- Idle watermark (C): finalize on inactivity ---
        # If no packets for X seconds, commit to k_seen_max and flush.
        self._idle_watermark_sec: float = float(
            idle_watermark_sec
            if idle_watermark_sec is not None
            else exp_cfg.get("IDLE_WATERMARK_SEC", 0.0)         # 0 disables the feature
        )
        self._last_activity_monotonic: float = time.monotonic()  # Updated on activity

        # Output files (single session timestamp)
        if ts_str is None:
            # Build once to keep filenames stable for the session
            ts_str = time.strftime("%Y-%m-%d_%H-%M-%S")
        out_cfg = exp_cfg.get("OUT", {})
        synced_dir = out_cfg.get("SYNCED_DIR", "data/synced")
        markers_dir = out_cfg.get("MARKERS_DIR", "data/markers")
        os.makedirs(synced_dir, exist_ok=True)
        os.makedirs(markers_dir, exist_ok=True)
        self._synced_path = os.path.join(synced_dir, f"synced_{ts_str}.csv")
        self._markers_path = os.path.join(markers_dir, f"markers_{ts_str}.csv")

        # Per-CSV enable flags (note: keys intentionally match config spelling)
        self._csv_signal_enabled: bool = bool(exp_cfg.get("CSV_SIGNAL_ENABLE", True))
        self._csv_marker_enabled: bool = bool(exp_cfg.get("CSV_MARKER_ENABLE", True))

        # Header / columns
        self._channels: List[str] = list(known_channels) if known_channels else []
        self._header_frozen = bool(self._channels)              # Freeze if provided

        # Sticky event defaults (match SyncManager rule)
        ev_map = CONFIG.get("events", {}).get("EVENT_KEYMAP", {})
        try:
            self._default_event = str(next(iter(ev_map.values())))
        except StopIteration:
            self._default_event = ""
        self._sticky_event = self._default_event                # Current sticky event

        # Row buffer and aux state
        self._open_rows: Dict[int, Dict[str, str]] = {}         # k -> col->val
        self._tq_by_k: Dict[int, float] = {}                    # k -> t_q
        self._event_changes: Dict[int, str] = {}                # k -> event label
        self._pending_committed: int = 0                        # Rows since last flush
        self._last_flush_time: float = time.monotonic()         # Periodic flush clock

        # Initial marker state
        self._initial_marker_emitted: bool = False              # Emit default event at first commit

        # IO objects
        self._synced_fh = None
        self._synced_writer: Optional[Any] = None
        self._markers_fh = None
        self._markers_writer: Optional[Any] = None


    # --- Public API ---
    def start(self) -> None:
        """Open CSV files, write headers, and start the consumer thread."""
        # Idempotent start: ignore if already running
        if self._thr is not None:
            return

        # Open output files (paths prepared in __init__)
        # Use newline="" to prevent extra blank lines on Windows CSV.
        if self._csv_signal_enabled:
            self._synced_fh = open(self._synced_path, "w", newline="", encoding="utf-8")
        else:
            self._synced_fh = None
        if self._csv_marker_enabled:
            self._markers_fh = open(self._markers_path, "w", newline="", encoding="utf-8")
        else:
            self._markers_fh = None

        # Create CSV writers
        import csv  # Local import keeps top clean and avoids shadowing issues
        self._synced_writer = csv.writer(self._synced_fh) if self._synced_fh is not None else None
        self._markers_writer = csv.writer(self._markers_fh) if self._markers_fh is not None else None

        # Log enablement state for each CSV
        if self._csv_signal_enabled and self._synced_fh is not None:
            logger.info("Export: signal CSV enabled -> %s", self._synced_path)
        else:
            logger.info("Export: signal CSV disabled")
        if self._csv_marker_enabled and self._markers_fh is not None:
            logger.info("Export: marker CSV enabled -> %s", self._markers_path)
        else:
            logger.info("Export: marker CSV disabled")

        # Write markers header immediately (stable schema)
        if self._markers_writer is not None:
            hdr = (["k"] if self._print_k else []) + ["t_q", "event", "spike", "source"]
            self._markers_writer.writerow(hdr)

        # Always write the synced header now, using channels provided by main.
        if self._synced_writer is not None:
            if not self._channels:
                # Safety net: main should have already failed earlier if empty
                raise RuntimeError("ExportSink: no known_channels provided by main")
            header = (["k"] if self._print_k else []) + ["t_q"] + list(self._channels) + ["spike", "event"]
            self._synced_writer.writerow(header)
            self._header_frozen = True  # Freeze schema strictly to provided channels


        # Start consumer thread (daemon so process can exit cleanly)
        self._stop.clear()
        self._thr = threading.Thread(target=self._run, name="ExportSink", daemon=True)
        self._thr.start()

    def stop(self) -> None:
        """Request stop, flush remaining, and close files."""
        if self._thr is None:
            return
        self._stop.set()
        try:
            self.q.put_nowait(("__stop__",))
        except Exception:
            pass
        self._thr.join(timeout=2.0)
        self._thr = None
        # Final flush best-effort
        self._commit_until(self._k_seen_max)
        # Close files
        try:
            if self._synced_fh:
                self._synced_fh.flush()
                self._synced_fh.close()
        finally:
            self._synced_fh = None
            self._synced_writer = None
        try:
            if self._markers_fh:
                self._markers_fh.flush()
                self._markers_fh.close()
        finally:
            self._markers_fh = None
            self._markers_writer = None

    # --- Internal loop ---
    def _run(self) -> None:
        """Drain queue; process packets; periodic flush by time/rows."""
        while not self._stop.is_set():
            # Timed wait to allow periodic flush cadence
            timeout = max(0.02, self._flush_period * 0.5)  # Small latency
            try:
                pkt = self.q.get(timeout=timeout)
            except queue.Empty:
                pkt = None

            now = time.monotonic()                          # Monotonic snapshot

            if pkt is not None:
                self._last_activity_monotonic = now         # Update activity timestamp
                tag = pkt[0]
                if tag == "__stop__":
                    break
                try:
                    if tag == "sample":
                        self._on_sample(pkt)               # Update buffers
                    elif tag == "event":
                        self._on_event(pkt)                # Update sticky + markers
                    elif tag == "spike":
                        self._on_spike(pkt)                # Mark spike + markers
                except Exception:
                    # Robustness: ignore malformed packet
                    pass

            # Commit up to k_commit using fixed lookahead
            k_commit = self._k_seen_max - self._L
            self._commit_until(k_commit)

            # --- Idle watermark (C): if idle for long, finalize to k_seen_max ---
            if self._idle_watermark_sec > 0.0:
                idle_for = now - self._last_activity_monotonic
                if idle_for >= self._idle_watermark_sec:
                    # Commit everything seen so far; then flush I/O
                    self._commit_until(self._k_seen_max)
                    self._flush_io()
                    # Bump the activity timestamp to avoid repeated flush loops
                    self._last_activity_monotonic = now

            # Periodic flush (IO)
            if (
                now - self._last_flush_time >= self._flush_period
                or self._pending_committed >= self._flush_rows_threshold
            ):
                self._flush_io()
                self._last_flush_time = now
                self._pending_committed = 0

        # Final IO flush on exit
        self._flush_io()

    # --- Packet handlers ---
    def _on_sample(self, pkt: tuple) -> None:
        """Handle ("sample", k, t_q, device, pairs). Latest-wins in bucket."""
        _, k, t_q, dev, pairs = pkt
        self._k_seen_max = max(self._k_seen_max, int(k))
        self._tq_by_k[int(k)] = float(t_q)
        row = self._open_rows.setdefault(int(k), {})

        # Strict schema: only write channels provided by main; ignore others
        for ch, val in pairs:
            key = f"{dev}:{ch}"
            if key not in self._channels:
                continue  # Ignore channels outside the fixed header
            row[key] = self._fmt_val(val)  # Latest-wins

        # If header not yet written but channels are known enough, write it now
        if not self._header_frozen:
            self._write_synced_header()                    # Freeze as-is

    def _on_event(self, pkt: tuple) -> None:
        """Handle ("event", k, t_q, label, source, current_event_after)."""
        _, k, t_q, _label, source, current_after = pkt
        k = int(k)
        t_q = float(t_q)
        # Record change for sticky propagation during commit (do not advance now)
        self._event_changes[k] = str(current_after)        # Change takes effect at k
        # Emit markers row now (low volume, no lookahead)
        self._write_marker(k, t_q, event=str(current_after), spike="", source=str(source))

    def _on_spike(self, pkt: tuple) -> None:
        """Handle ("spike", k, t_q, label, source). Latest-wins if multiple."""
        _, k, t_q, label, source = pkt
        k = int(k)
        t_q = float(t_q)
        row = self._open_rows.setdefault(k, {})
        row["spike"] = str(label)                          # Latest-wins for same k
        self._tq_by_k[k] = t_q
        self._k_seen_max = max(self._k_seen_max, k)
        # Emit markers row now
        self._write_marker(k, t_q, event="", spike=str(label), source=str(source))

    # --- Commit / flush helpers ---
    def _commit_until(self, k_commit: int) -> None:
        """Write rows for all k ≤ k_commit; drop late arrivals thereafter."""
        # Writer must exist; otherwise nothing to do.
        if self._synced_writer is None:
            return

        # Ensure header exists before writing any row.
        # If you still support dynamic headers, this would call _write_synced_header().
        # In our design, header is written in start(), so this is a no-op guard.
        if not self._header_frozen:
            # If channels are unknown, postpone committing rows.
            if not self._channels:
                return
            self._write_synced_header()

        # If there is no pending time index, nothing to commit.
        if not self._open_rows and not self._tq_by_k:
            return

        # Collect ks to commit in order (≤ k_commit).
        ks = sorted(k for k in self._tq_by_k.keys() if k <= k_commit)

        for k in ks:
            t_q = self._tq_by_k.get(k, k * self._delta)  # Fallback uses grid

            # Apply any sticky-event changes up to and including this k.
            if self._event_changes:
                pending = [kk for kk in self._event_changes.keys() if kk <= k]
                if pending:
                    for kk in sorted(pending):
                        self._sticky_event = self._event_changes[kk]
                        self._event_changes.pop(kk, None)

            # Emit initial marker exactly once at the first committed k.
            if not self._initial_marker_emitted:
                self._write_marker(k, t_q, event=self._sticky_event, spike="", source="sync")
                self._initial_marker_emitted = True

            # Build the CSV row for this k.
            row_map = self._open_rows.pop(k, {})  # Might be empty if only markers
            row: List[str] = (([str(k)] if self._print_k else []) + [self._fmt_val(t_q)])
            for ch in self._channels:
                row.append(row_map.get(ch, ""))    # Empty cell for missing values
            row.append(row_map.get("spike", ""))   # Spike is only set at its k
            row.append(self._sticky_event)         # Current sticky event at this k

            # Write the row and advance counters/cleanup.
            self._synced_writer.writerow(row)
            self._tq_by_k.pop(k, None)
            self._pending_committed += 1

        # Cleanup any leftover event changes already committed (safety).
        for kk in list(self._event_changes.keys()):
            if kk <= k_commit:
                self._event_changes.pop(kk, None)

    def _flush_io(self) -> None:
        """Flush file handles to ensure data is persisted periodically."""
        try:
            if self._synced_fh:
                self._synced_fh.flush()
            if self._markers_fh:
                self._markers_fh.flush()
        except Exception:
            pass

    # --- IO helpers ---
    def _write_synced_header(self) -> None:
        """No-op if channels were not provided; header is written in start()."""
        if self._synced_writer is None or self._header_frozen:
            return
        if not self._channels:
            return
        header = ["k", "t_q"] + list(self._channels) + ["spike", "event"]
        self._synced_writer.writerow(header)
        self._header_frozen = True


    def _write_marker(self, k: int, t_q: float, *, event: str, spike: str, source: str) -> None:
        """Write a single marker row immediately (no lookahead, low volume)."""
        if self._markers_writer is None:
            return
        row = (([str(k)] if self._print_k else []) + [self._fmt_val(t_q), event, spike, source])
        self._markers_writer.writerow(row)

    @staticmethod
    def _fmt_val(v: float | None) -> str:
        """Format numbers compactly; map None to empty cell; keep text as-is."""
        if v is None:
            return ""  # CSV empty cell for gaps/invalids
        try:
            return f"{float(v):.6f}"
        except Exception:
            return str(v)
