# TECHNICAL_DOC.md

This document provides a technical description of the project, detailing its architecture, modules, algorithms, and data flow.  
It is intended for developers, maintainers, and integrators who need to understand or extend the system beyond standard usage.


## 1. System Overview
This project present a real-time signal acquisition tool that can listen to multiple devices, align the time series on a shared clock, and feed the results to live plots or background exports without interrupting ongoing acquisition. At startup, the system reads its configuration, launches the selected device managers, and keeps them running in their own threads so that physical sensors, LSL streams, and synthetic sources can coexist smoothly.

Every producer adds to the internal queue timestamped samples into a central synchronizer that keeps a steady time grid, handles drifts by anchoring each device clock, and turns incoming data into tidy frames that downstream sinks can consume. Optional marker generators inject events (sticky markers) and spikes (instant markers) into the same flow, letting experiments record both continuous signals and annotations with consistent timing.
The plot sink redraws a rolling window at a decimated rate for responsiveness, while the exporter batches synchronizer rows asynchronously and flushes them according to configuration.

The codebase is laid out by responsibility:
- Acquisition modules for device-specific adapters and handlers,
- Processing for synchronization and filtering
- Export for persistence
- Visualization for the UI loop
- Utils for configuration, helpers, and logging


New devices follow the existing templates: add a threaded manager that emits channel/value pairs, register it in the configuration, and the main entry point will integrate it automatically.

The technical documentation aims to guide developers through this layered pipeline, explain how configuration toggles shape runtime behavior, and show the extension points that make the platform adaptable to new hardware, markers, or output formats.

---

## 2. Project Structure
```python
stage_project/
│
├── acquisition/
│   ├── device_template.py    # Skeleton device module for new implementation
│   ├── demo_rand.py          # Random signal generator for testing
│   ├── event_demo.py         # Demo module generating test events
│   ├── spike_demo.py         # Demo module generating test spikes
│   ├── shimmer_device.py     # Device manager for Shimmer sensors
│   ├── shimmer_timebase.py   # Shimmer ticks into seconds with rollover
│   ├── unicorn_lsl.py        # EEG acquisition via LSL
│   ├── unicorn_lsl_timebase.py # Removes LSL timestamp jitter
│   └── handlers/             # Channel-specific handlers
│       ├── handler_shimmer_gsr.py
│       ├── handler_shimmer_ppg.py
│       └── handler_shimmer_emg.py
│
├── processing/
│   ├── sync_controller.py    # Core synchronizer and time quantization
│   ├── rt_filter.py          # Real-time filtering utilities
│   ├── events.py             # Event bus and management
│   └── spikes.py             # Spike bus and management
│
├── export/
│   └── export_sink.py        # Asynchronous CSV writer
│
├── visualization/
│   └── plot_sink.py          # Live plot and marker recognition
│
├── utils/
│   ├── config.py             # Central extended configuration file
│   ├── logger.py             # Unified logger
│   ├── helpers.py            # Runtime helpers
│   └── dependencies.py       # Package verification and auto-install
│
├── logs/                     # Logs folder
│
├── data/
│   ├── markers/              # Marker CSVs folder
│   └── synced/               # Synchronized data CSVs folder
│
├── settings.py               # Central user simplified configuration file
├── main.py                   # Project entry point
└── requirements.txt          # Python dependencies
```

---

## 3. Core Architecture

## 3.1 Acquisition Layer
Handles communication with real or simulated devices. Each module starts a **producer thread** that streams timestamped samples to the synchronizer.

Each producer manages its own sampling rate and timestamp, emitting `(device_timestamp, device_name, channel_pairs)` in which `channel_pairs` consists of a set of tuples `(channel_name, value)`.

> Filtering is performed through `rt_filter.py` inside the producer (or channel handler) module.

### 3.1.1 Demo generators
#### `demo_rand.py`
Provides a lightweight synthetic signal source used mostly for demos. A `DemoRandManager` reads instance settings, enables the requested channels, and starts a background `_SineWaveThread`. This background thread keeps precise pacing, modulates amplitude and frequency within configured bounds, and pushes `(ts, device_name, channel_pairs)` tuples straight into `processing.sync_controller.sync_manager`. The only external dependencies are the shared logger and the sync manager; no other modules rely on it.

