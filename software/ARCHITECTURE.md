# Sorter V2 Architecture

This document describes the high-level architecture of the sorter-v2 system and how its main components interact.

## System Overview

Sorter-v2 is a LEGO sorting machine with three main subsystems:

1. **Sorter Client** (`software/sorter/`) — The machine controller running on an Orange Pi 5
2. **Frontend UI** (`software/sorter/frontend/`) — Web-based operator interface
3. **Hive** (`software/hive/`) — Cloud platform for data collection and model training

### High-Level Data Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                    Sorter Machine                               │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌────────────────┐         ┌───────────────────────────────┐  │
│  │   Raspberry    │◄────────►│  Sorter Backend              │  │
│  │   Pi Picos     │          │  (main.py / coordinator)     │  │
│  │  Firmware      │          │                             │  │
│  │                │          │  • Control loop             │  │
│  │ • Feeder       │          │  • Vision pipeline          │  │
│  │ • Distribution │          │  • Hardware abstraction     │  │
│  └────────────────┘          │  • Classification           │  │
│         ▲                     │  • Hive sync               │  │
│         │ USB serial          └───────────────────────────────┘  │
│         │                               ▲                       │
│         │                               │ HTTP/WebSocket         │
│    ┌────┴────────────────────────────────────┐                 │
│    │        Cameras / Servos / Motors        │                 │
│    └─────────────────────────────────────────┘                 │
│                                                                 │
│    Web UI ◄─────────────────────────────────────────────┐     │
│    (localhost:5173)                                      │     │
│                                                   Vite dev server│
└─────────────────────────────────────────────────────────────────┘
         ▲
         │ HTTPS/HTTP
         │
    ┌────┴──────────┐
    │   Browser     │
    │  Dashboard    │
    └───────────────┘
         ▲
         │
    ┌────┴──────────────────┐
    │      Hive Cloud       │
    │ (Training, Data Sync) │
    └───────────────────────┘
