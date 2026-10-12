import os
from sqlalchemy import create_engine, text

db_url = os.getenv("DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/sebi_rag_test")
engine = create_engine(db_url)

with engine.connect() as conn:
    conn.execute(text("INSERT INTO regulators (id, name, country, official_portal_url) VALUES ('TEST_REG', 'Test Reg', 'IND', 'https://test');"))
    conn.execute(text("INSERT INTO regulatory_domains (id, regulator_id, code, title) VALUES ('TEST_DOM', 'TEST_REG', 'CYBER', 'Cybersecurity');"))
    conn.execute(text("INSERT INTO regulatory_documents (id, domain_id, title) VALUES ('doc_a', 'TEST_DOM', 'Doc A'), ('doc_b', 'TEST_DOM', 'Doc B');"))
    conn.execute(text("""
        INSERT INTO regulatory_edges (id, source_document_id, target_document_id, relationship, scope, evidence_text, confidence, review_status)
        VALUES ('edge_test_old', 'doc_b', 'doc_a', 'AMENDS', 'full', 'Section 1 amends Doc A', 'high', 'verified');
    """))
    conn.commit()
    print("Inserted legacy edge into 0002 schema successfully.")
