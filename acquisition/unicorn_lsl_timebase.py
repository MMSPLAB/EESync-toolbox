# acquisition/unicorn_lsl_timebase.py
# Deterministic 1/fs timebase for Unicorn EEG to remove LSL timestamp jitter.

from __future__ import annotations

import threading


class UnicornLSLTimebase:
    """Uniform 1/fs tick generator anchored to the first seen LSL stamp.

    Summary: map incoming EEG samples to a deterministic device_ts at 1/fs.
    Body: on first use, anchor to the first LSL stamp; then each sample gets
    prev + 1/fs. Optional soft realign if a long inactivity is detected.
    """

    # ====== CONSTRUCTION ======
    def __init__(self, fs_hz: float) -> None:
        """Configure nominal fs and reset internal state."""
        self._fs = float(fs_hz) if fs_hz and fs_hz > 0 else 250.0  # Nominal fs
        self._dt = 1.0 / self._fs                                  # Uniform step
        self._lock = threading.Lock()                               # Thread safety

        self._anchored = False                                      # Anchor flag
        self._t_curr = 0.0                                          # Next tick ts
        self._last_wall = 0.0                                       # Last LSL stamp
        self._soft_gap_sec = 0.250                                  # Realign gap

    # ====== CONTROL ======
    def reset(self) -> None:
        """Clear anchor to restart from next first stamp."""
        with self._lock:
            self._anchored = False
            self._t_curr = 0.0
            self._last_wall = 0.0

    def prime_from_first_stamp(self, first_stamp: float) -> None:
        """Anchor on first LSL stamp and prepare next tick."""
        with self._lock:
            base = float(first_stamp)
            self._t_curr = base
            self._last_wall = base
            self._anchored = True

    # ====== MAPPING ======
    def next_tick(self, last_seen_lsl_stamp: float | None = None) -> float:
        """Return next deterministic ts; soft realign after long inactivity.

        If last_seen_lsl_stamp is given and a long gap is observed, shift the
        timeline to that stamp before continuing at 1/fs. Normal operation does
        not depend on LSL jitter.
        """
        with self._lock:
            if not self._anchored:
                # Defensive: if called unprimed, self-prime on provided stamp or 0
                base = float(last_seen_lsl_stamp or 0.0)
                self._t_curr = base
                self._last_wall = base
                self._anchored = True

            # Optional soft realign on long inactivity
            if last_seen_lsl_stamp is not None:
                wall = float(last_seen_lsl_stamp)
                if wall - self._last_wall >= self._soft_gap_sec:
                    # Re-anchor to recent wall clock to avoid large drift jumps
                    self._t_curr = wall
                self._last_wall = wall

            out = self._t_curr             # Emit current tick
            self._t_curr = out + self._dt  # Advance by 1/fs
            return out

    # ====== INFO ======
    @property
    def fs(self) -> float:
        """Return nominal fs."""
        return self._fs

    @property
    def dt(self) -> float:
        """Return uniform step 1/fs."""
        return self._dt

    @property
    def anchored(self) -> bool:
        """Return True if anchored to a first stamp."""
        return self._anchored