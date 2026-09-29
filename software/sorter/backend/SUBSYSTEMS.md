# Subsystems & Data Flow Architecture

Deep dive into the feeder, classification, and distribution subsystems, plus cross-cutting concerns.

## Subsystem Overview

The sorter's core logic is split into three independent state machines that run sequentially in the coordinator:

```
Coordinator Loop (100 Hz):
  1. distribution.step()       # 1-5 ms
  2. classification.step()     # 5-20 ms (with ML inference)
  3. feeder.step()             # 1-3 ms
  
  Total: ~10-30 ms per cycle
```

Each subsystem communicates via:
- **Shared gates** (binary flow control)
- **Shared variables** (piece state, detection results)
- **TickBus** (publish-subscribe events)

## Feeder Subsystem

**Responsibility**: Move pieces from C1 → C2 → C3 (waiting for handoff to classification)

### Architecture

```
Feeder Camera (OV9732 720p)
  ↓ MOG2 background subtraction
  ↓ Detects piece vs empty tray
CameraService (VisionManager)
  ↓ Caches latest detection
Feeder State Machine
  ├─ IDLE: waiting for piece at C1
  ├─ ADVANCING: piece detected, pulse rotor
  ├─ WAITING_FOR_C3: piece should be at C3, watching for jam
  └─ STALLED: stall detected, awaiting operator intervention
```

### Control Flow

```python
def feeder_step():
  """Runs every ~10ms (part of 100ms control cycle)"""
  
  # 1. Query vision for piece position on C-channel
  detection = vision.latest_detections["feeder"]
  
  # 2. Interpret detection as piece position
  if detection.confidence > THRESHOLD:
    # Piece visible
    piece_x = detection.bbox_center_x  # 0-720 pixels
    piece_progress = piece_x / 720.0   # 0.0 = C1, 1.0 = C3
  else:
    piece_progress = None
  
  # 3. Decide next action based on state + piece position
  if self.state == IDLE:
    if piece_at_input:
      self.state = ADVANCING
      pulse_stepper()  # Move one step
  
  elif self.state == ADVANCING:
    if piece_stalled():
      self.state = STALLED
      shared.set_feeder_gate(False, reason="stalled")  # Pause flow
    elif piece_at_c3_output:
      self.state = IDLE
      shared.transport.hand_off_from_c3()  # Send to classification
  
  # 4. Monitor for anomalies
  if recent_stall_count > STALL_THRESHOLD:
    self.state = STALLED
    gc.runtime_stats.recordIncident("feeder_stall")
```

### State Machine Diagram

```
    ┌─────────────┐
    │    IDLE     │◄─────────────────────────┐
    └──────┬──────┘                           │
           │ piece_at_input                   │
           ↓                                  │
    ┌──────────────┐                          │
    │  ADVANCING   │                          │
    └──────┬───────┘                          │
           │                                  │
           ├─ stall? ──────→ STALLED          │
           │                   │              │
           │                   └──→ operator home
           │                        then IDLE
           │
           └─ piece_at_c3 ────→ hand_off
                               then IDLE
```

### Integration Points

- **Input**: Feeder camera MOG2 detection
- **Output**: Handoff signal to Classification (via `shared.transport`)
- **Gate Control**: Stops when classification gate closed (C3 output blocked)
- **Telemetry**: Stall count, cycle time, piece positions

## Classification Subsystem

**Responsibility**: Identify piece at C4 carousel, decide which bin it belongs to

### Architecture

```
Carousel Camera (IMX415 4K)
  ├─ MOG2: Detect piece presence on platter
  ├─ YOLO: Detect mold edges/faces
  └─ Optional: Cropped region for secondary classification
    ↓
VisionManager + PerceptionService
  ├─ Run inference (500ms default window)
  └─ Cache latest detection + confidence
    ↓
Classification State Machine
  ├─ IDLE: No piece on carousel
  ├─ PIECE_ARRIVED: Piece detected, start rotation/inference
  ├─ INFERENCING: ML model running
  ├─ DECISION_MADE: Inference complete, bin assigned
  └─ AWAITING_DISTRIBUTION: Waiting for distribution to catch up
```