#### `event_demo.py` and `spike_demo.py`
Run a lightweight `_PeriodicEventRunner` (or `_PeriodicSpikeRunner`) thread that periodically calls `SYNC.set_event` (or `SYNC.trigger_spike`) with random labels pulled from `settings.py` (fallback to `config.py`) keymaps. It uses a configurable delay range for cadence.

### 3.1.2 Shimmer

#### `shimmer_device.py`
Encapsulates the Shimmer BLE pipeline: ShimmerManager reads per-instance config, finds which signal families are enabled, resets the device timebase, wires the appropriate handler factories, then opens pyserial/pyshimmer streams and routes handler outputs into `processing.sync_controller.sync_manager`. It also owns teardown (callback removal, stop/shutdown guard thread, serial close) so producers can be stopped cleanly from the main session controller.

#### `shimmer_timebase.py`
Keeps a device-specific, lock-protected counter state so all Shimmer handlers share drift-free timestamps. It anchors on the first tick, accumulates 16-bit rollovers, and exposes `device_time_s` for seconds conversion; reset is called by ShimmerManager before streaming so each device instance gets its own clock origin.

### Shimmer Handlers

`handler_shimmer_gsr.py`, `handler_shimmer_ppg.py`, and `handler_shimmer_emg.py` share a **factory** pattern: `build_<sensor_type>_handler` which reads instance-level electrical parameters, builds a **StreamingSOS filter chain** via `processing.rt_filter`, tracks **telemetry** for invalid samples, and returns a closure that emits **`(channel, value|None)`** pairs for the manager’s unified callback.  
All of them consume `shimmer_timebase.device_time_s`, honor stop events, and only touch SYNC through the manager.

`build_gsr_handler`:
- Reads the packed Shimmer GSR word (16 bits total: top 2 bits = range, lower 14 bits = ADC value)
- Decodes the range code (0–3) to the correct feedback resistor according to the Shimmer hardware map
- Converts the ADC code to input voltage using the reference voltage (`VREF_GSR`)
- Computes skin resistance using the equation *R_skin = R_feedback × (V_bias / V_in – 1)*
- Converts resistance to conductance (µS = 1e6 / R_skin) and rejects invalid samples when *V_in ≥ V_bias*
- Applies the real-time filter chain (`StreamingSOS`) only to valid conductance samples
- Tracks telemetry over a configurable window (`telemetry.WINDOW_S`), warning if invalid or missing samples occur
- Feeds both raw and filtered streams, logging once if the configured enum is absent

`build_ppg_handler`:
- Reads the configured ADC channel (default `INTERNAL_ADC_13`)
- Converts the 14-bit ADC code (0–16383) to millivolts using the reference voltage (`VREF_PPG`)
- Optionally inverts polarity when `PPG_INVERT=True` to match optical sensor orientation
- Applies the real-time filter chain (`StreamingSOS`) on valid samples only
- Marks invalid or missing samples as `None` while maintaining timestamp continuity
- Tracks telemetry across `telemetry.WINDOW_S` windows, warning when invalid data occurs
- Feeds both raw (`RAW_ppg_mV`) and filtered (`ppg_mV`) streams, logging once if the channel enum is missing

`build_emg_handler`:
- Reads up to two ADS1292R channels defined in configuration (`EMG_CHANNELS`)
- Converts signed ADC counts to microvolts using `(counts / full_scale) × (VREF_EXG / EXG_GAIN) × 1e6`
- Handles both 16-bit and 24-bit formats automatically by checking the enum suffix
- Applies an independent `StreamingSOS` filter chain for each physical channel
- Flags missing samples and invalid conversions through per-channel telemetry
- Emits both raw (`RAW_emgN_uV`) and filtered (`emgN_uV`) series based on configuration flags
- Logs once when channels are missing or misconfigured


### 3.1.3 Unicorn EEG
`unicorn_lsl.py` wraps the full LSL intake.  

