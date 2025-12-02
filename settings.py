# utils/config.py
# User configuration dictionary.

SETTINGS = {
    "system": {
        "CHECK_DEPENCENCIES": True,  # Enable dependency check at startup
    },
    

    # +–––––––––––––––––––––––––––––+
    # | Event & Spike configuration |
    # +–––––––––––––––––––––––––––––+

    "events": {
        "ENABLED": True,
        "EVENT_KEYMAP": {
            "0": "REST",    # First is DEFAULT event
            "7": "TASK_7",
            "8": "TASK_8",
            "9": "TASK_9"
        }
    },

    "spikes": {
        "ENABLED": True,
        "SPIKE_KEYMAP": {
            "q": "SPIKE_Q",
            "w": "SPIKE_W",
            "e": "SPIKE_E"
        }
    },


    # +––––––––––––––––––––––––––––––––––––––––––––––––––––––––+
    # | Data export, synchronizer, and Live Plot configuration |
    # +––––––––––––––––––––––––––––––––––––––––––––––––––––––––+

    "export": {
        "EXPORT_ENABLE": True,      # Global export enable/disable switch
        "CSV_SIGNAL_ENABLE": True,  # Flag to enable/disable signal csv creation
        "CSV_MARKER_ENABLE": True,  # Flag to enable/disable marker csv creation

        # Output directories (filenames include a single session timestamp)
        "OUT": {
            "SYNCED_DIR": "data/synced",
            "MARKERS_DIR": "data/markers",
        },
    },

    "ui": {  # Section: live plot / UI behavior
        "PLOT_ENABLE": True,        # Enable live plotting sink
        "SIGNAL_LINE_WIDTH": 0.75,  # Signal trace line width
        "MARKER_LINE_WIDTH": 3.0,   # Event/spike marker colored line width
        "WINDOW_SEC": 10.0,         # Visible time window in seconds
        "UPDATE_HZ": 30.0,          # Plot refresh rate in Hz
        "SHOW_FPS": True,           # Show FPS overlay
        "AA_LINES": False,          # Enable anti-aliasing for plot lines and markers (heavier CPU load but smoother visuals)
        "PLOT_DECIMATE_HZ": 50.0,   # Target plotting rate per channel (decimation to reduce CPU/GPU load); 0=disable
    },


    # +–––––––––––––––––––––––+
    # | Device configurations |
    # +–––––––––––––––––––––––+
    "devices": {
        "demo_rand": {
            "INSTANCES": [
            {
                "ENABLED": False,
                "DEVICE_NAME": "demo_1",
                "FS": 128.0,
                "PLOT_ENABLE": True,
                "EXPORT_ENABLE": True,
                "PARAMS": {
                    "AMP_BASE": 2.0,
                    "SIGNAL_FREQ_HZ": 1.5,
                },
                "CHANNELS": {
                    "ch_1": True,
                    "ch_2": True
                }
            },
            {
                "ENABLED": False,
                "DEVICE_NAME": "demo_2",
                "FS": 250.0,
                "PLOT_ENABLE": True,
                "EXPORT_ENABLE": True,
                "PARAMS": {
                    "AMP_BASE": 2.0,
                    "SIGNAL_FREQ_HZ": 1.5,
                },
                "CHANNELS": {
                    "ch_1": True,
                    "ch_2": True
                }
            }
            ]
        },

        "shimmer": {
            "INSTANCES": [
            {
                # GSR + PPG unit
                "ENABLED": True,
                "DEVICE_NAME": "sh_GSR+_5E5C",
                "FS": 128.0,
                "PLOT_ENABLE": False,
                "EXPORT_ENABLE": True,
                "PARAMS": {
                    "COM_PORT": "COM42",
                    "PPG_CHANNEL": "INTERNAL_ADC_13",
                },
                "CHANNELS": {
                    "gsr_uS": True,
                    "ppg_mV": True,
                }
            },
            {
                # EMG unit (EXG)
                "ENABLED": True,
                "DEVICE_NAME": "sh_EXG_8AB8",
                "FS": 512.0,
                "PLOT_ENABLE": True,
                "EXPORT_ENABLE": True,
                "PARAMS": {
                    "COM_PORT": "COM36",
                    "EMG_CHANNELS": [
                        "EXG_ADS1292R_1_CH1_24BIT",     # Change to 16BIT if needed
                        "EXG_ADS1292R_1_CH2_24BIT",     # Change to 16BIT if needed
                    ],
                },
                "CHANNELS": {
                    "emg1_uV": True,
                    "emg2_uV": True,
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
                },
                "ppg_mV": {
                    "BANDPASS_ENABLE": True,
                    "BANDPASS_ORDER": 4,
                    "LOW_HZ": 0.5,
                    "HIGH_HZ": 4.0,
                    "NOTCH": 50,        # 0=disable, suggested 50Hz (or 60Hz)
                },
                "emg_uV": {
                    "BANDPASS_ENABLE": True,
                    "BANDPASS_ORDER": 4,
                    "LOW_HZ": 20.0,
                    "HIGH_HZ": 200.0,
                    "NOTCH": 50,        # 0=disable, suggested 50Hz (or 60Hz)
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
                        # Assumes EEG are the first 8 channels in a 17-ch "Data" stream.
                        "EEG_INDEXES": [0, 1, 2, 3, 4, 5, 6, 7],
                    },
                    "CHANNELS": {
                        "eeg1_uV": True,  "eeg2_uV": True,  "eeg3_uV": True,  "eeg4_uV": True,
                        "eeg5_uV": True,  "eeg6_uV": True,  "eeg7_uV": True,  "eeg8_uV": True,
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
                }
            }
        }
    },


    # +–––––––––––––––––––––––––––––––––+
    # | Marker generators configuration |
    # +–––––––––––––––––––––––––––––––––+

    "marker_generators": {
        "event_demo": {
            "INSTANCES": [
                {
                    "GENERATOR_NAME": "event_demo_1",
                    "ENABLED": False,
                }
            ]
        },

        "spike_demo": {
            "INSTANCES": [
                {
                    "GENERATOR_NAME": "spike_demo_1",
                    "ENABLED": False,
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