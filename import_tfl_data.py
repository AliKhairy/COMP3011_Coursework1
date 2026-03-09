import sqlite3
import requests
from datetime import datetime, timezone # Import timezone

def fetch_tfl_data():
    print("Fetching live TfL Tube status...")
    url = "https://api.tfl.gov.uk/Line/Mode/tube/Status"
    
    try:
        # Added timeout to prevent the script from hanging indefinitely if TfL servers stall
        response = requests.get(url, timeout=10)
        response.raise_for_status() 
        tube_data = response.json()

        # Added timeout=15: If FastAPI is querying the DB, wait up to 15 seconds for the lock to clear
        conn = sqlite3.connect("transport_api.db", timeout=15.0)
        cursor = conn.cursor()

        # STRICT UTC IMPLEMENTATION
        current_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        records = []

        for line in tube_data:
            line_name = line.get("name", "Unknown")
            statuses = line.get("lineStatuses", [])
            
            if statuses:
                status = statuses[0].get("statusSeverityDescription", "Unknown")
                reason = statuses[0].get("reason", "No delays reported.")
            else:
                status = "Unknown"
                reason = "N/A"
                
            records.append((line_name, status, reason, current_time))

        cursor.executemany(
            "INSERT INTO tfl_live_status (line_name, status, reason, timestamp) VALUES (?, ?, ?, ?)",
            records
        )
        conn.commit()
        conn.close()
        print(f"Success! Updated {len(records)} Tube lines in the database at UTC {current_time}.")

    except requests.exceptions.RequestException as req_err:
        print(f"Network error fetching TfL data: {req_err}")
    except sqlite3.OperationalError as db_err:
        print(f"CRITICAL: Database locking issue - {db_err}")
    except Exception as e:
        print(f"Unexpected error: {e}")

if __name__ == "__main__":
    fetch_tfl_data()