### Control Flow

```python
def classification_step():
  """Runs every ~10ms"""
  
  # 1. Query carousel camera detection
  carousel_detection = vision.latest_detections["carousel"]
  
  # 2. Interpret detection
  if carousel_detection.confidence > THRESHOLD:
    piece_detected = True
    rotation_position = carousel_detection.centroid_x / 2560.0  # 0-1
  else:
    piece_detected = False
  
  # 3. Run state machine
  if self.state == IDLE:
    if piece_detected:
      self.state = PIECE_ARRIVED
      self.piece_id = uuid.uuid4()
      self.inference_start_time = time.time()
  
  elif self.state == PIECE_ARRIVED:
    # Optionally rotate carousel for better angle
    rotate_carousel_to_inspection_position()
    self.state = INFERENCING
  
  elif self.state == INFERENCING:
    # Wait for ML result
    if latest_inference_available:
      result = vision.get_classification("carousel")
      self.detected_mold = result.mold
      self.confidence = result.confidence
      
      # Query sorting profile for bin
      self.target_bin = sorting_profile.get_bin(self.detected_mold)
      
      self.state = DECISION_MADE
  
  elif self.state == DECISION_MADE:
    # Wait for distribution to be ready (servo moved)
    if shared.distribution_gate_open and distribution.servo_ready:
      self.state = AWAITING_DISTRIBUTION
      shared.transport.release_to_distribution(
        piece_id=self.piece_id,
        detected_mold=self.detected_mold,
        confidence=self.confidence,
        target_bin=self.target_bin
      )
  
  elif self.state == AWAITING_DISTRIBUTION:
    # Wait for distribution to finish
    if shared.transport.distribution_complete:
      self.state = IDLE
```

### State Machine Diagram

```
      ┌──────────┐
      │  IDLE    │◄──────────────────────────────────┐
      └────┬─────┘                                    │
           │ piece_detected                           │
           ↓                                          │
      ┌──────────────┐                                │
      │ PIECE_ARRIVED│                                │
      └────┬─────────┘                                │
           │ rotate_carousel                          │
           ↓                                          │
      ┌──────────────┐                                │
      │INFERENCING   │                                │
      └────┬─────────┘                                │
           │ inference_done                           │
           ↓                                          │
      ┌──────────────┐                                │
      │DECISION_MADE │                                │
      └────┬─────────┘                                │
           │ servo_ready                              │
           ↓                                          │
      ┌─────────────────────┐                         │
      │AWAITING_DISTRIBUTION│                         │
      └────┬────────────────┘                         │
           │ distribution_complete                    │
           └─────────────────────→ IDLE
```

### ML Inference Details

**Inference Chain** (per piece):
1. Capture carousel frame when piece at best angle
2. Extract region of interest (ROI) using MOG2 contour
3. Send to YOLO v8 model (detection task)
4. Parse output: class ID, confidence, bbox
5. Map class ID → mold type (e.g., "brick-1x2-red")
6. Look up in sorting profile → target bin

**Fallback**:
- If YOLO fails → try Hailo-8 (if available)
- If Hailo-8 fails → try Gemini Vision (cloud, slow)
- If all fail → Manual bin or "unknown" bin

**Confidence Filtering**:
- If confidence < MIN_THRESHOLD (e.g., 0.7) → flag as uncertain
- Operator can review flagged pieces later
- Option to retrain on high-uncertainty crops

### Incident Handling

**C4 Stall Watchdog**:
- If piece doesn't reach classification within timeout (5s)
- Trigger incident: "piece_stuck_at_c3"
- Pause gates and wait for operator
- Classification can still step to attempt recovery (e.g., jog feeder)

## Distribution Subsystem

**Responsibility**: Position chute servo to target bin, release piece

### Architecture

```
Sorting Profile (JSON rules)
  ├─ Maps mold_id → bin_index
  └─ Bin geometry (servo angle for each bin)
    ↓
Classification.target_bin → Distribution.target_servo_position
    ↓
Distribution State Machine
  ├─ IDLE: No piece, servo in park position
  ├─ POSITIONING: Servo moving to target
  ├─ PIECE_READY: Piece arriving from C3
  ├─ RELEASING: Piece sliding down chute
  └─ COMPLETE: Piece in bin, reset for next
```

