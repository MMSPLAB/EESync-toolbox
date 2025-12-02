# utils/logger.py
# Configuration and management of the centralized logging system for the project.

import logging
import os
from datetime import datetime
from threading import Lock
from typing import Optional

"""
Module: utils.logger
Short description:
    Centralized logger factory that writes to a single session log file under 'logs/'.
    It provides per-module loggers that share one FileHandler, with thread-safe
    initialization and a consistent formatter.

Responsibilities:
    - Create the 'logs/' directory and pick a timestamped log filename once per session.
    - Attach a shared FileHandler to each requested logger (no duplicate handlers).
    - Keep logging configuration local (no propagation to root by default).
"""

# === Global variables for the shared log file name ===
_log_file_created = False
_log_filename = ""

# Shared file handler to avoid multiple open handles to the same file
_shared_file_handler: Optional[logging.Handler] = None
_shared_stream_handler: Optional[logging.Handler] = None

# Guard initialization with a lock for thread safety
_INIT_LOCK = Lock()


def _ensure_file_handler() -> logging.Handler:
    """Create (once) and return the shared FileHandler for this session."""
    global _log_file_created, _log_filename, _shared_file_handler

    if _shared_file_handler is not None:
        return _shared_file_handler

    with _INIT_LOCK:
        # Double-checked locking to avoid duplicate creation
        if _shared_file_handler is not None:
            return _shared_file_handler

        # === File handler ===
        if not _log_file_created:
            os.makedirs("logs", exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            _log_filename = f"logs/log_{timestamp}.log"
            _log_file_created = True

        fh = logging.FileHandler(_log_filename, mode='a', encoding='utf-8')

        # === Common formatter ===
        # Keep the existing format; add thread name in the future if useful.
        formatter = logging.Formatter('[%(asctime)s] %(name)s: %(levelname)s - %(message)s')
        fh.setFormatter(formatter)

        _shared_file_handler = fh
        return fh

def _ensure_stream_handler() -> logging.Handler:
    """Create (once) and return a shared StreamHandler for console errors."""
    global _shared_stream_handler
    if _shared_stream_handler is not None:
        return _shared_stream_handler

    with _INIT_LOCK:
        if _shared_stream_handler is not None:
            return _shared_stream_handler

        sh = logging.StreamHandler()          # defaults to sys.stderr
        sh.setLevel(logging.ERROR)            # only ERROR and above to console
        formatter = logging.Formatter('[%(asctime)s] %(name)s: %(levelname)s - %(message)s')
        sh.setFormatter(formatter)
        _shared_stream_handler = sh
        return sh

def get_logger(name: str) -> logging.Logger:
    """
    Create or retrieve a module-level logger configured for this project.

    Behavior:
        - Level is set to INFO by default (can be adjusted per-logger later).
        - A shared FileHandler is attached once per-logger (no duplicates).
        - Propagation is disabled to prevent duplicate logs if root is configured.

    Args:
        name: Logger name (typically __name__ of the caller module).

    Returns:
        A configured logging.Logger instance.
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)  # set minimum log level
    logger.propagate = False       # keep logs local to this configuration

    # Ensure no duplicate handlers are attached to this specific logger
    if not logger.handlers:
        file_handler = _ensure_file_handler()
        logger.addHandler(file_handler)

        # Console only for errors
        stream_handler = _ensure_stream_handler()
        logger.addHandler(stream_handler)


    return logger


# Optional utility: expose the current log filename (useful in UIs/tests)
def get_log_filepath() -> str:
    """Return the absolute path to the current session log file."""
    if _log_file_created and _log_filename:
        return os.path.abspath(_log_filename)
    return ""