`UnicornManager` responsibilities:
- Resolve the correct pylsl stream (preferring the newest matches by name/type)
- Validate channel layout and sample rate
- Opens a StreamInlet and runs a reader thread that keeps consuming chunks
- Selects the configured EEG column indexes
- Routes raw values into optional StreamingSOS filters
- Handles telemetry for invalid samples, manages the inlet lifecycle
- Uses a per-device `UnicornLSLTimebase` so downstream consumers get **jitter-free timestamps**.
- Forwards channel/value tuples to `processing.sync_controller.sync_manager`

Unicorn Timebase

`unicorn_lsl_timebase.py` supplies a deterministic clock. `UnicornLSLTimebase` anchors on the first LSL stamp then produces evenly spaced ticks at 1/fs, with an optional soft realignment after long gaps. `UnicornManager` primes it when the first chunk arrives, queries `next_tick` for every packet, and resets it on stop, which isolates timestamp logic from the reader loop and keeps jitter correction centralized.


## 3.2 Processing Layer
Performs time quantization, synchronization, marker handling, and sends data to export and plot sinks.

### 3.2.1 Marker buses

`events.py` and `spikes.py` modules implement the marker buses that the rest of the pipeline subscribes to. Both wrap their state in small classes (`EventBus`, `SpikeBus`) that own the trigger keymaps, expose `subscribe` so sinks or controllers can attach callbacks, and broadcast changes. Each bus pulls its enable flags and keymaps from `utils.config.CONFIG`, so runtime configuration decides whether keyboard/API triggers are recognized and which labels they emit.

The two buses share the same pattern for API parity but differ in semantics.  
- `EventBus` keeps a sticky state: it initializes with a default label, stores the current event plus the monotonic timestamp of the last change, and toggles back to that default when the same key is pressed again. When `set_event` is called, it compares against the current value, updates the sticky state under lock, and notifies subscribers with `(ts, new_event, prev_event, source)` so downstream consumers can update UI overlays or persistent exports. It also offers `announce_change_at` for quantized timestamps coming back from the synchronizer without touching the sticky state.

- `SpikeBus` mirrors the interface but deliberately remains stateless: every trigger is a one-shot notification with no toggle behavior. `set_spike` timestamps the spike with `time.monotonic()` and broadcasts `(ts, label, source)` immediately, while `announce_at` allows the synchronizer to replay spikes at an externally supplied time.

Both buses respect `ENABLE_*_TRIGGERS`, warn when disabled triggers are invoked, and log once when a key is unmapped, ensuring misconfigurations show up quickly without crashing the producer threads.

### 3.2.2 Filtering

`rt_filter.py` centralizes all realtime filtering for the pipeline. It exposes two pieces that the acquisition layer composes:
- `design_sos` is a stateless factory that reads a config spec (band-pass enable/order, notch frequency/Q) and returns a list of SciPy second-order sections
- `StreamingSOS` wraps those stages with per-instance state (zi buffers) so producers can feed one sample at a time without building their own signal-processing loops.

The design path starts with `_parse_spec`, which normalizes and validates the raw config: it checks band edges against Nyquist, clamps notch options to 50/60 Hz, and logs a warning when the spec would generate unstable filters. That validated tuple of primitives drives `_design_sos_cached`, an lru_cached function (remembers the results of recent calls) keyed by (sensor_key, fs, bp params, notch params). The cache keeps the same topology shared across devices so handlers only pay the SciPy **design cost once per configuration**.  
When enabled, a notch stage is created via `signal.iirnotch` and a band-pass via `signal.butter(..., output="sos")`, both converted to `tf2sos` as immutable arrays; an empty tuple denotes an identity filter.

`StreamingSOS` then clones those SOS arrays into a per-device structure: on construction the class allocates zeroed zi arrays (`signal.sosfilt_zi(sos) * 0.0`) for each stage and logs the context tag (typically `device:channel`, set by handlers) so trace logs stay readable. `apply` accepts a single scalar, short-circuits NaNs to keep missing samples intact, and runs the value through each stage using `signal.sosfilt`, updating the corresponding zi slice after every call. If SciPy raises, the component logs and falls back to pass-through so acquisition threads never crash; `reset` reinitializes state when a device reconnects or a session restarts.

