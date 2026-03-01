from fastapi import FastAPI

# Initialize the application with metadata for the auto-generated docs
app = FastAPI(
    title="UK Transport Delay Analytics API",
    description="API for tracking and analyzing train delays across the UK.",
    version="1.0.0"
)

# A simple root endpoint to test if the server is alive
@app.get("/")
def read_root():
    return {"status": "success", "message": "Transport API is running."}