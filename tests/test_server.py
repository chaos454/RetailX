from __future__ import annotations

import json
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import server


@pytest.fixture
def app(monkeypatch, tmp_path):
    data = tmp_path / "data"
    monkeypatch.setattr(server, "DATA", data)
    monkeypatch.setattr(server, "UPLOADS", data / "uploads")
    monkeypatch.setattr(server, "OUTPUTS", data / "outputs")
    monkeypatch.setattr(server, "DB_PATH", data / "retailx.sqlite3")
    server.init_db()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.RetailXHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2)


def request(base, path, method="GET", body=None, content_type="application/json", headers=None):
    encoded = None if body is None else body if isinstance(body, bytes) else json.dumps(body).encode()
    request_headers = {"Content-Type": content_type, **(headers or {})}
    req = Request(base + path, data=encoded, method=method, headers=request_headers)
    try:
        with urlopen(req, timeout=5) as response:
            payload = response.read()
            return response.status, payload, response.headers
    except HTTPError as exc:
        return exc.code, exc.read(), exc.headers


def json_request(base, path, method="GET", body=None):
    status, payload, _ = request(base, path, method, body)
    return status, json.loads(payload) if payload else {}


def create_product(base, sku="TEST-01", quantity=4, threshold=5):
    return json_request(base, "/api/products", "POST", {
        "sku": sku,
        "name": "Test product",
        "category": "Pantry",
        "quantity": quantity,
        "unit": "units",
        "price": 2.5,
        "reorder_threshold": threshold,
    })


def test_product_crud_stock_movements_and_inventory_alert_lifecycle(app):
    status, result = create_product(app)
    assert status == 201
    product = result["item"]
    assert product["quantity"] == 4

    status, summary = json_request(app, "/api/summary")
    assert status == 200
    assert summary["products_total"] == 1
    assert summary["low_stock"] == 1
    assert summary["active_alerts"] == 1
    assert "Low stock" in summary["alerts"][0]["title"]

    status, adjusted = json_request(app, f"/api/products/{product['id']}/adjust", "POST", {"delta": 3, "reason": "Delivery"})
    assert status == 200
    assert adjusted["item"]["quantity"] == 7
    assert json_request(app, "/api/summary")[1]["active_alerts"] == 0

    status, adjusted = json_request(app, f"/api/products/{product['id']}/adjust", "POST", {"delta": -7, "reason": "Count"})
    assert status == 200
    assert adjusted["item"]["quantity"] == 0
    assert json_request(app, "/api/summary")[1]["out_of_stock"] == 1
    movements = json_request(app, "/api/movements")[1]["items"]
    assert [row["delta"] for row in movements] == [-7, 3, 4]

    status, invalid = json_request(app, f"/api/products/{product['id']}/adjust", "POST", {"delta": -1})
    assert status == 400 and "negative" in invalid["error"]
    assert json_request(app, f"/api/products/{product['id']}")[1]["item"]["quantity"] == 0

    status, edited = json_request(app, f"/api/products/{product['id']}", "PUT", {"name": "Updated product", "quantity": 6})
    assert status == 200 and edited["item"]["name"] == "Updated product"
    assert json_request(app, "/api/summary")[1]["active_alerts"] == 0
    assert json_request(app, f"/api/products/{product['id']}/delete", "POST")[0] == 200
    assert json_request(app, f"/api/products/{product['id']}")[0] == 404