All device-specific handlers (Shimmer GSR/PPG/EMG and Unicorn EEG) call `design_sos` with their own sensor key and sampling rate, stash the returned chain, and wrap it in `StreamingSOS` to maintain continuity. Because the filter recipe is cached once and each device keeps its own internal state, multiple devices can share the same filter definition without ever sharing samples. That keeps different acquisition threads independent even though they rely on identical filter settings.

### 3.3.3 Synchronizer

`sync_controller.py` is the **coordination hub that turns a set of asynchronous producers into a single, quantized event stream for exporters and live plots**. At construction, the `SyncManager` sets up the ingestion queue (optionally bounded with drop-oldest semantics), keeps per-session timing state, tracks sticky events, and records separate sink lists for full-rate consumers and decimated plot feeds.

When `start_session(delta)` is called from `main`, the manager:
- Reads the default event label from CONFIG
- Anchors its host-relative clock with `time.monotonic`
- Resets device anchors
- Calculates how many decimals it should retain when formatting quantized timestamps
- Reads `ui.PLOT_DECIMATE_HZ` from `CONFIG` to configure plot decimation before launching a daemon consumer thread that drains the internal queue.

`stop_session()` flips a stop flag and joins the consumer so that acquisition threads can be shut down cleanly before clearing sink registrations.

Producers call `enqueue_packet(device_ts, device_name, channel_pairs)` to **push raw device timestamps with their channel/value tuples**; if the queue is bounded and full, the manager drops the oldest payload first to avoid blocking.

For keyboard/API markers, it offers `set_event` and `trigger_spike`, which quantize the “now” timestamp, apply event-toggle rules, and forward tagged payloads through the same sink mechanism.

The consumer loop `_consume_loop`:
- Pulls packets
- Validates each dequeued item with the expected structure
- Hands them to `_handle_sample_packet`.

`_handle_sample_packet` maps each device timestamp into session-relative host time using `_map_to_host`, which instantiates `DeviceAnchor` on first sighting and re-anchors if a backward jump is detected (incrementing an epoch counter so logs show resets).  
Once mapped, `_quantize` rounds to the nearest time slot on the fixed grid, floors the result for stable formatting, and the manager emits a "sample" payload with a quantized timestamp (and an optional "k" grid index).

All outgoing payloads pass through `_emit_to_sinks`, which fans them out to registered queues and, when plot decimation is configured, calls `_emit_to_plot_sinks`. Plot decimation keeps one sample per device-channel per time bin, while events and spikes bypass decimation and always reach plots in real time.

Helper functions `_decimals_from_delta` and `_floor_to_decimals` choose a consistent timestamp precision based on the grid size.

At the very end, the module exposes a singleton `sync_manager` so acquisition modules can import and use it without manual wiring.

In short, producers just drop well‑formed tuples into a queue without grabbing locks, the synchronizer keeps track of each device’s clock so drift or resets get absorbed, and everything that listens downstream receives data on the same evenly spaced timeline.

## 3.3 Export Layer

The sink provides the writer end of the pipeline, translating the quantized packets into a wide CSV plus a markers sidecar under the export configuration.

`export_sink.py` implements `ExportSink`, the CSV writer that consumes packets from `SyncManager`. When instantiated:
- It sets up an internal queue
- Resolves lookahead (how many future timesteps it should wait before finalizing rows)
- Flush cadence
- Idle watermark from the export config (so partially filled rows don’t sit in memory indefinitely)
- Builds the output paths (`data/synced` and `data/markers`)
- Freezes the channel schema based on the `known_channels` provided by `main`
- Tracks sticky events
- Per-k row buffers
- Flush bookkeeping so the exporter can commit rows in order even when data arrives slightly late   

`start()` manages the following operations:
- Opens the optional signal and marker CSVs (honoring the enable flags)
- Writes headers `(k, t_q, channel columns, spike, event)` (k is optional)
- Logs the chosen output paths
- Launches a daemon thread that runs `_run`.

`_run` loop manages the following operations:
- Drains the queue with timeout waits to trigger periodic flushes
- Dispatches packets to `_on_sample`, `_on_event`, or `_on_spike`
- Commits rows up to `k_seen_max - lookahead` so late packets still merge into the correct frame.  

