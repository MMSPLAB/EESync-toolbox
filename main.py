# main.py
# Project entry point: compute fs_max→delta, start sync, attach sinks, start demos.

from __future__ import annotations

from utils.logger import get_logger

from utils.config import CONFIG

from utils.dependencies import ensure_requirements
from processing.sync_controller import sync_manager as SYNC
from utils.helpers import (
    compute_fs_max_from_config,
    collect_known_channels_from_config,
    iter_enabled_instances,
    setup_signal_handlers,
    wait_for_producers,
)

# ====== MAIN ======
def main() -> None:
    logger = get_logger(__name__)
    logger.info("Startup")

    setup_signal_handlers()  # <-- added: SIGINT/SIGTERM -> graceful shutdown

    # --- Optional dependency check ---
    if bool(CONFIG.get("system", {}).get("CHECK_DEPENCENCIES", False)):
        try:
            ensure_requirements()  # Reads requirements.txt next to dependencies.py
        except SystemExit:
            raise  # Propagate explicit exits (e.g., missing requirements.txt)
        except Exception as e:
            logger.error("Dependency check failed: %s", e)
            raise SystemExit(1)

    # --- Delta from fs_max ---
    fsmax = compute_fs_max_from_config(CONFIG)
    delta = 1.0 / fsmax  # Fixed grid step
    logger.info("fs_max=%s, delta=%s", fsmax, delta)

    # --- Start sync session ---
    SYNC.start_session(delta=delta)

    # --- Sinks: plot (optional) + export (if channels available) ---
    plot_sink = None
    if bool(CONFIG.get("ui", {}).get("PLOT_ENABLE", True)):
        from visualization.plot_sink import PlotSink
        plot_sink = PlotSink(delta=delta)
        SYNC.add_plot_sink_queue(plot_sink.queue)

    known_channels = collect_known_channels_from_config(CONFIG)  # Optional header

    # If no exportable channel is available, continue in plot-only mode
    if not known_channels:
        logger.warning("No exportable channels found. Continuing without export (plot-only).")

    export_sink = None  # <-- ensure defined even if export disabled
    if known_channels and bool(CONFIG.get("export", {}).get("EXPORT_ENABLE", True)):
        from export.export_sink import ExportSink
        export_sink = ExportSink(delta=delta, known_channels=known_channels)
        export_sink.start()                                     # Open files + thread
        SYNC.add_sink_queue(export_sink.q)                      # Fan-out to exporter

    # --- Start producers ---
    producers = []
    try:
        #enabled_instances = iter_enabled_instances(CONFIG)
        enabled_instances = list(iter_enabled_instances(CONFIG))
    # --- Devices: unified loop with simple type switch ---
        for typ, inst in enabled_instances:
            try:
                # === SHIMMER ===
                if typ == "shimmer":
                    from acquisition.shimmer_device import ShimmerManager
                    mgr = ShimmerManager(inst)
                    mgr.start_stream()
                    producers.append(mgr)
                    continue

                # === DEMO RAND ===
                if typ == "demo_rand":
                    from acquisition.demo_rand import DemoRandManager
                    mgr = DemoRandManager(inst)
                    mgr.start_stream()
                    producers.append(mgr)
                    continue

                # === UNICORN LSL ===
                if typ == "unicorn_lsl":
                    from acquisition.unicorn_lsl import UnicornManager
                    mgr = UnicornManager(inst)
                    mgr.start_stream()
                    producers.append(mgr)
                    continue
                
                # === DEVICE TEMPLATE ===
                if typ == "device_template":
                    from acquisition.device_template import DeviceTemplateManager
                    mgr = DeviceTemplateManager(inst)
                    mgr.start_stream()
                    producers.append(mgr)
                    continue

                # === DEFAULT / UNKNOWN ===
                # Unknown device type — skip silently but keep log trace.
                logger.warning("Unknown device type '%s' — skipping instance.", typ)
                continue

            except Exception as e:
                logger.error("Failed to start device type '%s' (instance='%s'): %s", typ, inst.get("DEVICE_NAME", typ), e)
                raise SystemExit(2)


        # Marker generators: events
        mg = CONFIG.get("marker_generators", {})
        for inst in mg.get("event_demo", {}).get("INSTANCES", []):
            if not inst.get("ENABLED", False):
                continue
            name = str(inst.get("GENERATOR_NAME", "event_demo")).strip()
            interval = float(inst.get("INTERVAL_S", 3.0))
            from acquisition.event_demo import start_event_demo
            producers.append(start_event_demo(name=name, interval_s=interval))

        # Marker generators: spikes
        for inst in mg.get("spike_demo", {}).get("INSTANCES", []):
            if not inst.get("ENABLED", False):
                continue
            name = str(inst.get("GENERATOR_NAME", "spike_demo")).strip()
            interval = float(inst.get("INTERVAL_S", 2.0))
            from acquisition.spike_demo import start_spike_demo
            producers.append(start_spike_demo(name=name, interval_s=interval))

        # --- Blocking section: plot loop or producers join ---
        if plot_sink is not None:
            plot_sink.run()                                # UI loop (blocking)
        else:
            wait_for_producers(producers)                  # cooperative wait

    except KeyboardInterrupt:
        pass
    finally:
        # Best-effort stop of producers
        for p in producers:
            try:
                p.stop()
            except Exception:
                pass
        # Stop sync session first to quiesce sources
        try:
            SYNC.stop_session()
        except Exception:
            pass
        # Then stop exporter to commit remaining buffered rows
        try:
            if export_sink is not None:                    # guard on exporter
                export_sink.stop()
        except Exception:
            pass

if __name__ == "__main__":
    main()
