# acquisition/unicorn_lsl.py
# LSL Unicorn EEG manager: resolve latest stream, read 17ch, pick first 8 EEG, filter, forward to SYNC.

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional, Tuple

from utils.logger import get_logger
from processing.rt_filter import StreamingSOS, design_sos
from processing.sync_controller import sync_manager as SYNC
from acquisition.unicorn_lsl_timebase import UnicornLSLTimebase
from utils.config import CONFIG

logger = get_logger(__name__)

try:
    from pylsl import resolve_byprop, resolve_streams, StreamInlet, StreamInfo
except Exception as e:  # pragma: no cover
    raise RuntimeError(
        "pylsl is required for Unicorn LSL acquisition. Install with: pip install pylsl"
    ) from e


# ====== RESOLUTION HELPERS ======
def _pick_latest_by_created_at(infos: List[StreamInfo]) -> Optional[StreamInfo]:
    """Return candidate with max created_at; fallback to first if attr missing."""
    if not infos:
        return None
    try:
        return max(infos, key=lambda inf: float(getattr(inf, "created_at", lambda: 0.0)() or 0.0))
    except Exception:
        return infos[0]


def _resolve_unicorn_stream(stream_name: str, stream_type: str, timeout_s: float) -> StreamInfo:
    """Resolve LSL by (name,type) and pick latest by created_at.

    Summary: find best matching Unicorn stream.
    Body: search by 'name' then by 'type'; if none, fallback to any; prefer the
    newest by created_at(). Raise TimeoutError on failure.
    """
    end = time.monotonic() + float(timeout_s)

    def remain() -> float:
        return max(0.0, end - time.monotonic())

    candidates: List[StreamInfo] = []

    # Try exact NAME (short slice).
    if stream_name and remain() > 0.0:
        try:
            t = min(1.0, remain())
            by_name = resolve_byprop("name", stream_name, minimum=1, timeout=t)
            candidates.extend(by_name or [])
        except Exception:
            pass

    # Then TYPE.
    if not candidates and stream_type and remain() > 0.0:
        try:
            by_type = resolve_byprop("type", stream_type, minimum=1, timeout=remain())
            candidates.extend(by_type or [])
        except Exception:
            pass

    # Fallback: any.
    if not candidates and remain() > 0.0:
        try:
            anylst = resolve_streams(wait_time=remain())
            candidates.extend(anylst or [])
        except Exception:
            pass

    # Filter by exact name/type if provided to avoid unrelated streams.
    filtered: List[StreamInfo] = []
    for inf in candidates:
        name_ok = (not stream_name) or ((inf.name() or "") == stream_name)
        type_ok = (not stream_type) or ((inf.type() or "") == stream_type)
        if name_ok and type_ok:
            filtered.append(inf)

    pool = filtered or candidates
    best = _pick_latest_by_created_at(pool)
    if not best:
        raise TimeoutError(
            f"Unable to resolve LSL stream (name={stream_name!r}, type={stream_type!r}) within {timeout_s:.1f}s"
        )
    return best


