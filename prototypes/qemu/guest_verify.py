"""Compatibility entry point for the supported QEMU runtime."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from qemu.guest_verify import *