> Idle detection (`IDLE_WATERMARK_SEC`) forces a final commit when the stream is quiet, and `FLUSH_PERIOD_SEC/FLUSH_ROWS` determine when buffers hit disk.

Samples populate `_open_rows` keyed by k, keeping only channels listed in the header, while events update the sticky event map and write marker rows immediately, and spikes mark the current row with `spike=<label>`.  

`_commit_until`:
- Writes rows in order
- Applies any pending event changes
- Emits the initial default event once
- Clears buffers as rows are flushed to CSV.  

`stop()` signals the thread, flushes remaining data, and closes file handles.  

## 3.4 Visualization Layer
`plot_sink.py` provides the live `Matplotlib` watcher that subscribes to `SyncManager`.  

`PlotSink` pulls its timing and UI defaults from `CONFIG`, builds a whitelist of device instances with `PLOT_ENABLE=True`, and prepares per-series buffers keyed by `device_channel`. It accepts quantized packets through a 4096-slot queue, honors the same event/spike keymaps as the rest of the system, and launches a `Matplotlib` figure with one subplot per series, an Alt+Q shutdown hint, and optional FPS overlay.

When `run()` is called, the sink disables clashing Matplotlib shortcuts (so keys can always trigger markers), wires a debounced keyboard handler to call `SYNC.set_event`/`SYNC.trigger_spike`, and starts a canvas timer to refresh at the configured rate. Each `_on_timer` tick drains pending packets: sample data is appended to per-series time/value deques after filtering by the device whitelist; newly seen channels cause the layout to rebuild; events and spikes accumulate in marker queues and update the always-visible text overlay.

The sink also manages:
- The x‑axis aligned with SYNC’s quantized clock, sliding a fixed window across time;
- Re-slices buffers on every frame, applies autoscale on Y
- Draws new markers as colored `axvline` overlays.
- Provides an EMA-based FPS counter measuring true redraw speed.

Marker deques are pruned based on a configurable margin so old annotations drop off gracefully.

> The sink only reads from the shared queue and avoids heavy work inside the timer.

## 3.5 Utilities

> `config.py` (and `settings.py`) will be explained later on.

### 3.5.1 Dependency check
`dependencies.py` houses `ensure_requirements`, an optional startup hook that the main script can call when `CHECK_DEPENCENCIES` flag is enabled. It opens `requirements.txt` beside the repository root, and for each spec attempts an `importlib.import_module` under the expected module name (with overrides like pyserial → serial). Missing modules trigger an in-process pip install. The function logs progress, stops the process if the requirements file is missing, and reports which packages were installed versus already present.

### 3.5.2 Logging
`logger.py` centralizes logging so every module pulls from the same session log file. The module keeps global state for the active log filename, a shared `FileHandler`, and a shared `StreamHandler`, protecting setup with `_INIT_LOCK` so multiple threads can request loggers safely.  
`_ensure_file_handler` lazily creates the `logs/` folder if needed, stamps a `log_<timestamp>.log` name the first time it’s called, configures a common formatter, and reuses that handler for every logger.  
`_ensure_stream_handler` builds a single console handler at level ERROR so only high-severity messages hit stderr, again using the same formatter.

`get_logger(name)` pulls or creates a `logging.Logger`, sets it to INFO, disables propagation to avoid duplicate messages, and attaches the shared file and console handlers if none are present yet.


### 3.5.3 Helper utilities used in `main.py`.
- `compute_fs_max_from_config(config)` scans `config["devices"]`, collects the sampling rates (FS) of enabled instances, logs a summary, and returns the maximum frequency (fallback to 250 Hz if nothing valid is found).
- `collect_known_channels_from_config(config)` builds a deduplicated list of device:channel strings for enabled instances with `EXPORT_ENABLE=True`, while counting empty channel sets and duplicates (so it can warn as needed).
- `iter_enabled_instances(config)` is a convenience generator yielding `(device_type, instance_dict)` for every enabled instance, letting main drive startup with a simple loop.  

