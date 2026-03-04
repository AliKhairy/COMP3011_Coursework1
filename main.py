from fastapi import FastAPI, HTTPException, Request, Depends
from pydantic import BaseModel
from typing import List
import sqlite3
from datetime import datetime
import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from import_tfl_data import fetch_tfl_data

import time # Add this to your imports at the top if you don't have it

# --- GLOBAL APP STATE ---
API_START_TIME = time.time()

# --- SECURITY: IN-MEMORY RATE LIMITER ---
RATE_LIMIT_STORE = defaultdict(list)
MAX_REPORTS_PER_HOUR = 3

def verify_rate_limit(request: Request):
    client_ip = request.client.host
    current_time = time.time()
    
    # 1. Purge timestamps older than 1 hour (3600 seconds) from this IP's history
    RATE_LIMIT_STORE[client_ip] = [t for t in RATE_LIMIT_STORE[client_ip] if current_time - t < 3600]
    
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
            logger.info("TfL data successfully updated. Sleeping for 5 minutes.")
        except Exception as e:
            logger.error(f"CRITICAL: Background task failed - {e}")
        
        await asyncio.sleep(300)

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(update_data_periodically())
    yield
    task.cancel()

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


class UserReportCreate(BaseModel):
    line_name: str
    delay_minutes: int
    issue_type: str

class UserReportResponse(BaseModel):
    id: int
    line_name: str
    delay_minutes: int
    issue_type: str
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
    issue_type: str
    incident_count: int
    average_delay_minutes: float
    peak_delay_minutes: int

class TemporalSummary(BaseModel):
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

# --- DATABASE HELPER ---
def get_db_connection():
    conn = sqlite3.connect("transport_api.db")
    conn.row_factory = sqlite3.Row
    return conn

# --- ENDPOINTS (User Reports CRUD) ---
@app.get("/")
def root():
    return {"message": "Welcome to the London Underground Reliability API"}

# 1. CREATE (Secured with IP Rate Limiting)
@app.post("/reports", response_model=UserReportResponse, status_code=201, dependencies=[Depends(verify_rate_limit)])
def create_report(report: UserReportCreate):
    conn = get_db_connection()
    cursor = conn.cursor()
    # Enforcing strict UTC for database inserts to prevent timezone drift
    report_date = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    
    cursor.execute(
        "INSERT INTO user_reports (line_name, delay_minutes, issue_type, report_date) VALUES (?, ?, ?, ?)",
        (report.line_name, report.delay_minutes, report.issue_type, report_date)
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    
    return {**report.model_dump(), "id": new_id, "report_date": report_date}

# 2. READ (Paginated & Filtered)
@app.get("/reports", response_model=List[UserReportResponse])
def get_reports(skip: int = 0, limit: int = 50, line_name: str | None = None):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if line_name:
        cursor.execute(
            "SELECT * FROM user_reports WHERE line_name = ? COLLATE NOCASE ORDER BY report_date DESC LIMIT ? OFFSET ?",
            (line_name, limit, skip)
        )
    else:
        cursor.execute(
            "SELECT * FROM user_reports ORDER BY report_date DESC LIMIT ? OFFSET ?",
            (limit, skip)
        )
        
    reports = cursor.fetchall()
    conn.close()
    return [dict(row) for row in reports]

# 3. UPDATE
@app.put("/reports/{report_id}", response_model=UserReportResponse)
def update_report(report_id: int, report: UserReportCreate):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # FIX: Changed 'cause' to 'issue_type' to match the Pydantic model
    cursor.execute(
        "UPDATE user_reports SET line_name = ?, delay_minutes = ?, issue_type = ? WHERE id = ?",
        (report.line_name, report.delay_minutes, report.issue_type, report_id)
    )
    if cursor.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="Report not found")
    
    conn.commit()
    
    cursor.execute("SELECT * FROM user_reports WHERE id = ?", (report_id,))
    updated_row = cursor.fetchone()
    conn.close()
    
    return dict(updated_row)

# 4. DELETE
@app.delete("/reports/{report_id}", status_code=204)
def delete_report(report_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM user_reports WHERE id = ?", (report_id,))
    if cursor.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="Report not found")
    conn.commit()
    conn.close()
    return None

# 5. READ (Live TfL Status)
@app.get("/live-status", response_model=List[TflStatusResponse])
def get_live_status():
    # The 'with' block ensures conn.close() happens automatically!
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tfl_live_status ORDER BY line_name ASC")
        statuses = cursor.fetchall()
    
    if not statuses:
        raise HTTPException(status_code=404, detail="No live data found. Please run the import script.")
        
    return [dict(row) for row in statuses]

