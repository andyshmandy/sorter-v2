# Backend Architecture

This document details the sorter backend implementation, organized by layer and responsibility.

## Directory Structure

```
backend/
├── main.py                  # Entry point, init, control loop
├── sorter_controller.py     # Lifecycle state machine
├── coordinator.py           # Real-time orchestration
│
├── hardware/                # MCU communication and device drivers
│   ├── bus.py              # Low-level serial protocol
│   ├── sorter_interface.py # Stepper/servo abstractions
│   ├── fault.py            # Error representations
│   ├── waveshare_bus_service.py  # Waveshare servo bus
│   ├── macos_uvc_controls.py     # macOS camera control
│   └── firmware_flash.py         # Pico bootloader detection
│
├── irl/                     # Machine configuration and hardware binding
│   ├── config.py           # Runtime dataclasses (IRLConfig, IRLInterface)
│   ├── parse_user_toml.py  # Load machine.toml
│   ├── leds.py             # Status LED control
│   └── bin_layout.py       # Chute geometry mapping
│
├── subsystems/              # Domain-specific state machines
│   ├── feeder/             # C-channel piece progression
│   ├── distribution/       # Chute positioning and piece targeting
│   ├── classification_channel/  # C4 carousel identification
│   ├── bus.py              # Subsystem event bus and ticking
│   └── shared_variables.py # Inter-subsystem communication
│
├── vision/                  # Camera and vision services
│   ├── camera_service.py   # Unified camera capture
│   ├── vision_manager.py   # ML inference worker management
│   └── detection_registry.py  # Algorithm selection and fallback
│
├── perception/              # RKNN/Hailo inference for Orange Pi 5
│   ├── service.py          # Worker pool and channel assignment
│   ├── tracking.py         # Object-level continuity across frames
│   └── (model-specific modules)
│
├── classification/          # Post-detection model selection
│   ├── providers.py        # Classification sources (YOLO, Gemini, etc.)
│   └── training.py         # Sample collection and labeling
│
├── server/                  # HTTP/WebSocket API
│   ├── api.py              # FastAPI app and CORS setup
│   ├── routers/            # Endpoint groups
│   │   ├── system.py       # Lifecycle, config, health
│   │   ├── cameras.py      # Camera control and calibration
│   │   ├── detection.py    # Model management and training
│   │   ├── pieces.py       # Piece history and filtering
│   │   └── status.py       # Real-time stats and telemetry
│   ├── shared_state.py     # Per-request controller/config access
│   ├── security.py         # CORS, auth, device identity
│   └── (model upload, training coordination)
│
├── global_config.py        # Singleton holding runtime state
├── sorter_controller.py    # Lifecycle state machine
├── coordinator.py          # Control loop orchestration
├── piece_transport.py      # Piece state through channels
│
├── (state and analytics)
│   ├── runtime_stats.py    # Real-time counters and telemetry
│   ├── runtime_stat_records.py
│   ├── lifetime_stats.py   # Cumulative machine stats
│   ├── run_recorder.py     # Per-run session data
│   └── incident_records.py # Error and exception logging
│
├── (storage and persistence)
│   ├── db.py               # SQLite connection management
│   ├── local_state.py      # Persistent KV store
│   ├── bin_contents.py     # Sorted piece inventory
│   ├── piece_image_store.py  # Detection crop storage
│   ├── channel_crop_store.py  # Training data
│   └── control_data_store.py  # Feeder dynamics data
│
├── (configuration and profiles)
│   ├── machine_toml.py     # Machine.toml parsing
│   ├── sorting_profiles/   # Sorting rule definitions
│   ├── sorting_profile.py  # Runtime rule loading
│   └── defs/               # Constants and enums
│
├── (logging and telemetry)
│   ├── logger.py           # Structured logging
│   ├── stepper_stall_monitor.py  # Stall detection alerting
│   ├── stepper_telemetry.py      # Motor diagnostics
│   ├── hive_telemetry.py         # Cloud sync state
│   └── message_queue/            # Async event handling
│
└── tests/                  # Unit and integration tests
```

## Initialization Flow

### Phase 1: Configuration Load (`main.py`)

1. Load `.env` and `machine.toml`
2. Parse IRLConfig (hardware topology, camera roles, stepper bindings)
3. Create GlobalConfig singleton with logger, registry, feature flags

### Phase 2: Hardware Discovery (`irl/config.py`)