#### For runtime control:
- `STOP_EVT` is a module-level `threading.Event` used as the global stop flag.
- `setup_signal_handlers()` registers `_term_handler` for `SIGINT` (and `SIGTERM` where available) so _Ctrl‑C_ or kill requests close any active Matplotlib UI and set `STOP_EVT`, letting producers exit cooperatively.
- `wait_for_producers(producers)` polls the producer list with short `join()` timeouts, exits when they’re done, and wakes early if `STOP_EVT` is set, ensuring the main thread doesn’t hang on blocking joins.

## 3.6 Project's entry point: main
`main.py` is the orchestrator that turns configuration into a running session. At startup, it grabs the shared logger, installs signal handlers so Ctrl+C or SIGTERM set the global `STOP_EVT`, and optionally runs `ensure_requirements` if `CHECK_DEPENCENCIES` flag is set to true. It calculates the synchronizer grid step as `delta = 1/fs_max` using `compute_fs_max_from_config`, then calls `SYNC.start_session(delta)` to launch the central synchronizer.

Output sinks are attached next: if plotting is enabled, it instantiates `PlotSink` and registers its queue via `SYNC.add_plot_sink_queue`.
It collects exportable channels, and if `EXPORT_ENABLE` is true, it spins up `ExportSink`, starts its thread, and wires its queue into `SYNC.add_sink_queue`. Missing channels trigger a warning but don’t stop the session, allowing plot-only runs.

Device producers are enumerated with `iter_enabled_instances`. For each enabled instance, the code switches on the `typ`, constructs the corresponding manager (`ShimmerManager`, `DemoRandManager`, `UnicornManager`, or `template`), starts the stream, and tracks the manager in a producers list for later teardown. Any errors during startup are logged and abort the session with exit code 2, so misconfigured devices fail fast. It also looks at `marker_generators` config to launch optional event and spike demo threads.

Finally, the script blocks either in `plot_sink.run()` (for interactive sessions) or `wait_for_producers` (when no UI is active). On shutdown (graceful exit or exception), it iterates over producers calling `stop()`, stops the synchronizer, and asks the exporter to flush/close. This ordering ensures the packet flow quiesces before the CSV sink commits and the program terminates cleanly.

---

## 4. Configuration System
`config.py` is a centralized configuration dictionary: it defines `_DEFAULT_CONFIG`, the safety net of project-wide settings with all runtime parameters defined.  
> Missing or invalid keys are automatically handled through fallback logic in each module.


`settings.py` itself simply populates `SETTINGS` (ordinary Python dict) to tweak defaults. It enables the dependency check, defines custom keymaps, adjusts export/UI flags, and activates specific Shimmer/Unicorn instances with their ports, channel sets, and filter specs. Because `CONFIG = _merge(...)`, these overrides take effect automatically everywhere the code imports `CONFIG`.

All of these defaults live in `_DEFAULT_CONFIG`, and `settings.py` defines a `SETTINGS` dictionary with project-specific overrides. The recursive merge in `utils/config.py` overlays `SETTINGS` onto the defaults (with keymaps replaced wholesale), so any value you set in `settings.py` automatically wins while unspecified nodes keep their documented defaults.


### Overview:
- System parameters
- Event configuration
- Spike configuration
- Export parameters
- UI parameters (*consist of a live plot at the current stage*)
- Devices
    - Demo random signal generator
    - Shimmer devices
        - Instances & relative channels
        - Filtering logic
    - Unicorn EEG device
        - Filtering logic
    - Template device
        - Instances & relative channels
        - Filtering logic
- Demo marker generators
- Telemetry parameters

*Merge logic at the end*

### In-depth description 

#### System and telemetry

- `system.CHECK_DEPENCENCIES`: run `ensure_requirements` on startup so missing pip packages are installed before acquisition.
- `telemetry.WINDOW_S`: size of the rolling window used by handlers and sinks before they log counts of invalid samples.

#### Event/Spike Layer
- `EVENT_KEYMAP`: label mapping for sticky events; the first value is the default event injected at session start.
- `SPIKE_KEYMAP`: keyboard → label mapping for instantaneous spikes.

