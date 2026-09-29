# Hive Architecture

This document describes the cloud-based training data platform.

## System Overview

Hive is a collaborative platform for collecting, curating, and training machine learning models for LEGO sorting.

**Key Goals**:
1. Centralized dataset repository across multiple machines
2. Model versioning and deployment
3. Benchmarking and performance tracking
4. Community contributions (optional)

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                          Hive Cloud                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌────────────────────────────────────────────────────────┐    │
│  │              Frontend (SvelteKit)                       │    │
│  │  • Dataset explorer, model manager, training dashboard  │    │
│  └────┬─────────────────────────────────────────────┬──────┘    │
│       │ HTTP                                        │            │
│  ┌────▼──────────────────────────────┬─────────────▼──────┐    │
│  │   FastAPI Backend (:8002)         │                    │    │
│  │  • Auth (GitHub OAuth)            │ PostgreSQL :5432  │    │
│  │  • Dataset ingestion/filtering    │                    │    │
│  │  • Model versioning               │ • Users, sessions  │    │
│  │  • Training job coordination      │ • Datasets, samples│    │
│  │  • Artifact storage (S3)          │ • Model versions   │    │
│  │                                   │ • Training jobs    │    │
│  └────┬──────────────────────────────┴─────────────┬──────┘    │
│       │ HTTPS                                      │            │
│       │ (samples, models)                          │            │
│  ┌────▼────────────────────────────────────────────▼──────┐    │
│  │  Object Storage (S3 / S3-compatible)                   │    │
│  │  • crops/ — Sample images (labeled)                    │    │
│  │  • models/ — ONNX, PyTorch, HEF artifacts             │    │
│  │  • datasets/ — Curated collections with metadata       │    │
│  └───────────────────────────────────────────────────────┘    │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
         ▲                                      ▲
         │                                      │
    ┌────┴────────┐                   ┌────────┴──────┐
    │  Machine    │◄──────────────────│   Hive UI     │
    │  Backend    │ Model download    │   (Browser)   │
    │             │                   │               │
    │ • Uploads   │ Training status   │ • Configure   │
    │   crops    │ Model artifacts   │   sync        │
    │ • Pulls     │                   │ • Search data │
    │   models   │                   │ • Review      │
    │             │                   │   samples     │
    └─────────────┘                   └───────────────┘
```

## Backend Components

### Directory Structure

```
hive/
├── backend/
│   ├── main.py              # FastAPI app setup
│   ├── models.py            # SQLAlchemy ORM models
│   ├── schemas.py           # Pydantic request/response schemas
│   ├── security.py          # Auth and user context
│   │
│   ├── routers/
│   │   ├── auth.py          # GitHub OAuth flow
│   │   ├── datasets.py      # Dataset CRUD + filtering
│   │   ├── samples.py       # Sample image upload/download
│   │   ├── models.py        # Model artifact versioning
│   │   ├── training.py      # Training job submission
│   │   └── benchmarks.py    # Performance comparisons
│   │
│   ├── db/
│   │   ├── connection.py    # PostgreSQL session management
│   │   └── migrations/      # Alembic migrations
│   │
│   ├── storage/
│   │   ├── s3.py            # S3 client wrapper
│   │   ├── local.py         # Local file storage (dev only)
│   │   └── upload.py        # Signed upload URL generation
│   │
│   ├── training/
│   │   ├── job_queue.py     # Training job scheduler
│   │   ├── worker.py        # Local GPU worker (optional)
│   │   └── estimators/      # Trainer implementations
│   │       ├── yolo_v8.py
│   │       └── gemini_vision.py
│   │
│   └── services/
│       ├── dataset_service.py   # Dataset aggregation
│       ├── model_service.py     # Model versioning
│       └── sync_service.py      # Machine ↔ Hive coordination
│
├── frontend/
│   ├── routes/
│   │   ├── +page.svelte     # Home (latest datasets)
│   │   ├── datasets/        # Dataset explorer
│   │   ├── models/          # Model manager
│   │   ├── training/        # Training job dashboard
│   │   └── admin/           # Dataset review (admins only)
│   │
│   └── lib/
│       ├── stores/          # SvelteKit stores (auth, datasets)
│       └── components/
│
├── docker-compose.yml       # Local dev (Postgres + Hive)
├── docker-compose.prod.yml  # Production (behind Traefik)
├── Dockerfile.backend
├── Dockerfile.frontend
├── Makefile                 # Build/deploy targets
└── README.md
```

### Data Models (SQLAlchemy)

```python
class User(Base):
  id: int
  email: str
  github_id: int
  created_at: datetime
  
class Machine(Base):
  id: int
  machine_id: str  # UUID from backend local_state.py
  owner_id: int
  last_sync: datetime
  metadata: dict  # Machine name, sorter model, etc.

