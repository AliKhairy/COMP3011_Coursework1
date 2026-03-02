import sqlite3
import requests
from datetime import datetime

def fetch_tfl_data():
    print("Fetching live TfL Tube status...")
    url = "https://api.tfl.gov.uk/Line/Mode/tube/Status"
    
    try:
        response = requests.get(url)
        response.raise_for_status() # Crashes safely if the API is down
        tube_data = response.json()

        conn = sqlite3.connect("transport_api.db")
        cursor = conn.cursor()

        # We wipe the table first so we only ever store the most recent snapshot
        cursor.execute("DELETE FROM tfl_live_status")

        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        records = []

        # Extract the relevant fields from TfL's complex nested JSON
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
        print(f"Success! Updated {len(records)} Tube lines in the database.")

    except Exception as e:
        print(f"Error fetching data: {e}")

if __name__ == "__main__":
    fetch_tfl_data()