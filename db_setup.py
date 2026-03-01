import sqlite3

def initialize_database():
    print("Initializing Transport API Database...")
    conn = sqlite3.connect("transport_api.db")
    cursor = conn.cursor()

    # We use executescript to run multiple SQL commands at once
    cursor.executescript("""
        -- Table 1: Fulfills the CRUD requirement (User Reports)
        CREATE TABLE IF NOT EXISTS user_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            line_name TEXT NOT NULL,
            delay_minutes INTEGER NOT NULL,
            cause TEXT NOT NULL,
            report_date TEXT NOT NULL
        );

        -- Table 2: Fulfills the novel data integration requirement (Official TfL Data)
        CREATE TABLE IF NOT EXISTS tfl_live_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            line_name TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT,
            timestamp TEXT NOT NULL
        );
    """)

    conn.commit()
    conn.close()
    print("Success: Database schema created with 'user_reports' and 'tfl_live_status' tables.")

if __name__ == "__main__":
    initialize_database()