class Sample(Base):
  id: int
  machine_id: int
  uuid: str
  captured_at: datetime
  role: str  # "feeder", "classification", etc.
  image_hash: str
  detected_mold: str | None
  
class Dataset(Base):
  id: int
  owner_id: int
  name: str
  description: str
  filters: dict  # {"mold": "brick-1x2", "quality": "verified"}
  sample_ids: list[int]
  
class ModelArtifact(Base):
  id: int
  name: str
  version: str
  algorithm: str  # "yolo_v8", "gemini", etc.
  s3_path: str
  input_shape: tuple
  output_format: str
  metrics: dict  # {"mAP": 0.92, "fps_rp5": 15}
  
class TrainingJob(Base):
  id: int
  creator_id: int
  dataset_id: int
  algorithm: str
  config: dict
  status: str  # "queued", "running", "completed", "failed"
  created_artifact_id: int | None
  logs: str
```

### REST API

**Authentication** (`/api/auth/`):
- `GET /auth/github` — OAuth flow start
- `GET /auth/github/callback` — OAuth callback (GitHub → token)
- `POST /auth/logout` — Clear session
- `GET /auth/me` — Current user + permissions

**Datasets** (`/api/datasets/`):
- `GET /` — List public datasets (paginated, filtered)
- `GET /{id}` — Dataset details + samples list
- `POST /` — Create new dataset (auth required)
- `PATCH /{id}` — Update filters or metadata (owner only)
- `POST /{id}/samples` — Add samples to dataset
- `DELETE /{id}` — Delete dataset (owner only)

**Samples** (`/api/samples/`):
- `GET /` — List samples (filtered by dataset, mold, quality)
- `GET /{id}/image` — Download original image
- `POST /` — Upload new sample (from machine)
- `PATCH /{id}` — Update label/flags (reviewer only)
- `DELETE /{id}` — Remove sample

**Models** (`/api/models/`):
- `GET /` — List available model versions
- `GET /{id}` — Model metadata + performance metrics
- `POST /` — Upload new model artifact
- `GET /{id}/download` — Download model (signed URL)
- `PATCH /{id}/deploy` — Mark as default for machines

**Training Jobs** (`/api/training/`):
- `POST /` — Submit training job
- `GET /jobs` — List jobs (paginated)
- `GET /jobs/{id}` — Job status + logs
- `POST /jobs/{id}/cancel` — Cancel queued job
- `GET /jobs/{id}/artifact` — Download trained model

## Integration Flow: Machine ↔ Hive

### Sample Upload (Machine → Hive)

1. **Capture Phase** (Backend control loop)
   ```python
   # In perception.py or sample_ingest.py
   crop = {
     "image": cv2.imwrite_jpg(detection_region),
     "role": "feeder",
     "machine_id": LOCAL_MACHINE_ID,
     "detected_mold": "brick-1x2",
     "confidence": 0.92,
     "timestamp": time.time()
   }
   LOCAL_QUEUE.put(crop)
   ```

2. **Async Upload** (Background thread)
   ```python
   # hive_uploader.py (in backend)
   while True:
     batch = collect_samples(batch_size=100)
     response = requests.post(
       f"{HIVE_URL}/api/samples/",
       files={"samples": batch},
       headers={"Authorization": f"Bearer {API_KEY}"}
     )
   ```

3. **Hive Storage** (Backend)
   ```python
   @router.post("/samples/")
   async def upload_samples(samples: UploadFile, user: User):
     for sample in samples:
       s3.put_object(
         Bucket="sorter-data",
         Key=f"crops/{user.id}/{uuid.uuid4()}.jpg",
         Body=sample.file
       )
       db.create(Sample, ...)
   ```

### Model Download (Hive → Machine)

1. **List Available Models** (Frontend)
   ```
   GET /api/models/ → [
     { id: 1, name: "yolo-v8-100k", version: "2025-09-01", fps_rp5: 15 },
     { id: 2, name: "yolo-v8-50k", version: "2025-08-15", fps_rp5: 25 },
   ]
   ```

2. **Machine Backend Polls** (Periodic check)
   ```python
   # In hive_models.py
   def sync_available_models():
     response = requests.get(
       f"{HIVE_URL}/api/models/",
       params={"algorithm": "yolo_v8", "target": "rp5"}
     )
     # Update local model registry
   ```

3. **Download on Demand**
   ```python
   def download_model(model_id: int, target_path: str):
     url = requests.get(f"{HIVE_URL}/api/models/{model_id}/download").signed_url
     response = requests.get(url, stream=True)
     with open(target_path, "wb") as f:
       f.write(response.content)
   ```

4. **Deploy** (UI or automatic)
   ```python
   @router.post("/models/{id}/deploy")
   async def deploy_model(id: int, user: User):
     machine = db.get(Machine, user.id)
     machine.active_model_id = id
     db.commit()
     # Next model inference uses new weights
   ```

## Training Pipeline

### Local Training (Backend GPU)

```python
# routers/training.py
@router.post("/training/jobs")
async def submit_job(job: TrainingJobRequest, user: User):
  # 1. Load dataset
  dataset = db.get(Dataset, job.dataset_id)
  samples = db.query(Sample).filter(Sample.id.in_(dataset.sample_ids)).all()
  
  # 2. Validate & download samples from S3
  training_data = []
  for sample in samples:
    img = s3.get_object(...).read()
    training_data.append((img, sample.detected_mold))
  
  # 3. Queue training job
  job_record = db.create(TrainingJob, status="queued", ...)
  TRAINING_QUEUE.put((job_record.id, training_data, job.algorithm))
  
  # 4. Return job ID
  return {"job_id": job_record.id}

