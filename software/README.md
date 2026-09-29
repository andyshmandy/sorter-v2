# Setup

## Prerequisites

- [Git](https://git-scm.com/)
- [Node.js](https://nodejs.org/) (v20+) and npm
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [Raspberry Pi Pico SDK](https://github.com/raspberrypi/pico-sdk) (for firmware builds)

The backend runs on Linux (production target: Orange Pi 5), macOS, and Windows.
On Windows, install Git, Node.js and `uv` the same way (their installers all
support Windows); the Pico SDK is only needed if you're building firmware
yourself rather than flashing a released `.uf2`. See
[Windows notes](#windows-notes) below for what differs.

## Clone

```bash
git clone https://github.com/basicallysource/sorter-v2.git
cd sorter-v2/software
```

## Firmware

Flash firmware to each Raspberry Pi Pico using the Makefile in `firmware/sorter_interface_firmware/`. Put one Pico into BOOTSEL mode at a time (so it mounts as `RPI-RP2`), then run the appropriate target — the build happens automatically:

```bash
cd firmware/sorter_interface_firmware
make flash-feeder       # builds and flashes feeder firmware
make flash-distribution # builds and flashes distribution firmware
```

Flash each Pico separately, one at a time in BOOTSEL mode.

## Environment

```bash
cp .env.example .env
cp machine.example.toml machine.toml
```

Edit the path in `.env` to match the real path to `machine.toml`.


Camera assignment happens in the UI: open the running frontend and use the Settings → Cameras page to map each OpenCV device index to its role (feeder, classification top/bottom, carousel, etc). The resulting assignments are written back to `machine_params.toml` under `[cameras]`.

## UI Dependencies

```bash
cd sorter/frontend
pnpm install --frozen-lockfile
```

---

# Running

You'll need two terminal tabs, both from `sorter-v2/software`.

## Terminal 1: UI

```bash
cd sorter/frontend
npm run dev
```

## Terminal 2: Client

```bash
cd sorter/backend
uv run python main.py
```

Or from `sorter-v2/software`, you can use the bundled dev runner:

```bash
./dev.sh backend
```

On Windows, use the PowerShell equivalent instead (same modes: `all`, `backend`, `api`, `frontend`):

```powershell
.\dev.ps1 backend
```

That starts the full machine client with the controller, hardware bindings, and API. If you only want the lightweight API shell for UI work, use:

```bash
./dev.sh api
```

`uv` will install Python 3.13 and all dependencies on first run. The `.env` file is loaded automatically.

On startup the client will:
1. Discover all connected Pico devices over USB serial
2. Scan each bus for SorterInterface firmware devices
3. Aggregate stepper and servo actuators across all discovered boards
4. Bind actuators to logical roles (carousel, chute, rotors) using firmware-reported names

**Windows**: Run PowerShell as Administrator to access serial ports.

---

# Windows notes

The backend and firmware flasher run natively on Windows — no WSL required.
A few things behave differently than on Linux/the Orange Pi:

- **Serial ports**: Run PowerShell as Administrator so the process can open
  the Pico's COM port.
- **`dev.ps1` execution policy**: if PowerShell refuses to run the script,
  either unblock it once (`Unblock-File .\dev.ps1`) or run
  `powershell -ExecutionPolicy Bypass -File .\dev.ps1 backend` for that
  invocation. This is a signing policy on the script file, not a code change.
- **Camera format negotiation**: Linux uses `v4l2-ctl` to force each camera
  into a specific resolution/FOURCC (MJPEG vs YUYV) before OpenCV opens it.
  Windows has no equivalent, so cameras open at their driver default instead.
  Three cameras sharing one USB 2.0 bus can saturate it if any of them default
  to uncompressed YUYV at full resolution — if a camera fails to open or
  drops frames, put it on its own USB controller/hub or move it to USB 3.
- **Process guard**: on Linux/macOS a single backend instance per checkout is
  enforced with an `flock`; on Windows this uses a named mutex instead. Same
  effect (a second `uv run python main.py` for the same repo checkout waits
  for/replaces the first), no separate setup needed.
- **Firmware flashing**: putting a Pico in BOOTSEL mode mounts it as a
  `RPI-RP2` drive letter the same as any USB flash drive; the in-app flasher
  finds it by volume label automatically. If the board doesn't re-enumerate
  after a reboot-to-bootloader request, replug it while holding BOOTSEL and
  retry.
- **NPU-accelerated detection**: `rknn-toolkit-lite2` (the Orange Pi 5's RK3588
  NPU runtime) only ships Linux/aarch64 wheels and isn't installed on Windows.
  Detection automatically falls back to ONNX or NCNN on the CPU instead —
  slower, but functional for development. This is the same fallback macOS dev
  boxes use.

---

# Further Reading

- [ARCHITECTURE.md](ARCHITECTURE.md) — Multi-Pico hardware abstraction, firmware roles, client init flow
- [../docs/runtime-status.md](../docs/runtime-status.md) — Current detector benchmark conclusions, deployment recommendations, and canonical local artifacts to keep
- [../docs/model-artifacts.md](../docs/model-artifacts.md) — Which detector exports and compiled formats exist, what they are for, and how they map to targets
- [../docs/device-benchmarking.md](../docs/device-benchmarking.md) — Reproducible detector benchmarking across Mac, Raspberry Pi, and Orange Pi targets
- [../docs/hailo-hef-workflow.md](../docs/hailo-hef-workflow.md) — `ONNX -> HEF` workflow for Raspberry Pi 5 AI HAT deployments
- [firmware/sorter_interface_firmware/README.md](firmware/sorter_interface_firmware/README.md) — Firmware build options and flashing
