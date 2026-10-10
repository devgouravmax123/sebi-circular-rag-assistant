# Backend Setup & Development Instructions

## Prerequisites
- Python 3.12.14
- Virtual environment at `backend/.venv`

## Local Setup

1. **Activate Virtual Environment (PowerShell):**
   ```powershell
   .\backend\.venv\Scripts\Activate.ps1
   ```

2. **Install Dependencies:**
   ```powershell
   pip install -r backend/requirements.txt
   ```

3. **Run the FastAPI Development Server:**
   ```powershell
   cd backend
   uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
   ```

4. **Verify Health Endpoint:**
   - Browser / HTTP Client: `http://127.0.0.1:8000/health`
   - Response: `{"status": "ok"}`

5. **Run Tests:**
   ```powershell
   .\backend\.venv\Scripts\pytest.exe backend/tests/
   ```