# 6. ADVANCED ANALYTICS (Find Unacknowledged Disruptions)
@app.get("/analytics/discrepancies", response_model=List[DiscrepancyResponse])
def get_discrepancies():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # ADVANCED SQL: CTEs and Time-Bounding
    # 1. Finds the absolute latest TfL status for each line
    # 2. Grabs only user reports from the last 2 hours
    # 3. Joins them to find current, unacknowledged disruptions
    query = """
        WITH LatestTfL AS (
            SELECT line_name, status 
            FROM tfl_live_status 
            WHERE id IN (SELECT MAX(id) FROM tfl_live_status GROUP BY line_name)
        ),
        RecentReports AS (
            SELECT line_name, delay_minutes, id
            FROM user_reports
            WHERE report_date >= datetime('now', '-2 hours')
        )
        SELECT 
            t.line_name,
            t.status AS official_status,
            COUNT(u.id) AS corroborating_reports,
            AVG(u.delay_minutes) AS avg_delay,
            MAX(u.delay_minutes) AS max_delay
        FROM LatestTfL t
        JOIN RecentReports u ON t.line_name = u.line_name
        WHERE t.status = 'Good Service'
        GROUP BY t.line_name
    """
    
    cursor.execute(query)
    discrepancies = cursor.fetchall()
    conn.close()
    
    if not discrepancies:
        raise HTTPException(
            status_code=404, 
            detail="No discrepancies found. TfL data matches user reports."
        )
        
    formatted_results = []
    for row in discrepancies:
        reports_count = row["corroborating_reports"]
        
        # Dynamic confidence scoring based on sample size
        if reports_count >= 5:
            confidence = "High (Confirmed by crowd)"
        elif reports_count >= 2:
            confidence = "Medium (Multiple reports)"
        else:
            confidence = "Low (Unverified single report)"

        formatted_results.append({
            "line_name": row["line_name"],
            "official_status": row["official_status"],
            "corroborating_reports": reports_count,
            "crowd_consensus_minutes": round(row["avg_delay"], 1),
            "peak_delay_minutes": row["max_delay"],
            "confidence_level": confidence
        })
        
    return formatted_results

# 7. HISTORICAL ANALYTICS (24-Hour Network Uptime)
@app.get("/analytics/uptime", response_model=List[LineUptime])
def get_network_uptime():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Mathematically sound counting. Sorting by bad_checks directly in the database.
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
    
    cursor.execute("""
        SELECT line_name, status 
        FROM tfl_live_status 
        WHERE id IN (SELECT MAX(id) FROM tfl_live_status GROUP BY line_name)
    """)
    latest_statuses = {row["line_name"]: row["status"] for row in cursor.fetchall()}
    conn.close()

    if not history:
        raise HTTPException(status_code=404, detail="Not enough historical data collected yet. Let the background worker run.")

    results = []
    for row in history:
        total = row["total_checks"]
        good = row["good_checks"]
        bad = row["bad_checks"]
        
        # Pure percentage, immune to polling frequency changes
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
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # True DB Ping
        cursor.execute("SELECT 1")
        health_report["database_status"] = "connected"
        
        # Background Worker Diagnostic
        cursor.execute("SELECT MAX(timestamp) as last_sync FROM tfl_live_status")
        result = cursor.fetchone()
        conn.close()
        
        if result and result["last_sync"]:
            last_sync_str = result["last_sync"]
            health_report["last_tfl_sync"] = last_sync_str
            
            last_sync_time = datetime.strptime(last_sync_str, "%Y-%m-%d %H:%M:%S")
            time_since_sync = (datetime.now() - last_sync_time).total_seconds()
            
            if time_since_sync > 600:
                health_report["background_worker_status"] = "degraded (stale data)"
            else:
                health_report["background_worker_status"] = "healthy"
        else:
            health_report["background_worker_status"] = "waiting for initial sync"
            
    except Exception as e:
        health_report["database_status"] = "disconnected or locked"
        health_report["background_worker_status"] = "unreachable"
        raise HTTPException(status_code=503, detail=health_report)

    return health_report