1. Enumerate USB serial ports for Pico bootloaders
2. Scan each bus for SorterInterface firmware devices
3. Build `IRLInterface` — aggregate of all discovered motors, servos, cameras
4. Apply hardware bindings (e.g., firmware "c_channel_1_rotor" → logical "c_channel_2_rotor")
5. Validate topology (expected motor count, camera count, servo layers)

### Phase 3: Service Startup (`main.py`)

1. **Cameras**: Initialize CameraService, verify accessible
2. **Vision**: Spawn VisionManager thread and PerceptionService workers
3. **Servo bus** (if Waveshare): Open bus connection, probe servo availability
4. **Databases**: Open SQLite connections (local_state, piece records, etc.)
5. **Coordinator**: Instantiate with loaded sorting profile

### Phase 4: API Server

1. Mount FastAPI routers (cameras, detection, pieces, system)
2. Start uvicorn in background thread
3. Broadcast initial hardware state to UI

## Control Loop (100 Hz)

```python
# Simplified main.py loop
while backend_running:
    gc.control_loop_stats.mark_iteration_start()
    
    # Execute coordinator step
    controller.step()  # if RUNNING state
    
    # Poll server events
    handle_server_commands()
    
    # Observe performance and watchdog
    if loop_took_too_long():
        gc.logger.warning(f"Stall detected: {duration_ms}ms")
    
    # Rate limit to ~100 Hz
    sleep_until_tick()
```

### Coordinator Step Breakdown

```
coordinator.step():
  1. Check for active incidents (stalls, timeouts)
     ├─ If incident: pause gates, block piece flow
     └─ Else: proceed normally
  
  2. distribution.step()      # ~1-5 ms
     ├─ Poll servo position
     ├─ Check piece readiness at C3 output
     └─ Update chute servo target
  
  3. classification.step()     # ~5-20 ms (varies with inference)
     ├─ Query VisionManager for latest C4 detection
     ├─ If piece detected: run ML model
     ├─ Store classification result
     └─ Signal distribution when ready
  
  4. feeder.step()            # ~1-3 ms
     ├─ Poll feeder camera for piece position
     ├─ Pulse rotor if piece needs progression
     └─ Detect jams or stalls
```

## Hardware Abstraction

### Device Classes

**StepperMotor** (`hardware/sorter_interface.py`)
- Properties: speed, acceleration, hold_current, run_current, microsteps
- Methods:
  - `move_steps(n, speed, accel)` — Blocking step command
  - `move_at_speed(speed)` — Continuous motion until stop
  - `home()` — Move to limit switch + zero position
  - `set_stall_config(threshold)` — Configure StallGuard sensitivity
  - `get_stall_count()` — Recent stalls detected
  
**ServoMotor** (`machine_platform/servo_controller.py`)
- Abstraction over PCA9685 (PWM) or Waveshare UART servo
- Properties: center (1500 μs), min/max (1000-2000 μs)
- Methods:
  - `set_position(0-1)` — Normalized position [0=min, 0.5=center, 1=max]
  - `enable()` / `disable()` — Power control
  - `is_available()` — Health check

**DigitalPin** (`hardware/sorter_interface.py`)
- GPIO abstraction (limit switches, LED outputs)
- Properties: inverted polarity, pull-up/pull-down
- Methods:
  - `read()` → bool (for inputs)
  - `write(bool)` (for outputs)

### IRLInterface Lifecycle

```
IRLConfig (parsed from TOML)
  ↓
mkIRLInterface(config)  # hardware discovery
  ↓
IRLInterface
  ├── c_channel_1_rotor_stepper: StepperMotor
  ├── c_channel_2_rotor_stepper: StepperMotor
  ├── c_channel_3_rotor_stepper: StepperMotor
  ├── carousel_stepper: StepperMotor
  ├── chute_stepper: StepperMotor
  ├── servos: [ServoMotor, ServoMotor, ...]
  └── cameras: {name → Camera}
```

## Vision Pipeline

### Camera Discovery and Assignment

1. **Linux** (Raspberry Pi 5 with RKNN):
   - Enumerate `/dev/video*` devices
   - Query v4l2-ctl for format capabilities
   - Select MJPEG if available, else YUYV
   - Spawn capture thread per camera

2. **macOS** (Development):
   - Use AVFoundation backend (CAP_AVFOUNDATION)
   - Query supported formats via Objective-C bridge
   - Apply UVC controls if needed (zoom, exposure)

