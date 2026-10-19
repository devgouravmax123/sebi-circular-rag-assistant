/**
 * TypeScript definitions for SEBI CSCRF Query API (POST /api/v1/query) and UI state.
 */

export type AnswerDisposition =
  | 'VERIFIED_HISTORICAL_EXCERPTS'
  | 'QUALIFIED_HISTORICAL_EXCERPTS'
  | 'CONTROLLED_ABSTENTION'
  | 'REJECTED_UNSAFE';

export type RegulatoryStatus =
  | 'UNRESOLVED'
  | 'INFORMATIONAL'
  | 'PARTIALLY_AMENDED'
  | 'SUPERSEDED'
  | 'ACTIVE_CURRENT';

export interface ValidatedExcerptItem {
  excerpt_id: string;
  source_chunk_id: string;
  document_id: string;
  circular_number: string | null;
  title: string | null;
  issue_date: string | null;
  page_start: number;
  page_end: number;
  excerpt_text: string;
  regulatory_status: RegulatoryStatus;
  status_reason: string;
  applicable_edge_ids: string[];
  acting_document_ids: string[];
}

export type SynthesisStatus =
  | 'FULLY_ANSWERED'
  | 'PARTIALLY_ANSWERED'
  | 'ABSTAINED'
  | 'PROVIDER_FAILED'
  | 'UNSUPPORTED_CLAIMS_REJECTED';

export interface CitationProvenance {
  source_chunk_id: string;
  document_id: string;
  circular_number: string | null;
  title: string | null;
  issue_date: string | null;
  sha256?: string | null;
  page_start?: number | null;
  page_end?: number | null;
  provision_id?: string | null;
  regulatory_status: string;
}

export interface ClaimCitation {
  claim_text: string;
  supporting_chunk_ids: string[];
  citations?: CitationProvenance[];
}

export interface UnresolvedSubquestion {
  subquestion: string;
  reason: string;
}

export interface QueryRequest {
  query: string;
  top_k?: number;
  include_superseded?: boolean;
}

export interface QueryResponse {
  query: string;
  disposition: AnswerDisposition;
  verified_excerpts: ValidatedExcerptItem[];
  historical_superseded_excerpts: ValidatedExcerptItem[];
  summary_narrative: string;
  technical_validation_passed: boolean;
  retrieval_candidate_count: number;
  abstention_reason: string | null;
  regulatory_warnings: string[];
  pilot_corpus_scope_limitation: string;
  answer_text?: string | null;
  synthesis_status?: SynthesisStatus | string | null;
  claim_citations?: ClaimCitation[];
  unresolved_subquestions?: UnresolvedSubquestion[];
  synthesis_failure_reason?: string | null;
}

export type QueryStatus = 'idle' | 'loading' | 'success' | 'error';

export interface QueryError {
  message: string;
  status?: number;
  detail?: string;
  isValidationError?: boolean;
  isNetworkError?: boolean;
}
