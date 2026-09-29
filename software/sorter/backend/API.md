# API & Data Model Reference

Reference documentation for the sorter backend API and data structures.

## REST API Overview

### Base URL
```
http://localhost:8000/api/
```

### Authentication
Most endpoints are local-only (no auth required). For cloud integration with Hive, use API keys stored in `.env`:
```
HIVE_API_KEY=<token>
```

## Endpoints

### System & Lifecycle

#### GET /config
Current machine configuration.

**Response**:
```json
{
  "machine_id": "uuid",
  "machine_name": "My Sorter",
  "hardware_topology": {
    "num_steppers": 5,
    "num_cameras": 3,
    "num_servo_layers": 1,
    "servos_per_layer": 6
  },
  "sorting_profile": {
    "name": "classic.json",
    "version": "1.0"
  },
  "detection_algorithm": "yolo_v8",
  "deployment": "localhost"
}
```

#### POST /lifecycle/resume
Start sorting (transition PAUSED → RUNNING).

**Response**: `{"status": "running"}`

#### POST /lifecycle/pause
Pause sorting (transition RUNNING → PAUSED).

**Response**: `{"status": "paused"}`

#### POST /lifecycle/reset
Emergency stop and home all motors.

**Response**: `{"status": "paused", "homed": true}`

---

### Cameras

#### GET /cameras
List assigned cameras with status.

**Response**:
```json
{
  "cameras": [
    {
      "role": "feeder",
      "device_id": 0,
      "available": true,
      "resolution": [1280, 720],
      "fps": 30,
      "algorithm": "mog2"
    },
    {
      "role": "carousel",
      "device_id": 1,
      "available": true,
      "resolution": [2560, 1440],
      "fps": 30,
      "algorithm": "yolo_v8"
    }
  ]
}
```

#### GET /cameras/{role}/stream.mjpg
MJPEG stream for live preview.

**Parameters**:
- `role` — "feeder", "carousel", "c_channel_2", "classification_bottom", etc.

**Response**: MJPEG video stream (Content-Type: multipart/x-mixed-replace)

#### POST /cameras/{role}/assign
Assign OpenCV device to a role.

**Request**:
```json
{
  "device_id": 0,
  "format": "auto"  // or "mjpeg", "yuyv"
}
```

**Response**: `{"role": "feeder", "device_id": 0, "assigned": true}`

#### GET /cameras/{role}/calibration
Reference image for camera calibration.

**Response**: JPEG image (last captured frame at C position)

#### POST /cameras/{role}/calibration/capture
Save current frame as reference.

**Response**: `{"capture_timestamp": "2025-09-29T12:34:56Z"}`

---

### Detection & Models

#### GET /models
Available detection models.

**Response**:
```json
{
  "models": [
    {
      "id": "yolo_v8_100k",
      "name": "YOLO v8 (100k images)",
      "algorithm": "yolo_v8",
      "input_size": 640,
      "file_size_mb": 45,
      "fps_rp5": 15,
      "mAP": 0.92,
      "deployed": true,
      "deployed_at": "2025-09-01T00:00:00Z"
    },
    {
      "id": "yolo_v8_50k",
      "name": "YOLO v8 (50k images)",
      "algorithm": "yolo_v8",
      "input_size": 320,
      "file_size_mb": 25,
      "fps_rp5": 25,
      "mAP": 0.88,
      "deployed": false
    }
  ]
}
```

#### POST /models/{id}/deploy
Switch active detection model.

**Response**: `{"deployed_model_id": "yolo_v8_100k", "effective_at": "next_start"}`

#### GET /models/{id}/download
Download model artifact.

**Response**: Binary ONNX or HEF file (Content-Type: application/octet-stream)

---

### Pieces & Sorting Results

#### GET /pieces
Recent sorting history.

