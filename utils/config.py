# utils/config.py
# Centralized configuration dictionary with user overrides from settings.py.

from __future__ import annotations

from typing import Any, Dict
from utils.logger import get_logger

logger = get_logger(__name__)

# ====== DEFAULT (SAFE) CONFIG ======
# NOTE: These values act as safe fallbacks. User overrides must be provided in
# settings.py as a top-level dict named SETTINGS. We validate and then merge.

_DEFAULT_CONFIG: Dict[str, Any] = {
    "system": {
        "CHECK_DEPENCENCIES": False,  # Enable dependency check at startup
    },

    # +–––––––––––––––––––––––––––––+
    # | Event & Spike configuration |
    # +–––––––––––––––––––––––––––––+
    "events": {
        "ENABLED": True,
        "EVENT_KEYMAP": {
            "0": "REST",    # First is DEFAULT event
            "1": "TASK_1",
            "2": "TASK_2",
            "3": "TASK_3",
            "4": "TASK_4"
        }
    },

    "spikes": {
        "ENABLED": True,
        "SPIKE_KEYMAP": {
            "a": "SPIKE_A",
            "s": "SPIKE_S",
            "d": "SPIKE_D",
            "f": "SPIKE_F"
        }
    },

    # +––––––––––––––––––––––––––––––––––––––––––––––––––––––––+
    # | Data export, synchronizer, and Live Plot configuration |
    # +––––––––––––––––––––––––––––––––––––––––––––––––––––––––+
    "export": {
        "EXPORT_ENABLE": True,      # Global export enable/disable switch
        "CSV_SIGNAL_ENABLE": True,  # Flag to enable/disable signal csv creation
        "CSV_MARKER_ENABLE": False, # Flag to enable/disable marker csv creation
        "LOOKAHEAD_SEC": 2.0,       # Countermeasure for late data from devices (BLE/USB/OS)
        "FLUSH_PERIOD_SEC": 0.5,    # Periodic flush to disk
        "FLUSH_ROWS": 0,            # 0 means "auto" = min(2048, max(64, round(fs_max * FLUSH_PERIOD_SEC)))
                                    # Alternative limit for flushing
        "IDLE_WATERMARK_SEC": 1,    # If no data –from every source– for this long, flush and close without explicit stop

        # Output directories (filenames include a single session timestamp)
        "OUT": {
            "SYNCED_DIR": "data/synced",
            "MARKERS_DIR": "data/markers",
        },

        # k is integer sample index in fs_max grid.
        "PRINT_K": False  # If True include 'k' column in both CSVs; else omit it.
    },

    "ui": {
        "PLOT_ENABLE": True,        # Enable live plotting sink
        "SIGNAL_LINE_WIDTH": 0.75,  # Signal trace line width
        "MARKER_LINE_WIDTH": 3.0,   # Event/spike marker colored line width
        "WINDOW_SEC": 10.0,         # Visible time window in seconds
        "UPDATE_HZ": 30.0,          # Plot refresh rate in Hz
        "SHOW_FPS": True,           # Show FPS overlay
        "FPS_EMA_ALPHA": 0.30,      # Smoothing alpha for FPS overlay EMA
        "PRUNING_MARGIN": 1.10,     # Multiplier for window_sec to define signal and marker pruning cutoff
        "PRUNE_MARKERS_MAX": 10,    # Max number of markers to keep per axis before pruning old ones
        "AA_LINES": True,           # Enable anti-aliasing for plot lines and markers (heavier CPU load but smoother visuals)
        "PLOT_DECIMATE_HZ": 64.0,   # Target plotting rate per channel (decimation to reduce CPU/GPU load); 0=disable
    },

    # +–––––––––––––––––––––––+
    # | Device configurations |
    # +–––––––––––––––––––––––+
    "devices": {
        "demo_rand": {
            "INSTANCES": [
            {
                "ENABLED": True,
                "DEVICE_NAME": "demo_1",
                "FS": 128.0,
                "PLOT_ENABLE": True,
                "EXPORT_ENABLE": True,
                "PARAMS": {
                    "AMP_BASE": 1.0,
                    "AMP_MIN_MULT": 0.5,
                    "AMP_MAX_MULT": 3.0,
                    "AMP_RATE_SCALE": 1.0,
                    "SIGNAL_FREQ_HZ": 2.0,
                    "FREQ_MIN_MULT": 0.5,
                    "FREQ_MAX_MULT": 2.0,
                    "FREQ_RATE_SCALE": 0.25
                },
                "CHANNELS": {
                    "ch_1": True,
                    "ch_2": True
                }
            },
            ]
        },

        "shimmer": {
            "INSTANCES": [
            {
                # GSR + PPG unit
                "ENABLED": False,
                "DEVICE_NAME": "sh_GSR+_5E5C",
                "FS": 128.0,
                "PLOT_ENABLE": True,
                "EXPORT_ENABLE": True,
                "PARAMS": {
                    "COM_PORT": "COM5",
                    "PPG_CHANNEL": "INTERNAL_ADC_13",
                    "PPG_INVERT": True,
                    "VREF_GSR": 3.0,
                    "V_BIAS": 0.5,
                    "VREF_PPG": 3.0,
                },
                "CHANNELS": {
                    # Filtered
                    "gsr_uS": True,
                    "ppg_mV": True,
                    # Raw
                    "RAW_gsr_uS": False,
                    "RAW_ppg_mV": False,
                }
            },
            {
                # EMG unit (EXG)
                "ENABLED": False,
                "DEVICE_NAME": "sh_EXG_8AB8",
                "FS": 512.0,
                "PLOT_ENABLE": True,
                "EXPORT_ENABLE": True,
                "PARAMS": {
                    "COM_PORT": "COM8",
                    "EMG_CHANNELS": [
                        "EXG_ADS1292R_1_CH1_24BIT",     # Change to 16BIT if needed
                        "EXG_ADS1292R_1_CH2_24BIT",     # Change to 16BIT if needed
                    ],
                    "EXG_GAIN": 12.0,
                    "VREF_EXG": 2.42
                },
                "CHANNELS": {
                    # Filtered
                    "emg1_uV": True,
                    "emg2_uV": True,
                    # Raw
                    "RAW_emg1_uV": False,
                    "RAW_emg2_uV": False,
                }
            },
            ],
            "FILTERS": {
                "gsr_uS": {
                    "BANDPASS_ENABLE": True,
                    "BANDPASS_ORDER": 4,
                    "LOW_HZ": 0.05,
                    "HIGH_HZ": 10.0,

                    "NOTCH": 50,        # 0=disable, suggested 50Hz (or 60Hz)
                    "NOTCH_Q": 30.0
                },
                "ppg_mV": {
                    "BANDPASS_ENABLE": True,
                    "BANDPASS_ORDER": 4,
                    "LOW_HZ": 0.5,
                    "HIGH_HZ": 4.0,

                    "NOTCH": 50,        # 0=disable, suggested 50Hz (or 60Hz)
                    "NOTCH_Q": 30.0
                },
                "emg_uV": {
                    "BANDPASS_ENABLE": True,
                    "BANDPASS_ORDER": 4,
                    "LOW_HZ": 20.0,
                    "HIGH_HZ": 200.0,
                    "NOTCH": 50,        # 0=disable, suggested 50Hz (or 60Hz)
                    "NOTCH_Q": 30.0
                },
            },
        },

        "unicorn_lsl": {
            "INSTANCES": [
                {
                    "ENABLED": True,
                    "DEVICE_NAME": "uni_EEG",
                    "FS": 250.0,
                    "PLOT_ENABLE": True,
                    "EXPORT_ENABLE": True,
                    "PARAMS": {
                        "STREAM_NAME": "EEG",         # MATCH THE UNICORN UTILITY
                        "STREAM_TYPE": "EEG",         # Or "Data" if utility publishes a combined stream
                        "RESOLVE_TIMEOUT_S": 5.0,
                        "INLET_CHUNK_LEN": 0,         # 0 = source-controlled chunking
                        "INLET_MAX_BUF_S": 10.0,

                        # Assumes EEG are the first 8 channels in a 17-ch "Data" stream.
                        "EEG_INDEXES": [0, 1, 2, 3, 4, 5, 6, 7],
                    },
                    "CHANNELS": {
                        # Filtered
                        "eeg1_uV": True,  "eeg2_uV": True,  "eeg3_uV": True,  "eeg4_uV": True,
                        "eeg5_uV": True,  "eeg6_uV": True,  "eeg7_uV": True,  "eeg8_uV": True,
                        # Raw
                        "RAW_eeg1_uV": False, "RAW_eeg2_uV": False, "RAW_eeg3_uV": False, "RAW_eeg4_uV": False,
                        "RAW_eeg5_uV": False, "RAW_eeg6_uV": False, "RAW_eeg7_uV": False, "RAW_eeg8_uV": False
                    }
                }
            ],
            "FILTERS": {
                "eeg_uV": {
                    "BANDPASS_ENABLE": True,
                    "BANDPASS_ORDER": 4,
                    "LOW_HZ": 1.0,
                    "HIGH_HZ": 40.0,
                    "NOTCH": 50,
                    "NOTCH_Q": 30.0
                }
            }
        },

        "device_template": {
            "INSTANCES": [
                {
                    "ENABLED": False,                 # Keep disabled by default
                    "DEVICE_NAME": "tpl_1",           # Instance name used in outputs
                    "FS": 128.0,                      # Nominal cadence (optional)
                    "PLOT_ENABLE": True,              # Plot routing hint (if used)
                    "EXPORT_ENABLE": True,            # Export routing hint (if used)
                    "PARAMS": {
                        # Add device-specific parameters here (COM port, IP, etc.)
                    },
                    "CHANNELS": {
                        # Two skeletal channels to illustrate the mapping.
                        # Enable/disable toggles which channels are routed out.
                        "tpl_ch1": True,
                        "tpl_ch2": True
                    }
                }
            ],
            "FILTERS": {
                # Example filter family for template values (not applied here).
                # Downstream handlers can read this to design bandpass + notch.
                "tpl_val": {
                    "BANDPASS_ENABLE": False,     # Enable when designing filters
                    "BANDPASS_ORDER": 4,          # Even order for SOS preferred
                    "LOW_HZ": 0.5,                # Example low cutoff
                    "HIGH_HZ": 30.0,              # Example high cutoff

                    "NOTCH": 50,                  # 0=disable; 50 or 60 typical
                    "NOTCH_Q": 30.0               # Typical quality factor
                }
            }
        },
    },

    # +–––––––––––––––––––––––––––––––––+
    # | Marker generators configuration |
    # +–––––––––––––––––––––––––––––––––+
    "marker_generators": {
        "event_demo": {
            "INSTANCES": [
                {
                    "GENERATOR_NAME": "event_demo_1",
                    "ENABLED": True,
                    "DELAY_RANGE_SEC": (2.0, 2.5)
                }
            ]
        },

        "spike_demo": {
            "INSTANCES": [
                {
                    "GENERATOR_NAME": "spike_demo_1",
                    "ENABLED": True,
                    "DELAY_RANGE_SEC": (3.0, 3.5)
                }
            ]
        },
    },

    # +–––––––––––––––––––––––––+
    # | Telemetry configuration |
    # +–––––––––––––––––––––––––+
    "telemetry": {
        "WINDOW_S": 10.0
    }
}


