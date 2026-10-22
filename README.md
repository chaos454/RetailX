# RetailX

**Smarter Stores. Real-Time Decisions.**

RetailX is a local-first retail intelligence prototype for SIH 26179. It includes inventory operations, persistent alert and movement history, report exports, and an optional real YOLO video pipeline. It starts without seeded operational data; the sample inventory is added only when a user chooses **Load sample dataset**.

This project uses a small dependency-free browser UI and Python standard-library HTTP/SQLite backend. Uploaded videos and the database remain on the local machine. The prototype is not connected to Supabase and contains no Supabase credentials or configuration.

## Requirements

- Windows 10/11, 64-bit
- Python 3.11 (3.10+ should work; 3.11 is the documented setup)
- Optional: internet access for package/model setup
- Optional: an NVIDIA GPU and a compatible PyTorch CUDA build for GPU inference. RetailX reports GPU availability only when PyTorch confirms it.

The dashboard, inventory, settings, reports and local SQLite database need only Python's standard library. Video inference is optional and needs the pinned packages in `requirements.txt`; the first video may download `yolo26n.pt` model weights. Neither the model weights nor any inference result is included as sample data.

## Windows PowerShell setup

From this folder:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
# Install this only if you want to enable local video inference.
python -m pip install -r requirements.txt
python server.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Stop the server with `Ctrl+C`.

The server creates `data/retailx.sqlite3`, `data/uploads/` and `data/outputs/` on first run. The SQLite database uses foreign keys, constraints, indexes and WAL journaling. To keep data in a different directory, set `RETAILX_DATA_DIR` before starting the server:

```powershell
$env:RETAILX_DATA_DIR = 'D:\RetailXData'
python server.py
```

`RETAILX_HOST` defaults to `127.0.0.1` and `RETAILX_PORT` defaults to `8000`. The server refuses non-loopback bind addresses. This prototype has no authentication and is not configured for public or multi-user deployment. Do not put it behind a public tunnel or reverse proxy; a real deployment needs authentication, authorization, HTTPS termination, privacy/legal review, and an operational database and backup plan.

## Demo flow

1. Open the Overview and switch between Midnight Platinum and Ivory & Burgundy.
2. In Inventory, choose **Load sample dataset** or add a verified product. The sample marker stays visible and its low/out-of-stock conditions are labeled as sample alerts.
3. Adjust quantity. Inventory movements and threshold alerts are written in one transaction; adjust back above the threshold to resolve the condition.
4. Open Queue Management and save a threshold and normalized frame region.
5. In Shopper analytics, upload a short local video. RetailX samples frames, runs person detection, records the timestamp/count, filters detections inside the saved queue region, and writes an annotated video.
6. Build a report for a selected date range, export CSVs or print the receipt-style report.
7. Stop and restart `python server.py`; SQLite inventory, alert history, settings and observations remain in `data/`.

For a video, use a short MP4/MOV/AVI/MKV/WebM that you recorded or have permission to process. Files over 200 MB are rejected. A video is saved locally before inference. The first inference can download Ultralytics model weights from the internet; after that, inference is local. To select another compatible Ultralytics model, set `RETAILX_MODEL` before launching, for example:

```powershell
$env:RETAILX_MODEL = 'yolo26n.pt'
python server.py
```

### NVIDIA GPU setup

The regular requirements setup is the simplest CPU fallback path. PyTorch wheel choice depends on the installed Windows driver, CUDA runtime and Python version. Use the current [official PyTorch Windows installation selector](https://pytorch.org/get-started/locally/) to choose the matching CUDA command, run it in the activated environment, then install `requirements.txt`. Afterward, Settings reports the device PyTorch actually detects. Do not infer GPU use from the presence of an NVIDIA card alone.

## Tests

```powershell
python -m pytest -q
```

Tests use temporary SQLite databases and a local HTTP server; they do not need a model download or sample video. They cover persistence, inventory CRUD and adjustments, alert behavior, settings, reports, CSV validation/exports, security headers, request-origin checks, loopback binding, interrupted-job recovery, and safe job deletion.

## Storage and privacy

- `data/retailx.sqlite3` stores products, movements, jobs, observations, alerts and settings.
- `data/uploads/` stores uploaded source videos; `data/outputs/` stores annotated output videos.
- No facial recognition, face embeddings, shopper identities, demographics or dwell-time calculation is implemented.
- Shopper analytics are sampled frame-level detections. The same person can appear in many observations; counts are not unique footfall.
- Queue counts estimate detections whose bounding-box centers fall inside the configured region. Camera angle, occlusion and model accuracy affect the result. Wait-time prediction is not implemented.
- The retention setting records a preferred period, but automated deletion is not implemented. Delete terminal video jobs in Shopper analytics to remove their managed source/output files and observations; inventory movements remain after product deletion. Back up the database and media together.
- This local prototype has not been independently security-audited.

### Backup and restore

Stop RetailX with `Ctrl+C` before copying `data/`. Copy the entire directory, including `retailx.sqlite3`, any SQLite `-wal`/`-shm` files that remain, `uploads/`, and `outputs/`. Use a new destination folder for each backup:

```powershell
$backupPath = 'D:\RetailX-backups\2026-10-22'
New-Item -ItemType Directory -Path $backupPath
Copy-Item -Path '.\data' -Destination $backupPath -Recurse
```

To run from a backup without overwriting the current `data/`, point RetailX at the copied data folder:

```powershell
$env:RETAILX_DATA_DIR = 'D:\RetailX-backups\2026-10-22\data'
python server.py
```

Protect backup media with the same care as the source computer. The application does not encrypt backups.

## Implementation notes and limits

- SQLite is the active storage backend, matching the local-prototype option in the build brief. Supabase is not wired into this build; no existing Supabase project appeared appropriate to modify. Cloud database setup is a separate step because the only connected project is an unrelated inactive project.
- No authentication, multi-store isolation, HTTPS configuration, or public deployment configuration is included.
- Low-stock and queue alerts are deduplicated while an underlying condition remains active. Resolving or clearing a condition allows a new alert when it recurs.
- Stock level is recorded from operator input and adjustments. Shelf-gap detection is not implemented and visual gaps are never converted into an asserted stock quantity.
- Operational metrics start empty and appear only after stored data exists. Only the explicitly imported sample inventory is illustrative.

## Project layout

```text
server.py                HTTP API, SQLite schema, inventory rules and inference worker
static/index.html        RetailX application shell and navigation
static/styles.css        Responsive dark/light themes and receipt print styles
static/app.js            Page rendering, charts, forms and API client
sample_inventory.csv    Clearly labeled sample data
tests/                   Local API and database tests
AUDIT.md                 Evidence-based security and readiness findings
data/                    Runtime database and local videos (git-ignored)
```

## Reference links

- [Ultralytics install guide](https://docs.ultralytics.com/quickstart) and [YOLO26 detection models](https://docs.ultralytics.com/models/yolo26). RetailX uses the pretrained COCO detection model; the first use downloads model weights.
- [OpenCV Python package releases](https://pypi.org/project/opencv-python/)
- [PyTorch Windows installation selector](https://pytorch.org/get-started/locally/)