**Parameters**:
- `limit` — Default 100, max 1000
- `offset` — For pagination
- `bin` — Filter by target bin
- `mold` — Filter by detected mold
- `status` — "sorted", "failed", "flagged"

**Response**:
```json
{
  "pieces": [
    {
      "id": "uuid",
      "detected_at": "2025-09-29T12:34:56Z",
      "mold": "brick-1x2",
      "color": "red",
      "material": "plastic",
      "confidence": 0.92,
      "target_bin": 2,
      "status": "sorted",
      "sorted_at": "2025-09-29T12:34:57Z"
    }
  ],
  "total": 1234
}
```

#### GET /pieces/{id}
Detailed piece record.

**Response**:
```json
{
  "id": "uuid",
  "feeder_crops": [
    {
      "timestamp": "2025-09-29T12:34:54Z",
      "image_base64": "..."
    }
  ],
  "carousel_crop": {
    "timestamp": "2025-09-29T12:34:56Z",
    "image_base64": "..."
  },
  "detected_mold": "brick-1x2",
  "confidence": 0.92,
  "target_bin": 2,
  "metadata": {}
}
```

#### GET /pieces/{id}/image
Get detected crop image.

**Parameters**:
- `role` — "feeder", "carousel", "classification"

**Response**: JPEG image

#### POST /pieces/{id}/flag
Mark piece for manual review.

**Request**:
```json
{
  "reason": "wrong_bin",
  "notes": "Should be in bin 4"
}
```

**Response**: `{"flagged": true, "reason": "wrong_bin"}`

---

### Statistics & Telemetry

#### GET /runtime-stats
Current machine state and performance.

**Response**:
```json
{
  "lifecycle": "RUNNING",
  "uptime_s": 3600,
  "pieces_sorted": 1234,
  "pieces_per_minute": 20.5,
  "error_rate": 0.02,
  "current_piece": {
    "id": "uuid",
    "mold": "brick-1x2",
    "confidence": 0.92,
    "target_bin": 2,
    "estimated_time_to_bin_s": 2.3
  },
  "performance": {
    "mean_cycle_ms": 10.2,
    "p99_cycle_ms": 22.5,
    "feeder_fps": 30,
    "carousel_fps": 30,
    "model_inference_ms": 15.3
  },
  "temperature": {
    "cpu_c": 42,
    "npu_c": 38
  },
  "hardware_status": "healthy"
}
```

#### WebSocket: /api/stream
Real-time updates (WebSocket, not HTTP).

**Connection**:
```
ws://localhost:8000/api/stream
```

**Messages** (from server):
```json
{
  "type": "runtime-stats",
  "data": { /* runtime stats */ }
}
```

```json
{
  "type": "piece-sorted",
  "data": {
    "id": "uuid",
    "mold": "brick-1x2",
    "bin": 2,
    "timestamp": "2025-09-29T12:34:57Z"
  }
}
```

```json
{
  "type": "error",
  "data": {
    "title": "Servo bus offline",
    "message": "Check USB and power.",
    "duration_s": 0
  }
}
```

---

### Configuration

#### PATCH /config
Update machine configuration (requires restart to apply).

**Request**:
```json
{
  "stepper_current_overrides": {
    "carousel": {
      "irun": 8,
      "ihold": 1
    }
  },
  "servo_settings": {
    "center_us": 1500,
    "min_us": 1000,
    "max_us": 2000
  }
}
```

**Response**: `{"config_updated": true, "restart_required": true}`

#### POST /calibration/feeder
Capture feeder camera calibration image.

**Request**:
```json
{
  "position": "c1"  // or "c2", "c3"
}
```

**Response**: `{"captured": true, "timestamp": "2025-09-29T12:34:56Z"}`

---

## Data Models (Python Dataclasses)

### IRLConfig

