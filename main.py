import uuid
from fastapi import FastAPI, HTTPException, Request, Depends, Header, Query
import sqlite3
from datetime import datetime, timezone
import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from import_tfl_data import fetch_tfl_data
import time  # Add this to your imports at the top if you don't have it
import math
from enum import Enum
from pydantic import BaseModel, Field
from contextlib import closing

# --- GLOBAL APP STATE ---
API_START_TIME = time.time()

# --- SECURITY: IN-MEMORY RATE LIMITER ---
RATE_LIMIT_STORE = defaultdict(list)
MAX_REPORTS_PER_HOUR = 3


def verify_rate_limit(request: Request):
    client_ip = request.client.host
    current_time = time.time()

    # 1. Purge timestamps older than 1 hour (3600 seconds) from this IP's history
    RATE_LIMIT_STORE[client_ip] = [
        t for t in RATE_LIMIT_STORE[client_ip] if current_time - t < 3600]

    # 2. Check if the user has hit the maximum allowed requests
    if len(RATE_LIMIT_STORE[client_ip]) >= MAX_REPORTS_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. To ensure data integrity, you are limited to 3 reports per hour."
        )

    # 3. Log the new request timestamp
    RATE_LIMIT_STORE[client_ip].append(current_time)


# --- PROFESSIONAL LOGGING SETUP ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- ROBUST BACKGROUND TASK ---


async def update_data_periodically():
    logger.info("Background auto-update task initialized.")
    while True:
        try:
            logger.info("Triggering TfL live data fetch...")
            # We run the fetch script and force Uvicorn to acknowledge it
            await asyncio.to_thread(fetch_tfl_data)
            logger.info(
                "TfL data successfully updated. Sleeping for 5 minutes.")
        except Exception as e:
            logger.error(f"CRITICAL: Background task failed - {e}")

        await asyncio.sleep(300)


async def cleanup_rate_limiter():
    """Background task to delete stale IP records from memory to prevent leaks."""
    while True:
        current_time = time.time()
        # Find IPs that haven't made a request in the last hour
        stale_ips = [
            ip for ip, timestamps in RATE_LIMIT_STORE.items()
            if not timestamps or current_time - timestamps[-1] > 3600
        ]
        # Delete them completely from memory
        for ip in stale_ips:
            del RATE_LIMIT_STORE[ip]

        await asyncio.sleep(3600)  # Run cleanup once every hour


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start both background tasks
    fetch_task = asyncio.create_task(update_data_periodically())
    cleanup_task = asyncio.create_task(cleanup_rate_limiter())
    yield
    fetch_task.cancel()
    cleanup_task.cancel()


app = FastAPI(
    title="London Underground Reliability API",
    description="A dual-model API tracking user-reported delays and official TfL live statuses with background auto-updating.",
    version="1.0.0",
    lifespan=lifespan
)

# --- PYDANTIC MODELS (Data Validation) ---


class SystemHealth(BaseModel):
    api_uptime_seconds: float
    database_status: str
    background_worker_status: str
    last_tfl_sync: str | None


class TfLLine(str, Enum):
    bakerloo = "Bakerloo"
    central = "Central"
    circle = "Circle"
    district = "District"
    elizabeth = "Elizabeth line"
    hammersmith = "Hammersmith & City"
    jubilee = "Jubilee"
    metropolitan = "Metropolitan"
    northern = "Northern"
    piccadilly = "Piccadilly"
    victoria = "Victoria"
    waterloo = "Waterloo & City"


class UserReportResponse(BaseModel):
    id: int
    line_name: str
    delay_minutes: int
    observed_experience: str
    report_date: str


class UserMetrics(BaseModel):
    total_reports: int
    crowd_consensus_delay: float
    peak_delay: int
    buffer_time_index: float


class TflStatusResponse(BaseModel):
    id: int
    line_name: str
    status: str
    reason: str | None
    timestamp: str


class DiscrepancyResponse(BaseModel):
    line_name: str
    official_status: str
    corroborating_reports: int
    crowd_consensus_minutes: float
    peak_delay_minutes: int
    confidence_level: str


class ReliabilityScore(BaseModel):
    line_name: str
    official_status: str
    reliability_percentage: float
    assessment: str
    user_metrics: UserMetrics


class DelayPattern(BaseModel):
    observed_experience: str
    incident_count: int
    average_delay_minutes: float
    peak_delay_minutes: int