def test_csv_import_validation_export_and_sample_label(app):
    csv_text = "sku,name,category,quantity,unit,price,reorder_threshold\nA-1,Tea,Pantry,3,boxes,4.2,5\n"
    status, imported, _ = request(app, "/api/products/import", "POST", csv_text.encode(), "text/csv")
    assert status == 200
    assert json.loads(imported)["added"] == 1

    duplicate_csv = "sku,name,category,quantity,unit,price,reorder_threshold\nB-1,Coffee,Pantry,3,bags,4,5\nB-1,Tea,Pantry,2,boxes,3,5\n"
    status, error, _ = request(app, "/api/products/import", "POST", duplicate_csv.encode(), "text/csv")
    assert status == 400 and b"duplicate SKUs" in error
    assert json_request(app, "/api/products?search=B-1")[1]["total"] == 0

    status, export, headers = request(app, "/api/export/inventory.csv")
    assert status == 200 and "text/csv" in headers["Content-Type"]
    assert b"sku,name,category" in export

    status, sample = json_request(app, "/api/products/sample", "POST")
    assert status == 200 and sample["added"] == 8
    listed = json_request(app, "/api/products?search=DRK-004")[1]["items"][0]
    assert listed["is_sample"] is True
    assert "Sample" in json_request(app, "/api/alerts?source=inventory")[1]["items"][0]["title"]


def test_alert_deduplication_resolution_and_recurrence(app):
    with server.connect() as db:
        with db:
            server.upsert_alert(db, "queue:threshold", "queue", "warning", "Queue exceeded", "Seven people detected")
            server.upsert_alert(db, "queue:threshold", "queue", "warning", "Queue exceeded", "Eight people detected")
    active = json_request(app, "/api/alerts?source=queue")[1]["items"]
    assert len(active) == 1
    assert active[0]["description"] == "Eight people detected"

    assert json_request(app, f"/api/alerts/{active[0]['id']}/resolve", "POST")[0] == 200
    with server.connect() as db:
        with db:
            server.upsert_alert(db, "queue:threshold", "queue", "warning", "Queue exceeded", "Again exceeded")
    all_alerts = json_request(app, "/api/alerts?source=queue&status=all")[1]["items"]
    assert len(all_alerts) == 2
    assert len([row for row in all_alerts if row["status"] == "active"]) == 1


def test_settings_persist_and_validate_region(app):
    status, settings = json_request(app, "/api/settings", "POST", {"theme": "light", "queue_threshold": 9, "video_sample_seconds": 2, "confidence_threshold": 0.4})
    assert status == 200
    assert settings["theme"] == "light" and settings["queue_threshold"] == 9
    persisted = json_request(app, "/api/settings")[1]
    assert persisted["video_sample_seconds"] == 2
    status, error = json_request(app, "/api/settings", "POST", {"queue_region": {"x": 0.9, "y": 0, "width": 0.2, "height": 1}})
    assert status == 400 and "fit inside" in error["error"]


def test_report_date_window_calculations(app):
    today = datetime.now(timezone.utc).date().isoformat()
    status, result = create_product(app, quantity=12, threshold=3)
    assert status == 201
    pid = result["item"]["id"]
    stamp = server.now_iso()
    job_id = "a" * 32
    with server.connect() as db:
        with db:
            db.execute("INSERT INTO video_jobs(id,filename,input_path,status,created_at,updated_at) VALUES(?,?,?,'complete',?,?)", (job_id, "test.mp4", "local", stamp, stamp))
            db.execute("INSERT INTO analytics_observations(id,job_id,timestamp_seconds,people_count,created_at) VALUES(?,?,?,?,?)", ("b" * 32, job_id, 0.0, 2, stamp))
            db.execute("INSERT INTO analytics_observations(id,job_id,timestamp_seconds,people_count,created_at) VALUES(?,?,?,?,?)", ("c" * 32, job_id, 1.0, 6, stamp))
            db.execute("INSERT INTO queue_observations(id,job_id,timestamp_seconds,people_count,created_at) VALUES(?,?,?,?,?)", ("d" * 32, job_id, 1.0, 6, stamp))
            server.upsert_alert(db, "queue:threshold", "queue", "warning", "Queue exceeded", "Threshold hit")
    status, report = json_request(app, f"/api/report?start={today}&end={today}")
    assert status == 200
    assert report["totals"]["products"] == 1
    assert report["totals"]["observations"] == 2
    assert report["totals"]["average_people"] == 4
    assert report["totals"]["peak_people"] == 6
    assert report["totals"]["queue_events"] == 1

    status, error = json_request(app, "/api/report?start=2026-10-23&end=2026-10-22")
    assert status == 400 and "come before" in error["error"]


