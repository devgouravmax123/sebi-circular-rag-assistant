/**
 * TypeScript definitions for Ephemeral Guest Sessions (Milestone 11).
 */

export interface GuestQuotas {
  max_collections: number;
  max_documents: number;
  max_upload_bytes: number;
  max_queries: number;
  ttl_hours: number;
}

export interface GuestSessionResponse {
  session_id: string;
  token: string;
  expires_at: string;
  quotas: GuestQuotas;
}

export interface GuestSessionStatusResponse {
  session_id: string;
  is_active: boolean;
  expires_at: string;
  time_remaining_seconds: number;
  collections_count: number;
  documents_count: number;
  bytes_uploaded: number;
  queries_count: number;
  quotas: GuestQuotas;
}

export interface GuestSessionTerminationResponse {
  session_id: string;
  is_active: boolean;
  cleaned_up: boolean;
  deleted_documents_count: number;
  deleted_bytes: number;
  message: string;
}

export interface GuestCollection {
  id: string;
  name: string;
  description?: string | null;
  policy_name: string;
  created_at: string;
  is_temporary: boolean;
}

export interface GuestDocument {
  id: string;
  title: string;
  collection_id: string;
  source_uri?: string;
  file_size_bytes?: number;
  created_at: string;
  is_published: boolean;
}

export interface GuestJob {
  id: string;
  document_id: string;
  collection_id: string;
  status: 'queued' | 'processing' | 'completed' | 'failed';
  stage: string;
  created_at: string;
  error_message?: string | null;
}

export interface GuestUploadResponse {
  job_id: string;
  document_id: string;
  collection_id: string;
  status: string;
  message: string;
  title?: string;
  file_size_bytes?: number;
}

export interface GuestHistoryRecord {
  id: string;
  query_text: string;
  collection_id: string;
  disposition: string;
  answer_text?: string | null;
  created_at: string;
  citations_count: number;
}
