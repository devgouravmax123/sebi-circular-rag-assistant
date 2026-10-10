# Frontend - SEBI Circular Assistant

React + TypeScript + Vite + Tailwind CSS starter application for the SEBI Circular Q&A Assistant.

## Prerequisites
- Node.js `v24.x` / npm `11.x`
- FastAPI backend running at `http://127.0.0.1:8000`

## Local Development Setup

1. **Install dependencies:**
   ```bash
   npm install
   ```

2. **Configure environment (Optional):**
   Copy `.env.example` to `.env` if custom backend URL is desired:
   ```bash
   cp .env.example .env
   ```
   *Default API URL:* `http://127.0.0.1:8000` (via `VITE_API_BASE_URL`).

3. **Start the development server:**
   ```bash
   npm run dev
   ```
   The application will be accessible at `http://localhost:5173`.

4. **Build production bundle:**
   ```bash
   npm run build
   ```
