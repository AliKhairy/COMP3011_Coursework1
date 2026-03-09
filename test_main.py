# Ensure these are at the VERY TOP of your test_main.py file
import pytest
from fastapi.testclient import TestClient
import sqlite3
from unittest.mock import patch
from datetime import datetime, timezone, timedelta
import main  # <-- Import the module, not the specific function
from main import app, RATE_LIMIT_STORE

# We use a shared IN-MEMORY database. No files are created, bypassing WinError 32!
TEST_DB = "file:memdb?mode=memory&cache=shared"


@pytest.fixture(autouse=True)
def setup_test_environment():
    """
    Runs BEFORE every test. Uses an in-memory DB and disables background tasks.
    """
    # 1. Clear the Rate Limiter
    RATE_LIMIT_STORE.clear()

    # 2. Patch the DB connection to use RAM
    def override_get_db_connection():
        # uri=True allows the special memory string to work across threads
        conn = sqlite3.connect(TEST_DB, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row

        # Create tables
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT, line_name TEXT, 
                delay_minutes INTEGER, observed_experience TEXT, 
                report_date TEXT, edit_token TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tfl_live_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT, line_name TEXT, 
                status TEXT, reason TEXT, timestamp TEXT
            )
        """)
        return conn

    # 3. Patch both the DB and disable the background worker simultaneously
    with patch('main.get_db_connection', side_effect=override_get_db_connection), \
            patch('main.update_data_periodically'):

        # Wipe tables clean before the test starts (since RAM is shared)
        with override_get_db_connection() as conn:
            conn.execute("DELETE FROM user_reports")
            conn.execute("DELETE FROM tfl_live_status")
            conn.commit()

        yield  # THIS IS WHERE THE TEST RUNS


@pytest.fixture
def client():
    return TestClient(app)

# --- 1. ENDPOINT TESTS: ROOT & HEALTH ---


def test_root_endpoint(client):
    response = client.get("/")
    assert response.status_code == 200


def test_health_check(client):
    response = client.get("/health")

    # It should be 200 OK because the API is alive, it's just waiting for data
    assert response.status_code == 200

    data = response.json()
    assert data["database_status"] == "connected"
    assert data["background_worker_status"] == "waiting for initial sync"

# --- 2. ENDPOINT TESTS: RATE LIMITING ---


def test_strict_rate_limiting(client):
    payload = {"line_name": "Victoria", "delay_minutes": 10,
               "observed_experience": "Stuck in tunnel"}

    # Send 3 successful requests
    for _ in range(3):
        assert client.post("/reports", json=payload).status_code == 201

    # 4th MUST be blocked
    res_blocked = client.post("/reports", json=payload)
    assert res_blocked.status_code == 429

# --- 3. ENDPOINT TESTS: DATA INTEGRITY ---


def test_create_report_invalid_line(client):
    payload = {"line_name": "Hogwarts Express", "delay_minutes": 15,
               "observed_experience": "Stuck in tunnel"}
    assert client.post("/reports", json=payload).status_code == 422


def test_create_report_impossible_delay(client):
    payload = {"line_name": "Central", "delay_minutes": 350,
               "observed_experience": "Stuck in tunnel"}
    assert client.post("/reports", json=payload).status_code == 422

# --- 4. ENDPOINT TESTS: ADVANCED Z-SCORE ANOMALY ---


def test_z_score_anomaly_rejection(client):
    from main import get_db_connection

    # 1. Seed baseline directly via SQL to avoid triggering the API rate limiter!
    with get_db_connection() as conn:
        for _ in range(5):
            conn.execute("""
                INSERT INTO user_reports (line_name, delay_minutes, observed_experience, report_date, edit_token)
                VALUES ('Jubilee', 5, 'Train moving at a crawling pace', datetime('now'), 'test-token')
            """)
        conn.commit()

    # 2. Hit the API with an extreme outlier
    outlier = client.post("/reports", json={"line_name": "Jubilee",
                          "delay_minutes": 60, "observed_experience": "Stuck in tunnel"})
    assert outlier.status_code == 422
    assert "anomaly detected" in outlier.json()["detail"].lower()

# --- 5. ENDPOINT TESTS: SECURITY (IDOR) ---


def test_security_put_and_delete_requires_token(client):
    create_res = client.post(
        "/reports", json={"line_name": "Piccadilly", "delay_minutes": 15, "observed_experience": "Stuck in tunnel"})
    report_id = create_res.json()["id"]
    real_token = create_res.json()["edit_token"]
    update_payload = {"line_name": "Piccadilly",
                      "delay_minutes": 20, "observed_experience": "Stuck in tunnel"}

    # Fails without token or with fake token
    assert client.put(f"/reports/{report_id}",
                      json=update_payload).status_code == 422
    assert client.put(f"/reports/{report_id}", json=update_payload,
                      headers={"x-edit-token": "hacker123"}).status_code == 403
    assert client.delete(
        f"/reports/{report_id}", headers={"x-edit-token": "hacker123"}).status_code == 403

    # Succeeds with real token
    assert client.delete(
        f"/reports/{report_id}", headers={"x-edit-token": real_token}).status_code == 204

# --- 6. ENDPOINT TESTS: DISCREPANCY ENGINE ---


def test_severity_mismatch_engine():
    from main import get_db_connection

    # 1. Seed TfL data saying "Good Service"
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO tfl_live_status (line_name, status, timestamp) VALUES ('Central', 'Good Service', datetime('now'))")

        # 2. Seed 3 user reports saying there is a 30 min delay
        for _ in range(3):
            conn.execute(
                "INSERT INTO user_reports (line_name, delay_minutes, report_date) VALUES ('Central', 30, datetime('now'))")
        conn.commit()

    client_test = TestClient(app)
    res = client_test.get("/analytics/discrepancies")
    assert res.status_code == 200

    data = res.json()[0]
    assert data["line_name"] == "Central"
    assert data["official_status"] == "Good Service"
    assert data["crowd_consensus_minutes"] == 30.0

# --- 7. ENDPOINT TESTS: PAGINATION & LIMITS ---


def test_pagination_and_limits(client):
    current_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Use main.get_db_connection() to guarantee we hit the mocked RAM DB
    with main.get_db_connection() as conn:
        for i in range(5):
            conn.execute("""
                INSERT INTO user_reports (line_name, delay_minutes, observed_experience, report_date, edit_token)
                VALUES ('Bakerloo', ?, 'Stuck in tunnel', ?, 'token')
            """, (10 + i, current_utc))
        conn.commit()

    res_limit = client.get("/reports?limit=2")
    assert res_limit.status_code == 200
    assert len(res_limit.json()) == 2

    res_skip = client.get("/reports?skip=3&limit=5")
    assert res_skip.status_code == 200
    assert len(res_skip.json()) == 2

    res_abuse = client.get("/reports?limit=101")
    assert res_abuse.status_code == 422

# --- 8. ENDPOINT TESTS: ENUM FILTERING ---


def test_query_filtering(client):
    current_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    with main.get_db_connection() as conn:
        records = [
            ('Victoria', 10, 'Stuck in tunnel', current_utc),
            ('Victoria', 15, 'Platform dangerously crowded', current_utc),
            ('District', 20, 'Stuck in tunnel', current_utc)
        ]
        for r in records:
            conn.execute("""
                INSERT INTO user_reports (line_name, delay_minutes, observed_experience, report_date, edit_token)
                VALUES (?, ?, ?, ?, 'token')
            """, r)
        conn.commit()

    res_line = client.get("/reports?line_name=Victoria")
    assert res_line.status_code == 200
    assert len(res_line.json()) == 2

    res_exp = client.get("/reports?experience=Stuck in tunnel")
    assert res_exp.status_code == 200
    assert len(res_exp.json()) == 2

    res_both = client.get("/reports?line_name=Victoria&experience=Stuck in tunnel")
    assert res_both.status_code == 200
    assert len(res_both.json()) == 1
    assert res_both.json()[0]["line_name"] == "Victoria"

# --- 9. ENDPOINT TESTS: SYSTEM HEALTH & STALE DATA ---


def test_health_check_healthy(client):
    current_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    with main.get_db_connection() as conn:
        conn.execute("DELETE FROM tfl_live_status")
        conn.execute("""
            INSERT INTO tfl_live_status (line_name, status, reason, timestamp)
            VALUES ('Central', 'Good Service', 'N/A', ?)
        """, (current_utc,))
        conn.commit()

    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["database_status"] == "connected"
    assert data["background_worker_status"] == "healthy"


def test_health_check_stale_data(client):
    # timedelta is now safely defined at the top of the file
    stale_time = (datetime.now(timezone.utc) - timedelta(minutes=15)
                  ).strftime("%Y-%m-%d %H:%M:%S")

    with main.get_db_connection() as conn:
        conn.execute("DELETE FROM tfl_live_status")
        conn.execute("""
            INSERT INTO tfl_live_status (line_name, status, reason, timestamp)
            VALUES ('Bakerloo', 'Severe Delays', 'Signal Failure', ?)
        """, (stale_time,))
        conn.commit()

    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["database_status"] == "connected"
    assert "degraded" in data["background_worker_status"]
