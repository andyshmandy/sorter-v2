# Sorter V2 System Architecture

Comprehensive architecture documentation for the sorter-v2 LEGO sorting machine project.

## Quick Links

- **[Backend Architecture](../software/sorter/backend/ARCHITECTURE.md)** — Control loop, hardware abstraction, subsystems, vision pipeline
- **[Frontend Architecture](../software/sorter/frontend/ARCHITECTURE.md)** — UI components, stores, WebSocket communication
- **[Firmware & Hardware Architecture](../software/firmware/ARCHITECTURE.md)** — Pico firmware, device drivers, communication protocol
- **[Hive Architecture](../software/hive/ARCHITECTURE.md)** — Cloud platform, training pipeline, data sync
- **[Main Software Architecture](../software/ARCHITECTURE.md)** — System overview, integration points, deployment models

## Project Structure

```
sorter-v2/
├── software/
│   ├── sorter/
│   │   ├── backend/          # Machine controller (Python/FastAPI)
│   │   └── frontend/         # Web UI (SvelteKit)
│   ├── hive/                 # Cloud platform (FastAPI + SvelteKit)
│   ├── firmware/             # Raspberry Pi Pico firmware (C)
│   └── [ARCHITECTURE.md]     # System-level overview
│
├── docs/                     # Documentation site (SvelteKit)
├── electronics/              # KiCad schematics (feeder/distribution boards)
├── mechanical/               # CAD files (frame, chutes, etc.)
├── parts-calculator/         # Interactive parts catalog
├── training/                 # ML training scripts
└── AGENTS.md                 # Agent guidelines
```

## System Components

### 1. Sorter Machine (On-Site)

**Hardware**:
- Orange Pi 5 (main controller)
- 2× Raspberry Pi Picos (firmware-based MCU bus)
- 5 stepper motors (4 feeder channels + 1 carousel + 1 chute)
- 3 camera lamps (OV9732 on C2/C3, IMX415 on C4)
- Servo motors (layer distribution)
- Limit switches, LED indicators

**Control Stack**:
1. **Backend** (`software/sorter/backend/`)
   - Python 3.13, FastAPI, OpenCV, YOLO ML
   - Runs on Orange Pi 5
   - Listens on HTTP :8000, WebSocket :8000/api/stream
   - Coordinates: feeder → classification → distribution

2. **Frontend** (`software/sorter/frontend/`)
   - SvelteKit TypeScript UI
   - Served via nginx (or Vite dev server)
   - Connects to backend HTTP/WebSocket
   - Allows operator to monitor and configure machine

3. **Firmware** (`software/firmware/`)
   - Two independent Pico instances
   - TMC2209 stepper drivers
   - PCA9685 or Waveshare servo bus
   - Responds to backend commands over USB serial

### 2. Hive Cloud Platform

**Purpose**: Centralized dataset and model management for the community

**Stack**:
- **Backend**: FastAPI + PostgreSQL + S3 storage
- **Frontend**: SvelteKit UI
- **Features**:
  - Sample upload/download from machines
  - Model artifact versioning
  - Training job coordination
  - Dataset filtering and curation
  - Performance benchmarking

**Deployment**:
- Docker containers (Traefik reverse proxy)
- Persistent PostgreSQL + S3 storage
- GitHub OAuth for authentication

### 3. Documentation Site

**Purpose**: Build guides, API documentation, architecture overview

**Stack**: SvelteKit + Markdown

## High-Level Data Flow

### 1. Real-Time Sorting

```
┌─────────────────────────────────────┐
│  Feeder Pico (via USB serial)       │
│  • Stepper: c_channel_1/2/3_rotor   │
│  • Cameras: OV9732 (2× 720p)        │
└────────────────┬────────────────────┘
                 │ MOG2 detection
                 ↓
          ┌──────────────┐
          │   Feeder     │  Piece progression C1→C2→C3
          └──────┬───────┘
                 │
                 ↓
          ┌──────────────┐
          │Classification│  YOLO detection + confidence
          └──────┬───────┘
                 │
                 ↓
          ┌──────────────┐
          │Distribution  │  Servo positioning + piece targeting
          └──────┬───────┘
                 │
                 ↓
     ┌────────────────────────┐
     │ Distribution Pico      │
     │ • Stepper: carousel    │
     │ • Servo: chute layers  │
     └────────────────────────┘
```

