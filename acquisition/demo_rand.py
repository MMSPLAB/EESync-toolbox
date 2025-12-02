# acquisition/demo_rand.py
# Demo sine generator with modulated amplitude and frequency per config.

from __future__ import annotations

import math
import threading
import time
from typing import Any, Dict, Optional

from utils.logger import get_logger
from processing.sync_controller import sync_manager as SYNC

logger = get_logger(__name__)


class _SineWaveThread(threading.Thread):
    """Background producer emitting two sine channels with slow modulation."""

    def __init__(
        self,
        device_name: str,
        emission_freq_hz: float,
        signal_freq_hz: float,
        amp_rate_scale: float,
        freq_rate_scale: float,
        base_amp: float,
        amp_min_mult: float,
        amp_max_mult: float,
        freq_min_mult: float,
        freq_max_mult: float,
        enable_ch1: bool,
        enable_ch2: bool,
        stop_evt: Optional[threading.Event] = None,
    ) -> None:
        super().__init__(name=f"DemoSine[{device_name}]", daemon=True)
        # Store device identity and normalized configuration knobs.
        self.device_name = device_name
        self.emission_freq_hz = float(emission_freq_hz)
        self.signal_freq_hz = float(signal_freq_hz)
        self.amp_rate_scale = max(float(amp_rate_scale), 0.0)
        self.freq_rate_scale = max(float(freq_rate_scale), 0.0)
        self.base_amp = abs(float(base_amp))
        self.enable_ch1 = bool(enable_ch1)
        self.enable_ch2 = bool(enable_ch2)
        self._stop_evt = stop_evt or threading.Event()

        # Maintain exact timing to preserve the requested emission cadence.
        self._period = 1.0 / self.emission_freq_hz if self.emission_freq_hz > 0.0 else 0.0
        self._start_ts = time.monotonic()
        self._next_emit = self._start_ts
        self._sample_idx = 0

        # Translate amplitude multipliers into usable numeric bounds.
        amp_min_mult_f = max(float(amp_min_mult), 0.0)
        amp_max_mult_f = max(float(amp_max_mult), 0.0)
        if amp_min_mult_f > amp_max_mult_f:
            amp_min_mult_f, amp_max_mult_f = amp_max_mult_f, amp_min_mult_f
        if amp_max_mult_f == amp_min_mult_f:
            amp_max_mult_f = amp_min_mult_f + 1.0

        self._amp_min = self.base_amp * amp_min_mult_f
        self._amp_max = self.base_amp * amp_max_mult_f
        if self._amp_max <= self._amp_min:
            add_span = max(self.base_amp, 1.0)
            self._amp_max = self._amp_min + add_span

        self._amp_range = self._amp_max - self._amp_min
        self._amp = min(max(self.base_amp, self._amp_min), self._amp_max)

        # Step size drives amplitude modulation speed within allowed range.
        rate_ratio = self.signal_freq_hz / max(self.emission_freq_hz, 1.0)
        amp_step = 0.1 * rate_ratio * self.amp_rate_scale * max(self._amp_range, 1e-6)
        self._amp_step = min(max(amp_step, 0.0), self._amp_range)
        self._amp_direction = 1.0 if self._amp_range > 0.0 and self._amp_step > 0.0 else 0.0

        # Derive frequency sweep guards relative to the nominal tone.
        base_freq = max(self.signal_freq_hz, 0.1)
        freq_min = max(float(freq_min_mult) * base_freq, 0.0)
        freq_max = max(float(freq_max_mult) * base_freq, freq_min + 0.1)
        if freq_min > freq_max:
            freq_min, freq_max = freq_max, freq_min
        self._freq_min = freq_min
        self._freq_max = freq_max
        self._freq = min(max(base_freq, self._freq_min), self._freq_max)

        self._freq_range = self._freq_max - self._freq_min
        # Frequency modulation step scales with requested rate and span.
        freq_step = 0.05 * base_freq * self.freq_rate_scale
        self._freq_step = min(max(freq_step, 0.0), self._freq_range)
        self._freq_direction = 1.0 if self._freq_range > 0.0 and self._freq_step > 0.0 else 0.0
        self._phase_ch2 = 0.0

    def run(self) -> None:
        if self._period <= 0.0:
            logger.warning("demo_rand '%s': non-positive FS, nothing to emit", self.device_name)
            return

        while not self._stop_evt.is_set():
            # Pace the loop so emission jitter stays bounded.
            now = time.monotonic()
            if now < self._next_emit:
                time.sleep(min(self._next_emit - now, 0.05))
                continue

            # Compute current sample values using elapsed time since start.
            elapsed = self._sample_idx * self._period
            ch1_value = self._amp * math.sin(2.0 * math.pi * self.signal_freq_hz * elapsed)
            device_ts = self._start_ts + elapsed

            pairs = []
            if self.enable_ch1:
                # ch_1 reports amplitude modulation applied to the base tone.
                pairs.append(("ch_1", ch1_value))

            if self.enable_ch2:
                # Phase accumulation preserves continuity for ch_2 frequency sweep.
                self._phase_ch2 += 2.0 * math.pi * self._freq * self._period
                self._phase_ch2 = math.fmod(self._phase_ch2, 2.0 * math.pi)
                value_ch2 = math.sin(self._phase_ch2)
                pairs.append(("ch_2", value_ch2))

            if pairs:
                # Emit packet containing enabled channel samples for this tick.
                SYNC.enqueue_packet(
                    device_ts=device_ts,
                    device_name=self.device_name,
                    channel_pairs=tuple(pairs),
                )

            self._sample_idx += 1
            self._next_emit += self._period
            if self._amp_step > 0.0 and self._amp_direction != 0.0:
                self._amp += self._amp_direction * self._amp_step
                if self._amp >= self._amp_max:
                    self._amp = self._amp_max
                    self._amp_direction = -1.0
                elif self._amp <= self._amp_min:
                    self._amp = self._amp_min
                    self._amp_direction = 1.0

            if self.enable_ch2 and self._freq_step > 0.0 and self._freq_direction != 0.0:
                self._freq += self._freq_direction * self._freq_step
                if self._freq >= self._freq_max:
                    self._freq = self._freq_max
                    self._freq_direction = -1.0
                elif self._freq <= self._freq_min:
                    self._freq = self._freq_min
                    self._freq_direction = 1.0

    def stop(self) -> None:
        # Allow external callers to end the loop gracefully.
        self._stop_evt.set()


