/**
 * TypeScript definitions for Workspaces, Memberships, and Collections (Milestone 9).
 */

export type WorkspaceRole = 'owner' | 'admin' | 'member' | 'viewer';

export interface Workspace {
  id: string;
  name: string;
  slug: string;
  description?: string | null;
  is_system: boolean;
  owner_id?: string | null;
  created_at: string;
  updated_at: string;
  current_user_role?: WorkspaceRole | null;
}

export interface WorkspaceListResponse {
  workspaces: Workspace[];
  total: number;
}

export interface WorkspaceCreateInput {
  name: string;
  slug: string;
  description?: string;
}

export interface WorkspaceMember {
  id: string;
  workspace_id: string;
  user_id: string;
  role: WorkspaceRole;
  created_at: string;
  updated_at: string;
  user_email?: string | null;
  user_full_name?: string | null;
}

export interface MemberListResponse {
  members: WorkspaceMember[];
  total: number;
}

export interface MemberAddInput {
  email: string;
  role: WorkspaceRole;
}

export interface WorkspaceCollection {
  id: string;
  workspace_id: string;
  name: string;
  description?: string | null;
  policy_name: string;
  enforce_regulatory_graph: boolean;
  is_system: boolean;
  is_public: boolean;
  created_at: string;
  updated_at: string;
}

export interface WorkspaceCollectionListResponse {
  collections: WorkspaceCollection[];
  total: number;
}

export interface WorkspaceCollectionCreateInput {
  id: string;
  name: string;
  description?: string;
  policy_name?: 'general_static' | 'sebi_cscrf_strict';
}

export type IngestionJobStatus = 'queued' | 'processing' | 'completed' | 'failed' | 'cancelled' | 'duplicate';

export interface CollectionIngestionJob {
  job_id: string;
  document_id: string;
  workspace_id: string;
  collection_id: string;
  status: IngestionJobStatus;
  stage: string;
  retry_count: number;
  error_message?: string | null;
  created_at: string;
  completed_at?: string | null;
  is_published?: boolean;
}

export interface CollectionIngestionJobListResponse {
  jobs: CollectionIngestionJob[];
  total: number;
}

export interface CollectionDocument {
  document_id: string;
  title: string;
  circular_number?: string | null;
  source_url?: string;
  workspace_id?: string | null;
  collection_id?: string | null;
  created_at: string;
  is_published?: boolean;
}

export interface CollectionDocumentListResponse {
  documents: CollectionDocument[];
  total: number;
}

export interface DocumentUploadResponse {
  job_id: string;
  document_id: string;
  workspace_id: string;
  collection_id: string;
  status: string;
  is_duplicate_registration: boolean;
  message: string;
}

