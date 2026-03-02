from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List
import sqlite3
from datetime import datetime

app = FastAPI(
    title="London Underground Reliability API",
    description="A dual-model API tracking user-reported delays and official TfL live statuses.",
    version="1.0.0"
)

# --- PYDANTIC MODELS (Data Validation) ---
class UserReportCreate(BaseModel):
    line_name: str
    delay_minutes: int
    cause: str

class UserReportResponse(BaseModel):
    id: int
    line_name: str
    delay_minutes: int
    cause: str
    report_date: str

class TflStatusResponse(BaseModel):
    id: int
    line_name: str
    status: str
    reason: str | None
    timestamp: str

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
        "INSERT INTO user_reports (line_name, delay_minutes, cause, report_date) VALUES (?, ?, ?, ?)",
        (report.line_name, report.delay_minutes, report.cause, report_date)
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