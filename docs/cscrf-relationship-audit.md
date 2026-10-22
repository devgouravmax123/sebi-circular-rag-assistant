# Source-Backed Relationship Audit: SEBI CSCRF Corpus

> [!NOTE]
> **Authoritative SEBI CSCRF Regression Fixture:**
> This document is the verified evidentiary ledger for the 9 regulatory relationships connecting the 5 circulars in the `sebi_cscrf` reference collection. Its relationships, verbatim quotations, and temporal analyses are permanent regression benchmarks.
>
> Generalization of the platform does **not** imply that other collections require supersession graphs or publication dates. Non-regulatory collections (e.g. technical manuals, organizational standards) operate without amendment graphs under a `PassthroughPolicy`.

## 1. Audit Overview & Methodology

This audit provides a rigorous, source-backed verification of regulatory relationships across the five registered Cybersecurity and Cyber Resilience Framework (CSCRF) circulars in `data/raw/` and `data/processed/`:

1. **`sebi_cscrf_2024_113`** (Master CSCRF Circular, Aug 20, 2024)
2. **`sebi_cscrf_2025_045`** (Extension Circular, Mar 28, 2025)
3. **`sebi_cscrf_2025_060`** (Clarifications & Categorization Revisions, Apr 30, 2025)
4. **`sebi_cscrf_2025_096`** (Further Extension Circular, Jun 30, 2025)
5. **`sebi_cscrf_2025_119`** (Technical Clarifications & Threshold Revisions, Aug 28, 2025)

### Strict Verification Standards
* **Verbatim Quotations:** All evidentiary claims are grounded in exact word-for-word source text. Paraphrases and analytical commentary are strictly segregated.
* **Dual Page Provenance:** Every evidence item records both the 1-indexed PDF page and the document's printed page identifier (`Page X of Y`).
* **Granular Provision Independence & Directed Graph Encoding:** Independent provision changes are audited in detail and consolidated cleanly into well-defined graph edges (`source -> RELATIONSHIP -> target`) where `affected_provisions` preserves exact clause scopes without ambiguity.
* **Informational vs. Operative Distinction:** Formal citations and preambles are classified as `REFERENCES` (`is_supersession_inducing = False`), ensuring they do not trigger legal supersession or invalidate earlier provisions.
* **Temporal Discipline:** Issue dates (`issue_date`), effective dates (`effective_date`), and provision-specific compliance deadlines (glide paths) are kept distinct. Whole-document effective dates are never fabricated from individual compliance deadlines.

---

## 2. Schema Capability & Expressiveness Assessment