#### Sync / Export Layer
- `EXPORT_ENABLE`: global gate for the CSV sink.
- `CSV_SIGNAL_ENABLE` / `CSV_MARKER_ENABLE`: turn signal or marker CSVs on/off independently.
- `LOOKAHEAD_SEC`: how far ahead the exporter waits (in seconds) so slightly late packets land in the right row.
- `FLUSH_PERIOD_SEC`: wall-clock interval after which buffered rows are written to disk.
- `FLUSH_ROWS`: maximum row count to buffer before forcing a flush; 0 or negative means *min(2048, max(64, round(fs_max * FLUSH_PERIOD_SEC)))*
- `IDLE_WATERMARK_SEC`: if no packets arrive for this long from any source, commit everything seen so far and flush, preventing half-filled CSVs.
- `OUT.SYNCED_DIR` / `OUT.MARKERS_DIR`: where signal and marker CSVs are stored.
- `PRINT_K`: include the quantized frame index k as the first CSV column.

#### UI / Plot Layer
- `WINDOW_SEC`: X-axis width of the rolling plot window.
- `UPDATE_HZ`: target refresh rate for the plot timer.
- `PLOT_DECIMATE_HZ`: max rate per channel forwarded to plots (0 disables decimation).
- `SIGNAL_LINE_WIDTH`, `MARKER_LINE_WIDTH`, `AA_LINES`: aesthetics for traces and marker strokes.
- `PRUNING_MARGIN`: extra time margin (multiplier) kept beyond the visible window before old samples/markers are dropped.
- `PRUNE_MARKERS_MAX`: hard cap of stored markers per axis to avoid unbounded growth.
- `SHOW_FPS`, `FPS_EMA_ALPHA`: enable the FPS overlay and control its smoothing.

#### Device Blocks

- #### Demo Rand
   - `demo_rand.INSTANCES[].PARAMS.*`: amplitude/frequency modulation knobs for the synthetic sine generator plus per-channel enable flags.

- #### Shimmer

   - `INSTANCES[].PARAMS.COM_PORT`: serial port for each Shimmer unit.
   - `PARAMS.PPG_CHANNEL` / `PARAMS.PPG_INVERT`: ADC source and polarity for PPG.
   - `PARAMS.VREF_*`, `PARAMS.V_BIAS`, `PARAMS.EXG_GAIN`: electrical constants used to convert raw counts to physical units.
   - `PARAMS.EMG_CHANNELS`: ADS1292R channel enums to read for EMG.
   - Channel toggles (`gsr_uS`, `RAW_gsr_uS`, etc.) route corresponding streams into sync.
   - `FILTERS.gsr_uS` / `FILTERS.ppg_mV` / `FILTERS.emg_uV`: band-pass and notch specs consumed by the handlers to build their real-time filters.

- #### Unicorn LSL
   - `PARAMS.STREAM_NAME` / `PARAMS.STREAM_TYPE`: LSL identifiers used to find the correct stream.
   - `PARAMS.RESOLVE_TIMEOUT_S`: how long to wait when resolving LSL.
   - `PARAMS.INLET_CHUNK_LEN`, `INLET_MAX_BUF_S`: chunking/buffer hints for `pylsl.StreamInlet`.
   - `PARAMS.EEG_INDEXES`: zero-based columns in the LSL stream that correspond to the eight EEG channels.
   - Channel toggles (`eegN_uV`, `RAW_eegN_uV`) route corresponding streams into sync.
   - `FILTERS.eeg_uV`: shared filter definition applied per EEG channel.

- #### Device Template
   Demonstrates the same pattern: `PARAMS` placeholder for custom hardware settings, `CHANNELS` as a minimal schema, and example `FILTERS.tpl_val` spec for consumers that want to reuse the filter pipeline.

#### Demo marker generators
- `event_demo/spike_demo.INSTANCES[]` enable the demo threads and control their cadence via `DELAY_RANGE_SEC`; they only run when `ENABLED` is true.  

---

## 5. Data Flow
```
Device Threads
   ↓
(Handlers, depending on the device used)
   ↓
(Filtering, depending on the configuration)
   ↓
Device Callback
   ↓
Sync Controller
   ↓
[PlotSink] — live display
[ExportCSV] — background export
   ↓
signals_<session>.csv
events_<session>.csv
```
Communication between modules is **non-blocking** via `queue.Queue`.