class ObservedExperience(str, Enum):
    stuck_in_tunnel = "Stuck in tunnel"
    crawling_pace = "Train moving at a crawling pace"
    platform_crowded = "Platform dangerously crowded"
    ghost_train = "Train cancelled/disappeared from board"
    unknown = "Not sure, just delayed"


class TemporalSummary(BaseModel):
    day_of_week: str
    hour_of_day: str
    total_incidents: int


class LineUptime(BaseModel):
    line_name: str
    uptime_percentage: float
    official_disruption_snapshots: int  # Replaces the fragile minutes calculation
    current_status: str


class DelayVelocity(BaseModel):
    line_name: str
    current_hour_delay_minutes: int
    previous_hour_delay_minutes: int
    trend: str
    velocity_assessment: str


class UserReportCreateResponse(BaseModel):
    id: int
    line_name: TfLLine
    delay_minutes: int
    observed_experience: ObservedExperience
    report_date: str
    edit_token: str  # The secret token given to the creator


class UserReportCreate(BaseModel):
    line_name: TfLLine
    # Enforce realistic math right at the front door
    delay_minutes: int = Field(..., gt=0, le=300,
                               description="Delay in minutes (1 to 300)")
    observed_experience: ObservedExperience

# --- DATABASE HELPER ---


def get_db_connection():
    # check_same_thread=False prevents crashes when FastAPI and background workers share the DB
    conn = sqlite3.connect("transport_api.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

# --- ENDPOINTS (User Reports CRUD) ---


@app.get("/")
def root():
    return {"message": "Welcome to the London Underground Reliability API"}

# 1. CREATE (Secured with Rate Limiting and Dynamic Anomaly Detection)


@app.post("/reports", response_model=UserReportCreateResponse, status_code=201, dependencies=[Depends(verify_rate_limit)])
def create_report(report: UserReportCreate):
    # Notice we don't need the 'if delay_minutes > 300' check here anymore.
    # Pydantic handles it automatically and returns a 422 error if violated!

    with closing(get_db_connection()) as conn:
        cursor = conn.cursor()

        # --- Z-SCORE ANOMALY DETECTION ---
        # report.line_name.value extracts the string from the Enum
        cursor.execute("""
            SELECT delay_minutes FROM user_reports 
            WHERE line_name = ? COLLATE NOCASE
            AND report_date >= datetime('now', '-2 hours')
        """, (report.line_name.value,))

        recent_delays = [row["delay_minutes"] for row in cursor.fetchall()]

        if len(recent_delays) >= 5:
            mean = sum(recent_delays) / len(recent_delays)
            variance = sum(
                (x - mean) ** 2 for x in recent_delays) / len(recent_delays)
            std_dev = math.sqrt(variance)

            # THE FIX: Check for a "Breaking Incident" (multiple high delays recently)
            breaking_incident = sum(1 for d in recent_delays if d >= 30) >= 2

            if std_dev > 0 and not breaking_incident:
                z_score = (report.delay_minutes - mean) / std_dev
                if z_score > 3.0 and report.delay_minutes > 30:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Statistical anomaly detected. Report rejected. (Z-Score: {round(z_score, 2)})"
                    )
            elif report.delay_minutes > mean + 45 and not breaking_incident:
                raise HTTPException(
                    status_code=422, detail="Statistical anomaly detected. Report rejected.")

        # --- GENERATE SECURE EDIT TOKEN ---
        # This creates a unique string like "123e4567-e89b-12d3-a456-426614174000"
        edit_token = str(uuid.uuid4())
        report_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # --- SAFE DATABASE INSERT ---
        cursor.execute("""
            INSERT INTO user_reports (line_name, delay_minutes, observed_experience, report_date, edit_token) 
            VALUES (?, ?, ?, ?, ?)
        """, (report.line_name.value, report.delay_minutes, report.observed_experience.value, report_date, edit_token))

        conn.commit()
        new_id = cursor.lastrowid

    # Return the data back to the user
    return {
        "id": new_id,
        "line_name": report.line_name,
        "delay_minutes": report.delay_minutes,
        "observed_experience": report.observed_experience,
        "report_date": report_date,
        "edit_token": edit_token
    }

# 2. READ (Paginated, Filtered, and Protected)