class DemoRandManager:
    """Minimal manager that starts/stops the sine wave producer."""

    def __init__(self, instance_cfg: Dict[str, Any]) -> None:
        # Retain the configuration snapshot for this instance.
        self.cfg = instance_cfg
        self.name = str(self.cfg.get("DEVICE_NAME", "demo_rand")).strip()
        self._thr: Optional[_SineWaveThread] = None

    def start_stream(self) -> None:
        if self._thr is not None:
            return

        # Resolve runtime parameters from the instance configuration tree.
        fs = float(self.cfg.get("FS", 64.0))
        params = self.cfg.get("PARAMS", {})
        signal_freq = float(params.get("SIGNAL_FREQ_HZ", 2.0))
        amp_rate_scale = float(params.get("AMP_RATE_SCALE", 1.0))
        freq_rate_scale = float(params.get("FREQ_RATE_SCALE", 0.25))
        base_amp = float(params.get("AMP_BASE", 1.0))
        amp_min_mult = float(params.get("AMP_MIN_MULT", 0.5))
        amp_max_mult = float(params.get("AMP_MAX_MULT", 3.0))
        freq_min_mult = float(params.get("FREQ_MIN_MULT", 0.5))
        freq_max_mult = float(params.get("FREQ_MAX_MULT", 2.0))

        # Honor per-channel enable flags before constructing the producer.
        chans = self.cfg.get("CHANNELS", {})
        enable_ch1 = bool(chans.get("ch_1", True))
        enable_ch2 = bool(chans.get("ch_2", True))
        if not (enable_ch1 or enable_ch2):
            # Avoid spinning up a thread when nothing would be emitted.
            logger.info("demo_rand '%s': no channels enabled; skipping", self.name)
            return

        # Spawn the background worker with the resolved configuration.
        self._thr = _SineWaveThread(
            device_name=self.name,
            emission_freq_hz=fs,
            signal_freq_hz=signal_freq,
            amp_rate_scale=amp_rate_scale,
            freq_rate_scale=freq_rate_scale,
            base_amp=base_amp,
            amp_min_mult=amp_min_mult,
            amp_max_mult=amp_max_mult,
            enable_ch1=enable_ch1,
            enable_ch2=enable_ch2,
            freq_min_mult=freq_min_mult,
            freq_max_mult=freq_max_mult,
        )
        self._thr.start()
        # Log resolved parameters to aid runtime diagnostics.
        logger.info(
            (
                "demo_rand '%s': emitting sine wave @ %.3f Hz (signal %.3f Hz, "
                "amp_base=%.3f, amp_rate_scale=%.3f, amp_min_mult=%.3f, amp_max_mult=%.3f, "
                "freq_rate_scale=%.3f, freq_min_mult=%.3f, freq_max_mult=%.3f)"
            ),
            self.name,
            fs,
            signal_freq,
            base_amp,
            amp_rate_scale,
            amp_min_mult,
            amp_max_mult,
            freq_rate_scale,
            freq_min_mult,
            freq_max_mult,
        )

    def stop(self) -> None:
        # Signal the worker thread to terminate, if it exists.
        thr = self._thr
        if thr is None:
            return
        try:
            thr.stop()
        finally:
            self._thr = None