3. **Windows** (Testing only):
   - Generic OpenCV with no format negotiation
   - Default to camera driver defaults (usually YUYV)
   - Manual fallback to MJPEG stream URL if needed

### Detection Pipeline (PerceptionService)

```
Per-channel thread pool:
  camera_thread        ML_worker_thread
       ↓                    ↑
    Capture frame    Query latest frame
       ↓                    ↓
    YUYV → RGB       RKNN inference
       ↓                    ↓
   Store frame      Confidence + bbox
       ↓                    ↓
   coordinator queries cached result ← 10 Hz update rate
```

**Fallback chain**:
1. Try Hailo-8 NPU (Raspberry Pi 5 AI HAT)
2. Fallback to RKNN (on-chip Orange Pi 5 accelerator)
3. Fallback to CPU YOLO (slow but always available)
4. Fallback to MOG2 background subtraction (no ML)

## Subsystems

### TickBus (Event Coordination)

Subsystems communicate via publish/subscribe:

```
TickBus:
  begin_tick()
  publish("piece_at_c3_output", piece_data)  # feeder announces
  subscribe("c3_ready_to_hand_off", fn)      # classification waits
  publish("distribution_complete", ...)
  end_tick()
```

### State Machines

Each subsystem is a pure state machine:

```python
class FeederState(Enum):
    IDLE = "idle"
    WAITING_FOR_PIECE = "waiting"
    ADVANCING = "advancing"
    STALLED = "stalled"

class FeederStateMachine:
    def __init__(self, ...): self.state = IDLE
    def step(self):
        if self.state == WAITING_FOR_PIECE:
            if piece_detected:
                self.state = ADVANCING
                pulse_rotor()
        elif self.state == ADVANCING:
            if piece_reached_c3:
                self.state = IDLE
                publish_handoff()
```

Each subsystem is independent; the coordinator invokes `step()` in sequence and checks gates between steps.

## Configuration

### machine.toml Structure

```toml
[cameras]
layout = "split_feeder"  # or "default"
feeder = 0              # OpenCV device index
c_channel_2 = 1
c_channel_3 = 2
carousel = 3
classification_bottom = "http://127.0.0.1:18081/stream.mjpg"  # URL for MJPEG
classification_top = 4

[stepper_bindings]
# Optional: remap firmware names to logical roles
# Default assumes firmware reports: c_channel_1_rotor, c_channel_2_rotor, etc.
carousel = "c_channel_4_rotor"

[stepper_direction_inverts]
c_channel_2_rotor = false
carousel = true

[stepper_current_overrides]
[stepper_current_overrides.carousel]
irun = 8
ihold = 1
ihold_delay = 8

[carousel]
home_pin_channel = 2  # GPIO channel number on feeder Pico
home_pin_inverted = false

[distribution]
servo_layers = 1
servos_per_layer = 6
servo_center_us = 1500
servo_min_us = 1000
servo_max_us = 2000

[sorting_profile]
path = "sorting_profiles/classic.json"
```

### parse_user_toml.py

Loader functions:
- `loadIRLConfig(toml_path)` → IRLConfig dataclass
- `CameraLayoutConfig` — Enum for camera topology
- `DEFAULT_STEPPER_CURRENTS` — Dict mapping motor names to (IRUN, IHOLD, delay)
- Fallback handling for missing config sections

## Error Handling

### Hardware Fault Handling

When an MCUBusError occurs during sorting:

```python
try:
    controller.step()
except MCUBusError as e:
    controller.pause()
    gc.runtime_stats.setHardwareStatus(
        state="error",
        error=HardwareFault("Control board link lost", str(e))
    )
```

UI receives error via WebSocket and shows red banner. Operator clicks "Resume" → triggers Safe Home sequence → rediscovery → resume.

### Process Guard

Prevents multiple backend instances from running simultaneously:

```python
try:
    acquire_backend_process_guard()
except ProcessGuardError:
    print("Another instance already running")
    sys.exit(1)
```

On Linux: uses fcntl file lock. On Windows: falls back to PID-only check (no file lock).

## Testing

### Unit Tests (`tests/`)
- Motor driver abstractions
- State machine logic (no hardware)
- Configuration parsing

### Integration Tests
- Full coordinator loop with mock hardware
- Subsystem interactions
- Error recovery paths

### Hardware Tests (on-machine)
- Motor jog and stall detection
- Servo positioning
- Camera frame rate and latency
