# acquisition/shimmer_device.py
# Communication module for Shimmer: per-instance config, handlers, callbacks, and clean stop.

from __future__ import annotations

import threading
from typing import List, Tuple, Optional, Union
from serial import Serial
from pyshimmer import ShimmerBluetooth, DEFAULT_BAUDRATE

from utils.logger import get_logger
from acquisition import shimmer_timebase
from acquisition.handlers.handler_shimmer_gsr import build_gsr_handler
from acquisition.handlers.handler_shimmer_ppg import build_ppg_handler
from acquisition.handlers.handler_shimmer_emg import build_emg_handler

logger = get_logger(__name__)

ValueT = Optional[Union[float, int]]


# ====== SHIMMER MANAGER CLASS ======
class ShimmerManager:
    """Facade for a single Shimmer device instance (instance-aware)."""

    def __init__(self, instance_cfg: dict):
        """Initialize ShimmerManager with instance configuration."""
        self.cfg = instance_cfg                                    # Keep original config ref
        self.params = dict(instance_cfg.get("PARAMS", {}))
        self.device_name = str(instance_cfg.get("DEVICE_NAME", "shimmer")).strip() or "shimmer"
        self.port = str(self.params.get("COM_PORT", ""))           # Serial/COM port
        self.fs_hz = float(instance_cfg.get("FS", 128.0))          # Sampling rate (Hz)

        # Extract channels defined at instance level
        channels = instance_cfg.get("CHANNELS", {})
        if not isinstance(channels, dict):
            raise ValueError(f"[{self.device_name}] CHANNELS must be a dict {{str: bool}}.")

        # Collect channel names where value=True
        self.enabled_chs = {k for k, v in channels.items() if v}

        # Derive channel flags
        self.want_gsr_raw = "RAW_gsr_uS" in self.enabled_chs
        self.want_gsr_flt = "gsr_uS" in self.enabled_chs
        self.want_ppg_raw = "RAW_ppg_mV" in self.enabled_chs
        self.want_ppg_flt = "ppg_mV" in self.enabled_chs
        self.want_emg1_raw = "RAW_emg1_uV" in self.enabled_chs
        self.want_emg2_raw = "RAW_emg2_uV" in self.enabled_chs
        self.want_emg1_flt = "emg1_uV" in self.enabled_chs
        self.want_emg2_flt = "emg2_uV" in self.enabled_chs


        # Unified stop event for streaming threads
        self._stop_evt = threading.Event()

        # Runtime state
        self.shim: Optional[ShimmerBluetooth] = None
        self.serial: Optional[Serial] = None
        self._cb = None  # Keep callback reference to allow removal on stop

    # ====== STREAM CONTROL ======
    def start_stream(self) -> None:
        """Connect device, create handlers, attach callback, and start streaming."""
        logger.info("[%s] Initializing Shimmer (port=%s, fs=%.1f Hz)...",
                    self.device_name, self.port, self.fs_hz)

        # Reset timebase for this instance
        shimmer_timebase.reset(key=self.device_name)

        # Build per-instance handlers
        hcfg = {
            "VREF_GSR": float(self.params.get("VREF_GSR", 3.0)),
            "V_BIAS":   float(self.params.get("V_BIAS", 0.5)),
            "VREF_PPG": float(self.params.get("VREF_PPG", 3.0)),
            "PPG_CHANNEL": str(self.params.get("PPG_CHANNEL", "INTERNAL_ADC_13")),
            "PPG_INVERT":  bool(self.params.get("PPG_INVERT", True)),
            "FS_HZ": float(self.fs_hz),
            "VREF_EXG": float(self.params.get("VREF_EXG", 2.42)),
            "EXG_GAIN": float(self.params.get("EXG_GAIN", 12.0)),
            "EMG_CHANNELS": list(self.params.get("EMG_CHANNELS", [
                "EXG_ADS1292R_1_CH1_24BIT",
                "EXG_ADS1292R_1_CH2_24BIT"
            ])),
        }

        # Build GSR handler if needed
        gsr_fn = None
        if self.want_gsr_raw or self.want_gsr_flt:
            gsr_fn = build_gsr_handler(
                handler_cfg=hcfg,
                timebase_key=self.device_name,
                stop_event=self._stop_evt,
                want_raw=self.want_gsr_raw,
                want_filtered=self.want_gsr_flt,
            )

        # Build PPG handler if needed
        ppg_fn = None
        if self.want_ppg_raw or self.want_ppg_flt:
            ppg_fn = build_ppg_handler(
                handler_cfg=hcfg,
                timebase_key=self.device_name,
                stop_event=self._stop_evt,
                want_raw=self.want_ppg_raw,
                want_filtered=self.want_ppg_flt,
            )

        # Build EMG handler if any EMG stream is requested (explicit per-channel flags)
        # Build EMG handler if any EMG stream is requested (explicit per-channel flags)
        emg_fn = None
        if self.want_emg1_raw or self.want_emg2_raw or self.want_emg1_flt or self.want_emg2_flt:
            emg_fn = build_emg_handler(
                handler_cfg=hcfg,
                timebase_key=self.device_name,
                stop_event=self._stop_evt,
                want_emg1_raw=self.want_emg1_raw,
                want_emg2_raw=self.want_emg2_raw,
                want_emg1_flt=self.want_emg1_flt,
                want_emg2_flt=self.want_emg2_flt,
            )

        # --- Serial connection setup ---
        try:
            self.serial = Serial(self.port, DEFAULT_BAUDRATE, timeout=None)  # blocking read
            self.shim = ShimmerBluetooth(self.serial)
            self.shim.initialize()
            logger.info("[%s] Connection established.", self.device_name)
        except Exception as e:
            # Fail fast: propagate so the caller can abort the whole session
            logger.error("[%s] Failed to connect to Shimmer: %s", self.device_name, e)
            self.serial = None
            self.shim = None
            raise RuntimeError(f"Shimmer connect failed for {self.device_name}")

        # --- Unified callback definition ---
        from processing.sync_controller import sync_manager as SYNC

        def _on_packet(pkt) -> None:
            """Handle incoming packet and forward valid data to SYNC."""
            if self._stop_evt.is_set():     # Early exit on stop
                return
            try:
                # Packet timestamp conversion in relative seconds
                t_s = shimmer_timebase.device_time_s(pkt, key=self.device_name)
                pairs: List[Tuple[str, ValueT]] = []    # Prepare result list as tuples

                # Collect handler outputs (best-effort)
                if gsr_fn:
                    try:
                        pairs.extend(gsr_fn(pkt))   # Call GSR handler and save result
                    except Exception as err:
                        logger.warning("[%s] GSR handler error: %s", self.device_name, err)

                if ppg_fn:
                    try:
                        pairs.extend(ppg_fn(pkt))   # Call PPG handler and save result
                    except Exception as err:
                        logger.warning("[%s] PPG handler error: %s", self.device_name, err)

                if emg_fn:
                    try:
                        pairs.extend(emg_fn(pkt))
                    except Exception as err:
                        logger.warning("[%s] EMG handler error: %s", self.device_name, err)


                # Push data if any channel produced output
                if pairs:
                    SYNC.enqueue_packet(
                        device_ts=float(t_s),
                        device_name=self.device_name,
                        channel_pairs=pairs,
                    )

            except Exception as e:
                logger.warning("[%s] Packet processing failed: %s", self.device_name, e)

        # Register callback and start streaming
        try:
            self._cb = _on_packet                                # Keep reference
            self.shim.add_stream_callback(self._cb)              # Register callback
            self.shim.start_streaming()
            logger.info("[%s] Streaming started.", self.device_name)
        except Exception as e:
            logger.error("[%s] Failed to start streaming: %s", self.device_name, e)
            self.stop()

    # ====== TERMINATION ======
    def stop(self) -> None:
        """Stop streaming and close serial port safely."""
        self._stop_evt.set()  # Block new work ASAP

        # Try to detach the callback to avoid late invocations during teardown
        try:
            if self.shim and self._cb:
                remove_cb = getattr(self.shim, "remove_stream_callback", None)
                if callable(remove_cb):
                    remove_cb(self._cb)  # Best-effort: API may or may not exist
        except Exception as e:
            logger.warning("[%s] Callback removal failed: %s", self.device_name, e)
        finally:
            self._cb = None

        # --- Step 1: stop streaming and shutdown device (with timeout guard) ---
        if self.shim:
            logger.info("[%s] Stopping streaming...", self.device_name)

            # Run stop+shutdown in a worker to avoid indefinite block
            done = threading.Event()  # Signals completion

            def _worker_stop():
                """Call stop_streaming() then shutdown(); always signal 'done'."""
                # Use a local snapshot to avoid races with self.shim being set to None
                shim = self.shim  # type: Optional[ShimmerBluetooth]
                try:
                    if shim is not None:
                        stop_fn = getattr(shim, "stop_streaming", None)  # May be absent
                        if callable(stop_fn):
                            try:
                                stop_fn()  # May block internally
                            except Exception:
                                pass
                        try:
                            shim.shutdown()  # Ensure device shutdown
                        except Exception:
                            pass
                finally:
                    done.set()

            t = threading.Thread(target=_worker_stop, name="ShimmerStop", daemon=True)
            t.start()

            # Wait bounded time; if it hangs, force-unwind via serial close
            if not done.wait(2):                         # 2s safety budget
                logger.warning("[%s] Stop/shutdown timed out; forcing serial close.", self.device_name)
                try:
                    if self.serial:
                        self.serial.close()              # Break underlying I/O
                except Exception:
                    pass
                finally:
                    # Give worker a short grace after serial close
                    done.wait(0.5)

            # Done: release shim ref regardless of outcome
            self.shim = None
            logger.info("[%s] Stop sequence 1/2: Streaming stop/shutdown attempted.", self.device_name)

        # --- Step 2: close serial to release OS resource ---
        if self.serial:
            try:
                self.serial.close()
                logger.info("[%s] Stop sequence 2/2: Serial port closed.", self.device_name)
            except Exception as e:
                logger.warning("[%s] Stop sequence 2/2: Serial close failed: %s", self.device_name, e)
            finally:
                self.serial = None