### 2. Machine ↔ Hive Sync

```
Machine Backend (Periodic)
  ├─ POST /api/samples/  — Upload detected crops
  ├─ GET /api/models/    — Check for new model versions
  └─ GET /api/models/{id}/download — Deploy trained model

Hive Backend (On Receive)
  ├─ Store samples in S3
  ├─ Index in PostgreSQL
  └─ WebSocket push to UI (new dataset)
```

### 3. Training Pipeline

```
Dataset (Hive)
  ├─ Filter by mold, quality, date range
  └─ Export as training data

Trainer (Hive Backend or External GPU Service)
  ├─ Load dataset from S3
  ├─ Train YOLO / Gemini / Custom model
  ├─ Evaluate on validation set
  └─ Upload trained artifact to S3

Model Deployment
  ├─ Version artifact in PostgreSQL
  ├─ Make available via GET /api/models/
  └─ Machine pulls on next sync
```

## Key Architectural Patterns

### 1. Real-Time Control Loop

The backend runs a ~100 Hz control loop that:
1. Polls hardware state (stepper position, servo feedback)
2. Executes coordinator step (feeder → classification → distribution)
3. Sends commands to Picos
4. Broadcasts state to UI
5. Sleeps to maintain cycle time

This balances responsiveness with allowing threads (vision, ML) to run.

### 2. Stateless Firmware

Pico firmware executes commands; the backend owns all state:
- **Position**: Backend calculates from steps
- **Configuration**: Backend stores in TOML, sends to Pico on boot
- **Stall Detection**: Backend queries and interprets stall flags

This simplifies firmware and allows graceful recovery from Pico restarts.

### 3. Hardware Abstraction Layer (IRLConfig → IRLInterface)

`machine.toml` defines hardware topology → backend builds device objects → coordinator works with abstract interfaces. Changing hardware (e.g., PCA9685 → Waveshare servo) requires only config change + new controller implementation, not coordinator logic.

### 4. Subsystems as Independent State Machines

Feeder, Classification, and Distribution are decoupled:
- Each has its own state machine
- They communicate via shared gates and transport objects
- Coordinator invokes step() in sequence
- Allows "classification-only" or "manual feed" modes

### 5. Multi-Worker Perception

Vision runs in separate thread pool:
- Capture threads per camera
- ML worker threads per channel
- Coordinator queries cached latest results (10 Hz update rate)
- Prevents slow inference from blocking stepper moves

### 6. Sorting Profile as Data

Sorting rules are JSON, not compiled:
- Load at startup
- Hot-reload via API
- Enables A/B testing and rule updates without restart

## Deployment Models

### Local Development
```
Orange Pi 5 (localhost)
  ├─ Backend main.py (:8000)
  ├─ Frontend npm dev (:5173)
  └─ Picos + Cameras (USB connected)
```

### SorterOS (Production)
```
Orange Pi 5 with SorterOS image
  ├─ Backend (systemd service)
  ├─ Frontend (nginx :80)
  └─ Auto-start on boot
```

### Cloud Integration
```
Machine → Hive (HTTPS)
  ├─ Upload samples
  ├─ Check model versions
  └─ Download trained models
```

## Error Handling & Resilience

| Failure Mode | Detection | Recovery |
|---|---|---|
| Stepper stall | StallGuard flag | Pause, operator Home |
| Motor timeout | Serial timeout | Retry 3×, then error |
| Servo offline | Position feedback fails | Pause, auto-recover on reconnect |
| Model missing | Load fails | Fallback to previous version |
| Perception crash | Thread exception | Vision disabled, alert operator |
| Camera disconnect | Capture timeout | Graceful degradation |
| Network (Hive) | Sync timeout | Retry with exponential backoff |

