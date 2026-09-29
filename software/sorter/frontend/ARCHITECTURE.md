# Frontend Architecture

This document describes the SvelteKit-based web UI for the sorter machine.

## Technology Stack

- **Framework**: SvelteKit (server-side rendering + client hydration)
- **Language**: TypeScript
- **Styling**: Tailwind CSS
- **Build Tool**: Vite
- **State Management**: Svelte stores (reactive primitives)
- **Communication**: 
  - HTTP (for config, model management, calibration)
  - WebSocket (for real-time state, camera frames, piece stream)

## Directory Structure

```
frontend/
├── src/
│   ├── app.html              # Root HTML template
│   ├── app.d.ts              # TypeScript globals
│   ├── routes/
│   │   ├── +page.svelte      # Dashboard (real-time state)
│   │   ├── settings/
│   │   │   ├── +page.svelte  # Settings hub
│   │   │   ├── cameras/      # Camera assignment and calibration
│   │   │   ├── machine/      # Machine config (stepper current, servo range)
│   │   │   └── features/     # Feature flags and experiment toggles
│   │   ├── models/
│   │   │   ├── +page.svelte  # Model browser and downloader
│   │   │   └── training/     # Training job monitor
│   │   ├── profiles/         # Sorting profile editor
│   │   └── analysis/         # Data export and reporting
│   │
│   ├── lib/
│   │   ├── components/       # Reusable UI components
│   │   │   ├── CameraGrid.svelte       # Live camera tiles
│   │   │   ├── PieceStream.svelte      # Real-time piece feed
│   │   │   ├── StateIndicator.svelte   # Machine lifecycle indicator
│   │   │   ├── ChutePosition.svelte    # Chute servo visualization
│   │   │   └── PerformanceChart.svelte # Real-time graphs
│   │   │
│   │   ├── api/              # Backend API client
│   │   │   ├── client.ts     # Axios-based HTTP wrapper
│   │   │   ├── models/       # TypeScript interfaces for API responses
│   │   │   ├── mutations/    # POST/PATCH operations
│   │   │   └── queries/      # GET operations
│   │   │
│   │   ├── stores/           # Svelte reactive stores
│   │   │   ├── machine.ts    # Machine state (running, error, config)
│   │   │   ├── realtime.ts   # WebSocket feed (piece stream, stats)
│   │   │   ├── cameras.ts    # Camera registry and assignments
│   │   │   ├── models.ts     # Available ML models
│   │   │   └── user.ts       # UI state (selected camera, sidebar)
│   │   │
│   │   ├── utils/            # Helper functions
│   │   │   ├── format.ts     # Number/date formatting
│   │   │   ├── color.ts      # Color schemes for materials
│   │   │   └── geometry.ts   # Canvas drawing helpers
│   │   │
│   │   └── websocket.ts      # WebSocket connection management
│   │
│   └── static/
│       └── (images, fonts, favicons)
│
├── vite.config.ts            # Build configuration
├── svelte.config.js          # SvelteKit adapter config
├── tsconfig.json             # TypeScript settings
└── package.json              # Dependencies
```

## Data Flow

### 1. App Initialization

```
SvelteKit hydration:
  1. Load +page.svelte (Dashboard)
  2. Client-side hydration
  3. Connect WebSocket (App.svelte or onMount)
  4. Fetch initial state from GET /api/config
  5. Subscribe to real-time updates
```

### 2. Real-Time Updates

```
WebSocket feed (backend broadcasts every ~500ms):
{
  "type": "runtime-stats",
  "lifecycle": "RUNNING",
  "pieces_sorted": 1234,
  "current_piece": { ... },
  "performance": { "mean_cycle_ms": 3.2, "p99_ms": 8.5 },
  "temperature": { "cpu": 42, "npu": 38 }
}

Store subscription:
  realtime.ts listens → updates $realtime_stats store
  → Dashboard.svelte reactively re-renders metrics
```

### 3. User Interaction

```
User clicks "Assign Camera":
  1. Select device index from dropdown
  2. POST /api/cameras/{role}/assign with device_index
  3. Backend persists to machine_params.toml
  4. WebSocket broadcasts updated camera list
  5. UI shows "Camera assigned" toast
```

## Page Components

### Dashboard (`routes/+page.svelte`)

**Purpose**: Real-time machine state and monitoring

**Key Elements**:
- **Header**: Machine name, lifecycle state (play/pause/stop buttons), uptime
- **Camera Grid** (2x3): Live video tiles with detection overlays
  - Auto-hide disabled cameras
  - Click to fullscreen
  - Click cross to toggle recording
- **Piece Stream**: 10-piece carousel showing recent sorts
  - Piece image thumbnail
  - Detected material/color
  - Classification confidence (percentage bar)
  - Bin assignment (icon)
  - Timestamp
- **Performance Metrics**: Real-time graph
  - Mean cycle time (ms)
  - P99 latency
  - Detection FPS per channel
  - Model inference time

