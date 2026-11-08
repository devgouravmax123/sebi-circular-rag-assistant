"""
Unit tests for Ephemeral Guest Sessions, Quotas, and Server-Side Cleanup (Milestone 11).
"""
import io
import pytest
from starlette.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_guest_session_lifecycle():
    # 1. Create a guest session
    create_resp = client.post("/api/v1/guest/session")
    assert create_resp.status_code == 201
    data = create_resp.json()
    assert "session_id" in data
    assert "token" in data
    token = data["token"]
    session_id = data["session_id"]
    assert data["quotas"]["max_collections"] == 1
    assert data["quotas"]["max_documents"] == 2

    headers = {"Authorization": f"Bearer {token}"}

    # 2. Inspect session status
    status_resp = client.get("/api/v1/guest/session", headers=headers)
    assert status_resp.status_code == 200
    st_data = status_resp.json()
    assert st_data["session_id"] == session_id
    assert st_data["is_active"] is True
    assert st_data["time_remaining_seconds"] > 86000  # ~24 hours

    # 3. Create temporary collection
    col_resp = client.post(
        "/api/v1/guest/collections",
        json={"name": "Temporary Research", "description": "Ephemeral papers"},
        headers=headers,
    )
    assert col_resp.status_code == 201
    col_data = col_resp.json()
    assert col_data["name"] == "Temporary Research"
    col_id = col_data["id"]

    # 4. Quota check: 2nd collection rejected (limit 1)
    col_rej = client.post(
        "/api/v1/guest/collections",
        json={"name": "Second Collection"},
        headers=headers,
    )
    assert col_rej.status_code == 403
    assert "Guest quota exceeded" in col_rej.json()["detail"]

    # 5. Upload temporary PDF document
    pdf_bytes = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\nxref\ntrailer<</Root 1 0 R>>\n%%EOF"
    upload_resp = client.post(
        "/api/v1/guest/upload",
        data={"collection_id": col_id, "title": "Test Paper"},
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        headers=headers,
    )
    assert upload_resp.status_code == 202
    up_data = upload_resp.json()
    assert up_data["status"] == "queued"
    assert "Document uploaded to temporary session storage" in up_data["message"]

    # 6. List guest documents and jobs
    docs_resp = client.get("/api/v1/guest/documents", headers=headers)
    assert docs_resp.status_code == 200
    assert len(docs_resp.json()) == 1

    jobs_resp = client.get("/api/v1/guest/jobs", headers=headers)
    assert jobs_resp.status_code == 200
    assert len(jobs_resp.json()) == 1

    # 7. Record guest research query history
    rec_resp = client.post(
        "/api/v1/guest/history/record",
        json={
            "query_text": "What is the primary contribution?",
            "collection_id": col_id,
            "disposition": "FULLY_ANSWERED",
            "answer_text": "The primary contribution is a novel neural architecture.",
            "citations_count": 1,
        },
        headers=headers,
    )
    assert rec_resp.status_code == 201

    hist_resp = client.get("/api/v1/guest/history", headers=headers)
    assert hist_resp.status_code == 200
    assert hist_resp.json()["total"] == 1

    # 8. Terminate guest session and execute cleanup
    term_resp = client.post("/api/v1/guest/session/terminate", headers=headers)
    assert term_resp.status_code == 200
    term_data = term_resp.json()
    assert term_data["cleaned_up"] is True
    assert term_data["is_active"] is False

    # 9. Verify subsequent requests are rejected (HTTP 401)
    subseq_resp = client.get("/api/v1/guest/session", headers=headers)
    assert subseq_resp.status_code == 401