# ====== LOAD USER SETTINGS (REQUIRED) ======
try:
    # Import user-provided SETTINGS dict from project root.
    from settings import SETTINGS as _USER_SETTINGS  # type: ignore
except Exception as e:
    # Fail fast if settings.py cannot be imported.
    logger.error("Failed to import settings.py or SETTINGS: %s", e)  # Log import error
    raise SystemExit(1)  # Terminate immediately

# Validate that SETTINGS exists and is a dict.
if not isinstance(_USER_SETTINGS, dict):
    logger.error("Invalid SETTINGS: expected dict, got %s", type(_USER_SETTINGS).__name__)
    raise SystemExit(1)  # Terminate immediately

# ====== MERGE (SETTINGS OVERRIDE DEFAULTS) ======
# Simple recursive merge: dict keys in SETTINGS override defaults; missing keys
# fall back to defaults. Non-dict nodes are replaced as-is.

# --- Replace-only merge for keymaps; recursive merge for the rest ---
def _merge(defaults: Any, overrides: Any, *, _key: str | None = None) -> Any:
    """Recursively merge overrides into defaults with special-casing for keymaps.

    - EVENT_KEYMAP and SPIKE_KEYMAP are treated as atomic params: override replaces.
    - Other dict nodes are merged recursively; missing keys fall back to defaults.
    - Non-dict nodes: override wins if provided, else default.
    """
    # Atomic replacement for keymaps if an override dict is provided
    if _key in ("EVENT_KEYMAP", "SPIKE_KEYMAP") and isinstance(overrides, dict):
        return overrides  # Fully replace default keymap

    # Primitive types or mismatched structures: prefer override if not None
    if not isinstance(defaults, dict) or not isinstance(overrides, dict):
        return overrides if overrides is not None else defaults

    out: Dict[str, Any] = {}
    # Union of keys: keep any extras the user may add
    for k in set(defaults.keys()) | set(overrides.keys()):
        dv = defaults.get(k)
        has_ov = k in overrides
        ov = overrides.get(k) if has_ov else None

        if isinstance(dv, dict) and isinstance(ov, dict):
            # Recurse and pass current key to detect keymap nodes
            out[k] = _merge(dv, ov, _key=k)
        elif has_ov:
            out[k] = ov  # Override wins (can be None by design)
        else:
            out[k] = dv  # Fallback to default
    return out

# Build final CONFIG by overlaying settings on the defaults.
CONFIG: Dict[str, Any] = _merge(_DEFAULT_CONFIG, _USER_SETTINGS)

# Optional: brief debug to confirm merge complete.
logger.debug("CONFIG loaded with user overrides from settings.py")
