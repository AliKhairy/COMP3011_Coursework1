import sqlite3
from datetime import datetime, timezone, timedelta
import random
import uuid

# --- CONFIGURATION ---
DB_NAME = "transport_api.db"
LINES = [
    "Bakerloo", "Central", "Circle", "District", "Elizabeth line", 
    "Hammersmith & City", "Jubilee", "Metropolitan", "Northern", 
    "Piccadilly", "Victoria", "Waterloo & City"
]
EXPERIENCES = [
    "Stuck in tunnel", "Train moving at a crawling pace", 
    "Platform dangerously crowded", "Train cancelled/disappeared from board", 
    "Not sure, just delayed"
]

def seed_database():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # 1. WIPE EXISTING DATA FOR A CLEAN SLATE
    print("🧹 Clearing old data...")
    cursor.execute("DELETE FROM user_reports")
    cursor.execute("DELETE FROM tfl_live_status")
    
    now = datetime.now(timezone.utc)

    # ==========================================
    # 2. SEED TFL LIVE STATUS (For Uptime & Discrepancies)
    # ==========================================
    print("🚇 Seeding TfL live status history (24 hours)...")
    for i in range(48): # 48 half-hour intervals in 24 hours
        ping_time = now - timedelta(minutes=30 * i)
        time_str = ping_time.strftime("%Y-%m-%d %H:%M:%S")
        
        for line in LINES:
            # Make the Central line have "Minor Delays" right now for our Discrepancy test
            if line == "Central" and i == 0:
                status = "Minor Delays"
            # Make the Northern line terrible yesterday to test uptime percentages
            elif line == "Northern" and i > 20:
                status = "Severe Delays"
            else:
                # 90% chance of good service normally
                status = "Good Service" if random.random() > 0.1 else "Part Suspended"
                
            cursor.execute("""
                INSERT INTO tfl_live_status (line_name, status, reason, timestamp) 
                VALUES (?, ?, ?, ?)
            """, (line, status, "Simulated data", time_str))

    # ==========================================
    # 3. SEED USER REPORTS (Edge Cases)
    # ==========================================
    print("🧑‍🤝‍🧑 Seeding User Reports...")

    # Edge Case A: IDOR / CRUD Testing (Bakerloo Line)
    # We use a hardcoded token so you can easily copy-paste it into Swagger to test PUT/DELETE
    cursor.execute("""
        INSERT INTO user_reports (line_name, delay_minutes, observed_experience, report_date, edit_token)
        VALUES (?, ?, ?, ?, ?)
    """, ("Bakerloo", 15, "Stuck in tunnel", now.strftime("%Y-%m-%d %H:%M:%S"), "TEST-TOKEN-12345"))

    # Edge Case B: Z-Score Anomaly Priming (Jubilee Line)
    # 6 reports in the last hour, all exactly 5 or 6 minutes. Low variance.
    # (If you POST a 40 min delay to Jubilee now, it WILL be rejected!)
    for _ in range(6):
        r_time = (now - timedelta(minutes=random.randint(5, 50))).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("""
            INSERT INTO user_reports (line_name, delay_minutes, observed_experience, report_date, edit_token)
            VALUES (?, ?, ?, ?, ?)
        """, ("Jubilee", random.choice([5, 6]), "Train moving at a crawling pace", r_time, str(uuid.uuid4())))

    # Edge Case C: Velocity & Discrepancy Trigger (Central Line)
    # PREVIOUS Hour (60-120 mins ago): Mild delays (~10 mins)
    for _ in range(5):
        r_time = (now - timedelta(minutes=random.randint(65, 115))).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("""
            INSERT INTO user_reports (line_name, delay_minutes, observed_experience, report_date, edit_token)
            VALUES (?, ?, ?, ?, ?)
        """, ("Central", random.randint(8, 12), "Platform dangerously crowded", r_time, str(uuid.uuid4())))

    # CURRENT Hour (0-60 mins ago): Severe delays (~45 mins) - this proves an accelerating trend!
    for _ in range(8):
        r_time = (now - timedelta(minutes=random.randint(5, 55))).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("""
            INSERT INTO user_reports (line_name, delay_minutes, observed_experience, report_date, edit_token)
            VALUES (?, ?, ?, ?, ?)
        """, ("Central", random.randint(40, 50), "Stuck in tunnel", r_time, str(uuid.uuid4())))

    # Edge Case D: Historical Patterns (Scatter data over 30 days)
    for _ in range(100):
        days_ago = random.randint(1, 29)
        hours_ago = random.randint(0, 23)
        r_time = (now - timedelta(days=days_ago, hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
        line = random.choice(LINES)
        exp = random.choice(EXPERIENCES)
        delay = random.randint(5, 45)
        
        cursor.execute("""
            INSERT INTO user_reports (line_name, delay_minutes, observed_experience, report_date, edit_token)
            VALUES (?, ?, ?, ?, ?)
        """, (line, delay, exp, r_time, str(uuid.uuid4())))

    conn.commit()
    conn.close()
    print("✅ Database successfully seeded! Ready for testing.")

if __name__ == "__main__":
    seed_database()