@app.get("/reports", response_model=list[UserReportResponse])
def get_reports(
    skip: int = Query(0, ge=0, description="Records to skip for pagination"),
    limit: int = Query(
        50, ge=1, le=100, description="Max 100 records per request to prevent DoS"),
    line_name: TfLLine | None = None,
    experience: ObservedExperience | None = None
):
    with closing(get_db_connection()) as conn:
        cursor = conn.cursor()

        # Dynamic query building based on provided filters
        query = "SELECT id, line_name, delay_minutes, observed_experience, report_date FROM user_reports WHERE 1=1"
        params = []

        if line_name:
            query += " AND line_name = ?"
            params.append(line_name.value)

        if experience:
            query += " AND observed_experience = ?"
            params.append(experience.value)

        query += " ORDER BY report_date DESC LIMIT ? OFFSET ?"
        params.extend([limit, skip])

        cursor.execute(query, params)
        reports = cursor.fetchall()

    return [dict(row) for row in reports]

# 3. UPDATE (Secured with Edit Token)


@app.put("/reports/{report_id}", response_model=UserReportResponse)
def update_report(
    report_id: int,
    report: UserReportCreate,
    x_edit_token: str = Header(
        ..., description="The secret token provided when the report was created")
):
    with closing(get_db_connection()) as conn:
        cursor = conn.cursor()

        # Security Check: Retrieve the existing token from the database
        cursor.execute(
            "SELECT edit_token FROM user_reports WHERE id = ?", (report_id,))
        existing_report = cursor.fetchone()

        if not existing_report:
            raise HTTPException(status_code=404, detail="Report not found.")

        # Verify the token
        if existing_report["edit_token"] != x_edit_token:
            raise HTTPException(
                status_code=403, detail="Forbidden: Invalid edit token. You can only edit your own reports.")

        # If the token matches, proceed with the update!
        cursor.execute("""
            UPDATE user_reports 
            SET line_name = ?, delay_minutes = ?, observed_experience = ? 
            WHERE id = ?
        """, (report.line_name.value, report.delay_minutes, report.observed_experience.value, report_id))

        conn.commit()

        # Fetch the updated data to return to the user
        cursor.execute(
            "SELECT id, line_name, delay_minutes, observed_experience, report_date FROM user_reports WHERE id = ?", (report_id,))
        updated_row = cursor.fetchone()

    return dict(updated_row)

# 4. DELETE (Secured with Edit Token)


@app.delete("/reports/{report_id}", status_code=204)
def delete_report(
    report_id: int,
    x_edit_token: str = Header(
        ..., description="The secret token provided when the report was created")
):
    with closing(get_db_connection()) as conn:
        cursor = conn.cursor()

        # Security Check: Retrieve the existing token
        cursor.execute(
            "SELECT edit_token FROM user_reports WHERE id = ?", (report_id,))
        existing_report = cursor.fetchone()

        if not existing_report:
            raise HTTPException(status_code=404, detail="Report not found.")

        # Verify the token
        if existing_report["edit_token"] != x_edit_token:
            raise HTTPException(
                status_code=403, detail="Forbidden: Invalid edit token. You can only delete your own reports.")

        # Token is valid, delete the record
        cursor.execute("DELETE FROM user_reports WHERE id = ?", (report_id,))
        conn.commit()

    return None

# 5. READ (Live TfL Status - Latest Snapshot Only)


@app.get("/live-status", response_model=list[TflStatusResponse])
def get_live_status():
    with closing(get_db_connection()) as conn:
        cursor = conn.cursor()

        # Retrieve ONLY the most recent status check for each individual line
        cursor.execute("""
            SELECT * FROM tfl_live_status 
            WHERE id IN (SELECT MAX(id) FROM tfl_live_status GROUP BY line_name)
            ORDER BY line_name ASC
        """)
        statuses = cursor.fetchall()

    if not statuses:
        raise HTTPException(
            status_code=404, detail="No live data found. Please run the import script.")

    return [dict(row) for row in statuses]

# NEW ENDPOINT 5b: Targeted History Search