# ====== MANAGER ======
class UnicornManager:
    """Manager for one Unicorn EEG LSL stream (uniform 8-ch EEG handling)."""

    def __init__(self, instance_cfg: dict) -> None:
        """Store config and precompute flags. Keep implementation minimal."""
        self.cfg = instance_cfg  # Original instance config
        params = dict(instance_cfg.get("PARAMS", {}))

        # Instance metadata
        self.device_name = str(instance_cfg.get("DEVICE_NAME", "unicorn")).strip() or "unicorn"
        self.fs_nominal = float(instance_cfg.get("FS", 250.0))  # For logs only

        # Stream resolution parameters (name/type must match publisher)
        self.stream_name = str(params.get("STREAM_NAME", "EEG_EEG"))
        self.stream_type = str(params.get("STREAM_TYPE", "EEG"))
        self.resolve_timeout_s = float(params.get("RESOLVE_TIMEOUT_S", 5.0))

        # Inlet parameters
        self.inlet_chunk_len = int(params.get("INLET_CHUNK_LEN", 0))  # 0 → source-controlled
        self.inlet_max_buf_s = float(params.get("INLET_MAX_BUF_S", 10.0))

        # Index selection: which columns of the incoming stream are EEG (0-based).
        # Default: first 8 indices (common Unicorn "Data" layout: EEG first).
        self.eeg_indexes: List[int] = list(params.get("EEG_INDEXES", list(range(8))))

        # Expected logical EEG outputs (always 8 for our project).
        self.expected_eeg_channels = 8

        # Channel enables from config (no discovery; names are project-defined).
        channels = self.cfg.get("CHANNELS", {})
        if not isinstance(channels, dict):
            raise ValueError(f"[{self.device_name}] CHANNELS must be a dict {{str: bool}}.")
        self.enabled_chs = {k for k, v in channels.items() if v}

        # Derived sets of channel indices (1..8) for RAW and filtered outputs.
        self.want_raw_idx = {i for i in range(1, 9) if f"RAW_eeg{i}_uV" in self.enabled_chs}
        self.want_flt_idx = {i for i in range(1, 9) if f"eeg{i}_uV" in self.enabled_chs}

        # Runtime state
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._inlet: Optional[StreamInlet] = None
        self._srate: float = 0.0

        # Per-channel filters (allocated on start if needed)
        self._pipes: Dict[int, StreamingSOS] = {}

        self._tb: Optional[UnicornLSLTimebase] = None  # Deterministic 1/fs timebase

        # --- Telemetry config/state (identical philosophy to Shimmer handlers) ---
        self._telemetry_window_s: float = float(CONFIG.get("telemetry", {}).get("WINDOW_S", 10.0))  # Window size (s)
        self._telem_invalid_count: int = 0  # Invalid samples counter in current window
        self._telem_last_t0: Optional[float] = None  # Window start time (device time)

    # --- Telemetry helper (same syntax/behavior as Shimmer) ---
    def _telemetry_update(self, t_s: float, invalid: bool) -> None:
        """Aggregate invalid samples and emit every WINDOW_S seconds."""
        if self._telem_last_t0 is None:
            self._telem_last_t0 = t_s  # Initialize window start
        if invalid:
            self._telem_invalid_count += 1  # Count invalid sample
        elapsed = t_s - self._telem_last_t0
        if elapsed >= self._telemetry_window_s:
            if self._telem_invalid_count > 0:
                logger.warning(
                    "Telemetry window: sensor=EEG[%s] invalid_samples=%d window=%.1fs",
                    self.device_name, self._telem_invalid_count, elapsed
                )
                self._telem_invalid_count = 0  # Reset counter
            self._telem_last_t0 = t_s  # Roll window

    # ====== PUBLIC API ======
    def start_stream(self) -> None:
        """Resolve stream synchronously; on failure raise to let main abort."""
        # Resolve LSL stream (fail-fast path).
        try:
            info = _resolve_unicorn_stream(self.stream_name, self.stream_type, self.resolve_timeout_s)
        except Exception as e:
            logger.error("[%s] LSL resolve failed: %s", self.device_name, e)
            raise

        name = info.name() or "Unicorn"
        srate = float(info.nominal_srate() or 0.0)
        chn_count = int(info.channel_count() or 0)
        self._srate = srate

        # Validate stream parameters explicitly (simple and clear).
        if chn_count <= 0 or srate <= 0.0:
            raise RuntimeError(
                f"[{self.device_name}] Invalid stream params: ch={chn_count} fs={srate:.3f} "
                f"(name={name} type={info.type() or '?'})"
            )

        # Validate EEG indexes against incoming channel_count.
        if any((idx < 0 or idx >= chn_count) for idx in self.eeg_indexes):
            raise RuntimeError(
                f"[{self.device_name}] EEG_INDEXES out of range for ch_count={chn_count}: {self.eeg_indexes}"
            )
        if len(self.eeg_indexes) != self.expected_eeg_channels:
            raise RuntimeError(
                f"[{self.device_name}] EEG_INDEXES must list exactly {self.expected_eeg_channels} elements; got {len(self.eeg_indexes)}"
            )

        logger.info(
            "[%s] LSL resolved: name=%s type=%s ch=%d fs=%.3f (using EEG indexes=%s)",
            self.device_name, name, info.type() or "?", chn_count, srate, self.eeg_indexes
        )

        # Build deterministic timebase anchored on first stamp
        self._tb = UnicornLSLTimebase(fs_hz=self._srate)

        # Open inlet and check initial liveness.
        try:
            inlet = StreamInlet(info, max_buflen=int(self.inlet_max_buf_s))
            inlet.open_stream(timeout=self.resolve_timeout_s)
            self._inlet = inlet
        except Exception as e:
            self._inlet = None
            raise RuntimeError(f"[{self.device_name}] Failed to open LSL inlet: {e}")

        if not self._liveness_check(self._inlet):
            self._safe_close_inlet()
            raise RuntimeError(f"[{self.device_name}] No data received in liveness window.")

        # Prepare filters: one spec 'eeg_uV' applied to all 8 filtered channels.
        try:
            spec = dict(
                CONFIG.get("devices", {})
                .get("unicorn_lsl", {})
                .get("FILTERS", {})
                .get("eeg_uV", {})
            )
        except Exception:
            spec = {}

        sos_chain = design_sos(sensor_key=f"{self.device_name}:eeg_uV", fs_hz=self._srate, spec=spec)

        self._pipes.clear()
        for i in range(1, self.expected_eeg_channels + 1):
            if i in self.want_flt_idx:
                self._pipes[i] = StreamingSOS(sos_chain, context=f"{self.device_name}:eeg{i}_uV")

        # Spawn reader thread (read loop only; resolution already done).
        self._stop_evt.clear()
        t = threading.Thread(target=self._read_loop, name=f"Unicorn[{self.device_name}]", daemon=True)
        t.start()
        self._thread = t
        logger.info("[%s] Streaming started (chunk_len=%s).",
                    self.device_name, "src" if self.inlet_chunk_len <= 0 else str(self.inlet_chunk_len))

    def stop(self) -> None:
        """Signal stop and close inlet safely."""
        self._stop_evt.set()
        if self._tb is not None:
            try:
                self._tb.reset()
            except Exception:
                pass
        self._safe_close_inlet()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        logger.info("[%s] Unicorn LSL stopped.", self.device_name)

    # ====== INTERNALS ======
    def _liveness_check(self, inlet: StreamInlet) -> bool:
        """Return True if at least one chunk arrives within ~1.0 s."""
        t0 = time.monotonic()
        while (time.monotonic() - t0) < 1.0 and not self._stop_evt.is_set():
            try:
                s, _ = inlet.pull_chunk(timeout=0.1)
                if s:
                    return True
            except Exception:
                break
        return False

    def _safe_close_inlet(self) -> None:
        """Close inlet with guards and clear reference."""
        try:
            if self._inlet is not None:
                try:
                    self._inlet.close_stream()
                except Exception:
                    pass
        finally:
            self._inlet = None
            logger.info("[%s] Inlet closed.", self.device_name)

    def _read_loop(self) -> None:
        """Pull chunks, pick EEG columns, filter, and enqueue to SYNC."""
        inlet = self._inlet
        if inlet is None:
            return

        try:
            while not self._stop_evt.is_set():
                # Use source-controlled chunking unless inlet_chunk_len > 0.
                if self.inlet_chunk_len > 0:
                    samples, stamps = inlet.pull_chunk(max_samples=self.inlet_chunk_len, timeout=0.2)
                else:
                    samples, stamps = inlet.pull_chunk(timeout=0.2)

                if not samples:
                    continue  # Idle gaps are normal on LSL; keep loop light.

                # Prime timebase on the very first stamp of the first non-empty chunk
                if self._tb is not None and not self._tb.anchored:
                    try:
                        first_stamp = float(stamps[0])
                        self._tb.prime_from_first_stamp(first_stamp)
                    except Exception:
                        # If stamps[] is empty/unexpected, prime lazily in next_tick()
                        pass

                for row, ts in zip(samples, stamps):
                    # Extract only the EEG columns by configured indexes (0-based).
                    # If row shorter than expected, pad with NaN; if longer, it's fine.
                    vals: List[float] = []
                    for idx in self.eeg_indexes:
                        v = float(row[idx]) if idx < len(row) else float("nan")
                        vals.append(v)

                    pairs: List[Tuple[str, Optional[float]]] = []

                    # RAW emissions (1..8).
                    for i in self.want_raw_idx:
                        v = float(vals[i - 1])  # Pass-through RAW in uV
                        pairs.append((f"RAW_eeg{i}_uV", v))

                    # Filtered emissions (1..8).
                    invalid_sample = False  # Track invalidity for telemetry
                    for i in self.want_flt_idx:
                        v = float(vals[i - 1])
                        pipe = self._pipes.get(i)
                        if pipe is not None:
                            v_f = pipe.apply(v)
                            # Convert non-finite to None for downstream safety.
                            if not (isinstance(v_f, (int, float)) and (v_f == v_f)):
                                export_val: Optional[float] = None
                                invalid_sample = True  # Mark invalid filtered sample
                            else:
                                export_val = float(v_f)
                            pairs.append((f"eeg{i}_uV", export_val))

                    if pairs:
                        try:
                            # Use deterministic 1/fs timebase to remove LSL jitter
                            dev_ts = float(ts)  # Fallback value
                            if self._tb is not None:
                                try:
                                    dev_ts = self._tb.next_tick(last_seen_lsl_stamp=float(ts))
                                except Exception:
                                    # Best-effort: on any error, fall back to raw LSL stamp
                                    dev_ts = float(ts)

                            # --- Telemetry update based on filtered invalidity (like Shimmer) ---
                            self._telemetry_update(dev_ts, invalid_sample)

                            SYNC.enqueue_packet(
                                device_ts=dev_ts,
                                device_name=self.device_name,
                                channel_pairs=pairs,
                            )
                        except Exception as e:
                            logger.warning("[%s] enqueue_packet failed: %s", self.device_name, e)

        except Exception as e:
            logger.error("[%s] Read loop error: %s", self.device_name, e)
        finally:
            self._safe_close_inlet()