```

## Sorter Backend Architecture

The backend is organized into layered components:

### Control Loop (`main.py` → `sorter_controller.py` → `coordinator.py`)

**Entry Point: `main.py`**
- Initializes hardware (Pico buses, cameras, servos)
- Starts FastAPI server and WebSocket broadcaster
- Instantiates the `SorterController`
- Runs the main control loop at ~100 Hz

**State Machine: `SorterController`**
- Maintains lifecycle state: INITIALIZING → PAUSED → RUNNING
- Delegates sorting logic to `Coordinator`
- Broadcasts state changes to UI via WebSocket

**Orchestration: `Coordinator`**
- Runs the real-time control loop every tick
- Coordinates three subsystems:
  1. `Feeder` — C-channel progression (pulse-based or continuous motion)
  2. `Classification` — C4 platter rotation and identification
  3. `Distribution` — Chute positioning and bin assignment
- Manages gates (feed control, classification flow) based on piece state
- Observes incidents (stalls, timeouts) and pauses as needed

### Hardware Abstraction Layer (`hardware/`, `irl/`)

**IRLConfig & IRLInterface**
- Parse machine-specific parameters from TOML
- Build runtime interface to Pico boards, cameras, servos, motors
- Provide uniform abstraction over hardware variants (PCA9685 vs Waveshare servos, etc.)

**Bus Communication (`hardware/bus.py`)**
- Low-level MCU communication protocol (COBS framing, CRC32)
- Shared UART command/response between all Picos on the same bus
- Timeout and retry handling for robust serial communication

**Stepper/Servo/Motor Drivers**
- `StepperMotor` — Interface to TMC2209 drivers (microstepping, stall detection, current control)
- `ServoMotor` — Abstraction over PCA9685 (PWM) or Waveshare (UART servo bus)
- `DigitalInputPin` / `DigitalOutputPin` — GPIO (limit switches, LEDs)

### Vision Pipeline (`vision/`, `perception/`)

**VisionManager**
- Owns all camera capture threads
- Runs MOG2 background-subtraction detectors per channel
- Caches detection results for the coordinator to query
- Interfaces with ML inference workers (YOLO, NCNN, Hailo, etc.)

**PerceptionService (Rev04)**
- Multi-worker NPU inference (one per channel on Orange Pi 5)
- RKNN runtime for on-device object detection
- Channel-crop capture for training data collection

**Detection Registry**
- Pluggable algorithm selection (MOG2, YOLO, Gemini AI)
- Per-role and per-scope configuration
- Fallback chains for missing models

### Subsystems (`subsystems/`)

Three main execution branches run in parallel during normal sorting:

1. **Feeder** (`feeder/`) — Pulse-perception feeding
   - Tracks piece position on C-channel via camera
   - Pulses rotor to move piece from C1→C2→C3
   - Monitors for stalls and jams

2. **Classification Channel** (`classification_channel/`) — C4 sorting decision
   - Receives pieces from C3 handoff
   - Rotates carousel to classification window
   - Queries ML model for piece identity
   - Awaits distribution readiness before release

3. **Distribution** (`distribution/`) — Bin targeting and chute positioning
   - Maps identified piece to target bin
   - Positions chute servo to bin center
   - Confirms piece exit before next acceptance

### State & Telemetry

**Global Config (`global_config.py`)**
- Singleton holding runtime parameters
- Logger, model registry, feature flags
- Links to controllers, services, stats trackers

**Runtime Stats (`runtime_stats.py`)**
- Real-time counters: pieces sorted, errors, performance metrics
- Lifecycle state and incident tracking
- Broadcasted to UI every 500ms

**Piece Tracking (`piece_transport.py`, `defs/known_object.py`)**
- `KnownObject` — Unified piece record from detection through distribution
- Carries detection data, confidence scores, classification history
- Merged from multiple detection sources (feeder, carousel, classification)

## Frontend Architecture

**Stack**: SvelteKit + TypeScript + Tailwind CSS

**Core Pages**:
- **Dashboard** — Real-time machine state, live camera feeds, piece counts
- **Settings** — Hardware calibration, camera assignment, feature toggles
- **Models** — Detection model management and training status
- **Profiles** — Sorting rule editor with live preview

**Data Binding**:
- WebSocket feed from backend provides live updates
- HTTP endpoints for config, model, and calibration state
- Optimistic UI updates with backend confirmation

**Key Components**:
- Camera preview tiles with detection overlays
- Piece stream with classification confidence
- Chute position visualization
- Real-time performance graph

## Hive Architecture

Hive is a cloud platform for collaborative data collection and model training.

**Backend** (FastAPI + PostgreSQL)
- User authentication (GitHub OAuth + JWT)
- Training data ingestion from multiple machines
- Model artifact storage and versioning
- REST API for training coordination

**Frontend** (SvelteKit)
- Dataset explorer with filtering and search
- Model training dashboard
- Performance benchmarking tools
- Parts catalog integration

**Key Flows**:
1. Machine captures crops (pieces, channels, detection context)
2. Samples sync to Hive via HTTPS
3. Training jobs run on Hive backend or remote GPU compute
4. Trained models download back to machines for inference

## Firmware Architecture

**Two Picos per machine** communicate independently:

1. **Feeder Pico**
   - Stepper control: C-channel rotors (up to 3)
   - Servo control: Optional stepper servo (old 2-channel designs)
   - GPIO: Limit switches, LED indicators

2. **Distribution Pico**
   - Stepper control: Carousel and chute motors
   - Servo control: Layer distribution servos (up to 6 per layer)
   - GPIO: Endstops, status LEDs

**Firmware Protocol**:
- Command/response over 576 kbaud serial (COBS + CRC32)
- Each Pico can address multiple devices on its bus (UART Daisy-chain)
- Stateless command set (no persistent state beyond registers)

## Integration Points

### Backend ↔ UI
- **HTTP REST**: Configuration, calibration, system control
- **WebSocket**: Real-time state, camera frames, detection overlays
- **File upload**: Model artifacts, calibration images

### Backend ↔ Hive
- **HTTPS POST**: Sample uploads (crop images, metadata)
- **HTTPS GET**: Model downloads, training status polling
- **Authentication**: API keys stored in `local_state.py`

### Backend ↔ Hardware
- **USB Serial**: MCU communication to Picos (one bus per Pico)
- **USB Video**: Camera capture via OpenCV (Linux) or generic (Windows)
- **GPIO**: Direct digital I/O for switches and status signals

## Key Architectural Decisions

### Real-Time Control Loop
The coordinator runs at ~100 Hz, yielding frequently to allow ML inference, camera capture, and server requests to proceed without blocking. This balances responsiveness with hardware utilization.

### Hardware Abstraction
`IRLConfig` → `IRLInterface` → concrete device classes insulates business logic from specific hardware variants. New servo types or motor drivers require only new controller implementations, not coordinator changes.

### Multi-Worker Perception
Inference workers run in thread pools, decoupled from the control loop. The coordinator queries a cache of latest results instead of blocking on inference. This prevents slow models from blocking stepper moves.

### Stateless Firmware
Pico firmware only executes commands; state (position, configuration) lives in the backend. This simplifies firmware and allows the backend to recover from Pico restarts without losing context.

### Modular Subsystems
Feeder, Classification, and Distribution are independent state machines that communicate via shared gates. This allows features like "classification-only mode" (no feeder) or "manual feed mode" (no automatic feeder).

### Sorting Profile as Data
Sorting rules are loaded from JSON at startup, not compiled in. This enables runtime rule changes and A/B testing without firmware reflash.

## Deployment Models

### Local (Development)
```
Orange Pi 5 (localhost:5173)
  ├─ Backend main.py
  ├─ Frontend npm dev server
  └─ Picos + Cameras (directly connected)
```

### SorterOS (Production)
```
Orange Pi 5 running SorterOS
  ├─ Backend main.py (systemd service)
  ├─ Frontend (nginx reverse proxy)
  └─ Auto-startup on boot
```

### Cloud Integration
```
Machine                  Hive Cloud
  │                        │
  ├─ Capture crops      ◄──┤
  ├─ Sync samples       ──►│
  └─ Download models    ◄──┤
```

## Error Handling & Resilience

**Hardware Faults**:
- MCU bus timeout → pause, show error banner, allow home/reconnect
- Servo offline → paused state, recovery on reconnect
- Stall detection → auto-pause, incident logged

**Software**:
- Model load failure → fallback to previous version
- Perception service crash → vision disabled, operator notified
- Camera disconnect → graceful degradation to remaining cameras

**Data Consistency**:
- Piece records persist across pauses
- State machines are idempotent (safe to re-run same command)
- Hive upload retries on transient network failure