@app.get("/live-status/{line_name}/history", response_model=list[TflStatusResponse])
def get_live_status_history(
    line_name: TfLLine,
    limit: int = Query(
        20, ge=1, le=100, description="Limit the number of historical records returned")
):
    """Fetches the recent history of official TfL statuses for a specific line."""
    with closing(get_db_connection()) as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT * FROM tfl_live_status 
            WHERE line_name = ? COLLATE NOCASE
            ORDER BY timestamp DESC LIMIT ?
        """, (line_name.value, limit))
        history = cursor.fetchall()

    if not history:
        raise HTTPException(
            status_code=404, detail=f"No history found for {line_name.value}.")

    return [dict(row) for row in history]


# --- HELPER DICTIONARY FOR SEVERITY ---
TFL_SEVERITY_MINUTES = {
    "Good Service": 0,
    "Minor Delays": 15,
    "Severe Delays": 45,
    "Part Suspended": 60,
    "Suspended": 120,
    "Planned Closure": 0  # Ignore planned closures for this specific metric
}

# 6. ADVANCED ANALYTICS (The Severity Mismatch Engine)


@app.get("/analytics/discrepancies", response_model=list[DiscrepancyResponse])
def get_discrepancies():
    with closing(get_db_connection()) as conn:
        cursor = conn.cursor()

        # 1. Get the absolute latest TfL status for each line
        cursor.execute("""
            SELECT line_name, status 
            FROM tfl_live_status 
            WHERE id IN (SELECT MAX(id) FROM tfl_live_status GROUP BY line_name)
        """)
        tfl_latest = cursor.fetchall()

        # 2. Get user consensus from the last 2 hours
        cursor.execute("""
            SELECT 
                line_name, 
                COUNT(id) AS corroborating_reports,
                AVG(delay_minutes) AS avg_delay,
                MAX(delay_minutes) AS max_delay
            FROM user_reports
            WHERE report_date >= datetime('now', '-2 hours')
            GROUP BY line_name
        """)
        user_reports = cursor.fetchall()

    # Convert reports to a dictionary for easy lookup
    crowd_data = {row["line_name"]: row for row in user_reports}

    formatted_results = []

    # 3. The Mismatch Engine
    for official in tfl_latest:
        line = official["line_name"]
        status = official["status"]

        if line in crowd_data:
            crowd = crowd_data[line]
            reports_count = crowd["corroborating_reports"]
            avg_delay = round(crowd["avg_delay"], 1)

            # Estimate what TfL's status *should* mean in minutes
            expected_tfl_delay = TFL_SEVERITY_MINUTES.get(status, 0)

            # If the crowd is experiencing delays at least 15 minutes WORSE than TfL admits
            if (avg_delay - expected_tfl_delay) >= 15:

                if reports_count >= 5:
                    confidence = "High (Confirmed by crowd)"
                elif reports_count >= 2:
                    confidence = "Medium (Multiple reports)"
                else:
                    confidence = "Low (Unverified single report)"

                formatted_results.append({
                    "line_name": line,
                    "official_status": status,
                    "corroborating_reports": reports_count,
                    "crowd_consensus_minutes": avg_delay,
                    "peak_delay_minutes": crowd["max_delay"],
                    "confidence_level": confidence
                })

    if not formatted_results:
        raise HTTPException(
            status_code=404,
            detail="No discrepancies found. TfL data currently matches crowd reality."
        )

    return formatted_results

# 7. HISTORICAL ANALYTICS (24-Hour Network Uptime)


@app.get("/analytics/uptime", response_model=list[LineUptime])
def get_network_uptime():
    with closing(get_db_connection()) as conn:
        cursor = conn.cursor()

        # This query counts good vs bad statuses over the last 24 hours
        query = """
            SELECT 
                line_name,
                COUNT(id) as total_checks,
                SUM(CASE WHEN status = 'Good Service' THEN 1 ELSE 0 END) as good_checks,
                SUM(CASE WHEN status != 'Good Service' THEN 1 ELSE 0 END) as bad_checks
            FROM tfl_live_status
            WHERE timestamp >= datetime('now', '-24 hours')
            GROUP BY line_name
            ORDER BY bad_checks DESC
        """

        cursor.execute(query)
        history = cursor.fetchall()

        # Grab the latest statuses to attach to the payload
        cursor.execute("""
            SELECT line_name, status 
            FROM tfl_live_status 
            WHERE id IN (SELECT MAX(id) FROM tfl_live_status GROUP BY line_name)
        """)
        latest_statuses = {row["line_name"]: row["status"]
                           for row in cursor.fetchall()}

    if not history:
        raise HTTPException(
            status_code=404, detail="Not enough historical data collected yet. Let the background worker run.")

    results = []
    for row in history:
        total = row["total_checks"]
        good = row["good_checks"]
        bad = row["bad_checks"]

        uptime_pct = round((good / total) * 100, 1) if total > 0 else 0.0

        results.append({
            "line_name": row["line_name"],
            "uptime_percentage": uptime_pct,
            "official_disruption_snapshots": bad,
            "current_status": latest_statuses.get(row["line_name"], "Unknown")
        })

    return results

# --- INFRASTRUCTURE & VELOCITY ---

# 8. SYSTEM HEALTH CHECK (Deep Diagnostic & Uptime)


@app.get("/health", response_model=SystemHealth)
def get_health_check():
    # Calculate exact API uptime dynamically
    current_uptime = round(time.time() - API_START_TIME, 2)

    health_report = {
        "api_uptime_seconds": current_uptime,
        "database_status": "unknown",
        "background_worker_status": "unknown",
        "last_tfl_sync": None
    }

    try:
        # The 'with' block guarantees the connection closes, even if it crashes!
        with closing(get_db_connection()) as conn:
            cursor = conn.cursor()

            # True DB Ping
            cursor.execute("SELECT 1")
            health_report["database_status"] = "connected"

            # Background Worker Diagnostic
            cursor.execute(
                "SELECT MAX(timestamp) as last_sync FROM tfl_live_status")
            result = cursor.fetchone()

            if result and result["last_sync"]:
                last_sync_str = result["last_sync"]
                health_report["last_tfl_sync"] = last_sync_str

                # Enforce UTC timezone for accurate comparison
                last_sync_time = datetime.strptime(
                    last_sync_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                current_time = datetime.now(timezone.utc)
                time_since_sync = (
                    current_time - last_sync_time).total_seconds()

                # If background worker hasn't updated in 10+ minutes (600s), flag it
                if time_since_sync > 600:
                    health_report[
                        "background_worker_status"] = f"degraded (stale data: {int(time_since_sync)}s old)"
                else:
                    health_report["background_worker_status"] = "healthy"
            else:
                health_report["background_worker_status"] = "waiting for initial sync"

    except Exception as e:
        health_report["database_status"] = "disconnected or locked"
        health_report["background_worker_status"] = f"unreachable: {str(e)}"
        # 503 Service Unavailable is the correct industry standard here
        raise HTTPException(status_code=503, detail=health_report)

    return health_report

# 9. DELAY VELOCITY (Trending Analysis - UTC and Severity Based)


@app.get("/analytics/velocity/{line_name}", response_model=DelayVelocity)
def get_delay_velocity(line_name: TfLLine):  # <-- Upgraded to strict Enum
    with closing(get_db_connection()) as conn:
        cursor = conn.cursor()

        # Calculate AVERAGE and MAX severity in the current 60-minute window
        cursor.execute("""
            SELECT 
                AVG(delay_minutes) as avg_delays,
                MAX(delay_minutes) as max_delays
            FROM user_reports 
            WHERE line_name = ? COLLATE NOCASE
            AND report_date >= datetime('now', '-60 minutes')
        """, (line_name.value,))
        current_result = cursor.fetchone()

        current_avg = round(current_result["avg_delays"] or 0, 1)
        current_max = current_result["max_delays"] or 0

        # Calculate AVERAGE and MAX severity in the PREVIOUS 60-minute window
        cursor.execute("""
            SELECT 
                AVG(delay_minutes) as past_avg,
                MAX(delay_minutes) as past_max
            FROM user_reports 
            WHERE line_name = ? COLLATE NOCASE
            AND report_date >= datetime('now', '-120 minutes')
            AND report_date < datetime('now', '-60 minutes')
        """, (line_name.value,))
        past_result = cursor.fetchone()

        past_avg = round(past_result["past_avg"] or 0, 1)
        past_max = past_result["past_max"] or 0

    # Determine mathematical trajectory based on Average Consensus
    difference = round(current_avg - past_avg, 1)

    if current_avg == 0 and past_avg == 0:
        trend = "Stable"
        assessment = "No recent disruptions."
    elif difference > 0:
        trend = f"+{difference} minutes"
        assessment = f"Accelerating (Peak severity currently at {current_max} mins)"
    elif difference < 0:
        trend = f"{difference} minutes"
        assessment = f"Resolving (Peak severity dropped from {past_max} to {current_max} mins)"
    else:
        trend = "0.0 minutes"
        assessment = "Stagnant (Disruption severity is unchanged)"

    return {
        "line_name": line_name.value.capitalize(),
        "current_hour_delay_minutes": int(current_avg),
        "previous_hour_delay_minutes": int(past_avg),
        "trend": trend,
        "velocity_assessment": assessment
    }

# --- ADVANCED ANALYTICS (Section 3b Requirements) ---

# 1. ENHANCED Route-Level Reliability Scores (Strict Assessment)


@app.get("/analytics/reliability/{line_name}", response_model=ReliabilityScore)
def get_reliability_score(line_name: TfLLine):  # <-- Enforce Enum here
    with closing(get_db_connection()) as conn:
        cursor = conn.cursor()

        # 1. Get ONLY the Latest Official Status
        cursor.execute("""
            SELECT status FROM tfl_live_status 
            WHERE line_name = ? COLLATE NOCASE 
            ORDER BY timestamp DESC LIMIT 1
        """, (line_name.value,))
        tfl_data = cursor.fetchone()

        if not tfl_data:
            raise HTTPException(
                status_code=404, detail=f"Line '{line_name.value}' not found in TfL data.")

        official_status = tfl_data["status"]

        # 2. Get User Metrics strictly from the last 2 hours
        cursor.execute("""
            SELECT 
                COUNT(id) as total_reports,
                AVG(delay_minutes) as avg_delay,
                MAX(delay_minutes) as max_delay
            FROM user_reports 
            WHERE line_name = ? COLLATE NOCASE
            AND report_date >= datetime('now', '-2 hours')
        """, (line_name.value,))
        metrics = cursor.fetchone()

    total_reports = metrics["total_reports"] or 0
    avg_delay = round(metrics["avg_delay"] or 0.0, 1)
    max_delay = metrics["max_delay"] or 0

    # Calculate the Buffer Time Index (Unpredictability)
    buffer_time = round(float(max_delay - avg_delay), 1)

    # STRICT SCORING ALGORITHM
    score = 100.0

    # 1. Official Infrastructure Penalty
    if official_status != "Good Service":
        score -= 30.0

    # 2. Consensus Delay Penalty (1.5 points per average minute lost)
    score -= (avg_delay * 1.5)

    # 3. Variance Penalty (Strictly punishing unpredictability)
    if buffer_time > 15:
        score -= 20.0  # Massive penalty if the delay fluctuates wildly
    elif buffer_time > 5:
        score -= 10.0

    score = max(0.0, min(100.0, round(score, 1)))

    # Stricter categorization
    if score >= 90:
        assessment = "Optimal (Highly Reliable)"
    elif score >= 70:
        assessment = "Acceptable Variance"
    elif score >= 40:
        assessment = "Degraded (High Commuter Risk)"
    else:
        assessment = "System Failure (Avoid Route)"

    return {
        "line_name": line_name.value,
        "official_status": official_status,
        "reliability_percentage": score,
        "assessment": assessment,
        "user_metrics": {
            "total_reports": total_reports,
            "crowd_consensus_delay": avg_delay,
            "peak_delay": max_delay,
            "buffer_time_index": buffer_time
        }
    }

# 2. ENHANCED Delay Patterns


@app.get("/analytics/patterns", response_model=list[DelayPattern])
def get_delay_patterns():
    with closing(get_db_connection()) as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT 
                observed_experience, 
                COUNT(id) as incident_count,
                AVG(delay_minutes) as average_delay_minutes,
                MAX(delay_minutes) as peak_delay_minutes
            FROM user_reports 
            WHERE report_date >= datetime('now', '-30 days')
            GROUP BY observed_experience 
            ORDER BY incident_count DESC, peak_delay_minutes DESC
        """)
        patterns = cursor.fetchall()

    return [dict(row) for row in patterns]

# 3. Temporal Performance Summaries (Delays by time of day)


# Swapped List for list
@app.get("/analytics/temporal", response_model=list[TemporalSummary])
def get_temporal_summary():
    # THE FIX: Safely wrap the connection
    with closing(get_db_connection()) as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT 
                strftime('%w', report_date) as day_numeric,
                strftime('%H', report_date) as hour_of_day, 
                COUNT(id) as total_incidents
            FROM user_reports
            WHERE report_date >= datetime('now', '-30 days')
            GROUP BY day_numeric, hour_of_day
            ORDER BY day_numeric ASC, hour_of_day ASC
        """)
        summaries = cursor.fetchall()

    day_mapping = {"0": "Sunday", "1": "Monday", "2": "Tuesday",
                   "3": "Wednesday", "4": "Thursday", "5": "Friday", "6": "Saturday"}

    return [
        {
            "day_of_week": day_mapping.get(row["day_numeric"], "Unknown"),
            "hour_of_day": row["hour_of_day"],
            "total_incidents": row["total_incidents"]
        }
        for row in summaries
    ]
