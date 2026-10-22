"""Local-first RetailX demo server.

The API uses SQLite and serves the browser UI from the same origin. Video
inference is optional and runs locally through OpenCV + Ultralytics when those
packages and a YOLO model are available.
"""

from __future__ import annotations

import csv
import ipaddress
import io
import json
import logging
import math
import mimetypes
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse, urlsplit

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA = Path(os.environ.get("RETAILX_DATA_DIR", ROOT / "data")).resolve()
# OpenCV 4.14 Windows wheels have a reported intermittent exit crash after
# an FFmpeg decoder failure; FATAL logging is the reporter's tested workaround.
# Keep the FFmpeg 8.1.2 security fix in this build and suppress that failure path.
if os.name == "nt":
    os.environ["OPENCV_LOG_LEVEL"] = "FATAL"
UPLOADS = DATA / "uploads"
OUTPUTS = DATA / "outputs"
DB_PATH = DATA / "retailx.sqlite3"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
ALLOWED_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
JOB_LOCK = threading.Lock()
INFERENCE_THREADS: dict[str, threading.Thread] = {}
MODEL_LOCK = threading.Lock()
MODEL_CACHE = {"name": None, "model": None}
SCHEMA_VERSION = 1
REQUEST_LIMITER = None

DEFAULT_SETTINGS = {
    "theme": "dark",
    "queue_threshold": 5,
    "queue_region": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
    "video_sample_seconds": 1.0,
    "confidence_threshold": 0.25,
    "retention_days": 30,
}


