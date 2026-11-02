"""Turns the twice-daily launchd job on and off. launchd itself is the record of whether it is on."""
import os
import subprocess

from .config import ROOT

LABEL = "com.adityakapoor.jobseeker"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")


def _target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def is_enabled() -> bool:
    return subprocess.run(["launchctl", "print", _target()], capture_output=True).returncode == 0


def enable() -> None:
    subprocess.run([str(ROOT / "scripts" / "install_schedule.sh")], check=True, capture_output=True)


def disable() -> None:
    subprocess.run(["launchctl", "bootout", _target()], capture_output=True)
    if os.path.exists(PLIST):
        os.remove(PLIST)