### Servo Mapping

**Bin → Servo Angle**:
```
Bin 0: servo_center - 2 * bin_spacing = 1300 μs
Bin 1: servo_center - 1 * bin_spacing = 1400 μs
Bin 2: servo_center                   = 1500 μs (center)
Bin 3: servo_center + 1 * bin_spacing = 1600 μs
Bin 4: servo_center + 2 * bin_spacing = 1700 μs
...
```

Where `bin_spacing` is calculated from `servo_min`/`max` and `bin_count`.

### Control Flow

```python
def distribution_step():
  """Runs every ~10ms"""
  
  # 1. Get current target from feeder
  if self.state == IDLE and shared.transport.piece_ready:
    self.target_bin = shared.transport.target_bin
    self.target_servo_angle = servo_angle_for_bin(self.target_bin)
    self.state = POSITIONING
  
  # 2. Move servo toward target
  if self.state == POSITIONING:
    current_angle = servo.current_position
    error = self.target_servo_angle - current_angle
    
    if abs(error) < ANGLE_TOLERANCE:
      self.state = PIECE_READY
    else:
      servo.set_position(current_angle + SERVO_SPEED * error)
  
  # 3. Detect piece arrival at chute
  if self.state == PIECE_READY:
    # Wait for feeder to release piece
    if piece_at_chute_exit_sensor():
      self.state = RELEASING
  
  # 4. Confirm piece in bin (optional, if exit sensor exists)
  if self.state == RELEASING:
    # Time the piece to reach bin
    sleep(CHUTE_TRANSIT_TIME)
    
    # Verify bin via weight sensor (optional)
    if confirmed_in_bin():
      bin_layout.increment_bin_count(self.target_bin)
      gc.runtime_stats.observePieceSorted(self.target_bin)
    
    self.state = COMPLETE
  
  # 5. Reset for next piece
  if self.state == COMPLETE:
    servo.move_to_park_position()
    shared.set_distribution_gate(True)  # Ready for next piece
    self.state = IDLE
```

### State Machine Diagram

```
    ┌──────────┐
    │  IDLE    │◄──────────────────────────┐
    └────┬─────┘                            │
         │ transport.piece_ready            │
         ↓                                  │
    ┌──────────────┐                        │
    │ POSITIONING  │                        │
    └────┬─────────┘                        │
         │ servo_aligned                    │
         ↓                                  │
    ┌──────────────┐                        │
    │ PIECE_READY  │                        │
    └────┬─────────┘                        │
         │ piece_at_chute                   │
         ↓                                  │
    ┌──────────────┐                        │
    │  RELEASING   │                        │
    └────┬─────────┘                        │
         │ transit_time_complete            │
         ↓                                  │
    ┌──────────────┐                        │
    │  COMPLETE    │                        │
    └────┬─────────┘                        │
         │ reset_servo                      │
         └────────────────────→ IDLE
```

## Shared State & Communication

### Piece Transport (`piece_transport.py`)

```python
class ClassificationChannelTransport:
  """Mediates handoffs between subsystems"""
  
  def __init__(self):
    self.piece_at_c3_output = False
    self.piece_at_classification = False
    self.piece_at_chute_input = False
    self.current_piece = None  # KnownObject
  
  def hand_off_from_c3(self, piece: KnownObject):
    """Feeder calls this when piece reaches C3 output"""
    self.piece_at_c3_output = True
    self.current_piece = piece
  
  def release_to_distribution(self, **piece_attrs):
    """Classification calls this when ready to sort"""
    self.piece_at_chute_input = True
    self.current_piece.update(**piece_attrs)
  
  def distribution_complete(self):
    """Distribution calls this after piece lands"""
    self.piece_at_chute_input = False
    self.current_piece = None
```

### Shared Gates