def test_unsupported_video_is_rejected_before_storage(app):
    status, error = json_request(app, "/api/videos?filename=not-a-video.txt", "POST", {})
    assert status == 400
    assert "Choose an MP4" in error["error"]

    status, error, _ = request(app, "/api/videos?filename=not-a-video.mp4", "POST", b"not a video", "application/octet-stream")
    assert status == 400 and b"do not match" in error
    assert json_request(app, "/api/jobs")[1]["items"] == []
    assert list(server.UPLOADS.iterdir()) == []


def test_security_headers_host_origin_and_real_not_found(app):
    status, body, headers = request(app, "/api/summary")
    assert status == 200
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Robots-Tag"] == "noindex, nofollow"

    status, body, headers = request(app, "/missing-route")
    assert status == 404 and b"Page not found" in body
    status, _, _ = request(app, "/%2e%2e/server.py")
    assert status == 404

    product = {"sku": "ORIGIN-01", "name": "Should not be created", "quantity": 1}
    status, body, _ = request(app, "/api/products", "POST", product, headers={"Origin": "https://attacker.example"})
    assert status == 403
    assert json_request(app, "/api/products?search=ORIGIN-01")[1]["total"] == 0

    status, _, _ = request(app, "/api/summary", headers={"Host": "attacker.example"})
    assert status == 421


def test_numeric_validation_and_product_csv_formula_safety(app):
    status, error = json_request(app, "/api/settings", "POST", {"queue_region": {"x": True, "y": 0, "width": 1, "height": 1}})
    assert status == 400 and "coordinates" in error["error"]
    status, error = json_request(app, "/api/settings", "POST", {"video_sample_seconds": True})
    assert status == 400
    status, error = json_request(app, "/api/products", "POST", {
        "sku": "FRACTION-01", "name": "Bad quantity", "quantity": 1.5,
    })
    assert status == 400 and "whole number" in error["error"]

    status, result = create_product(app, sku="CSV-01")
    assert status == 201
    status, result = json_request(app, f"/api/products/{result['item']['id']}", "PUT", {"name": "=HYPERLINK(\"https://bad.example\")"})
    assert status == 200
    status, exported, _ = request(app, "/api/export/inventory.csv")
    assert status == 200
    assert b"'=HYPERLINK" in exported


def test_import_requires_csv_content_type_and_rejects_extra_columns_atomically(app):
    csv_text = "sku,name,category,quantity,unit,price,reorder_threshold\nA-1,Tea,Pantry,3,boxes,4.2,5\n"
    status, _, _ = request(app, "/api/products/import", "POST", csv_text.encode(), "application/json")
    assert status == 400

    malformed = "sku,name,category,quantity,unit,price,reorder_threshold\nA-1,Tea,Pantry,3,boxes,4.2,5,extra\n"
    status, error, _ = request(app, "/api/products/import", "POST", malformed.encode(), "text/csv")
    assert status == 400 and b"more values" in error
    assert json_request(app, "/api/products?search=A-1")[1]["total"] == 0


def test_schema_version_and_startup_recovers_interrupted_jobs(app):
    job_id = "e" * 32
    stamp = server.now_iso()
    with server.connect() as db:
        with db:
            db.execute("INSERT INTO video_jobs(id,filename,input_path,status,created_at,updated_at) VALUES(?,?,?,'processing',?,?)", (job_id, "unfinished.mp4", "local", stamp, stamp))
        assert db.execute("PRAGMA user_version").fetchone()[0] == server.SCHEMA_VERSION
    server.init_db()
    with server.connect() as db:
        job = db.execute("SELECT status,error FROM video_jobs WHERE id=?", (job_id,)).fetchone()
    assert job["status"] == "failed"
    assert "Upload the video again" in job["error"]


