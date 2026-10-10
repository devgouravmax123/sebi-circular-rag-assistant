import subprocess
import sys
import time
import httpx

def test_integration():
    print("--- 1. Testing Disconnected State (Backend Stopped) ---")
    try:
        httpx.get("http://127.0.0.1:8000/health", timeout=1.0)
        print("ERROR: Backend was already running!")
    except Exception as e:
        print(f"Verified backend is stopped: {type(e).__name__}")

    print("\n--- 2. Starting Backend Server ---")
    backend_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000", "--log-level", "warning"],
        cwd="backend"
    )

    try:
        time.sleep(2)
        print("--- 3. Testing Connected State (Backend Running) ---")
        res = httpx.get("http://127.0.0.1:8000/health", timeout=3.0)
        print(f"Backend HTTP Response: {res.status_code} -> {res.json()}")
        assert res.status_code == 200
        assert res.json() == {"status": "ok"}
        print("Backend connected verification PASSED!")
    finally:
        print("\n--- 4. Stopping Backend Server ---")
        backend_proc.terminate()
        backend_proc.wait()
        print("Backend terminated.")

    print("\n--- 5. Testing Disconnected State Again ---")
    try:
        httpx.get("http://127.0.0.1:8000/health", timeout=1.0)
        print("ERROR: Backend still running!")
    except Exception as e:
        print(f"Verified disconnected state: {type(e).__name__}")
        print("Integration verification COMPLETE!")

if __name__ == "__main__":
    test_integration()
