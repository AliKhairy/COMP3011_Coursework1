import sqlite3

def initialize_bulletproof_db():
    print("Setting up production-grade SQLite database...")
    
    # timeout=20 allows concurrent reads/writes without instantly crashing
    conn = sqlite3.connect("transport_api.db", timeout=20.0)
    cursor = conn.cursor()

    # 1. FIX THE LOCKING ERROR: Enable Write-Ahead Logging
    # This allows FastAPI to read data at the exact same time the background worker is writing it.
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    
    # 2. CREATE TABLES (If they don't exist)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            line_name TEXT,
            delay_minutes INTEGER,
            observed_experience TEXT,
            report_date TEXT,
            edit_token TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tfl_live_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            line_name TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT,
            timestamp TEXT NOT NULL
        )
    """)

    # 3. CREATE INDEXES: Prevent Full Table Scans
    # This makes looking up the last 30 days or the last 2 hours virtually instant
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_reports_date ON user_reports(report_date);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_reports_line ON user_reports(line_name);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tfl_status_line_time ON tfl_live_status(line_name, timestamp);")

    # 4. CREATE SELF-CLEANING TRIGGERS (The Sliding Windows)
    
    # Trigger A: Automatically delete User Reports older than 30 days after every new insert
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS prune_user_reports
        AFTER INSERT ON user_reports
        BEGIN
            DELETE FROM user_reports WHERE report_date < datetime('now', '-30 days');
        END;
    """)

    # Trigger B: Automatically delete TfL Statuses older than 48 hours after every new insert
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS prune_tfl_status
        AFTER INSERT ON tfl_live_status
        BEGIN
            DELETE FROM tfl_live_status WHERE timestamp < datetime('now', '-48 hours');
        END;
    """)

    conn.commit()
    conn.close()
    print("Success! Database is now indexed, running in WAL mode, and self-cleaning.")

if __name__ == "__main__":
    initialize_bulletproof_db()