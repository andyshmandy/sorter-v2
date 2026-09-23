# Setup

## Prerequisites

- [Git](https://git-scm.com/)
- [Node.js](https://nodejs.org/) (v20+) and npm
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [Raspberry Pi Pico SDK](https://github.com/raspberrypi/pico-sdk) (for firmware builds)

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

On Windows PowerShell, the equivalent setup is:

```powershell
Copy-Item .env.example .env
Copy-Item machine.example.toml machine.toml
```


Camera assignment happens in the UI: open the running frontend and use the Settings → Cameras page to map each OpenCV device index to its role (feeder, classification top/bottom, carousel, etc). The resulting assignments are written back to `machine_params.toml` under `[cameras]`.

## UI Dependencies

```bash
cd sorter/frontend
npm install
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

On Windows PowerShell, use:

```powershell
.\dev.ps1 backend
```

That starts the full machine client with the controller, hardware bindings, and API. If you only want the lightweight API shell for UI work, use:

```bash
./dev.sh api
```

On Windows PowerShell, use:

```powershell
.\dev.ps1 api
```

## Windows (Native, No WSL)

The frontend runs natively on Windows.

The backend also runs natively on Windows, with these caveats:

- The backend process guard falls back to metadata-only mode on Windows, so it does not enforce single-instance locking there.
- Camera format probing and `v4l2-ctl` controls are Linux-only; on Windows the backend falls back to generic OpenCV camera capture.
- Firmware flashing now detects the `RPI-RP2` bootloader drive by Windows drive label. If the board does not re-enumerate cleanly after a reboot-to-bootloader command, use BOOTSEL/recovery mode and retry the flash.

### Windows bring-up checklist

Open a PowerShell window and install the baseline tools:

```powershell
winget install Git.Git
winget install OpenJS.NodeJS.LTS
powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

If OpenCV or PyTorch later fail with missing runtime DLL errors, install the Microsoft Visual C++ Redistributable:

```powershell
winget install Microsoft.VCRedist.2015+.x64
```

Open a fresh PowerShell window and verify the toolchain:

```powershell
git --version
node --version
npm --version
uv --version
```

Clone the repo and create the local config files:

```powershell
git clone https://github.com/basicallysource/sorter-v2.git
cd sorter-v2/software
Copy-Item .env.example .env
Copy-Item machine.example.toml machine.toml
```

Edit `.env` so the machine config path points at your local `machine.toml`.

Install the frontend dependencies:

```powershell
cd sorter/frontend
npm install
cd ../..
```

Install Python 3.12 through `uv` and verify the backend imports:

```powershell
cd sorter/backend
uv python install 3.12
uv run python -c "import cv2, fastapi, serial; print('backend imports ok')"
cd ..\..
```

If PowerShell blocks local scripts, allow them for the current shell only:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

Start the frontend:

```powershell
cd sorter/frontend
npm run dev
```

In a second PowerShell window, start the backend for UI work first:

```powershell
cd sorter-v2/software
.\dev.ps1 api
```

If you want the full backend instead of API-only:

```powershell
cd sorter-v2/software
.\dev.ps1 backend
```

Basic Windows hardware checks:

- Plug each Pico in once and confirm it appears as a COM port in Device Manager.
- Put each Pico into BOOTSEL mode once and confirm Windows mounts a drive labeled `RPI-RP2`.
- If serial access fails, close any serial monitor apps and rerun PowerShell as Administrator.
- If camera indexes move after reboot or replug, fix them from Settings → Cameras in the UI.

First successful launch means:

- Frontend dev server is reachable at the Vite URL printed by `npm run dev`.
- Backend answers on `http://127.0.0.1:8000/health`.
- The UI loads and can reach the API.

Recommended Windows workflow:

```powershell
cd sorter-v2/software
Copy-Item .env.example .env
Copy-Item machine.example.toml machine.toml
cd sorter/frontend
npm install
npm run dev
```

In a second PowerShell window:

```powershell
cd sorter-v2/software/sorter/backend
uv run python api_only.py
```

For the full backend instead of API-only:

```powershell
cd sorter-v2/software
.\dev.ps1 backend
```

Recommended Windows prerequisites checklist:

- Install Node.js 20 or newer.
- Install `uv` and let it manage the Python 3.12 environment for the backend.
- Install the Visual C++ Redistributable if OpenCV or PyTorch wheels complain about missing runtime DLLs.
- Plug each Pico in once and confirm it shows up both as a serial device and, in BOOTSEL mode, as a drive labeled `RPI-RP2`.
- If Windows blocks serial access, close any other serial monitor apps and rerun PowerShell as Administrator.
- If camera indexes shift between reboots, reassign them from the UI under Settings → Cameras.

If you want to use an Android phone as the carousel camera on macOS, run:

```bash
./scripts/android_camera_bridge.sh
```

Then point `[cameras].carousel` at `http://127.0.0.1:18081/carousel.mjpg`.

`uv` will install Python 3.12 and all dependencies on first run. The `.env` file is loaded automatically.

On startup the client will:
1. Discover all connected Pico devices over USB serial
2. Scan each bus for SorterInterface firmware devices
3. Aggregate stepper and servo actuators across all discovered boards
4. Bind actuators to logical roles (carousel, chute, rotors) using firmware-reported names

**Windows**: If serial access is denied, rerun PowerShell as Administrator.

---

# Further Reading

- [ARCHITECTURE.md](ARCHITECTURE.md) — Multi-Pico hardware abstraction, firmware roles, client init flow
- [../docs/runtime-status.md](../docs/runtime-status.md) — Current detector benchmark conclusions, deployment recommendations, and canonical local artifacts to keep
- [../docs/model-artifacts.md](../docs/model-artifacts.md) — Which detector exports and compiled formats exist, what they are for, and how they map to targets
- [../docs/device-benchmarking.md](../docs/device-benchmarking.md) — Reproducible detector benchmarking across Mac, Raspberry Pi, and Orange Pi targets
- [../docs/hailo-hef-workflow.md](../docs/hailo-hef-workflow.md) — `ONNX -> HEF` workflow for Raspberry Pi 5 AI HAT deployments
- [firmware/sorter_interface_firmware/README.md](firmware/sorter_interface_firmware/README.md) — Firmware build options and flashing
