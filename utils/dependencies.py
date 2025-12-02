# dependencies.py
# Management and verification of the project dependencies.

from __future__ import annotations

import sys
import subprocess
import importlib
from pathlib import Path
from typing import Dict, List

from utils.logger import get_logger

logger = get_logger(__name__)


def ensure_requirements() -> None:
    """Ensure all packages listed in requirements.txt are importable.

    Behavior:
        - Reads 'requirements.txt' placed next to this file.
        - For each non-empty, non-comment line, tries to import the corresponding module.
        - If import fails, installs the exact spec via pip, then continues.
        - Logs overall status at the end.
        - Exits the process (code 1) if 'requirements.txt' is missing.

    Notes:
        - Some packages have a different importable module name than their PyPI name;
          handle those via `pkg_to_module` mapping below (e.g., 'pyserial' -> 'serial').
        - This utility is designed to be called at startup when dependency checks
          are enabled in configuration.
    """
    # Exceptions only: PyPI package name → importable module name
    pkg_to_module: Dict[str, str] = {
        "pyserial": "serial",
    }

    req_file = Path(__file__).resolve().parents[1] / "requirements.txt"  # go up 1 dir
    if not req_file.exists():
        logger.error('Error: "requirements.txt" not found.')
        sys.exit(1)

    with req_file.open(encoding="utf-8") as f:
        required: List[str] = [
            line.strip()
            for line in f
            if line.strip() and not line.startswith("#")
        ]

    if not required:
        logger.info("No dependencies listed in requirements.txt.")
        return

    logger.info("Checking dependencies...")
    all_present = True
    installed_pkgs: List[str] = []

    for spec in required:
        pkg_name = spec.split("==")[0].strip()  # tolerate pinned specs
        module_name = pkg_to_module.get(pkg_name, pkg_name)

        try:
            importlib.import_module(module_name)
        except ImportError:
            all_present = False
            logger.info(f"'{pkg_name}' not found: installing {spec} ...")
            try:
                subprocess.check_call([
                    sys.executable,
                    "-m", "pip",
                    "install",
                    "--disable-pip-version-check",
                    spec
                ])
                installed_pkgs.append(spec)
                logger.info(f"Installed: {spec}")
            except subprocess.CalledProcessError as e:
                logger.error(f"Installation failed for '{spec}': {e}")
                raise

    if all_present:
        logger.info("All dependencies were already installed.")
    else:
        # Optional summary of what got installed during this run
        logger.info("Installed during check: %s", ", ".join(installed_pkgs) or "<none>")

    logger.info("Dependencies verified")