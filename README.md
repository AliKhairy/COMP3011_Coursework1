# London Underground Reliability API

> This API operates as a dual-model "Truth Engine," validating real-time, crowd-sourced commuter delays against official Transport for London (TfL) infrastructure reports. By calculating statistical consensus and peak severity trajectories, it mathematically flags instances where official data downplays real-world commuter friction.

---

## API Documentation
Full endpoint specifications, request/response schemas, and error codes are available in the accompanying repository file: `London Underground Reliability API - ReDoc.pdf`. Alternatively, boot the local server and navigate to `/docs` for the interactive FastAPI Swagger UI.

---

## System Architecture & Engineering Defenses
Rather than functioning as a simple CRUD application, this system is defensively engineered to maintain data integrity under adversarial conditions and concurrent loads.

* **Database Concurrency:** To prevent `database is locked` bottlenecks during asynchronous FastAPI operations, the SQLite connection enforces **Write-Ahead Logging** (`PRAGMA journal_mode=WAL;`). This allows concurrent reads and writes, ensuring the background TfL sync worker does not block user-facing analytics endpoints.
* **Statistical Anomaly Detection:** To protect against malicious dataset poisoning (e.g., trolls reporting 500-minute delays), the `POST /reports` endpoint utilizes a **Z-Score Engine**. It calculates a localized standard deviation of delays for that specific train line over the last two hours. Reports that mathematically deviate too far from the norm (Z-Score > 3.0) are algorithmically rejected.
* **Stateless IDOR Protection:** To prevent Insecure Direct Object Reference (IDOR) vulnerabilities without the overhead of a full authentication system, the API utilizes an **Edit Token** pattern. Upon creating a report, the creator receives a unique `UUIDv4`. Subsequent `PUT` or `DELETE` requests require this token to be passed securely via the `X-Edit-Token` HTTP header.
* **In-Memory Rate Limiting:** An asynchronous, garbage-collected sliding window restricts submissions to **3 reports per hour per IP address** to mitigate API abuse and spam loops.
* **Infrastructure Resilience:** A dedicated `GET /health` endpoint strictly monitors the temporal validity of the data, automatically flagging the system as degraded if the upstream TfL background fetcher goes offline for more than 600 seconds.

---

## Core Analytical Models
The analytics endpoints do not merely return raw data; they interpret it to provide actionable insights.

| Model | Function |
| :--- | :--- |
| **The Discrepancy Engine** | Applies a severity weight to official TfL statuses and compares it against the rolling average of crowd reports to quantify unacknowledged delays. |
| **Delay Velocity** | Maps peak severity trajectories (current hour vs. previous hour) to determine if a disruption is resolving, stagnant, or sharply accelerating. |
| **Route Reliability Score** | Calculates a dynamic score by combining an infrastructure penalty, a consensus delay penalty, and a strict buffer-time variance penalty. |

---

## Setup & Local Execution
Follow these steps to deploy the API locally. This project uses strict versioning to guarantee deterministic builds.

1. Initialize your virtual environment and install the exact project dependencies:
```bash
pip install -r requirements.txt
```
2. [Optional] Seed the database:
```bash
python seed_database.py
```
3. Boot the Uvicorn server:
```bash
python -m uvicorn main:app --reload
```
4. The background worker will automatically initialize and fetch the first snapshot of TfL data. Access the interactive API documentation at ```http://127.0.0.1:8000/docs```.

## Automated Testing
To verify boundary limits, anomaly detection, and edge cases, execute the Pytest suite. Testing dependencies are already included in the requirements file.

1. Execute the automated system checks in the root directory:
```bash
pytest -v
```
### Manual Security Testing (IDOR Protection)
To manually verify the stateless edit token architecture via Swagger UI (`/docs`):

1. Execute a `POST /reports` request to generate a new delay report.
2. Extract the `edit_token` string from the `201 Created` response payload.
3. Attempt to `PUT` or `DELETE` that specific report ID without the token (Expected Result: `422 Validation Error`).
4. Inject the token into the `x-edit-token` header parameter and re-send the request (Expected Result: `200 OK` or `204 No Content`).