#!/usr/bin/env python3
"""Self-check of the parts that are easy to break: python3 test_pve_cloud_backup.py.

Runs anywhere (no Proxmox, no rclone, no root): it only touches the settings
file and the rclone command line.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("tool", Path(__file__).with_name("pve_cloud_backup.py"))
assert spec and spec.loader
tool = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = tool  # dataclasses looks the module up by name
spec.loader.exec_module(tool)

SETTINGS = tool.Settings(
    storage="hdd-backup",
    rclone_config="/root/.config/rclone/rclone.conf",
    remote="proton",
    folder="Backups/Proxmox/node1",
    mode="copy",
    use_trash=False,
    bwlimit="30M",
)


def test_settings_round_trip(directory: Path) -> None:
    tool.CONFIG_PATH = directory / "pve-cloud-backup.conf"
    SETTINGS.save()
    assert tool.Settings.load() == SETTINGS


def test_legacy_config_is_read(directory: Path) -> None:
    """A pve-gdrive-backup configuration still loads, DRIVE_REMOTE included."""
    legacy = directory / "pve-gdrive-backup.conf"
    legacy.write_text("STORAGE=hdd\nRCLONE_CONFIG=/x/rclone.conf\nDRIVE_REMOTE=gdrive\nFOLDER=Backups/node1\n")
    settings = tool.Settings.load(legacy)
    assert settings.remote == "gdrive"
    assert settings.destination == "gdrive:Backups/node1/dump"


def test_destination() -> None:
    assert SETTINGS.destination == "proton:Backups/Proxmox/node1/dump"
    assert tool.replace(SETTINGS, encrypt=True).destination == f"{tool.CRYPT_REMOTE}:dump"


def test_backend_flags() -> None:
    """Only a Drive remote gets the Drive flags; the trash flag follows the setting."""
    dump = Path("/mnt/hdd/dump")
    drive = tool.build_rclone_command(tool.replace(SETTINGS, remote="gdrive"), dump, "drive")
    assert drive[1] == "copy"  # mode=copy
    assert "--drive-stop-on-upload-limit" in drive
    assert "--drive-use-trash=false" in drive
    assert "--drive-use-trash=true" in tool.build_rclone_command(tool.replace(SETTINGS, use_trash=True), dump, "drive")

    proton = tool.build_rclone_command(tool.replace(SETTINGS, mode="mirror"), dump, "protondrive")
    assert proton[1] == "sync"
    assert [flag for flag in proton if flag.startswith("--drive-")] == []
    assert "--protondrive-replace-existing-draft=true" in proton
    assert proton[-2:] == ["--bwlimit", "30M"]


def test_bwlimit_words_mean_no_limit() -> None:
    """A limit already set needs a word to go away: an empty line keeps the default."""
    for word in ("", "unlimited", " Unlimited ", "none", "off"):
        assert tool.normalize_bwlimit(word) == ""
    assert tool.normalize_bwlimit(" 30M ") == "30M"
    assert tool.normalize_bwlimit("08:00,5M 23:00,off") == "08:00,5M 23:00,off"
    unlimited = tool.build_rclone_command(tool.replace(SETTINGS, bwlimit=""), Path("/mnt/hdd/dump"), "drive")
    assert "--bwlimit" not in unlimited


def test_stall_watch() -> None:
    """Fires after STALL_STATS stats with the same bytes; the files line and progress reset nothing wrongly."""
    stuck = "Transferred:   15.767 GiB / 109.124 GiB, 14%, 0 B/s, ETA -\n"
    files = "Transferred:           15 / 57, 26%\n"
    watch = tool.StallWatch()
    fired = [watch.feed(line) for _ in range(tool.STALL_STATS + 1) for line in (stuck, files)]
    assert fired.count(True) == 1 and fired[-2] is True

    moving = tool.StallWatch()
    for gib in range(tool.STALL_STATS * 3):
        assert not moving.feed(f"Transferred:   {gib}.5 GiB / 109.124 GiB, 14%, 10 MiB/s, ETA 1h\n")
        assert not moving.feed(files)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as name:
        test_settings_round_trip(Path(name))
        test_legacy_config_is_read(Path(name))
    test_destination()
    test_backend_flags()
    test_bwlimit_words_mean_no_limit()
    test_stall_watch()
    print("ok")