---

## 6. Error Handling
- Each producer and sink runs in isolated threads.  
- Exceptions are caught and logged with global termination if present before sync execution.  
- Exceptions are caught and logged without halting the main process during sync execution.  
- Missing or corrupted data is logged using a telemetry update.

> For problems and troubleshooting please refer to [USER_GUIDE.md](./USER_GUIDE.md)

---

## 7. Extensibility
### Add a new device:

Implementing new logic for a new device can be challenging, so the developer has provided **[device_template.py](./acquisition/device_template.py)** code skeleton to ease the process.

The provided code follows the following logic:
1. Create `acquisition/device_name.py`.  
2. Implement a threaded producer emitting `(channel, value, ts)`.  
3. Register the instance in `config.py`, with sampling frequency and channel specification.  
4. Add a device-specific handler under `handlers/` if preprocessing is required.
5. Add specific start logic in `main.py`

### New marker generator
> Marker logic has also been added to **[device_template.py](./acquisition/device_template.py)**

In general, simply use the following code snippet:
```python
SYNC.set_event(label, source_name)
SYNC.set_spike(label, source_name)
```

Remember to retrieve the correct labels using:
```python
labels = (CONFIG.get("events", {}).get("EVENT_KEYMAP", {}) or {}).values()
```
---

## 8. Telemetry and Logging
### Telemetry
Telemetry is used sparingly: each streaming handler keeps a running count of invalid or missing samples over `telemetry.WINDOW_S` and posts a warning when the window closes with any bad data. You’ll see that pattern in the Shimmer handlers (e.g. `acquisition/handlers/handler_shimmer_emg.py`).

### Logging
`utils/logger.py` builds a shared file logger the first time any module asks for it: it creates the `logs/` directory with a timestamped filename, attaches a single `FileHandler` with a consistent formatter, and adds a shared `StreamHandler` (errors only) so every module writes to the same session log. Because `get_logger` disables propagation, there is no duplication; each module simply calls `get_logger(__name__)`.

From there the codebase leans on `INFO` for lifecycle breadcrumbs, `WARNING` for recoverable issues, and `ERROR` for actionable failures:
- Startup: `main.py` (line 14) logs “Startup,” delta calculations, device-launch success or “Unknown device type…,” and emits warnings if an exporter can’t be created.
- Dependency guard: `utils/dependencies.py` logs “Checking dependencies…,” installation status, and errors if pip fails or `requirements.txt` is missing.
- Sync core: `processing/sync_controller.py` logs when sinks register, sessions start/stop, device anchors are created, queue backpressure triggers, and clock jumps are detected (warnings).
- Export sink: `export/export_sink.py` records output paths, flush cadence, liveness warnings (idle watermark), and any CSV write issues it catches.
- Plot sink: `visualization/plot_sink.py` announces init settings, new series discovery, timer lifecycle, and reports backend failures (canvas draw errors).
- Acquisition layers: each manager logs connection attempts, handler setup, streaming start/stop, and warns on callback failures or malformed packets. Handler factories themselves log once when constructed and when they encounter missing channels or invalid samples using telemetry updates.
- Event/spike buses: report trigger enablement, unmapped keys, and subscriber failures.
- Helpers and bootstrap: `utils/helpers.py` (line 11) summarizes derived values (max FS, exportable channels) and warns about misconfigurations; `utils/config.py` (line 200) issues errors if `settings.py` is missing or malformed.


Taken together, telemetry keeps an eye on data quality, while the logging system provides a complete narrative—from configuration merge through device setup, runtime events, and graceful shutdown—all written to one rotating log file plus stderr for high-severity issues.

Logger logic can be implemented using the following code snippet:

```python
from utils.logger import get_logger
logger = get_logger(__name__)

logger.info("example")
logger.warning("example")
logger.error("example")

```
---

## 9. Versioning
- **Current version:** 0.9.0 (Beta)  
- **Changelog:** see repository commits.

---

### End of Technical Documentation