class RollingRateLimiter:
    """Small in-memory limiter suitable for this single-process local demo."""

    def __init__(self):
        self._lock = threading.Lock()
        self._events: dict[tuple[str, str], deque[float]] = {}

    def consume(self, scope: str, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - window_seconds
        bucket_key = (scope, key)
        with self._lock:
            events = self._events.setdefault(bucket_key, deque())
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                return False, max(1, math.ceil(window_seconds - (now - events[0])))
            events.append(now)
            if len(self._events) > 1024:
                for stale_key, stale in list(self._events.items()):
                    while stale and stale[0] <= cutoff:
                        stale.popleft()
                    if not stale:
                        self._events.pop(stale_key, None)
            return True, 0


REQUEST_LIMITER = RollingRateLimiter()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def connect() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


def init_db() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    UPLOADS.mkdir(parents=True, exist_ok=True)
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError("This database was created by a newer RetailX version.")
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
                id TEXT PRIMARY KEY,
                sku TEXT NOT NULL COLLATE NOCASE UNIQUE,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'General',
                quantity INTEGER NOT NULL CHECK (quantity >= 0),
                unit TEXT NOT NULL DEFAULT 'units',
                price REAL NOT NULL DEFAULT 0 CHECK (price >= 0),
                reorder_threshold INTEGER NOT NULL DEFAULT 0 CHECK (reorder_threshold >= 0),
                is_sample INTEGER NOT NULL DEFAULT 0 CHECK (is_sample IN (0, 1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS products_name_idx ON products(name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS products_category_idx ON products(category COLLATE NOCASE);
            CREATE TABLE IF NOT EXISTS inventory_movements (
                id TEXT PRIMARY KEY,
                product_id TEXT REFERENCES products(id) ON DELETE SET NULL,
                sku TEXT NOT NULL,
                product_name TEXT NOT NULL,
                delta INTEGER NOT NULL,
                quantity_after INTEGER NOT NULL CHECK (quantity_after >= 0),
                reason TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS inventory_movements_created_idx ON inventory_movements(created_at DESC);
            CREATE TABLE IF NOT EXISTS video_jobs (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                input_path TEXT NOT NULL,
                output_path TEXT,
                status TEXT NOT NULL CHECK (status IN ('queued','processing','complete','failed','canceled')),
                progress REAL NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 100),
                total_frames INTEGER,
                sampled_frames INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS video_jobs_created_idx ON video_jobs(created_at DESC);
            CREATE TABLE IF NOT EXISTS analytics_observations (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES video_jobs(id) ON DELETE CASCADE,
                timestamp_seconds REAL NOT NULL CHECK (timestamp_seconds >= 0),
                people_count INTEGER NOT NULL CHECK (people_count >= 0),
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS analytics_job_timestamp_idx ON analytics_observations(job_id, timestamp_seconds);
            CREATE INDEX IF NOT EXISTS analytics_created_idx ON analytics_observations(created_at);
            CREATE TABLE IF NOT EXISTS queue_observations (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES video_jobs(id) ON DELETE CASCADE,
                timestamp_seconds REAL NOT NULL CHECK (timestamp_seconds >= 0),
                people_count INTEGER NOT NULL CHECK (people_count >= 0),
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS queue_job_timestamp_idx ON queue_observations(job_id, timestamp_seconds);
            CREATE INDEX IF NOT EXISTS queue_created_idx ON queue_observations(created_at);
            CREATE TABLE IF NOT EXISTS alerts (
                id TEXT PRIMARY KEY,
                condition_key TEXT NOT NULL,
                source TEXT NOT NULL CHECK (source IN ('inventory','queue')),
                severity TEXT NOT NULL CHECK (severity IN ('critical','warning','info')),
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','resolved')),
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                resolved_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS alerts_one_active_condition_idx
                ON alerts(condition_key) WHERE status = 'active';
            CREATE INDEX IF NOT EXISTS alerts_status_seen_idx ON alerts(status, last_seen DESC);
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        for key, value in DEFAULT_SETTINGS.items():
            db.execute(
                "INSERT OR IGNORE INTO app_settings(key,value,updated_at) VALUES(?,?,?)",
                (key, json.dumps(value), now_iso()),
            )
        db.execute(
            "UPDATE video_jobs SET status='failed',error='Processing stopped before the server closed. Upload the video again to retry.',updated_at=? WHERE status IN ('queued','processing')",
            (now_iso(),),
        )


def get_settings(db: sqlite3.Connection) -> dict:
    values = {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT key,value FROM app_settings")}
    return {**DEFAULT_SETTINGS, **values}


def set_setting(db: sqlite3.Connection, key: str, value) -> None:
    db.execute(
        "INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (key, json.dumps(value), now_iso()),
    )


def upsert_alert(db: sqlite3.Connection, key: str, source: str, severity: str, title: str, description: str) -> None:
    stamp = now_iso()
    existing = db.execute("SELECT id FROM alerts WHERE condition_key=? AND status='active'", (key,)).fetchone()
    if existing:
        db.execute(
            "UPDATE alerts SET severity=?,title=?,description=?,last_seen=? WHERE id=?",
            (severity, title, description, stamp, existing["id"]),
        )
    else:
        db.execute(
            "INSERT INTO alerts(id,condition_key,source,severity,title,description,status,first_seen,last_seen) "
            "VALUES(?,?,?,?,?,?,'active',?,?)",
            (uuid.uuid4().hex, key, source, severity, title, description, stamp, stamp),
        )


def sync_inventory_alert(db: sqlite3.Connection, product: sqlite3.Row) -> None:
    sample = bool(product["is_sample"])
    key = f"{'sample:' if sample else ''}stock:{product['id']}"
    stale_key = f"{'stock:' if sample else 'sample:stock:'}{product['id']}"
    db.execute("UPDATE alerts SET status='resolved',resolved_at=? WHERE condition_key=? AND status='active'", (now_iso(), stale_key))
    qty = int(product["quantity"])
    threshold = int(product["reorder_threshold"])
    if qty == 0:
        label = "Sample · " if sample else ""
        upsert_alert(db, key, "inventory", "critical", f"{label}Out of stock: {product['name']}", f"SKU {product['sku']} has no stock remaining." + (" This is sample data." if sample else ""))
    elif threshold > 0 and qty <= threshold:
        label = "Sample · " if sample else ""
        upsert_alert(db, key, "inventory", "warning", f"{label}Low stock: {product['name']}", f"SKU {product['sku']} has {qty} {product['unit']} remaining; reorder threshold is {threshold}." + (" This is sample data." if sample else ""))
    else:
        db.execute("UPDATE alerts SET status='resolved',resolved_at=? WHERE condition_key=? AND status='active'", (now_iso(), key))


def insert_movement(db: sqlite3.Connection, product: sqlite3.Row, delta: int, quantity_after: int, reason: str, source: str = "manual") -> None:
    db.execute(
        "INSERT INTO inventory_movements(id,product_id,sku,product_name,delta,quantity_after,reason,source,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, product["id"], product["sku"], product["name"], delta, quantity_after, reason, source, now_iso()),
    )


def whole_number(value, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a whole number.")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value.strip())
    raise ValueError(f"{label} must be a whole number.")


def date_window(start: str, end: str) -> tuple[str, str]:
    try:
        start_date = datetime.strptime(start, "%Y-%m-%d").date()
        end_date = datetime.strptime(end, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("Dates must use YYYY-MM-DD format.")
    if start_date > end_date:
        raise ValueError("Start date must come before the end date.")
    end_exclusive = f"{end_date.isoformat()}T23:59:59.9999999" if end_date == datetime.max.date() else f"{(end_date + timedelta(days=1)).isoformat()}T00:00:00"
    return f"{start_date.isoformat()}T00:00:00", end_exclusive


def safe_csv_cell(value):
    if isinstance(value, str) and value.lstrip(" \t\r\n")[:1] in {"=", "+", "-", "@"}:
        return "'" + value
    if isinstance(value, str) and value.startswith(("\t", "\r")):
        return "'" + value
    return value


def validate_product(data: dict, require_sku: bool = True) -> dict:
    def text_value(name: str, required: bool = True, default: str = "") -> str:
        value = str(data.get(name, default)).strip()
        if required and not value:
            raise ValueError(f"{name.replace('_', ' ').capitalize()} is required.")
        if len(value) > 160:
            raise ValueError(f"{name.replace('_', ' ').capitalize()} is too long.")
        return value

    sku = text_value("sku", require_sku).upper()
    if sku and not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,39}", sku):
        raise ValueError("SKU can contain letters, numbers, dot, underscore and hyphen.")
    name = text_value("name")
    category = text_value("category", False, "General") or "General"
    unit = text_value("unit", False, "units") or "units"
    quantity = whole_number(data.get("quantity", 0), "Quantity")
    threshold = whole_number(data.get("reorder_threshold", 0), "Reorder threshold")
    if isinstance(data.get("price", 0), bool):
        raise ValueError("Price must be a valid number.")
    try:
        price = float(data.get("price", 0))
    except (ValueError, TypeError, OverflowError):
        raise ValueError("Price must be a valid number.")
    if not math.isfinite(price):
        raise ValueError("Price must be a finite number.")
    if quantity < 0 or threshold < 0 or price < 0:
        raise ValueError("Quantity, reorder threshold and price cannot be negative.")
    if quantity > 2_000_000_000 or threshold > 2_000_000_000 or price > 1_000_000_000:
        raise ValueError("One or more values exceed the supported range.")
    return {"sku": sku, "name": name, "category": category, "quantity": quantity, "unit": unit, "price": round(price, 2), "reorder_threshold": threshold}


def product_dict(row: sqlite3.Row) -> dict:
    result = dict(row)
    result["is_sample"] = bool(result["is_sample"])
    return result


def runtime_info() -> dict:
    try:
        import cv2  # noqa: F401
        cv_ready = True
    except Exception:
        cv_ready = False
    try:
        import ultralytics  # noqa: F401
        yolo_ready = True
    except Exception:
        yolo_ready = False
    cuda = False
    if yolo_ready:
        try:
            import torch
            cuda = bool(torch.cuda.is_available())
        except Exception:
            pass
    return {
        "opencv_available": cv_ready,
        "ultralytics_available": yolo_ready,
        "model_name": os.environ.get("RETAILX_MODEL", "yolo26n.pt"),
        "device": "NVIDIA GPU available" if cuda else "CPU",
        "storage": "Local application data directory (see README for location)",
        "max_video_bytes": MAX_UPLOAD_BYTES,
    }


def robots_text() -> str:
    return "User-agent: *\nDisallow: /\n"


def sitemap_xml() -> bytes:
    return b'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'


def run_inference(job_id: str, input_path: Path, filename: str) -> None:
    """Stream frames through a real local YOLO detector and persist observations."""
    capture = None
    writer = None
    output_path = OUTPUTS / f"{job_id}.mp4"
    completed = False
    canceled = False
    try:
        import cv2
        import torch
        from ultralytics import YOLO
        model_name = os.environ.get("RETAILX_MODEL", "yolo26n.pt")
        with MODEL_LOCK:
            if MODEL_CACHE["name"] != model_name or MODEL_CACHE["model"] is None:
                MODEL_CACHE["model"] = YOLO(model_name)
                MODEL_CACHE["name"] = model_name
            model = MODEL_CACHE["model"]
        device = 0 if torch.cuda.is_available() else "cpu"
        # Select the bundled FFmpeg decoder explicitly instead of probing every
        # optional backend on malformed input (notably unstable in some wheels).
        capture = cv2.VideoCapture(str(input_path), cv2.CAP_FFMPEG)
        if not capture.isOpened():
            raise ValueError("The video could not be decoded. Try an MP4 encoded with H.264.")
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        if not math.isfinite(fps) or fps <= 0:
            fps = 25.0
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if width <= 0 or height <= 0:
            raise ValueError("The video has invalid frame dimensions.")
        if width * height > 16_000_000:
            raise ValueError("The video frame is too large to process safely (maximum 16 megapixels).")
        if frame_count and frame_count / fps > 7200:
            raise ValueError("Video duration exceeds the 2-hour processing limit.")
        writer = cv2.VideoWriter(str(output_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
        if not writer.isOpened():
            raise ValueError("Could not create the annotated video on this system.")
        with connect() as db:
            with db:
                settings = get_settings(db)
                interval = max(0.25, min(10.0, float(settings["video_sample_seconds"])))
                confidence = max(0.05, min(0.95, float(settings["confidence_threshold"])))
                region = settings["queue_region"]
                threshold = int(settings["queue_threshold"])
                changed = db.execute("UPDATE video_jobs SET status='processing',total_frames=?,updated_at=? WHERE id=? AND status='queued'", (frame_count, now_iso(), job_id)).rowcount
                if not changed:
                    canceled = True
        if canceled:
            return
        stride = max(1, round(fps * interval))
        frame_no = 0
        sampled = 0
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            if frame_no >= int(fps * 7200):
                raise ValueError("Video duration exceeds the 2-hour processing limit.")
            annotated = frame
            if frame_no % stride == 0:
                results = model.predict(frame, conf=confidence, imgsz=640, device=device, verbose=False)
                result = results[0]
                all_count = 0
                queue_count = 0
                if result.boxes is not None:
                    for box in result.boxes:
                        if int(box.cls.item()) != 0:
                            continue
                        all_count += 1
                        x1, y1, x2, y2 = [float(value) for value in box.xyxy[0].tolist()]
                        center_x, center_y = ((x1 + x2) / 2 / width, (y1 + y2) / 2 / height)
                        if region["x"] <= center_x <= region["x"] + region["width"] and region["y"] <= center_y <= region["y"] + region["height"]:
                            queue_count += 1
                        cv2.rectangle(annotated, (int(x1), int(y1)), (int(x2), int(y2)), (166, 138, 255), 2)
                    cv2.putText(annotated, f"People: {all_count}  |  Queue region: {queue_count}", (20, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.85, (255, 255, 255), 2, cv2.LINE_AA)
                timestamp = frame_no / fps
                with connect() as db:
                    with db:
                        current = db.execute("SELECT status FROM video_jobs WHERE id=?", (job_id,)).fetchone()
                        if not current or current["status"] == "canceled":
                            canceled = True
                        else:
                            stamp = now_iso()
                            db.execute("INSERT INTO analytics_observations(id,job_id,timestamp_seconds,people_count,created_at) VALUES(?,?,?,?,?)", (uuid.uuid4().hex, job_id, timestamp, all_count, stamp))
                            db.execute("INSERT INTO queue_observations(id,job_id,timestamp_seconds,people_count,created_at) VALUES(?,?,?,?,?)", (uuid.uuid4().hex, job_id, timestamp, queue_count, stamp))
                            if queue_count > threshold:
                                upsert_alert(db, "queue:threshold", "queue", "warning", "Queue threshold exceeded", f"Estimated {queue_count} people in the configured region; threshold is {threshold}. Observation from {filename} at {timestamp:.1f}s.")
                            else:
                                db.execute("UPDATE alerts SET status='resolved',resolved_at=? WHERE condition_key='queue:threshold' AND status='active'", (stamp,))
                            db.execute("UPDATE video_jobs SET sampled_frames=?,progress=?,updated_at=? WHERE id=?", (sampled + 1, min(99.0, frame_no * 100.0 / frame_count) if frame_count else 0.0, stamp, job_id))
                if canceled:
                    break
                sampled += 1
            writer.write(annotated)
            frame_no += 1
            if frame_no % 100 == 0 and output_path.exists() and output_path.stat().st_size > 2 * 1024 * 1024 * 1024:
                raise ValueError("Annotated video exceeds the 2 GB output limit.")
            if canceled:
                break
        with connect() as db:
            current = db.execute("SELECT status FROM video_jobs WHERE id=?", (job_id,)).fetchone()
            if current and current["status"] == "canceled":
                db.execute("UPDATE video_jobs SET progress=0,updated_at=? WHERE id=?", (now_iso(), job_id))
                canceled = True
            elif sampled == 0:
                raise ValueError("No frames were available for detection.")
            else:
                db.execute("UPDATE video_jobs SET status='complete',progress=100,sampled_frames=?,output_path=?,error=NULL,updated_at=? WHERE id=?", (sampled, str(output_path), now_iso(), job_id))
                completed = True
    except Exception as exc:
        if isinstance(exc, ModuleNotFoundError):
            safe_message = "Video processing dependencies are missing. Install them with: python -m pip install -r requirements.txt"
        elif isinstance(exc, ValueError) and str(exc) in {
            "The video could not be decoded. Try an MP4 encoded with H.264.",
            "The video has invalid frame dimensions.",
            "The video frame is too large to process safely (maximum 16 megapixels).",
            "Video duration exceeds the 2-hour processing limit.",
            "Could not create the annotated video on this system.",
            "No frames were available for detection.",
            "Annotated video exceeds the 2 GB output limit.",
        }:
            safe_message = str(exc)
        else:
            safe_message = "Video processing failed. Check that the file is supported and try again."
            logging.warning("RetailX inference failed for job %s (%s)", job_id, type(exc).__name__)
        with connect() as db:
            db.execute("UPDATE video_jobs SET status='failed',error=?,updated_at=? WHERE id=? AND status!='canceled'", (safe_message, now_iso(), job_id))
    finally:
        if capture is not None:
            capture.release()
        if writer is not None:
            writer.release()
        if not completed and output_path.exists():
            output_path.unlink(missing_ok=True)
        if canceled:
            with connect() as db:
                db.execute("UPDATE video_jobs SET progress=0,updated_at=? WHERE id=? AND status='canceled'", (now_iso(), job_id))


def run_tracked_inference(job_id: str, input_path: Path, filename: str) -> None:
    try:
        run_inference(job_id, input_path, filename)
    finally:
        with JOB_LOCK:
            INFERENCE_THREADS.pop(job_id, None)


def summarize() -> dict:
    with connect() as db:
        settings = get_settings(db)
        products = db.execute("SELECT COUNT(*) AS total, SUM(CASE WHEN quantity <= reorder_threshold AND quantity > 0 AND reorder_threshold > 0 THEN 1 ELSE 0 END) AS low, SUM(CASE WHEN quantity=0 THEN 1 ELSE 0 END) AS out FROM products").fetchone()
        active_count = db.execute("SELECT COUNT(*) AS n FROM alerts WHERE status='active'").fetchone()["n"]
        alerts = [dict(row) for row in db.execute("SELECT * FROM alerts WHERE status='active' ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, last_seen DESC LIMIT 8")]
        latest_job = db.execute("SELECT * FROM video_jobs ORDER BY created_at DESC LIMIT 1").fetchone()
        queue = db.execute("SELECT people_count,created_at FROM queue_observations ORDER BY created_at DESC,id DESC LIMIT 1").fetchone()
        recent = [dict(row) for row in db.execute("SELECT id,sku,product_name,delta,quantity_after,reason,source,created_at FROM inventory_movements ORDER BY created_at DESC LIMIT 6")]
        latest_observation = db.execute("SELECT job_id FROM analytics_observations ORDER BY created_at DESC,id DESC LIMIT 1").fetchone()
        trend_rows = []
        if latest_observation:
            samples = [dict(row) for row in db.execute(
                "SELECT timestamp_seconds,people_count FROM analytics_observations WHERE job_id=? ORDER BY timestamp_seconds",
                (latest_observation["job_id"],),
            )]
            stride = max(1, math.ceil(len(samples) / 500))
            trend_rows = samples[::stride]
            if samples and trend_rows[-1] is not samples[-1]:
                trend_rows.append(samples[-1])
        return {
            "products_total": int(products["total"] or 0),
            "low_stock": int(products["low"] or 0),
            "out_of_stock": int(products["out"] or 0),
            "active_alerts": int(active_count),
            "alerts": alerts,
            "latest_job": dict(latest_job) if latest_job else None,
            "queue_estimate": int(queue["people_count"]) if queue else None,
            "queue_observed_at": queue["created_at"] if queue else None,
            "queue_threshold": int(settings["queue_threshold"]),
            "recent_activity": recent,
            "traffic_trend": trend_rows,
            "updated_at": now_iso(),
        }


class RetailXHandler(BaseHTTPRequestHandler):
    server_version = "RetailX"
    sys_version = ""

    def log_message(self, fmt, *args):
        safe_path = re.sub(r"[\x00-\x1f\x7f]", "?", urlparse(getattr(self, "path", "")).path[:180])
        logging.info("%s %s %s", self.address_string(), getattr(self, "command", "?"), safe_path)

    def send_headers(self, status: int, content_length: int, content_type: str, headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), usb=()")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; img-src 'self' data:; media-src 'self' blob:; font-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'")
        self.send_header("Cache-Control", "no-store" if self.path.startswith(("/api/", "/media/")) else "no-cache")
        if self.path.startswith(("/api/", "/media/")):
            self.send_header("X-Robots-Tag", "noindex, nofollow")
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()

    def is_local_host(self, value: str) -> bool:
        try:
            host = (urlsplit(f"//{value}").hostname or "").lower()
            if host == "localhost" or host.endswith(".localhost"):
                return host == "localhost"
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def same_origin(self, value: str) -> bool:
        try:
            origin = urlsplit(value)
            if origin.scheme != "http" or not origin.netloc or origin.path not in ("", "/") or origin.query or origin.fragment or not self.is_local_host(origin.netloc):
                return False
            expected_port = int(self.server.server_address[1])
            return (origin.port or 80) == expected_port
        except ValueError:
            return False

    def check_request(self, write: bool = False) -> bool:
        if self.headers.get("Transfer-Encoding", "").lower() not in ("", "identity"):
            self.json(400, {"error": "Chunked request bodies are not supported."})
            return False
        host_header = self.headers.get("Host", "")
        try:
            parsed_host = urlsplit(f"//{host_header}")
            host_port = parsed_host.port
        except ValueError:
            parsed_host = None
            host_port = None
        if (
            not parsed_host
            or parsed_host.path
            or parsed_host.username is not None
            or parsed_host.password is not None
            or not self.is_local_host(host_header)
            or (host_port is not None and host_port != self.server.server_address[1])
        ):
            self.json(421, {"error": "This local RetailX server only accepts loopback requests."})
            return False
        if write:
            origin = self.headers.get("Origin")
            fetch_site = self.headers.get("Sec-Fetch-Site", "").lower()
            if (origin and not self.same_origin(origin)) or fetch_site == "cross-site":
                self.json(403, {"error": "Cross-origin changes are not allowed."})
                return False
            allowed, retry = REQUEST_LIMITER.consume("write", self.client_address[0], 120, 60)
        else:
            allowed, retry = REQUEST_LIMITER.consume("read", self.client_address[0], 600, 60)
        if not allowed:
            self.send_bytes(429, json.dumps({"error": "Too many requests. Wait briefly and try again."}).encode("utf-8"), headers={"Retry-After": str(retry)})
            return False
        return True

    def send_bytes(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8", headers: dict | None = None):
        self.send_headers(status, len(body), content_type, headers)
        self.wfile.write(body)

    def send_file(self, path: Path, content_type: str, headers: dict | None = None):
        try:
            length = path.stat().st_size
            source = path.open("rb")
        except OSError:
            return self.json(404, {"error": "The requested file is unavailable."})
        with source:
            self.send_headers(200, length, content_type, headers)
            while chunk := source.read(1024 * 1024):
                self.wfile.write(chunk)

    def json(self, status: int, body):
        self.send_bytes(status, json.dumps(body, ensure_ascii=False).encode("utf-8"))

    def read_body(self, maximum: int = 2_000_000) -> bytes:
        raw_length = self.headers.get("Content-Length", "0")
        if not re.fullmatch(r"\d{1,10}", raw_length):
            raise ValueError("Request length is invalid.")
        length = int(raw_length)
        if length < 0 or length > maximum:
            raise ValueError("Request is too large.")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("Request ended before the complete body was received.")
        return body

    def read_json(self) -> dict:
        raw = self.read_body()
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Request body must be valid JSON.")
        if not isinstance(value, dict):
            raise ValueError("Request body must be a JSON object.")
        return value

    def do_GET(self):
        try:
            if not self.check_request():
                return
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            query = parse_qs(parsed.query)
            if path == "/robots.txt":
                return self.send_bytes(200, robots_text().encode("utf-8"), "text/plain; charset=utf-8")
            if path == "/sitemap.xml":
                return self.send_bytes(200, sitemap_xml(), "application/xml; charset=utf-8")
            if path == "/api/summary":
                return self.json(200, summarize())
            if path == "/api/runtime":
                return self.json(200, runtime_info())
            if path == "/api/settings":
                with connect() as db:
                    return self.json(200, get_settings(db))
            if path == "/api/products":
                search = query.get("search", [""])[0].strip()[:120]
                category = query.get("category", [""])[0].strip()[:120]
                stock = query.get("stock", ["all"])[0]
                if stock not in ("all", "low", "out", "healthy"):
                    raise ValueError("Unknown stock filter.")
                try:
                    limit = min(100, max(1, int(query.get("limit", ["25"])[0])))
                    offset = max(0, int(query.get("offset", ["0"])[0]))
                except ValueError:
                    raise ValueError("Invalid page size.")
                if offset > 10_000_000:
                    raise ValueError("Page offset is too large.")
                where = ["(sku LIKE ? OR name LIKE ?)"]
                args = [f"%{search}%", f"%{search}%"]
                if category:
                    where.append("category=?")
                    args.append(category)
                if stock == "low":
                    where.append("quantity > 0 AND reorder_threshold > 0 AND quantity <= reorder_threshold")
                elif stock == "out":
                    where.append("quantity=0")
                elif stock == "healthy":
                    where.append("quantity > reorder_threshold OR reorder_threshold=0")
                clause = " AND ".join(where)
                with connect() as db:
                    total = db.execute(f"SELECT COUNT(*) AS n FROM products WHERE {clause}", args).fetchone()["n"]
                    rows = db.execute(f"SELECT * FROM products WHERE {clause} ORDER BY name COLLATE NOCASE LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
                    categories = [r["category"] for r in db.execute("SELECT DISTINCT category FROM products ORDER BY category COLLATE NOCASE")]
                return self.json(200, {"items": [product_dict(row) for row in rows], "total": total, "limit": limit, "offset": offset, "categories": categories})
            match = re.fullmatch(r"/api/products/([0-9a-f]{32})", path)
            if match:
                with connect() as db:
                    row = db.execute("SELECT * FROM products WHERE id=?", (match.group(1),)).fetchone()
                return self.json(200, {"item": product_dict(row)}) if row else self.json(404, {"error": "Product not found."})
            if path == "/api/alerts":
                status = query.get("status", ["all"])[0]
                source = query.get("source", ["all"])[0]
                clauses, args = [], []
                if status in ("active", "resolved"):
                    clauses.append("status=?"); args.append(status)
                if source in ("inventory", "queue"):
                    clauses.append("source=?"); args.append(source)
                where = " WHERE " + " AND ".join(clauses) if clauses else ""
                with connect() as db:
                    rows = [dict(row) for row in db.execute("SELECT * FROM alerts" + where + " ORDER BY last_seen DESC LIMIT 300", args)]
                return self.json(200, {"items": rows})
            if path == "/api/report":
                start = query.get("start", [datetime.now(timezone.utc).date().isoformat()])[0]
                end = query.get("end", [datetime.now(timezone.utc).date().isoformat()])[0]
                start_at, end_at = date_window(start, end)
                with connect() as db:
                    products = db.execute("SELECT COUNT(*) AS total,SUM(CASE WHEN quantity>0 AND reorder_threshold>0 AND quantity<=reorder_threshold THEN 1 ELSE 0 END) AS low,SUM(CASE WHEN quantity=0 THEN 1 ELSE 0 END) AS out FROM products").fetchone()
                    obs = db.execute("SELECT COUNT(*) AS n,AVG(people_count) AS avg,MAX(people_count) AS peak FROM analytics_observations WHERE created_at>=? AND created_at<?", (start_at, end_at)).fetchone()
                    queue_events = db.execute("SELECT COUNT(*) AS n FROM alerts WHERE source='queue' AND first_seen>=? AND first_seen<?", (start_at, end_at)).fetchone()["n"]
                    movements = db.execute("SELECT * FROM inventory_movements WHERE created_at>=? AND created_at<? ORDER BY created_at DESC LIMIT 5000", (start_at, end_at)).fetchall()
                    alerts = db.execute("SELECT * FROM alerts WHERE first_seen>=? AND first_seen<? ORDER BY first_seen DESC LIMIT 5000", (start_at, end_at)).fetchall()
                    movement_count = db.execute("SELECT COUNT(*) AS n FROM inventory_movements WHERE created_at>=? AND created_at<?", (start_at, end_at)).fetchone()["n"]
                return self.json(200, {"start": start, "end": end, "totals": {"products": int(products["total"] or 0), "low_stock": int(products["low"] or 0), "out_of_stock": int(products["out"] or 0), "observations": int(obs["n"] or 0), "average_people": round(obs["avg"], 2) if obs["avg"] is not None else None, "peak_people": obs["peak"], "queue_events": queue_events, "movements": movement_count}, "movements": [dict(r) for r in movements], "alerts": [dict(r) for r in alerts]})
            if path == "/api/movements":
                with connect() as db:
                    rows = [dict(row) for row in db.execute("SELECT * FROM inventory_movements ORDER BY created_at DESC LIMIT 500")]
                return self.json(200, {"items": rows})
            if path == "/api/jobs":
                with connect() as db:
                    rows = [dict(row) for row in db.execute("SELECT id,filename,status,progress,total_frames,sampled_frames,error,created_at,updated_at FROM video_jobs ORDER BY created_at DESC LIMIT 100")]
                return self.json(200, {"items": rows})
            match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})", path)
            if match:
                with connect() as db:
                    job = db.execute("SELECT id,filename,status,progress,total_frames,sampled_frames,error,created_at,updated_at FROM video_jobs WHERE id=?", (match.group(1),)).fetchone()
                    if not job:
                        return self.json(404, {"error": "Processing job not found."})
                    observations = [dict(r) for r in db.execute("SELECT a.timestamp_seconds,a.people_count,q.people_count AS queue_count FROM analytics_observations a LEFT JOIN queue_observations q ON q.job_id=a.job_id AND q.timestamp_seconds=a.timestamp_seconds WHERE a.job_id=? ORDER BY a.timestamp_seconds", (match.group(1),))]
                    output = db.execute("SELECT output_path FROM video_jobs WHERE id=?", (match.group(1),)).fetchone()["output_path"]
                data = dict(job)
                data["observations"] = observations
                data["peak_count"] = max((r["people_count"] for r in observations), default=None)
                data["average_count"] = round(sum(r["people_count"] for r in observations) / len(observations), 2) if observations else None
                data["output_url"] = f"/media/{match.group(1)}.mp4" if output and Path(output).is_file() else None
                return self.json(200, data)
            match = re.fullmatch(r"/media/([0-9a-f]{32})\.mp4", path)
            if match:
                with connect() as db:
                    row = db.execute("SELECT output_path FROM video_jobs WHERE id=? AND status='complete'", (match.group(1),)).fetchone()
                if not row or not row["output_path"]:
                    return self.json(404, {"error": "Annotated video is unavailable."})
                output = Path(row["output_path"]).resolve()
                if output.parent != OUTPUTS or not output.is_file():
                    return self.json(404, {"error": "Annotated video is unavailable."})
                return self.send_file(output, "video/mp4", {"Content-Disposition": 'inline; filename="retailx-annotated.mp4"'})
            if path.startswith("/api/export/"):
                kind = path.rsplit("/", 1)[-1]
                return self.export_csv(kind)
            return self.static_file(path)
        except ValueError as exc:
            self.json(400, {"error": str(exc)})
        except Exception as exc:
            logging.error("RetailX GET failed (%s)", type(exc).__name__)
            self.json(500, {"error": "The request could not be completed."})

    def export_csv(self, kind: str):
        queries = {
            "inventory.csv": ("SELECT sku,name,category,quantity,unit,price,reorder_threshold,is_sample,updated_at FROM products", "inventory.csv", None),
            "movements.csv": ("SELECT sku,product_name,delta,quantity_after,reason,source,created_at FROM inventory_movements", "inventory-movements.csv", "created_at"),
            "alerts.csv": ("SELECT source,severity,title,description,status,first_seen,last_seen,resolved_at FROM alerts", "alerts.csv", "first_seen"),
            "observations.csv": ("SELECT j.filename,a.timestamp_seconds,a.people_count,q.people_count AS queue_count,a.created_at FROM analytics_observations a JOIN video_jobs j ON j.id=a.job_id LEFT JOIN queue_observations q ON q.job_id=a.job_id AND q.timestamp_seconds=a.timestamp_seconds", "shopper-observations.csv", "a.created_at"),
        }
        if kind not in queries:
            return self.json(404, {"error": "Unknown export."})
        sql, filename, date_field = queries[kind]
        query = parse_qs(urlparse(self.path).query)
        start = query.get("start", [""])[0]
        end = query.get("end", [""])[0]
        clauses, date_args = [], []
        if start or end:
            try:
                start_date = datetime.strptime(start, "%Y-%m-%d").date() if start else None
                end_date = datetime.strptime(end, "%Y-%m-%d").date() if end else None
            except ValueError:
                raise ValueError("Export dates must use YYYY-MM-DD format.")
            if start_date and end_date and start_date > end_date:
                raise ValueError("Export start date must come before the end date.")
            if date_field:
                if start_date:
                    clauses.append(f"{date_field}>=?")
                    date_args.append(f"{start_date.isoformat()}T00:00:00")
                if end_date and end_date < datetime.max.date():
                    clauses.append(f"{date_field}<?")
                    date_args.append(f"{(end_date + timedelta(days=1)).isoformat()}T00:00:00")
                elif end_date:
                    clauses.append(f"{date_field}<=?")
                    date_args.append(f"{end_date.isoformat()}T23:59:59.9999999")
                sql += " WHERE " + " AND ".join(clauses)
        order_by = "name COLLATE NOCASE" if kind == "inventory.csv" else "a.created_at DESC" if kind == "observations.csv" else "created_at DESC" if kind == "movements.csv" else "first_seen DESC"
        sql += " ORDER BY " + order_by
        with connect() as db:
            result = db.execute(sql, date_args)
            columns = [column[0] for column in result.description]
            rows = result.fetchall()
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerows([tuple(safe_csv_cell(value) for value in row) for row in rows])
        return self.send_bytes(200, stream.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8", {"Content-Disposition": f'attachment; filename="{filename}"'})

    def static_file(self, path: str):
        if path in ("", "/"):
            path = "/index.html"
        relative = Path(path.lstrip("/"))
        target = (STATIC / relative).resolve()
        if not target.is_relative_to(STATIC.resolve()) or not target.is_file():
            not_found = (STATIC / "404.html").read_bytes()
            return self.send_bytes(404, not_found, "text/html; charset=utf-8")
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        content = target.read_bytes()
        if target.name == "404.html":
            return self.send_bytes(404, content, "text/html; charset=utf-8")
        if target.name in ("privacy.html", "terms.html"):
            content = content.replace(b"<!--PUBLIC_SEO-->", b'<meta name="robots" content="noindex, nofollow">')
        return self.send_bytes(200, content, content_type)

    def do_POST(self):
        try:
            if not self.check_request(write=True):
                return
            path = unquote(urlparse(self.path).path)
            if path == "/api/products":
                return self.create_product(self.read_json())
            if path == "/api/products/import":
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type != "text/csv":
                    raise ValueError("CSV import requires a text/csv request body.")
                return self.import_csv(self.read_body(3_000_000).decode("utf-8-sig"))
            if path == "/api/products/sample":
                return self.import_sample()
            if path == "/api/videos":
                return self.upload_video()
            if path == "/api/settings":
                return self.update_settings(self.read_json())
            match = re.fullmatch(r"/api/products/([0-9a-f]{32})/adjust", path)
            if match:
                return self.adjust_product(match.group(1), self.read_json())
            match = re.fullmatch(r"/api/products/([0-9a-f]{32})/delete", path)
            if match:
                return self.delete_product(match.group(1))
            match = re.fullmatch(r"/api/alerts/([0-9a-f]{32})/resolve", path)
            if match:
                return self.resolve_alert(match.group(1))
            match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/cancel", path)
            if match:
                return self.cancel_job(match.group(1))
            match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/delete", path)
            if match:
                return self.delete_video_job(match.group(1))
            return self.json(404, {"error": "API route not found."})
        except ValueError as exc:
            self.json(400, {"error": str(exc)})
        except sqlite3.IntegrityError as exc:
            message = "A product with that SKU already exists." if "sku" in str(exc).lower() else "The change conflicts with existing data."
            self.json(409, {"error": message})
        except Exception as exc:
            logging.error("RetailX POST failed (%s)", type(exc).__name__)
            self.json(500, {"error": "The request could not be completed."})

    def do_PUT(self):
        try:
            if not self.check_request(write=True):
                return
            path = unquote(urlparse(self.path).path)
            match = re.fullmatch(r"/api/products/([0-9a-f]{32})", path)
            if match:
                return self.update_product(match.group(1), self.read_json())
            return self.json(404, {"error": "API route not found."})
        except ValueError as exc:
            self.json(400, {"error": str(exc)})
        except sqlite3.IntegrityError:
            self.json(409, {"error": "A product with that SKU already exists."})
        except Exception as exc:
            logging.error("RetailX PUT failed (%s)", type(exc).__name__)
            self.json(500, {"error": "The request could not be completed."})

    def create_product(self, data: dict):
        product = validate_product(data)
        stamp = now_iso()
        pid = uuid.uuid4().hex
        with connect() as db:
            with db:
                db.execute("INSERT INTO products(id,sku,name,category,quantity,unit,price,reorder_threshold,is_sample,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,0,?,?)", (pid, *product.values(), stamp, stamp))
                row = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
                if product["quantity"]:
                    insert_movement(db, row, product["quantity"], product["quantity"], "Initial stock", "manual")
                sync_inventory_alert(db, row)
        return self.json(201, {"item": product_dict(row)})

    def update_product(self, pid: str, data: dict):
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
            if not old:
                return self.json(404, {"error": "Product not found."})
            combined = dict(old)
            combined.update(data)
            product = validate_product(combined)
            stamp = now_iso()
            with db:
                db.execute("UPDATE products SET sku=?,name=?,category=?,quantity=?,unit=?,price=?,reorder_threshold=?,is_sample=0,updated_at=? WHERE id=?", (*product.values(), stamp, pid))
                row = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
                delta = product["quantity"] - old["quantity"]
                if delta:
                    insert_movement(db, row, delta, product["quantity"], "Product edit", "manual")
                sync_inventory_alert(db, row)
        return self.json(200, {"item": product_dict(row)})

    def adjust_product(self, pid: str, data: dict):
        try:
            delta = whole_number(data.get("delta"), "Adjustment")
        except ValueError:
            raise ValueError("Adjustment must be a whole number.")
        reason = str(data.get("reason", "Stock adjustment")).strip()[:160] or "Stock adjustment"
        if delta == 0 or abs(delta) > 2_000_000_000:
            raise ValueError("Adjustment must be a non-zero whole number.")
        with connect() as db:
            with db:
                changed = db.execute("UPDATE products SET quantity=quantity+?,updated_at=?,is_sample=0 WHERE id=? AND quantity+? BETWEEN 0 AND 2000000000 RETURNING *", (delta, now_iso(), pid, delta)).fetchone()
                if not changed:
                    old = db.execute("SELECT id,quantity FROM products WHERE id=?", (pid,)).fetchone()
                    if not old:
                        return self.json(404, {"error": "Product not found."})
                    if old["quantity"] + delta < 0:
                        raise ValueError("Adjustment would make stock negative.")
                    raise ValueError("Adjustment would exceed the supported stock limit.")
                row = changed
                quantity = int(row["quantity"])
                insert_movement(db, row, delta, quantity, reason, "manual")
                sync_inventory_alert(db, row)
        return self.json(200, {"item": product_dict(row)})

    def delete_product(self, pid: str):
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
            if not row:
                return self.json(404, {"error": "Product not found."})
            with db:
                key = f"{'sample:' if row['is_sample'] else ''}stock:{pid}"
                db.execute("UPDATE alerts SET status='resolved',resolved_at=? WHERE condition_key=? AND status='active'", (now_iso(), key))
                db.execute("DELETE FROM products WHERE id=?", (pid,))
        return self.json(200, {"ok": True})

    def import_csv(self, text: str):
        if len(text.encode("utf-8")) > 2_800_000:
            raise ValueError("CSV file is too large.")
        try:
            reader = csv.DictReader(io.StringIO(text), strict=True)
        except csv.Error:
            raise ValueError("Could not read this CSV file.")
        expected = {"sku", "name", "category", "quantity", "unit", "price", "reorder_threshold"}
        raw_headers = [str(header or "").strip().lower() for header in (reader.fieldnames or [])]
        headers = set(raw_headers)
        if len(headers) != len(raw_headers):
            raise ValueError("CSV column names must be unique.")
        if not expected.issubset(headers):
            raise ValueError("CSV needs these columns: sku, name, category, quantity, unit, price, reorder_threshold.")
        records = []
        try:
            for line, row in enumerate(reader, start=2):
                if None in row:
                    raise ValueError(f"Row {line}: the CSV row has more values than its header.")
                try:
                    normalized = {str(k).strip().lower(): v for k, v in row.items() if k}
                    records.append(validate_product(normalized))
                except ValueError as exc:
                    raise ValueError(f"Row {line}: {exc}") from None
                if len(records) > 5000:
                    raise ValueError("A CSV import can contain at most 5,000 products.")
        except csv.Error:
            raise ValueError("The CSV file is malformed.") from None
        if not records:
            raise ValueError("The CSV has no product rows.")
        skus = [record["sku"] for record in records]
        if len(set(skus)) != len(skus):
            raise ValueError("The CSV contains duplicate SKUs.")
        added, updated = 0, 0
        stamp = now_iso()
        with connect() as db:
            with db:
                for item in records:
                    old = db.execute("SELECT * FROM products WHERE sku=?", (item["sku"],)).fetchone()
                    if old:
                        delta = item["quantity"] - old["quantity"]
                        db.execute("UPDATE products SET name=?,category=?,quantity=?,unit=?,price=?,reorder_threshold=?,is_sample=0,updated_at=? WHERE id=?", (item["name"], item["category"], item["quantity"], item["unit"], item["price"], item["reorder_threshold"], stamp, old["id"]))
                        row = db.execute("SELECT * FROM products WHERE id=?", (old["id"],)).fetchone()
                        if delta:
                            insert_movement(db, row, delta, item["quantity"], "CSV import", "csv")
                        updated += 1
                    else:
                        pid = uuid.uuid4().hex
                        db.execute("INSERT INTO products(id,sku,name,category,quantity,unit,price,reorder_threshold,is_sample,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,0,?,?)", (pid, *item.values(), stamp, stamp))
                        row = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
                        if item["quantity"]:
                            insert_movement(db, row, item["quantity"], item["quantity"], "CSV import", "csv")
                        added += 1
                    sync_inventory_alert(db, row)
        return self.json(200, {"added": added, "updated": updated})

    def import_sample(self):
        sample_path = ROOT / "sample_inventory.csv"
        text = sample_path.read_text(encoding="utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        records = [validate_product({str(k).strip().lower(): v for k, v in row.items()}) for row in reader]
        stamp = now_iso()
        inserted = 0
        with connect() as db:
            with db:
                for item in records:
                    found = db.execute("SELECT id FROM products WHERE sku=?", (item["sku"],)).fetchone()
                    if found:
                        continue
                    pid = uuid.uuid4().hex
                    db.execute("INSERT INTO products(id,sku,name,category,quantity,unit,price,reorder_threshold,is_sample,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,1,?,?)", (pid, *item.values(), stamp, stamp))
                    row = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
                    if item["quantity"]:
                        insert_movement(db, row, item["quantity"], item["quantity"], "Clearly labeled sample dataset", "sample")
                    sync_inventory_alert(db, row)
                    inserted += 1
        return self.json(200, {"added": inserted, "label": "Sample dataset"})

    def upload_video(self):
        query = parse_qs(urlparse(self.path).query)
        raw_name = query.get("filename", [""])[0]
        filename = Path(raw_name.replace("\\", "/")).name
        ext = Path(filename).suffix.lower()
        if not filename or len(filename) > 180 or ext not in ALLOWED_VIDEO_EXTS:
            raise ValueError("Choose an MP4, MOV, AVI, MKV or WebM video file.")
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/octet-stream":
            raise ValueError("Video uploads must use the application/octet-stream content type.")
        raw_length = self.headers.get("Content-Length", "0")
        if not re.fullmatch(r"\d{1,10}", raw_length):
            raise ValueError("Video length is invalid.")
        length = int(raw_length)
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            raise ValueError("Video must be smaller than 200 MB.")
        allowed, retry = REQUEST_LIMITER.consume("video-upload", self.client_address[0], 3, 600)
        if not allowed:
            return self.send_bytes(429, json.dumps({"error": "Video upload limit reached. Wait before uploading another video."}).encode("utf-8"), headers={"Retry-After": str(retry)})
        self.connection.settimeout(60)
        with JOB_LOCK:
            with connect() as db:
                active = db.execute("SELECT 1 FROM video_jobs WHERE status IN ('queued','processing') LIMIT 1").fetchone()
            worker_active = any(worker.is_alive() for worker in INFERENCE_THREADS.values())
            if active or worker_active:
                return self.json(409, {"error": "A video is already processing. Wait for it to finish before uploading another."})
            job_id = uuid.uuid4().hex
            path = UPLOADS / f"{job_id}{ext}"
            remaining = length
            signature = bytearray()
            try:
                with path.open("xb") as target:
                    while remaining:
                        block = self.rfile.read(min(1024 * 1024, remaining))
                        if not block:
                            raise ValueError("Upload ended before the complete video was received.")
                        if len(signature) < 16:
                            signature.extend(block[:16 - len(signature)])
                        target.write(block)
                        remaining -= len(block)
                header = bytes(signature)
                valid_signature = (
                    (ext in {".mp4", ".mov"} and header[4:8] == b"ftyp")
                    or (ext == ".avi" and header[:4] == b"RIFF" and header[8:12] == b"AVI ")
                    or (ext in {".mkv", ".webm"} and header[:4] == b"\x1a\x45\xdf\xa3")
                )
                if not valid_signature:
                    raise ValueError("The file contents do not match a supported video format.")
                stamp = now_iso()
                with connect() as db:
                    with db:
                        db.execute("INSERT INTO video_jobs(id,filename,input_path,status,progress,sampled_frames,created_at,updated_at) VALUES(?,?,?,'queued',0,0,?,?)", (job_id, filename, str(path), stamp, stamp))
                thread = threading.Thread(target=run_tracked_inference, args=(job_id, path, filename), daemon=True)
                INFERENCE_THREADS[job_id] = thread
                thread.start()
            except Exception:
                path.unlink(missing_ok=True)
                INFERENCE_THREADS.pop(job_id, None)
                with connect() as db:
                    with db:
                        db.execute("DELETE FROM video_jobs WHERE id=? AND status='queued'", (job_id,))
                raise
        return self.json(202, {"id": job_id, "status": "queued"})

    def update_settings(self, data: dict):
        changes = {}
        if "theme" in data:
            if data["theme"] not in ("dark", "light"):
                raise ValueError("Theme must be dark or light.")
            changes["theme"] = data["theme"]
        if "queue_threshold" in data:
            try:
                value = whole_number(data["queue_threshold"], "Queue threshold")
            except ValueError:
                raise ValueError("Queue threshold must be a whole number.")
            if value < 1 or value > 500:
                raise ValueError("Queue threshold must be between 1 and 500.")
            changes["queue_threshold"] = value
        if "video_sample_seconds" in data:
            if isinstance(data["video_sample_seconds"], bool):
                raise ValueError("Frame sampling interval must be a number.")
            try:
                value = float(data["video_sample_seconds"])
            except (ValueError, TypeError):
                raise ValueError("Frame sampling interval must be a number.")
            if not 0.25 <= value <= 10:
                raise ValueError("Frame sampling interval must be between 0.25 and 10 seconds.")
            changes["video_sample_seconds"] = value
        if "confidence_threshold" in data:
            if isinstance(data["confidence_threshold"], bool):
                raise ValueError("Confidence threshold must be a number.")
            try:
                value = float(data["confidence_threshold"])
            except (ValueError, TypeError):
                raise ValueError("Confidence threshold must be a number.")
            if not 0.05 <= value <= 0.95:
                raise ValueError("Confidence threshold must be between 0.05 and 0.95.")
            changes["confidence_threshold"] = value
        if "retention_days" in data:
            try:
                value = whole_number(data["retention_days"], "Retention period")
            except ValueError:
                raise ValueError("Retention period must be a whole number.")
            if not 1 <= value <= 365:
                raise ValueError("Retention period must be between 1 and 365 days.")
            changes["retention_days"] = value
        if "queue_region" in data:
            region = data["queue_region"]
            if not isinstance(region, dict):
                raise ValueError("Queue region must be an object.")
            try:
                if any(isinstance(region[key], bool) for key in ("x", "y", "width", "height")):
                    raise ValueError
                x, y = float(region["x"]), float(region["y"])
                width, height = float(region["width"]), float(region["height"])
            except (KeyError, ValueError, TypeError):
                raise ValueError("Queue region coordinates are invalid.")
            if not all(math.isfinite(value) for value in (x, y, width, height)) or x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
                raise ValueError("Queue region must fit inside the frame.")
            changes["queue_region"] = {"x": x, "y": y, "width": width, "height": height}
        with connect() as db:
            with db:
                for key, value in changes.items():
                    set_setting(db, key, value)
            settings = get_settings(db)
        return self.json(200, settings)

    def resolve_alert(self, alert_id: str):
        with connect() as db:
            with db:
                changed = db.execute("UPDATE alerts SET status='resolved',resolved_at=? WHERE id=? AND status='active'", (now_iso(), alert_id)).rowcount
        if not changed:
            return self.json(404, {"error": "Active alert not found."})
        return self.json(200, {"ok": True})

    def cancel_job(self, job_id: str):
        with connect() as db:
            row = db.execute("SELECT status FROM video_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return self.json(404, {"error": "Processing job not found."})
            if row["status"] in ("complete", "failed", "canceled"):
                return self.json(409, {"error": "This job has already finished."})
            with db:
                changed = db.execute("UPDATE video_jobs SET status='canceled',updated_at=? WHERE id=? AND status IN ('queued','processing')", (now_iso(), job_id)).rowcount
            if not changed:
                return self.json(409, {"error": "This job has already finished."})
        return self.json(200, {"ok": True})

    def delete_video_job(self, job_id: str):
        with JOB_LOCK:
            worker = INFERENCE_THREADS.get(job_id)
            if worker is not None and worker.is_alive():
                return self.json(409, {"error": "The video worker is still stopping. Wait briefly before deleting this job."})
            with connect() as db:
                row = db.execute("SELECT status,input_path,output_path FROM video_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return self.json(404, {"error": "Processing job not found."})
            if row["status"] in ("queued", "processing"):
                return self.json(409, {"error": "Cancel the active job and wait for it to stop before deleting it."})
            safe_paths = []
            for value, directory in ((row["input_path"], UPLOADS), (row["output_path"], OUTPUTS)):
                if not value:
                    continue
                candidate = Path(value).resolve()
                if candidate.parent != directory.resolve():
                    return self.json(400, {"error": "The stored video path is outside RetailX's managed folders."})
                safe_paths.append(candidate)
            try:
                for candidate in safe_paths:
                    candidate.unlink(missing_ok=True)
            except OSError:
                logging.warning("RetailX could not remove a managed video file for job %s", job_id)
                return self.json(500, {"error": "The local video file could not be removed."})
            with connect() as db:
                with db:
                    db.execute("DELETE FROM video_jobs WHERE id=? AND status NOT IN ('queued','processing')", (job_id,))
        return self.json(200, {"ok": True})


class RetailXHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, server_address, handler):
        self._request_slots = threading.BoundedSemaphore(24)
        super().__init__(server_address, handler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(60)
        return request, address

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


def loopback_bind_host(requested_host: str) -> str:
    value = (requested_host or "127.0.0.1").strip().lower()
    if value == "localhost":
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise ValueError("RetailX only binds to an IPv4 loopback address (127.0.0.1 or localhost).")
    if not address.is_loopback or address.version != 4:
        raise ValueError("RetailX is a local-only prototype and refuses non-loopback network binding.")
    return str(address)


def main() -> None:
    try:
        host = loopback_bind_host(os.environ.get("RETAILX_HOST", "127.0.0.1"))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    try:
        port = int(os.environ.get("RETAILX_PORT", "8000"))
    except ValueError:
        raise SystemExit("RETAILX_PORT must be an integer between 1 and 65535.")
    if not 1 <= port <= 65535:
        raise SystemExit("RETAILX_PORT must be an integer between 1 and 65535.")
    init_db()
    server = RetailXHTTPServer((host, port), RetailXHandler)
    print(f"RetailX is ready at http://{host}:{port}")
    print(f"Local data directory: {DATA}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nRetailX server stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
