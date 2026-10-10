"""Temporary verification script to test live uvicorn server startup and HTTP response."""
import time
import subprocess
import sys
import httpx

server_process = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000", "--log-level", "warning"],
    cwd="backend",
)

try:
    time.sleep(2)
    response = httpx.get("http://127.0.0.1:8000/health", timeout=5.0)
    print(f"HTTP_STATUS: {response.status_code}")
    print(f"HTTP_BODY: {response.json()}")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    print("LIVE_HEALTH_CHECK_SUCCESS")
finally:
    server_process.terminate()
    server_process.wait()