```python
@dataclass
class IRLConfig:
  machine_id: str
  machine_name: str
  
  # Hardware topology
  feeder_stepper_count: int  # 1, 2, or 3
  has_carousel: bool
  has_chute: bool
  
  # Cameras
  camera_layout: str  # "default", "split_feeder"
  cameras: dict[str, int | str]  # role → device_id or URL
  
  # Servo distribution
  servo_layers: int
  servos_per_layer: int
  servo_settings: ServoSettings
  
  # Sorting profile
  sorting_profile_path: str
  
  # Detection algorithm
  detection_algorithm: str  # "yolo_v8", "gemini", etc.
```

### KnownObject

```python
@dataclass
class KnownObject:
  id: str  # UUID
  
  # Detection
  detected_mold: str  # e.g., "brick-1x2-red"
  confidence: float  # 0.0-1.0
  
  # Routing
  target_bin: int
  
  # Status
  status: str  # "detected", "sorted", "failed"
  
  # Timeline
  detected_at: datetime
  sorted_at: datetime | None
  
  # Metadata
  metadata: dict
```

### RuntimeStats

```python
@dataclass
class RuntimeStats:
  lifecycle: str  # "INITIALIZING", "PAUSED", "RUNNING", "ERROR"
  uptime_s: float
  pieces_sorted: int
  error_rate: float
  current_piece: KnownObject | None
  
  performance: PerformanceMetrics
  temperature: TemperatureStatus
  hardware_status: str  # "healthy", "warning", "error"
```

---

## Error Responses

### 4xx Client Errors

```json
{
  "error": "invalid_camera_role",
  "message": "Role 'feeder' is not configured",
  "detail": "Available roles: carousel, classification_top"
}
```

### 5xx Server Errors

```json
{
  "error": "hardware_fault",
  "message": "MCU bus timeout",
  "title": "Control board link lost",
  "recovery": "Home the machine to reconnect"
}
```

---

## Sorting Profile Format

JSON file that maps detected pieces to bins.

**Example** (`sorting_profiles/classic.json`):
```json
{
  "version": "1.0",
  "description": "Sort LEGO bricks by color",
  "rules": [
    {
      "condition": {
        "mold": "brick-1x2",
        "color": "red"
      },
      "target_bin": 0
    },
    {
      "condition": {
        "mold": "brick-1x2",
        "color": "blue"
      },
      "target_bin": 1
    },
    {
      "condition": {
        "mold": "*",
        "color": "*"
      },
      "target_bin": 2,
      "priority": 99  // Catch-all, lowest priority
    }
  ]
}
```

**Matching**:
- Rules evaluated top-to-bottom
- First match wins
- Wildcards (`*`) match any value
- If no match, piece goes to default bin (usually 0)

---

## WebSocket Message Types

### Client → Server

```json
{"cmd": "pause"}
{"cmd": "resume"}
{"cmd": "reset"}
{"cmd": "get_runtime_stats"}
{"cmd": "flag_piece", "piece_id": "uuid", "reason": "wrong_bin"}
```

### Server → Client

| Type | When | Payload |
|------|------|---------|
| `runtime-stats` | Every 500ms | RuntimeStats |
| `piece-sorted` | When piece lands in bin | KnownObject |
| `piece-flagged` | When operator marks piece | {piece_id, reason} |
| `error` | Hardware fault | {title, message, duration_s} |
| `state-change` | Lifecycle transition | {from, to} |
| `model-deployed` | Model switch | {model_id, deployed_at} |

---

## Rate Limiting & Performance

### API Rate Limits

- GET requests: 1000 req/min (local-only, not enforced)
- POST requests: 100 req/min
- WebSocket: No rate limit (single connection)

### Timeouts

- API endpoint response: 30s
- Hardware command: 100ms (retried 3×)
- Model inference: 60s (logged as warning, continues)

---

## Backward Compatibility

API versioning via content negotiation:

```
Accept: application/vnd.sorter+json;version=1
```

Current version: **v1** (no version required yet)

Future versions will support `/api/v2/` paths while maintaining `/api/` for v1.
