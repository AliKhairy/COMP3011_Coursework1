import sqlite3
import random
from datetime import datetime, timedelta

def setup_database():
    # Connect to SQLite (this creates the file if it doesn't exist)
    conn = sqlite3.connect("transport_api.db")
    cursor = conn.cursor()

    # Create Tables
    cursor.executescript("""
        DROP TABLE IF EXISTS delay_reports;
        DROP TABLE IF EXISTS routes;

        CREATE TABLE routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            operator TEXT NOT NULL
        );

        CREATE TABLE delay_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route_id INTEGER,
            delay_minutes INTEGER NOT NULL,
            cause TEXT NOT NULL,
            report_date TEXT NOT NULL,
            FOREIGN KEY (route_id) REFERENCES routes (id)
        );
    """)

    # Seed Routes
    routes_data = [
        ("Leeds", "London Kings Cross", "LNER"),
        ("Manchester Piccadilly", "London Euston", "Avanti West Coast"),
        ("Leeds", "York", "TransPennine Express"),
        ("Birmingham New Street", "Manchester Piccadilly", "CrossCountry")
    ]
    cursor.executemany("INSERT INTO routes (origin, destination, operator) VALUES (?, ?, ?)", routes_data)

    # Seed Delay Reports (Generating 50 random historical delays)
    causes = ["Signal Failure", "Weather", "Staff Shortage", "Train Fault", "Trespassers"]
    delay_data = []
    
    for _ in range(50):
        route_id = random.randint(1, 4)
        delay_minutes = random.randint(5, 120)
        cause = random.choice(causes)
        # Generate random dates over the past 30 days
        random_days_ago = random.randint(0, 30)
        report_date = (datetime.now() - timedelta(days=random_days_ago)).strftime("%Y-%m-%d %H:%M:%S")
        
        delay_data.append((route_id, delay_minutes, cause, report_date))

    cursor.executemany("""
        INSERT INTO delay_reports (route_id, delay_minutes, cause, report_date) 
        VALUES (?, ?, ?, ?)
    """, delay_data)

    conn.commit()
    conn.close()
    print("Database 'transport_api.db' successfully created and seeded with data.")

if __name__ == "__main__":
    setup_database()