**State Subscription**:
```typescript
import { realtime_stats, machine_state } from '$lib/stores'

$: sorting_active = $machine_state.lifecycle === "RUNNING"
$: cycles_per_minute = $realtime_stats.pieces_sorted * 60 / $realtime_stats.uptime_s
```

### Settings → Cameras (`routes/settings/cameras/+page.svelte`)

**Purpose**: Assign and calibrate hardware cameras

**Workflow**:
1. List all available devices from GET /api/cameras/devices
2. Show unmapped devices with "Assign" button
3. Modal selector:
   - Choose role (feeder, c_channel_2, classification_bottom, etc.)
   - Test stream from selected device
   - Confirm assignment → POST /api/cameras/{role}/assign
4. For each assigned camera:
   - Live feed with crosshair overlay
   - Slider for brightness/contrast/exposure
   - Button to capture reference frame for calibration

**Implementation Pattern**:
```svelte
<script lang="ts">
  import { cameras_list, camera_assignments } from '$lib/stores'
  
  let assignment_modal_open = false
  let selected_device: number | null = null
  
  async function assign_camera() {
    await api.cameras.assign(selected_role, selected_device)
    cameras_list.invalidate()
    selected_device = null
  }
</script>

<div class="camera-grid">
  {#each $cameras_list as camera}
    <CameraPreview {camera} />
  {/each}
</div>
```

### Settings → Machine (`routes/settings/machine/+page.svelte`)

**Purpose**: Hardware tuning (stepper current, servo range, etc.)

**Fields**:
- Stepper current (IRUN, IHOLD) — Per motor, default vs override
- Stepper microsteps — 1, 2, 4, 8, 16
- Servo center position (μs) — 1000-2000
- Servo min/max (μs) — Dead-zone tuning
- Carousel home pin — GPIO channel selection
- Feature flags (experimental features)

**Persistence**:
- POST /api/config/machine with updated values
- Backend writes to machine.toml
- Requires restart to apply

### Models (`routes/models/+page.svelte`)

**Purpose**: Detection model management

**Features**:
- **Model Browser**: Table of available models
  - Name, version, task (detection, classification)
  - Size, latency (FPS on RP5)
  - Deployed status (active or staged)
- **Download Dialog**: 
  - List remote models from Hive
  - Download to local storage
  - Progress bar during download
  - Auto-extract and register
- **Training Status**:
  - Jobs in progress (backend logs)
  - Last 10 completed trainings (date, artifact hash, model count)
  - Link to Hive dashboard

### Sorting Profiles (`routes/profiles/+page.svelte`)

**Purpose**: Edit and preview sorting rules

**Editor**:
- Visual rule builder (drag-and-drop conditions → bin assignments)
- Or: JSON editor with schema validation
- Preview pane: Sample pieces with predicted bins
- Version history (git-like diff viewer)

**Save/Publish**:
- Save draft locally
- Publish → POST /api/profiles
- Backend reloads on next controller.step()
- Live effect (no restart)

## Store Architecture

### Machine Store (`lib/stores/machine.ts`)

```typescript
export interface MachineState {
  lifecycle: "INITIALIZING" | "PAUSED" | "RUNNING" | "ERROR"
  hardware_status: HardwareStatus | null
  config: IRLConfig
  updated_at: number
}

export const machine_state = writable<MachineState>(initial_state)
```

Subscriptions:
- GET /api/config on load
- WebSocket updates lifecycle and error state
- Manual refresh on Settings save

### Real-Time Store (`lib/stores/realtime.ts`)

```typescript
export interface RuntimeStats {
  lifecycle: string
  pieces_sorted: number
  pieces_errored: number
  current_piece: KnownObject | null
  performance: {
    mean_cycle_ms: number
    p99_cycle_ms: number
    per_channel_fps: Record<string, number>
    model_inference_ms: number
  }
  temperature: { cpu: number, npu: number }
}

export const realtime_stats = writable<RuntimeStats>(empty)
```

**Update Source**: WebSocket stream (backend broadcasts every 500ms)

### Cameras Store (`lib/stores/cameras.ts`)

```typescript
export interface CameraInfo {
  role: string           // "feeder", "c_channel_2", etc.
  device_id: number | string
  available: boolean
  resolution: [number, number]  // [width, height]
  fps: number
}

export const cameras_list = readable<CameraInfo[]>(
  fetch_cameras_on_mount(),
  (set) => {
    ws.on("cameras", set)  // WebSocket update
  }
)
```

## WebSocket Protocol

**Connection**: `ws://localhost:8000/api/stream`

**Messages** (from backend to UI):