# 9. DELAY VELOCITY (Trending Analysis - UTC and Severity Based)
@app.get("/analytics/velocity/{line_name}", response_model=DelayVelocity)
def get_delay_velocity(line_name: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Calculate AVERAGE and MAX severity in the current 60-minute window (UTC)
    cursor.execute("""
        SELECT 
            AVG(delay_minutes) as avg_delays,
            MAX(delay_minutes) as max_delays
        FROM user_reports 
        WHERE line_name = ? COLLATE NOCASE
        AND report_date >= datetime('now', '-60 minutes')
    """, (line_name,))
    current_result = cursor.fetchone()
    
    current_avg = round(current_result["avg_delays"] or 0, 1)
    current_max = current_result["max_delays"] or 0
    
    # Calculate AVERAGE and MAX severity in the PREVIOUS 60-minute window (UTC)
    cursor.execute("""
        SELECT 
            AVG(delay_minutes) as past_avg,
            MAX(delay_minutes) as past_max
        FROM user_reports 
        WHERE line_name = ? COLLATE NOCASE
        AND report_date >= datetime('now', '-120 minutes')
        AND report_date < datetime('now', '-60 minutes')
    """, (line_name,))
    past_result = cursor.fetchone()
    
    past_avg = round(past_result["past_avg"] or 0, 1)
    past_max = past_result["past_max"] or 0
    conn.close()

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

    # We reuse the existing DelayVelocity Pydantic model structure, 
    # but feed it our statistically accurate integers/floats
    return {
        "line_name": line_name.capitalize(),
        "current_hour_delay_minutes": int(current_avg), 
        "previous_hour_delay_minutes": int(past_avg),
        "trend": trend,
        "velocity_assessment": assessment
    }

# --- ADVANCED ANALYTICS (Section 3b Requirements) ---

# 1. CREATE (Secured with Rate Limiting and Dynamic Anomaly Detection)
@app.post("/reports", response_model=UserReportResponse, status_code=201, dependencies=[Depends(verify_rate_limit)])
def create_report(report: UserReportCreate):
    # Absolute Hard Cap (The physical limits of reality)
    if report.delay_minutes > 300:
        raise HTTPException(status_code=400, detail="Anomaly detected: Delay exceeds realistic physical limits (5 hours).")

    conn = get_db_connection()
    cursor = conn.cursor()
    
    # --- Z-SCORE ANOMALY DETECTION ---
    # Fetch recent delays for this specific line to establish a statistical baseline
    cursor.execute("""
        SELECT delay_minutes FROM user_reports 
        WHERE line_name = ? COLLATE NOCASE
        AND report_date >= datetime('now', '-2 hours')
    """, (report.line_name,))
    
    recent_delays = [row["delay_minutes"] for row in cursor.fetchall()]
    
    # We only apply statistical filtering if we have a viable sample size
    if len(recent_delays) >= 5:
        mean = sum(recent_delays) / len(recent_delays)
        variance = sum((x - mean) ** 2 for x in recent_delays) / len(recent_delays)
        std_dev = math.sqrt(variance)
        
        # Calculate Z-Score (how many standard deviations away from the mean this report is)
        if std_dev > 0:
            z_score = (report.delay_minutes - mean) / std_dev
            
            # Reject if it's beyond 3 standard deviations AND mathematically significant (> 30 mins)
            if z_score > 3.0 and report.delay_minutes > 30:
                conn.close()
                raise HTTPException(
                    status_code=422, 
                    detail=f"Statistical anomaly detected. Report rejected. (Z-Score: {round(z_score, 2)})"
                )
        elif report.delay_minutes > mean + 45:
            # Fallback if std_dev is 0 (all recent reports were exactly the same)
            conn.close()
            raise HTTPException(status_code=422, detail="Statistical anomaly detected. Report rejected.")
    
    # --- SAFE DATABASE INSERT ---
    report_date = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    
    cursor.execute(
        "INSERT INTO user_reports (line_name, delay_minutes, issue_type, report_date) VALUES (?, ?, ?, ?)",
        (report.line_name, report.delay_minutes, report.issue_type, report_date)
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    
    return {**report.model_dump(), "id": new_id, "report_date": report_date}
    
    # 2. Get User Metrics strictly from the last 2 hours
    cursor.execute("""
        SELECT 
            COUNT(id) as total_reports,
            AVG(delay_minutes) as avg_delay,
            MAX(delay_minutes) as max_delay
        FROM user_reports 
        WHERE line_name = ? COLLATE NOCASE
        AND report_date >= datetime('now', '-2 hours')
    """, (line_name,))
    
    metrics = cursor.fetchone()
    conn.close()

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
        "line_name": line_name.capitalize(),
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
@app.get("/analytics/patterns", response_model=List[DelayPattern])
def get_delay_patterns():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT 
            issue_type, 
            COUNT(id) as incident_count,
            AVG(delay_minutes) as average_delay_minutes,
            MAX(delay_minutes) as peak_delay_minutes
        FROM user_reports 
        WHERE report_date >= datetime('now', '-30 days')
        GROUP BY issue_type 
        ORDER BY incident_count DESC, peak_delay_minutes DESC
    """)
    patterns = cursor.fetchall()
    conn.close()
    
    formatted_patterns = []
    for row in patterns:
        formatted_patterns.append({
            "issue_type": row["issue_type"],
            "incident_count": row["incident_count"],
            "average_delay_minutes": round(row["average_delay_minutes"] or 0.0, 1),
            "peak_delay_minutes": row["peak_delay_minutes"] or 0
        })
        
    return formatted_patterns

# 3. Temporal Performance Summaries (Delays by time of day)
@app.get("/analytics/temporal", response_model=List[TemporalSummary])
def get_temporal_summary():
    conn = get_db_connection()
    cursor = conn.cursor()
    # strftime extracts just the Hour (00-23) from our YYYY-MM-DD HH:MM:SS timestamps
    cursor.execute("""
        SELECT strftime('%H', report_date) as hour_of_day, COUNT(id) as total_incidents
        FROM user_reports
        WHERE report_date >= datetime('now', '-30 days')
        GROUP BY hour_of_day
        ORDER BY hour_of_day ASC
    """)
    summaries = cursor.fetchall()
    conn.close()
    return [dict(row) for row in summaries]