"""
AID PLUS+ — Launcher
=====================
Run this file directly:  py run.py

This script patches sys.path so that the 'aidplus_aidsystem_software' folder
is importable as the 'aidplus' package (as the code internally expects).
"""
import sys
import os

# ── Fix Windows console encoding (cp1252 → UTF-8) ──────────────────────────
# The UI uses Unicode box-drawing characters that cp1252 cannot encode.
os.environ["PYTHONIOENCODING"] = "utf-8"
if sys.platform == "win32":
    os.system("chcp 65001 >nul 2>&1")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Make the parent directory available so that the package folder
# 'aidplus_aidsystem_software' can be imported as 'aidplus'.
_here   = os.path.dirname(os.path.abspath(__file__))   # …/aidplus_aidsystem_software
_parent = os.path.dirname(_here)                        # …/Aid System B29

# Register the package folder under the name 'aidplus'
import importlib.util, types

spec = importlib.util.spec_from_file_location(
    "aidplus",
    os.path.join(_here, "__init__.py"),
    submodule_search_locations=[_here],
)
pkg = importlib.util.module_from_spec(spec)
sys.modules["aidplus"] = pkg
spec.loader.exec_module(pkg)

# Now we can safely run main
from aidplus.main import main_menu
main_menu()