## Technology Choices

| Component | Choice | Why |
|---|---|---|
| **Backend** | Python + FastAPI | Fast iteration, rich ML ecosystem, easy prototyping |
| **Frontend** | SvelteKit | Reactive, fast, server-side rendering |
| **Firmware** | C (Pico SDK) | Direct hardware control, minimal overhead |
| **Vision** | OpenCV + YOLO | Community support, flexible backends (CPU/GPU/NPU) |
| **Database** | SQLite (machine) + PostgreSQL (cloud) | Simplicity + ACID compliance |
| **Communication** | HTTP/WebSocket | Firewall-friendly, browser-native |
| **Hardware** | Raspberry Pi Pico | Low cost, USB, UART, GPIO, easy to program |
| **ML Framework** | PyTorch + YOLO v8 | State-of-the-art, mobile-friendly export |

## Known Limitations & Trade-offs

### Windows Support
- No fcntl (process guard falls back to PID-only)
- No v4l2-ctl (camera format negotiation unavailable)
- Multiple USB 2.0 cameras may saturate bandwidth
- Recommended: Linux for production, Windows for development/testing only

### Firmware State
- All state lives in backend (Picos are stateless)
- Firmware restart requires backend reconfiguration
- No persistent configuration on Picos (simplifies firmware)

### Perception Latency
- Vision runs asynchronously (10 Hz update rate)
- Coordinator queries cache, not latest frame
- Accepts slight staleness for low-latency control loop

### Scaling
- Single Orange Pi 5 bottleneck for ML inference
- Hailo-8 NPU helps but still limited to ~30 FPS at 4K
- For higher throughput: multiple machines, ensemble models

## Documentation Structure

```
docs/
├── ARCHITECTURE.md              ← YOU ARE HERE
├── src/content/
│   ├── hardware/
│   │   ├── assembly/           # Build guides
│   │   ├── parts/              # Part list, connector pinouts
│   │   └── cad/                # 3D CAD models
│   ├── software/
│   │   ├── api/                # Backend REST API reference
│   │   ├── configuration/      # machine.toml guide
│   │   └── troubleshooting/    # Common issues
│   ├── deployment/             # SorterOS image, setup
│   └── training/               # ML model training guide
└── [AGENTS.md]                 # Agent build guidelines
```

## Glossary

| Term | Meaning |
|---|---|
| **C-channel** | Feed channel (C1, C2, C3 are the three main channels; C4 is the carousel) |
| **Feeder** | Subsystem for moving pieces C1→C2→C3 |
| **Carousel (C4)** | Rotating platform for classification platter |
| **Classification** | ML inference to identify piece material/color |
| **Distribution** | Subsystem for moving chute servo to correct bin |
| **Chute** | Movable slide that directs pieces into bins |
| **Bin** | Physical container for sorted pieces |
| **IRLConfig** | In-Real-Life Config — machine topology description |
| **Sorting Profile** | JSON rules mapping detected pieces → bins |
| **StallGuard** | TMC2209 feature that detects motor stalls via back-EMF |
| **RKNN** | Rockchip Neural Network (on-device ML runtime for Orange Pi) |
| **Hailo-8** | Dedicated vision accelerator (optional for RP5 AI HAT) |
| **HEF** | Hailo Executable Format (optimized model format) |

## Getting Started

1. **Setup Local Dev**: See [software/README.md](../software/README.md)
2. **Understand Backend**: Read [Backend Architecture](../software/sorter/backend/ARCHITECTURE.md)
3. **Build Hardware**: Follow [Electronics README](../electronics/README.md)
4. **Deploy to Machine**: See [SorterOS README](../software/sorteros/README.md)
5. **Train Models**: See [training/README.md](../training/README.md)

## Contributing

Architecture decisions and new subsystems:
1. Discuss in an issue before starting
2. Follow existing patterns (see [AGENTS.md](../AGENTS.md))
3. Document in appropriate ARCHITECTURE.md
4. Add tests and examples
