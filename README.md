# EESync-toolbox
### The Electrophysiological and Electroencephalographic data Syncronization (EESync) toolbox is a toolbox intended to allow the recording of data from multiple sensors in a syncronized manner.

## 1. Purpose
This project enables real-time acquisition, synchronization, filtering, visualization, and export of physiological signals from multiple devices.  
It provides a unified timebase, event/spike logging, and robust CSV export — ideal for lab sessions, experiments, or multimodal studies.

## 2. What the software does
The system acquires data from one or more supported devices (e.g., **Shimmer3** or **Unicorn Hybrid Black EEG**), filters the signals in real time, aligns samples across sources, records **sticky events** and **instant spikes**, and displays synchronized live plots while continuously exporting to CSV.

**Key features:**
- Multi-source signal acquisition (GSR, PPG, EMG, EEG)  
- Real-time filtering and signal synchronization  
- Event & spike logging via keyboard or automated modules  
- Real-time visualization with autoscale  
- Robust asynchronous CSV export  
- Centralized logging and telemetry

## 3. Supported Platforms
- **Operating systems:**  
  Runs entirely on Python; tested on **Windows 11** (recommended).  
  Simulation and visualization modules also work on macOS and Linux.  
  > Hardware drivers for Shimmer and Unicorn are Windows-only.

- **Devices:**  
  - **Shimmer3 GSR+ Unit** (PPG, GSR via `pyshimmer`)  
  - **Shimmer3 EXG Unit** (2-channel EMG via `pyshimmer`)  
  - **g.tec Unicorn Hybrid Black** (8-channel EEG via LSL)

- **Python environment:**  
  - Python 3.13.7  
  - pip ≥ 25.0  

## 4. System Requirements (summary)
- **CPU:** Multi-core ≥ 3.0 GHz  
- **RAM:** ≥ 8 GB  
- **Bluetooth:** High-quality adapter (5.0+). Use an external USB adapter if needed (see Unicorn EEG documentation).  
- **Storage:** ≥ 500 MB free (more for long sessions)  
- **Power:** Keep the system plugged in during recordings  

## 5. Installation (Quick Start)

### Windows PowerShell
```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

> Dependencies (automatically checked and installed during startup): matplotlib, numpy, scipy, pylsl, pyserial, pyshimmer

## 6. Typical Use Cases
- Laboratory experiments with multiple synchronized sensors  
- Physiological signal recording with event and spike tagging  
- Exporting time-aligned data for offline analysis

## 7. Version
- **Current version:** 0.9.0 (Beta) — feature complete, under testing, and awaiting GUI integration  
- **Changelog:** See the repository releases page

### Documentation
- **User Guide:** see [USER_GUIDE.md](./USER_GUIDE.md)  
- **Technical Documentation:** see [TECHNICAL_DOC.md](./TECHNICAL_DOC.md)