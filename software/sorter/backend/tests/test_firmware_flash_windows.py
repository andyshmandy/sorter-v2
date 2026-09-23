import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import hardware.firmware_flash as firmware_flash


class FirmwareFlashWindowsTests(unittest.TestCase):
    def test_find_bootloader_mount_uses_windows_drive_lookup(self) -> None:
        with (
            patch.object(firmware_flash.platform, "system", return_value="Windows"),
            patch.object(firmware_flash, "_windows_bootloader_mount", return_value="R:\\") as lookup,
        ):
            self.assertEqual(firmware_flash.findBootloaderMount(), "R:\\")
            lookup.assert_called_once_with()

    def test_wait_for_bootloader_mount_skips_linux_mount_attempts_on_windows(self) -> None:
        gc = SimpleNamespace(logger=SimpleNamespace(warning=lambda *args, **kwargs: None))
        cancel = threading.Event()

        with (
            patch.object(firmware_flash.platform, "system", return_value="Windows"),
            patch.object(firmware_flash, "findBootloaderMount", side_effect=[None, "R:\\"]),
            patch("hardware.firmware_flash.subprocess.run") as run_mock,
            patch("hardware.firmware_flash.time.sleep") as sleep_mock,
        ):
            self.assertEqual(
                firmware_flash.waitForBootloaderMount(gc, cancel, timeout_s=2.0),
                "R:\\",
            )
            run_mock.assert_not_called()
            sleep_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()