# training/worker.py (separate process)
def training_worker():
  while True:
    job_id, data, algorithm = TRAINING_QUEUE.get()
    
    if algorithm == "yolo_v8":
      trainer = YOLOv8Trainer(data, epochs=100)
      model_path = trainer.train()
    
    artifact = upload_artifact(model_path)
    db.update(TrainingJob, status="completed", created_artifact_id=artifact.id)
```

### Remote Training (Third-party GPU)

For larger datasets or specialized hardware:
1. Machine uploads samples to Hive
2. External training service (Lambda, Paperspace) polls Hive for training jobs
3. Trainer runs YOLO/custom model, uploads artifact back to Hive
4. Machine downloads trained model on next sync

## Frontend Features

### Dashboard (`/`)
- Latest datasets added by community
- Top models (by benchmark score)
- Recent training completions
- Machine sync status (if logged in)

### Dataset Explorer (`/datasets/`)
- Filter by:
  - Mold type (brick-1x2, slope-45, plate-1x4, etc.)
  - Quality (verified, unverified, flagged)
  - Date range
  - Source machine
- Bulk download samples (ZIP)
- Visualize data distribution (histogram of mold counts)

### Model Manager (`/models/`)
- Table of available models
  - Version, algorithm, upload date
  - Benchmark scores (mAP, FPS on RP5, file size)
  - Deployment status (active, staged, archived)
- Upload new model (admin only)
- Trigger retraining from latest dataset

### Training Dashboard (`/training/`)
- Submit training job
  - Select dataset
  - Choose algorithm (YOLO v8, Gemini, custom)
  - Set hyperparameters (epochs, batch size, learning rate)
- Monitor job progress
  - Status (queued, running, completed)
  - Live logs
  - ETA
- Download trained model on completion
- Compare metrics (this job vs previous versions)

### Admin Review (`/admin/`)
- Flag suspicious samples (bad crops, mislabeled)
- Bulk-update labels (for quality control)
- Merge duplicate molds
- Archive old datasets

## Deployment

### Development

```bash
docker compose up -d  # Postgres :5432 + Hive :8002, :5174

# Auto-run migrations on startup
make migrate

# Bootstrap admin user
make bootstrap-admin
```

### Production (Behind Traefik)

```bash
# 1. Set environment
export HIVE_BACKEND_IMAGE=ghcr.io/basicallysource/hive-backend:v0.1.0
export HIVE_FRONTEND_IMAGE=ghcr.io/basicallysource/hive-frontend:v0.1.0

# 2. Deploy
docker compose -f docker-compose.prod.yml up -d

# 3. Health check
curl https://hive.example.com/api/health
```

**Stack Setup**:
- Traefik reverse proxy
- Backend and Frontend in separate containers
- PostgreSQL persistent volume
- S3 bucket for artifacts

## Security

### Authentication
- GitHub OAuth for user login
- JWT tokens (short-lived: 15 min, refresh token: 30 days)
- API keys for machine backend

### Authorization
- Sample upload restricted to owner's machine
- Dataset edit restricted to creator + admins
- Model deployment restricted to admins
- Training job submission restricted to registered users

### Data Privacy
- All uploads encrypted in transit (HTTPS)
- At-rest encryption (S3 default)
- Audit log of all data access
- GDPR-compliant data deletion

## Monitoring & Logging

### Metrics
- Training job completion rate
- Average dataset size
- Model download frequency
- API response times

### Logging
- All training job logs persisted (for debugging)
- Machine sync errors logged per machine
- Sample upload failures tracked

### Alerting
- Training job failure → email creator
- Disk full on storage → alert admin
- API latency > 5s → alert ops
