from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List
import sqlite3
from datetime import datetime
import asyncio
import logging
from contextlib import asynccontextmanager

from import_tfl_data import fetch_tfl_data

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
    total_minutes_lost: int
    average_delay_minutes: float
    worst_single_delay: int

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
    total_minutes_lost: int
    average_delay_minutes: float

class TemporalSummary(BaseModel):
    hour_of_day: str
    total_incidents: int


# --- DATABASE HELPER ---
def get_db_connection():
    conn = sqlite3.connect("transport_api.db")
    conn.row_factory = sqlite3.Row
    return conn

# --- ENDPOINTS (User Reports CRUD) ---
@app.get("/")
def root():
    return {"message": "Welcome to the London Underground Reliability API"}

# 1. CREATE
@app.post("/reports", response_model=UserReportResponse, status_code=201)
def create_report(report: UserReportCreate):
    conn = get_db_connection()
    cursor = conn.cursor()
    report_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    cursor.execute(
        "INSERT INTO user_reports (line_name, delay_minutes, issue_type, report_date) VALUES (?, ?, ?, ?)",
        (report.line_name, report.delay_minutes, report.issue_type, report_date)
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    
    return {**report.model_dump(), "id": new_id, "report_date": report_date}

# 2. READ
@app.get("/reports", response_model=List[UserReportResponse])
def get_reports():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM user_reports ORDER BY report_date DESC")
    reports = cursor.fetchall()
    conn.close()
    return [dict(row) for row in reports]

# 3. UPDATE
@app.put("/reports/{report_id}", response_model=UserReportResponse)
def update_report(report_id: int, report: UserReportCreate):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE user_reports SET line_name = ?, delay_minutes = ?, cause = ? WHERE id = ?",
        (report.line_name, report.delay_minutes, report.cause, report_id)
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
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM tfl_live_status ORDER BY line_name ASC")
    statuses = cursor.fetchall()
    conn.close()
    
    if not statuses:
        # Proper error handling if someone hits the endpoint before running the import script
        raise HTTPException(status_code=404, detail="No live data found. Please run the import script.")
        
    return [dict(row) for row in statuses]

# 6. ADVANCED ANALYTICS (Find Unacknowledged Disruptions)
@app.get("/analytics/discrepancies", response_model=List[DiscrepancyResponse])
def get_discrepancies():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # We replace the flawed SUM with AVG and MAX to find the true delay envelope
    query = """
        SELECT 
            t.line_name,
            t.status AS official_status,
            COUNT(u.id) AS corroborating_reports,
            AVG(u.delay_minutes) AS avg_delay,
            MAX(u.delay_minutes) AS max_delay
        FROM tfl_live_status t
        JOIN user_reports u ON t.line_name = u.line_name
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

# --- ADVANCED ANALYTICS (Section 3b Requirements) ---

# 1. ENHANCED Route-Level Reliability Scores
@app.get("/analytics/reliability/{line_name}", response_model=ReliabilityScore)
def get_reliability_score(line_name: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Get Official Status
    cursor.execute("SELECT status FROM tfl_live_status WHERE line_name = ? COLLATE NOCASE", (line_name,))
    tfl_data = cursor.fetchone()
    
    if not tfl_data:
        conn.close()
        raise HTTPException(status_code=404, detail=f"Line '{line_name}' not found in TfL data.")
        
    official_status = tfl_data["status"]
    
    # 2. Get Advanced User Metrics using SQL Aggregations
    cursor.execute("""
        SELECT 
            COUNT(id) as total_reports,
            SUM(delay_minutes) as total_minutes,
            AVG(delay_minutes) as avg_delay,
            MAX(delay_minutes) as max_delay
        FROM user_reports 
        WHERE line_name = ? COLLATE NOCASE
    """, (line_name,))
    
    metrics = cursor.fetchone()
    conn.close()

    # Safely handle None values if there are zero user reports for this line yet
    total_reports = metrics["total_reports"] or 0
    total_minutes = metrics["total_minutes"] or 0
    avg_delay = round(metrics["avg_delay"] or 0.0, 1)
    max_delay = metrics["max_delay"] or 0

    # Calculate complex score
    score = 100.0
    if official_status != "Good Service":
        score -= 40.0 
    
    # Dynamic penalty based on actual severity, not just report count
    score -= (total_reports * 2.0) + (total_minutes * 0.1) 
    score = max(0.0, min(100.0, round(score, 1)))
    
    assessment = "Excellent" if score > 80 else "Degraded" if score > 50 else "Severe Failure"

    return {
        "line_name": line_name.capitalize(),
        "official_status": official_status,
        "reliability_percentage": score,
        "assessment": assessment,
        "user_metrics": {
            "total_reports": total_reports,
            "total_minutes_lost": total_minutes,
            "average_delay_minutes": avg_delay,
            "worst_single_delay": max_delay
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
            SUM(delay_minutes) as total_minutes_lost,
            AVG(delay_minutes) as average_delay_minutes
        FROM user_reports 
        GROUP BY issue_type 
        ORDER BY total_minutes_lost DESC
    """)
    patterns = cursor.fetchall()
    conn.close()
    
    formatted_patterns = []
    for row in patterns:
        formatted_patterns.append({
            "issue_type": row["issue_type"],
            "incident_count": row["incident_count"],
            "total_minutes_lost": row["total_minutes_lost"] or 0,
            "average_delay_minutes": round(row["average_delay_minutes"] or 0.0, 1)
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
        GROUP BY hour_of_day
        ORDER BY hour_of_day ASC
    """)
    summaries = cursor.fetchall()
    conn.close()
    return [dict(row) for row in summaries]