```json
// Heartbeat
{"type": "ping"}

// Real-time stats (every 500ms when RUNNING)
{
  "type": "runtime-stats",
  "data": { "lifecycle": "RUNNING", "pieces_sorted": 100, ... }
}

// Piece detected
{
  "type": "piece-detected",
  "data": {
    "piece_id": "uuid",
    "detected_at": "2025-09-29T12:34:56Z",
    "role": "feeder",
    "material": "plastic",
    "color": "red",
    "confidence": 0.92,
    "image": "base64_jpeg"
  }
}

// Machine state change
{
  "type": "state-change",
  "data": { "from": "PAUSED", "to": "RUNNING" }
}

// Error banner
{
  "type": "error",
  "data": {
    "title": "Control board link lost",
    "message": "Home the machine to reconnect.",
    "duration_s": 0  // 0 = stay until resolved
  }
}
```

## HTTP Endpoints

### Read-Only (Queries)

- `GET /api/config` — Current machine configuration
- `GET /api/cameras/devices` — Available camera indices
- `GET /api/cameras/{role}` — Assigned camera details
- `GET /api/models` — Available detection models
- `GET /api/models/{id}/info` — Model metadata (latency, size)
- `GET /api/pieces?limit=100` — Recent piece history
- `GET /api/stats/summary` — Aggregated statistics

### Mutation (POST/PATCH)

- `POST /api/lifecycle/resume` — Start sorting
- `POST /api/lifecycle/pause` — Pause sorting
- `POST /api/lifecycle/reset` — Emergency stop + safe home
- `POST /api/cameras/{role}/assign` — Bind camera
- `PATCH /api/config` — Update machine.toml
- `POST /api/models/{id}/deploy` — Switch active model
- `POST /api/calibration/feeder` — Capture reference image
- `POST /api/profiles/publish` — Deploy sorting profile

## Component Patterns

### Reactive Forms

```svelte
<script lang="ts">
  import { machine_state } from '$lib/stores'
  import { api } from '$lib/api'
  
  let form_data = { irun: 4 }
  let saving = false
  
  async function submit() {
    saving = true
    try {
      await api.config.updateStepper("carousel", form_data)
      alert("Saved. Restart machine to apply.")
    } finally {
      saving = false
    }
  }
</script>

<form on:submit|preventDefault={submit}>
  <label>
    IRUN Current:
    <input type="number" bind:value={form_data.irun} min="1" max="31" />
  </label>
  <button type="submit" disabled={saving}>
    {saving ? "Saving..." : "Save"}
  </button>
</form>
```

### Camera Preview Grid

```svelte
<script lang="ts">
  import { cameras_list } from '$lib/stores'
</script>

<div class="grid grid-cols-3 gap-4">
  {#each $cameras_list as camera (camera.role)}
    {#if camera.available}
      <div class="camera-tile">
        <video
          src={`http://localhost:8000/api/cameras/${camera.role}/stream.mjpg`}
          autoplay
          muted
        />
        <div class="label">{camera.role}</div>
      </div>
    {/if}
  {/each}
</div>
```

### Real-Time Chart

```svelte
<script lang="ts">
  import { realtime_stats } from '$lib/stores'
  import { Chart, LineController } from 'chart.js'
  
  let canvas: HTMLCanvasElement
  let chart: Chart
  
  onMount(() => {
    chart = new Chart(canvas, { type: 'line', ... })
  })
  
  $: if (chart && $realtime_stats) {
    chart.data.datasets[0].data.push($realtime_stats.performance.mean_cycle_ms)
    chart.update('none')  // silent update
  }
</script>

<canvas bind:this={canvas} />
```

## Error Handling

### Network Errors

```typescript
// lib/api/client.ts
export const api = {
  async get(path: string) {
    try {
      const res = await fetch(`http://localhost:8000${path}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.json()
    } catch (err) {
      // Show toast or retry modal
      machine_state.update(s => ({ ...s, api_error: true }))
    }
  }
}
```

### WebSocket Reconnection

```typescript
// lib/websocket.ts
export function connect_websocket() {
  const ws = new WebSocket(`ws://localhost:8000/api/stream`)
  
  ws.onclose = () => {
    console.log("Disconnected, reconnecting in 3s...")
    setTimeout(connect_websocket, 3000)
  }
  
  ws.onerror = (err) => {
    machine_state.update(s => ({ ...s, api_error: true }))
  }
}
```

## Build & Deployment

### Development

```bash
npm install
npm run dev  # Vite dev server on :5173
```

### Production

```bash
npm run build              # Build to .svelte-kit/
npm run preview            # Test build locally
# SorterOS nginx reverse-proxies this
```

### Environment Variables

- `PUBLIC_API_BASE_URL` — Backend URL (default: same origin)
- `PUBLIC_WS_URL` — WebSocket URL (default: same origin, scheme: ws/wss)

## Testing

### Unit Tests (Vitest)
- Store logic (not yet in place)
- API client error handling
- Utility functions (formatting, etc.)

### Integration Tests (Playwright)
- Dashboard loads and streams data
- Camera assignment flow
- Model deployment workflow