```python
class SharedVariables:
  """Flow control between subsystems"""
  
  def set_feeder_gate(self, open: bool, reason: str):
    """If closed, feeder stops moving pieces to C3"""
    self.feeder_gate_open = open
    self.feeder_gate_reason = reason
  
  def set_classification_gate(self, open: bool, reason: str):
    """If closed, pieces don't enter classification"""
    self.classification_gate_open = open
  
  def set_distribution_gate(self, open: bool, reason: str):
    """If closed, pieces don't enter distribution"""
    self.distribution_gate_open = open
```

**Use Cases**:
- Incident occurs → pause all gates → operator Home → resume
- Classification backlog → close feeder gate → prevent overflow
- Servo bus offline → close distribution gate → pieces wait at C3

### TickBus Events

```
TickBus (publish/subscribe within a tick)
  ├─ publish("piece_at_c3_output", piece_id)
  ├─ subscribe("c3_ready_to_hand_off", callback)
  │
  ├─ publish("classification_ready", target_bin)
  ├─ subscribe("piece_ready_for_distribution", callback)
  │
  ├─ publish("piece_in_bin", bin_id)
  └─ subscribe("distribution_complete", callback)
```

## Performance Profile

### Cycle Breakdown

Typical 100 Hz control loop (10 ms per tick):

```
Distribution step:      1-2 ms (servo position update)
Classification step:    5-20 ms (varies with inference)
  ├─ Vision query:      0.5 ms
  ├─ Inference cache:   ~0 ms (async result)
  └─ State machine:     0.1 ms
Feeder step:           1-3 ms
  ├─ Vision query:     0.5 ms
  ├─ Stepper command:  0.2 ms
  └─ State machine:    0.1 ms
────────────────────────
Total:                 8-25 ms
Budget:                10 ms (100 Hz)
```

**Headroom**: When inference is expensive (20 ms), loop stalls; backend logs warning and broadcasts to UI. Operator can increase cycle time in settings if needed.

### Inference Latency

```
YOLO v8 on Orange Pi 5 (RKNN accelerator):
  • 640×640 image: ~30-50 ms
  • 320×320 image: ~15-20 ms
  
Fallback (CPU PyTorch):
  • 640×640 image: ~500 ms
  • 320×320 image: ~100 ms

Hailo-8 (if available):
  • 640×640 image: ~5-10 ms
```

**Strategy**: Use 320×320 for real-time (15 FPS), larger for high-accuracy training labels.

## Error Recovery

### Stall Detection

1. **Detection**: TMC2209 StallGuard flag or timeout
2. **Action**: Coordinator pauses all gates, logs incident
3. **UI**: Red banner "Piece stalled at C2" with Home button
4. **Recovery**: 
   - Operator manually clears jam
   - Press Home → feeder homing sequence
   - Resume → gates reopen, sorting continues

### Servo Timeout

1. **Detection**: Servo doesn't reach target position in time
2. **Action**: Pause distribution gate
3. **UI**: "Servo bus offline" banner
4. **Recovery**:
   - Check servo bus USB and power
   - Press Resume → servo health check
   - If servo available → continue

### Classification Timeout

1. **Detection**: Piece sits at C3 for >5s (C4 stall watchdog)
2. **Action**: Incident recorded, gates paused
3. **UI**: "Piece stuck at C3" banner
4. **Recovery**:
   - Check classification camera and servo
   - Press Home → reset subsystems
   - Press Resume → restart

## Data Structures

### KnownObject (`defs/known_object.py`)

```python
@dataclass
class KnownObject:
  id: str  # UUID
  
  # Detection metadata
  feeder_detection: Detection  # MOG2 result
  carousel_detection: Detection  # YOLO result
  confidence: float  # ML confidence score
  
  # Classification result
  detected_mold: str  # e.g. "brick-1x2-red"
  color: str
  material: str
  
  # Routing
  target_bin: int
  sorted_at: datetime | None
  
  # Timeline
  created_at: datetime
  c3_arrival_time: datetime
  classification_complete_time: datetime | None
  distribution_complete_time: datetime | None
```

### Detection Result

```python
@dataclass
class Detection:
  confidence: float  # 0-1
  bbox: (x1, y1, x2, y2)  # pixels
  class_id: int
  class_name: str
```