def test_canceled_job_cannot_be_deleted_while_worker_is_still_alive(app):
    job_id = "f" * 32
    input_path = server.UPLOADS / f"{job_id}.mp4"
    output_path = server.OUTPUTS / f"{job_id}.mp4"
    input_path.write_bytes(b"source")
    output_path.write_bytes(b"output")
    stamp = server.now_iso()
    with server.connect() as db:
        with db:
            db.execute("INSERT INTO video_jobs(id,filename,input_path,output_path,status,created_at,updated_at) VALUES(?,?,?,?,'canceled',?,?)", (job_id, "cancelled.mp4", str(input_path), str(output_path), stamp, stamp))
    stop = threading.Event()
    worker = threading.Thread(target=stop.wait, daemon=True)
    server.INFERENCE_THREADS[job_id] = worker
    worker.start()
    try:
        status, error = json_request(app, f"/api/jobs/{job_id}/delete", "POST")
        assert status == 409 and "still stopping" in error["error"]
        assert input_path.is_file() and output_path.is_file()
    finally:
        stop.set()
        worker.join(timeout=2)
    status, _ = json_request(app, f"/api/jobs/{job_id}/delete", "POST")
    assert status == 200
    assert not input_path.exists() and not output_path.exists()
    assert json_request(app, f"/api/jobs/{job_id}")[0] == 404
    server.INFERENCE_THREADS.pop(job_id, None)


def test_new_video_upload_waits_for_canceled_worker_to_exit(app):
    job_id = "d" * 32
    stop = threading.Event()
    worker = threading.Thread(target=stop.wait, daemon=True)
    server.INFERENCE_THREADS[job_id] = worker
    worker.start()
    try:
        status, error, _ = request(app, "/api/videos?filename=valid-looking.mp4", "POST", b"0000ftypxxxx", "application/octet-stream")
        assert status == 409 and b"already processing" in error
        assert json_request(app, "/api/jobs")[1]["items"] == []
    finally:
        stop.set()
        worker.join(timeout=2)
        server.INFERENCE_THREADS.pop(job_id, None)


def test_loopback_binding_and_rate_limiter():
    assert server.loopback_bind_host("localhost") == "127.0.0.1"
    assert server.loopback_bind_host("127.0.0.2") == "127.0.0.2"
    for host in ("0.0.0.0", "192.0.2.10", "retailx.example"):
        with pytest.raises(ValueError):
            server.loopback_bind_host(host)

    limiter = server.RollingRateLimiter()
    assert limiter.consume("test", "127.0.0.1", 2, 60)[0]
    assert limiter.consume("test", "127.0.0.1", 2, 60)[0]
    allowed, retry = limiter.consume("test", "127.0.0.1", 2, 60)
    assert not allowed and 1 <= retry <= 60


def test_static_assets_robots_sitemap_and_empty_export_headers(app):
    for path in ("/", "/styles.css", "/app.js", "/favicon.ico", "/favicon.svg", "/social-preview.png", "/privacy.html", "/terms.html"):
        status, _, _ = request(app, path)
        assert status == 200, path

    status, robots, _ = request(app, "/robots.txt")
    assert status == 200 and b"Disallow: /" in robots
    status, sitemap, _ = request(app, "/sitemap.xml")
    assert status == 200
    import xml.etree.ElementTree as ET
    assert ET.fromstring(sitemap).tag.endswith("urlset")

    for name, header in (("inventory.csv", b"sku,name,category"), ("movements.csv", b"sku,product_name"), ("alerts.csv", b"source,severity"), ("observations.csv", b"filename,timestamp_seconds")):
        status, body, _ = request(app, f"/api/export/{name}")
        assert status == 200 and body.lstrip(b"\xef\xbb\xbf").startswith(header), name


def test_rate_limiter_returns_retry_after_header(app, monkeypatch):
    class BlockEverything:
        def consume(self, scope, key, limit, window_seconds):
            return False, 17

    monkeypatch.setattr(server, "REQUEST_LIMITER", BlockEverything())
    status, body, headers = request(app, "/api/summary")
    assert status == 429 and headers["Retry-After"] == "17"
    assert b"Too many requests" in body