We inspected the current graph schemas in [`backend/app/schemas/graph.py`](file:///c:/Users/Raja/OneDrive/Desktop/sebi-rag-assistant/backend/app/schemas/graph.py) and collection helpers in [`backend/app/schemas/graph_collection.py`](file:///c:/Users/Raja/OneDrive/Desktop/sebi-rag-assistant/backend/app/schemas/graph_collection.py) against the verification requirements:

1. **Informational References vs. Operative Supersession:**
   - `GraphEdge` explicitly supports `RelationshipType.REFERENCES` alongside `AMENDS` and `RESCINDS`.
   - The property `is_supersession_inducing` returns `True` strictly for `AMENDS` and `RESCINDS`, and `False` for `REFERENCES`. This guarantees that background legal references do not invalidate base documents.
2. **Provision-Specific Partial Amendments:**
   - `ScopeType.PARTIAL` is enforced via a Pydantic model validator requiring `affected_provisions` to contain at least one non-empty string identifier (e.g., `["Section 2.1", "Table 1"]`).
   - This prevents partial amendments from erroneously acting as full-document cancellations.
3. **Directional Invariant:**
   - Edges enforce the direction `source_document_id` (acting / modifying newer circular) → `target_document_id` (affected / base older circular), with strict self-reference prevention (`source != target`).
4. **Conclusion:** The current schema cleanly supports all granular, partial, and informational relationships required for the SEBI CSCRF corpus without semantic distortion or code modifications.

---

## 3. Detailed Evidence Ledger & Granular Provision Audit

### 3.1. Relationships Originating from `sebi_cscrf_2025_045` (Issue Date: 2025-03-28)

#### Edge 1: `sebi_cscrf_2025_045` → `AMENDS` → `sebi_cscrf_2024_113`
- **Affected Circular:** `sebi_cscrf_2024_113` (Master CSCRF)
- **Specific Affected Provision:** Paragraph 17 (Implementation Period / Compliance Timelines)
- **Scope:** `partial` (`affected_provisions: ["Paragraph 17", "Implementation Period"]`)
- **PDF Page:** Page 2
- **Printed Page:** Page 2 of 3
- **Verbatim Evidence Quotation:**
  > *"2. SEBI had received multiple requests for CSCRF compliance timelines extension to ensure ease of compliance for them. Therefore, it has been decided to extend the compliance timelines by three (3) months, i.e., till June 30, 2025 to all REs, except Market Infrastructure Institutions (MIIs), KYC Registration Agencies (KRAs), and Qualified Registrars to an Issue and Share Transfer Agents (QRTAs)."*
- **Effective Date:** Immediate upon circular issuance (Para 4: *"The provisions of this Circular shall come into force with immediate effect."*)
- **Operative Compliance Deadlines:** Extended till June 30, 2025 for all REs (except MIIs, KRAs, and QRTAs).
- **Audit Verification Status:** `verified`

#### Edge 2: `sebi_cscrf_2025_045` → `REFERENCES` → `sebi_cscrf_2024_113`
- **Affected Circular:** `sebi_cscrf_2024_113`
- **Specific Affected Provision:** Preamble Background
- **Scope:** `full` (Informational citation of base framework)
- **PDF Page:** Page 2
- **Printed Page:** Page 2 of 3
- **Verbatim Evidence Quotation:**
  > *"1. Recognising the need for robust cybersecurity measures and protection of data and IT infrastructure, Securities and Exchange Board of India (SEBI) has issued ‘Cybersecurity and Cyber Resilience Framework (CSCRF) for SEBI Regulated Entities (REs)’ vide circular SEBI/HO/ ITD-1/ITD_CSC_EXT/P/CIR/2024/113 dated August 20, 2024."*
- **Audit Verification Status:** `verified`

---

### 3.2. Relationships Originating from `sebi_cscrf_2025_060` (Issue Date: 2025-04-30)

#### Edge 3: `sebi_cscrf_2025_060` → `AMENDS` → `sebi_cscrf_2024_113` (Thresholds, Categorizations & Cloud HSM)
- **Affected Circular:** `sebi_cscrf_2024_113`
- **Specific Affected Provisions:**
  - Section 2.1 (Stock Brokers / Table 1)
  - Section 2.2 (Depository Participants / Table 2)
  - Section 2.3 (Investment Advisers)
  - Section 2.4 (Research Analysts)
  - Section 2.5 (KYC Registration Agencies)
  - Section 2.7 (AIFs and VCFs / Table 4)
  - Annexure-J / Clause 5 (Cloud Hardware Security Module)
- **Scope:** `partial`
- **PDF Page:** Pages 2–6
- **Printed Page:** Pages 2–6 of 7
- **Verbatim Evidence Quotation:**
  > *"2. Based on further discussions with REs, it has been decided to revise the thresholds and categorization of following REs as contained in this circular... 2.1. Stock brokers: 2.1.1. Stockbrokers fulfilling any one of the following parameters shall be classified accordingly (the parameters shall be applied independently)... Table 1: Criteria and thresholds for Stockbroker categorisation... 2.2. Depository Participants (DPs)... 2.3. Investment Advisers (IAs): IAs not registered with SEBI in other capacities shall be exempted from provisions of CSCRF... 2.4. Research Analysts (RAs): RAs not registered with SEBI in any other capacity shall be exempted from the CSCRF framework... 2.5. KYC Registration Agencies (KRAs): KRAs shall be re-categorised from MIIs to Qualified REs... 2.7. Alternate Investment Funds (AIFs) and Venture Capital Funds (VCFs): Categorisation shall be at the manager level instead of AIF level... 5. W.r.t. ‘Framework for Adoption of Cloud Services by SEBI Regulated Entities (REs)’ issued vide SEBI circular SEBI/HO/ITD/ITD_VAPT/P/CIR/2023/033 dated March 06, 2023 (CSCRF: Annexure-J), implementation of a dedicated Hardware Security Module (HSM) shall be made mandatory for MIIs and Qualified REs (as per the classification given in CSCRF). However, mid-size REs, small-size REs, and self-certification REs shall be allowed to implement any alternative of HSM based on their risk assessment."*
- **Audit Verification Status:** `verified`

#### Edge 4: `sebi_cscrf_2025_060` → `REFERENCES` → `sebi_cscrf_2025_045`
- **Affected Circular:** `sebi_cscrf_2025_045`
- **Specific Affected Provision:** Paragraph 2 (Background compliance timeline context)
- **Scope:** `full` (Informational citation)
- **PDF Page:** Page 2 & Page 7
- **Printed Page:** Page 2 of 7 (Para 1) & Page 7 of 7 (Para 10)
- **Verbatim Evidence Quotation:**
  > *"10. As already communicated vide SEBI circular SEBI/HO/ITD-1/ ITD_CSC_EXT/P/ CIR/2025/45 dated March 28, 2025, timeline for compliance is June 30, 2025, for REs covered in the said circular."*
- **Audit Verification Status:** `verified`

---

### 3.3. Relationships Originating from `sebi_cscrf_2025_096` (Issue Date: 2025-06-30)

#### Edge 5: `sebi_cscrf_2025_096` → `AMENDS` → `sebi_cscrf_2025_045` (Compliance Timeline Extension to August 31, 2025)
- **Affected Circular:** `sebi_cscrf_2025_045`
- **Specific Affected Provision:** Paragraph 2 (Compliance Timelines)
- **Scope:** `partial` (`affected_provisions: ["Paragraph 2", "Compliance Timelines"]`)
- **PDF Page:** Page 2
- **Printed Page:** Page 2 of 2
- **Verbatim Evidence Quotation:**
  > *"2. SEBI has received multiple requests for CSCRF compliance timelines extension to ensure ease of compliance for them. Therefore, it has been decided to extend the compliance timelines by two (2) months, i.e., till August 31, 2025 to all REs, except Market Infrastructure Institutions (MIIs), KYC Registration Agencies (KRAs), and Qualified Registrars to an Issue and Share Transfer Agents (QRTAs)."*
- **Critical Finding on Affected Categories & Extension Dates:**
  - *Correction of Premise:* Earlier audit drafts incorrectly stated that `2025/96` extended deadlines from June 30, 2025 to July 31, 2025 for Qualified and Mid-size REs.
  - *Source Verification:* The official source text in `CSCRF_2025_096.pdf` (Page 2, Para 2) explicitly states an extension by **two (2) months, i.e., till August 31, 2025** to **all REs, except Market Infrastructure Institutions (MIIs), KYC Registration Agencies (KRAs), and Qualified Registrars to an Issue and Share Transfer Agents (QRTAs)**.
- **Effective Date:** 2025-06-30 (Para 4: *"The provisions of this Circular shall come into force with immediate effect."*)
- **Audit Verification Status:** `verified`

#### Edge 6: `sebi_cscrf_2025_096` → `REFERENCES` → `sebi_cscrf_2024_113`
- **Affected Circular:** `sebi_cscrf_2024_113`
- **Specific Affected Provision:** Paragraph 1 (Background citation)
- **Scope:** `full` (Informational citation)
- **PDF Page:** Page 2
- **Printed Page:** Page 2 of 2
- **Verbatim Evidence Quotation:**
  > *"1. Recognising the need for robust cybersecurity measures and protection of data and IT infrastructure, Securities and Exchange Board of India (SEBI) has issued ‘Cybersecurity and Cyber Resilience Framework (CSCRF) for SEBI Regulated Entities (REs)’ vide circular SEBI/HO/ ITD-1/ITD_CSC_EXT/P/CIR/2024/113 dated August 20, 2024."*
- **Audit Verification Status:** `verified`

---

### 3.4. Relationships Originating from `sebi_cscrf_2025_119` (Issue Date: 2025-08-28)

#### Edge 7: `sebi_cscrf_2025_119` → `AMENDS` → `sebi_cscrf_2024_113` (Technical Clarifications & Guideline Relaxations)
- **Affected Circular:** `sebi_cscrf_2024_113`
- **Specific Affected Provisions:**
  - Part-A (Principles of Exclusivity & Equivalence)
  - Definitions (Page 26, Item f / Clause 6.1)
  - PR.AA.S4 & PR.AA.S5 (Zero-Trust Security / Clause 6.2)
  - PR.AA.S16 (Mobile Application Security / Clause 6.3)
  - DE.CM.S3 (Guideline 3.c - BAS/CART Security Solutions / Clause 6.5)
  - RC.RP.S2 (Two-Hour RTO & 15-min RPO / Clause 6.10)
  - Section 4.2 & PR.IP.S16 (ISO 27001 Recommendation / Clause 6.11)
- **Scope:** `partial`
- **PDF Page:** Pages 3–9
- **Printed Page:** Pages 3–9 of 11
- **Verbatim Evidence Quotation:**
  > *"5.1. Following Principle of Exclusivity and Principle of Equivalence have been formulated... 6.1. Clarification: Above-mentioned para (f) shall now be read as under: Any other system which is on the same network segment where systems mentioned in para (a) to (e) are deployed... 6.2. Clarification: REs shall implement suggested strategies/ methodologies such as Zero-trust networks, segmentation... 6.3. Clarification: Above-mentioned guidelines are recommendatory (not mandatory) in nature... 6.5. Clarification: It is recommended that REs consider deploying a range of security solutions in consultation with their IT Committee... 6.10. Clarification: Resumption within two hours (i.e. two-hour RTO)... RPO for critical systems shall be 15 minutes as per SEBI circular dated March 22, 2021... 6.11. Clarification: Qualified REs are encouraged and recommended (not mandatory) to obtain ISO 27001 certification."*
- **Audit Verification Status:** `verified`

#### Edge 8: `sebi_cscrf_2025_119` → `AMENDS` → `sebi_cscrf_2025_060` (Portfolio Managers & Merchant Bankers Categorization)
- **Affected Circular:** `sebi_cscrf_2025_060`
- **Specific Affected Provision:** Section 7.1 (Table 1: Portfolio Managers) & Section 7.2 (Table 2: Merchant Bankers)
- **Scope:** `partial` (`affected_provisions: ["Section 7.1 (Table 1: Portfolio Managers)", "Section 7.2 (Table 2: Merchant Bankers)"]`)
- **PDF Page:** Page 10
- **Printed Page:** Page 10 of 11
- **Verbatim Evidence Quotation:**
  > *"7. Based on further discussions, it has been decided to revise the thresholds and categorization of following REs as under: 7.1. Portfolio Managers Table 1: Criteria and thresholds for Portfolio Managers categorization... 7.2. Merchant Bankers (MBs) Table 2: Criteria and thresholds for MBs categorization (All active Merchant Bankers -> Small-size REs; All inactive Merchant Bankers -> Exempt from CSCRF)"*
- **Audit Verification Status:** `verified`

#### Edge 9: `sebi_cscrf_2025_119` → `REFERENCES` → `sebi_cscrf_2025_060` (Market-SOC Cross Reference)
- **Affected Circular:** `sebi_cscrf_2025_060`
- **Specific Affected Provision:** Clause 2.2, 2.6, 2.7 and 3 (Market-SOC Onboarding)
- **Scope:** `partial` (`affected_provisions: ["Clause 2.2", "Clause 2.6", "Clause 2.7", "Clause 3", "Clause 6.9"]`)
- **PDF Page:** Page 8
- **Printed Page:** Page 8 of 11
- **Verbatim Evidence Quotation:**
  > *"Clarification: small-size and self-certification with few exclusions as mentioned in (CSCRF Box Item 11, DE.CM.S3 Guidelines (c-d) – Page 118) and (Clause 2.2, 2.6, 2.7 and 3 of SEBI circular vide SEBI/HO/ ITD-1/ITD_CSC_EXT/P/CIR/2025/60 dated April 30, 2025) have been mandated to be on boarded on Market-SOC."*
- **Audit Verification Status:** `verified`

---

## 4. Historical Temporal Query Analysis (`as_of = "2025-07-01"`)

### Query Scenario
* **Target Question:** "What is the operative compliance deadline for Qualified and Mid-size REs as of July 1, 2025?"

### Supporting Evidence & Chain of Legal Validity
1. **Issue Date Sequence:**
   - On **2024-08-20** (`2024/113`), baseline implementation glide paths were established (Jan 1, 2025 for existing cyber circular entities; Apr 1, 2025 for new entities).
   - On **2025-03-28** (`2025/045`), the compliance timeline was extended till **June 30, 2025** for all covered REs (excluding MIIs, KRAs, QRTAs).
   - On **2025-06-30** (`2025/096`), circular `2025/96` came into force immediately (Para 4) on **June 30, 2025**, extending the timeline by two months to **August 31, 2025** for all covered REs.
2. **Temporal Snapshot as of 2025-07-01:**
   - Because `2025/096` was issued and in force on `2025-06-30`, as of `2025-07-01` the operative compliance deadline was **August 31, 2025**.
   - The prior deadline of `2025-06-30` from `2025/045` was superseded prior to July 1, 2025.
3. **Graph Limitations & Resolution Requirements:**
   - A naive RAG search retrieving only `2025/045` would falsely conclude the deadline expired on June 30, 2025.
   - Accurately resolving this query requires a **2-hop temporal graph traversal**:
     $$\text{sebi\_cscrf\_2025\_096} \xrightarrow[\text{effective: 2025-06-30}]{\text{AMENDS}} \text{sebi\_cscrf\_2025\_045} \xrightarrow[\text{effective: 2025-03-28}]{\text{AMENDS}} \text{sebi\_cscrf\_2024\_113}$$
   - Any query evaluation engine must filter edges by `effective_date <= 2025-07-01` to traverse and return the active August 31, 2025 deadline.

---

## 5. Concise Approval & Classification Summary Table

| Edge ID | Acting Circular (`source`) | Affected Circular (`target`) | Relationship | Scope | Target Provision | Source Page (PDF / Print) | Classification | Classification Explanation |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `edge_cscrf_2025_045_amends_2024_113_timelines` | `sebi_cscrf_2025_045` | `sebi_cscrf_2024_113` | `AMENDS` | `partial` | Para 17 (Timelines) | PDF p. 2 / Print p. 2 of 3 | **`verified`** | Verbatim text extends compliance deadline to June 30, 2025. |
| `edge_cscrf_2025_045_references_2024_113_preamble` | `sebi_cscrf_2025_045` | `sebi_cscrf_2024_113` | `REFERENCES` | `full` | Master Framework | PDF p. 2 / Print p. 2 of 3 | **`verified`** | Foundational preamble citation without operative amendment. |
| `edge_cscrf_2025_060_amends_2024_113_stock_brokers_and_entities` | `sebi_cscrf_2025_060` | `sebi_cscrf_2024_113` | `AMENDS` | `partial` | Section 2.1–2.7, Annexure-J | PDF pp. 2–6 / Print pp. 2–6 of 7 | **`verified`** | Explicitly replaces criteria/threshold tables and modifies HSM mandate. |
| `edge_cscrf_2025_060_references_2025_045_timeline` | `sebi_cscrf_2025_060` | `sebi_cscrf_2025_045` | `REFERENCES` | `full` | Compliance Timelines Context | PDF p. 2 & 7 / Print p. 2 & 7 of 7 | **`verified`** | Contextual citation reiterating June 30, 2025 deadline. |
| `edge_cscrf_2025_096_amends_2025_045_timelines_august` | `sebi_cscrf_2025_096` | `sebi_cscrf_2025_045` | `AMENDS` | `partial` | Para 2 (Timelines) | PDF p. 2 / Print p. 2 of 2 | **`verified`** | Extends compliance timelines by 2 months till August 31, 2025. |
| `edge_cscrf_2025_096_references_2024_113_preamble` | `sebi_cscrf_2025_096` | `sebi_cscrf_2024_113` | `REFERENCES` | `full` | Master Framework | PDF p. 2 / Print p. 2 of 2 | **`verified`** | Preamble background citation. |
| `edge_cscrf_2025_119_amends_2024_113_technical_clarifications` | `sebi_cscrf_2025_119` | `sebi_cscrf_2024_113` | `AMENDS` | `partial` | Part-A, Definitions, PR.AA, DE.CM, RC.RP, PR.IP | PDF pp. 3–9 / Print pp. 3–9 of 11 | **`verified`** | Inserts multi-regulator principles and relaxes ISO 27001, Mobile, BAS/CART. |
| `edge_cscrf_2025_119_amends_2025_060_pm_mb_thresholds` | `sebi_cscrf_2025_119` | `sebi_cscrf_2025_060` | `AMENDS` | `partial` | Section 7.1 & 7.2 (PM & MB Categories) | PDF p. 10 / Print p. 10 of 11 | **`verified`** | Replaces PM AUM tables and exempts inactive MBs. |
| `edge_cscrf_2025_119_references_2025_060_market_soc` | `sebi_cscrf_2025_119` | `sebi_cscrf_2025_060` | `REFERENCES` | `partial` | Clause 2.2, 2.6, 2.7, 3 (Market SOC) | PDF p. 8 / Print p. 8 of 11 | **`verified`** | Operational cross-reference to Market-SOC onboarding. |

---

## 6. External Boundary Ledger (Informational / Out of Corpus)

| Acting Circular | Referenced External Document / Standard | Legal Context | Corpus Status | Classification |
| :--- | :--- | :--- | :--- | :--- |
| `2025/060`, `2025/119` | `SEBI/HO/ITD-1/ITD_CSC_EXT/P/CIR/2024/184` (Dated Dec 31, 2024) | Clarifications to CSCRF. | Not in 5-PDF corpus | `needs human review` (Boundary edge) |
| `2024/113` (Master) | `CIR/MRD/CSC/148/2018` (Dated Dec 07, 2018) | Earlier Cybersecurity guidelines for Stock Exchanges / Depositories. | Superseded historical circular | `not established` (Pre-CSCRF) |
| `2025/119` | `SEBI circular dated March 22, 2021` | Guidelines for BCP & DR of MIIs (RPO 15-min definition). | External guideline reference | `needs human review` (Boundary edge) |
| `2025/119` | CERT-In Comprehensive Cyber Security Audit Policy Guidelines | External audit policy guidelines. | External standard reference | `needs human review` (Boundary edge) |
