/**
 * TypeScript definitions for Persistent Query History (Milestone 9).
 */

export interface QueryHistoryItem {
  id: string;
  collection_id: string;
  workspace_id?: string | null;
  query_text: string;
  disposition: string;
  verified_excerpts_count: number;
  technical_validation_passed: boolean;
  created_at: string;
  answer_preview?: string | null;
  citation_count?: number;
}

export interface QueryHistoryDetail extends QueryHistoryItem {
  query_options: Record<string, unknown>;
  summary_narrative?: string | null;
  answer_text?: string | null;
  claim_citations: Array<Record<string, unknown>>;
}

export interface QueryHistoryListResponse {
  items: QueryHistoryItem[];
  total: number;
  limit: number;
  offset: number;
}

