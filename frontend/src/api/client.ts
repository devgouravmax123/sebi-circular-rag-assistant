/**
 * Robust API Client for SEBI CSCRF Regulatory Intelligence Assistant & Multi-Tenant SaaS.
 * Connects to:
 * - Auth endpoints: /api/v1/auth/signup, /api/v1/auth/login, /api/v1/auth/logout, /api/v1/auth/me
 * - Workspace endpoints: /api/v1/workspaces, /api/v1/workspaces/{id}/members, /api/v1/workspaces/{id}/collections
 * - Collections: /api/v1/collections
 * - Query: /api/v1/query
 * - History: /api/v1/query-history, /api/v1/query-history/{id}
 * - Health: /health
 */

import type { QueryRequest, QueryResponse, QueryError, CollectionSummary } from '../types/query';
import type { User, AuthResponse, LoginCredentials, SignupCredentials } from '../types/auth';
import type {
  Workspace,
  WorkspaceListResponse,
  WorkspaceCreateInput,
  WorkspaceMember,
  MemberListResponse,
  MemberAddInput,
  WorkspaceCollection,
  WorkspaceCollectionListResponse,
  WorkspaceCollectionCreateInput,
  CollectionIngestionJob,
  CollectionIngestionJobListResponse,
  CollectionDocument,
  CollectionDocumentListResponse,
  DocumentUploadResponse,
} from '../types/workspace';
import type { QueryHistoryDetail, QueryHistoryListResponse } from '../types/history';
import type {
  GuestSessionResponse,
  GuestSessionStatusResponse,
  GuestSessionTerminationResponse,
  GuestCollection,
  GuestDocument,
  GuestJob,
  GuestHistoryRecord,
  GuestUploadResponse,
} from '../types/guest';

const rawBaseUrl = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.trim();
export const API_BASE_URL: string = rawBaseUrl
  ? rawBaseUrl.replace(/\/+$/, '').replace(/\/api\/v1$/, '')
  : typeof window !== 'undefined' && window.location?.hostname && window.location.hostname !== 'localhost' && window.location.hostname !== '127.0.0.1'
  ? 'https://sebi-circular-rag-assistant.onrender.com'
  : 'http://127.0.0.1:8000';

const DEFAULT_TIMEOUT_MS = 35000;

/**
 * Builds request headers, attaching Authorization Bearer header only when a valid token is provided.
 */
function buildHeaders(token?: string | null, additionalHeaders: Record<string, string> = {}): HeadersInit {
  const headers: Record<string, string> = {
    Accept: 'application/json',
    ...additionalHeaders,
  };
  if (token && token.trim().length > 0) {
    headers['Authorization'] = `Bearer ${token.trim()}`;
  }
  return headers;
}

/**
 * Parses and formats API error details from HTTP response without leaking stack traces.
 */
async function parseErrorResponse(response: Response, defaultMessage: string): Promise<Error> {
  let detailMsg = `HTTP ${response.status}: ${response.statusText}`;
  try {
    const errorData = await response.json();
    if (typeof errorData?.detail === 'string') {
      detailMsg = errorData.detail;
    } else if (Array.isArray(errorData?.detail)) {
      detailMsg = errorData.detail
        .map((d: { msg?: string; loc?: string[] }) => d.msg || JSON.stringify(d))
        .join('; ');
    }
  } catch {
    // Keep HTTP status string
  }

  const err = new Error(detailMsg || defaultMessage);
  (err as unknown as { status: number }).status = response.status;
  return err;
}

/**
 * Executes health probe against GET /health.
 */
export async function probeHealth(timeoutMs: number = 5000): Promise<{ ok: boolean; statusText?: string }> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const res = await fetch(`${API_BASE_URL}/health`, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      signal: controller.signal,
    });

    clearTimeout(timer);
    if (!res.ok) {
      return { ok: false, statusText: `HTTP ${res.status} ${res.statusText}` };
    }
    const data = await res.json();
    return { ok: data?.status === 'ok', statusText: data?.status || 'ok' };
  } catch (err: unknown) {
    clearTimeout(timer);
    const msg = err instanceof Error ? err.message : 'Unreachable';
    return { ok: false, statusText: msg };
  }
}

// ============================================================================
// 1. Authentication APIs
// ============================================================================

export async function authSignup(credentials: SignupCredentials): Promise<AuthResponse> {
  const response = await fetch(`${API_BASE_URL}/api/v1/auth/signup`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(credentials),
  });

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Signup failed.');
  }

  return response.json();
}

export async function authLogin(credentials: LoginCredentials): Promise<AuthResponse> {
  const response = await fetch(`${API_BASE_URL}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(credentials),
  });

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Login failed.');
  }

  return response.json();
}

export async function authLogout(token: string): Promise<void> {
  try {
    await fetch(`${API_BASE_URL}/api/v1/auth/logout`, {
      method: 'POST',
      headers: buildHeaders(token),
    });
  } catch {
    // Stateless logout - ignore network errors on termination
  }
}

export async function authGetMe(token: string): Promise<User> {
  const response = await fetch(`${API_BASE_URL}/api/v1/auth/me`, {
    method: 'GET',
    headers: buildHeaders(token),
  });

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Failed to retrieve authenticated user identity.');
  }

  return response.json();
}

// ============================================================================
// 2. Workspace & Membership APIs
// ============================================================================

export async function fetchWorkspaces(token: string): Promise<Workspace[]> {
  const response = await fetch(`${API_BASE_URL}/api/v1/workspaces`, {
    method: 'GET',
    headers: buildHeaders(token),
  });

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Failed to fetch workspaces.');
  }

  const data: WorkspaceListResponse = await response.json();
  return data.workspaces || [];
}

export async function createWorkspace(input: WorkspaceCreateInput, token: string): Promise<Workspace> {
  const response = await fetch(`${API_BASE_URL}/api/v1/workspaces`, {
    method: 'POST',
    headers: buildHeaders(token, { 'Content-Type': 'application/json' }),
    body: JSON.stringify(input),
  });

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Failed to create workspace.');
  }

  return response.json();
}

export async function fetchWorkspaceMembers(workspaceId: string, token: string): Promise<WorkspaceMember[]> {
  const response = await fetch(`${API_BASE_URL}/api/v1/workspaces/${workspaceId}/members`, {
    method: 'GET',
    headers: buildHeaders(token),
  });

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Failed to fetch workspace members.');
  }

  const data: MemberListResponse = await response.json();
  return data.members || [];
}

export async function addWorkspaceMember(
  workspaceId: string,
  input: MemberAddInput,
  token: string
): Promise<WorkspaceMember> {
  const response = await fetch(`${API_BASE_URL}/api/v1/workspaces/${workspaceId}/members`, {
    method: 'POST',
    headers: buildHeaders(token, { 'Content-Type': 'application/json' }),
    body: JSON.stringify(input),
  });

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Failed to add workspace member.');
  }

  return response.json();
}

export async function removeWorkspaceMember(
  workspaceId: string,
  userId: string,
  token: string
): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/api/v1/workspaces/${workspaceId}/members/${userId}`, {
    method: 'DELETE',
    headers: buildHeaders(token),
  });

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Failed to remove workspace member.');
  }
}

// ============================================================================
// 3. Collections APIs
// ============================================================================

/**
 * Fetches publicly queryable collections from GET /api/v1/collections.
 */
export async function fetchCollections(timeoutMs: number = 5000): Promise<CollectionSummary[]> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const res = await fetch(`${API_BASE_URL}/api/v1/collections`, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      signal: controller.signal,
    });

    clearTimeout(timer);
    if (!res.ok) {
      throw new Error(`Failed to fetch collections: HTTP ${res.status} ${res.statusText}`);
    }

    const data = await res.json();
    if (!Array.isArray(data)) {
      throw new Error('Malformed collections response: expected an array.');
    }
    return data as CollectionSummary[];
  } catch (err: unknown) {
    clearTimeout(timer);
    throw err;
  }
}

/**
 * Fetches private collections for a specific workspace.
 */
export async function fetchWorkspaceCollections(
  workspaceId: string,
  token: string
): Promise<WorkspaceCollection[]> {
  const response = await fetch(`${API_BASE_URL}/api/v1/workspaces/${workspaceId}/collections`, {
    method: 'GET',
    headers: buildHeaders(token),
  });

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Failed to fetch workspace collections.');
  }

  const data: WorkspaceCollectionListResponse = await response.json();
  return data.collections || [];
}

/**
 * Creates a private collection within an authorized workspace.
 */
export async function createWorkspaceCollection(
  workspaceId: string,
  input: WorkspaceCollectionCreateInput,
  token: string
): Promise<WorkspaceCollection> {
  const response = await fetch(`${API_BASE_URL}/api/v1/workspaces/${workspaceId}/collections`, {
    method: 'POST',
    headers: buildHeaders(token, { 'Content-Type': 'application/json' }),
    body: JSON.stringify(input),
  });

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Failed to create collection in workspace.');
  }

  return response.json();
}

// ============================================================================
// 3b. Collection Document Upload & Ingestion Tracking APIs (Milestone 10)
// ============================================================================

/**
 * Uploads a PDF document to a private collection to start ingestion.
 */
export async function uploadCollectionDocument(
  workspaceId: string,
  collectionId: string,
  file: File,
  title: string | undefined,
  circularNumber: string | undefined,
  token: string
): Promise<DocumentUploadResponse> {
  const formData = new FormData();
  formData.append('file', file);
  if (title && title.trim()) {
    formData.append('title', title.trim());
  }
  if (circularNumber && circularNumber.trim()) {
    formData.append('circular_number', circularNumber.trim());
  }

  const response = await fetch(
    `${API_BASE_URL}/api/v1/workspaces/${workspaceId}/collections/${collectionId}/upload`,
    {
      method: 'POST',
      headers: buildHeaders(token),
      body: formData,
    }
  );

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Failed to upload document for ingestion.');
  }

  return response.json();
}

/**
 * Fetches the list of ingestion jobs for a collection.
 */
export async function fetchCollectionIngestionJobs(
  workspaceId: string,
  collectionId: string,
  token: string
): Promise<CollectionIngestionJob[]> {
  const response = await fetch(
    `${API_BASE_URL}/api/v1/workspaces/${workspaceId}/collections/${collectionId}/ingestion-jobs`,
    {
      method: 'GET',
      headers: buildHeaders(token),
    }
  );

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Failed to fetch ingestion jobs.');
  }

  const data: CollectionIngestionJobListResponse = await response.json();
  return data.jobs || [];
}

/**
 * Fetches the detailed status of a single ingestion job.
 */
export async function fetchCollectionIngestionJob(
  workspaceId: string,
  collectionId: string,
  jobId: string,
  token: string
): Promise<CollectionIngestionJob> {
  const response = await fetch(
    `${API_BASE_URL}/api/v1/workspaces/${workspaceId}/collections/${collectionId}/ingestion-jobs/${jobId}`,
    {
      method: 'GET',
      headers: buildHeaders(token),
    }
  );

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Failed to fetch ingestion job details.');
  }

  return response.json();
}

/**
 * Fetches documents registered in a collection.
 */
export async function fetchCollectionDocuments(
  workspaceId: string,
  collectionId: string,
  token: string
): Promise<CollectionDocument[]> {
  const response = await fetch(
    `${API_BASE_URL}/api/v1/workspaces/${workspaceId}/collections/${collectionId}/documents`,
    {
      method: 'GET',
      headers: buildHeaders(token),
    }
  );

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Failed to fetch collection documents.');
  }

  const data: CollectionDocumentListResponse = await response.json();
  return data.documents || [];
}

/**
 * Returns the download URL for a collection document.
 */
export function getDocumentDownloadUrl(
  workspaceId: string,
  collectionId: string,
  documentId: string
): string {
  return `${API_BASE_URL}/api/v1/workspaces/${workspaceId}/collections/${collectionId}/documents/${documentId}/download`;
}

export async function downloadCollectionDocumentBlob(
  workspaceId: string,
  collectionId: string,
  documentId: string,
  token: string
): Promise<Blob> {
  const url = getDocumentDownloadUrl(workspaceId, collectionId, documentId);
  const response = await fetch(url, {
    method: 'GET',
    headers: buildHeaders(token),
  });
  if (!response.ok) {
    const errorBody = await response.json().catch(() => ({}));
    throw new Error(errorBody.detail || `Download failed with HTTP ${response.status}`);
  }
  return await response.blob();
}

// ============================================================================
// 4. Query API (Public & Private Multi-Tenant)
// ============================================================================

export async function queryRegulatoryAssistant(
  request: QueryRequest,
  token?: string | null,
  timeoutMs: number = DEFAULT_TIMEOUT_MS
): Promise<QueryResponse> {
  const normalizedQuery = request.query.trim();

  if (normalizedQuery.length < 2) {
    const error: QueryError = {
      message: 'Query must contain at least 2 non-whitespace characters.',
      isValidationError: true,
    };
    throw error;
  }

  if (normalizedQuery.length > 500) {
    const error: QueryError = {
      message: 'Query cannot exceed 500 characters.',
      isValidationError: true,
    };
    throw error;
  }

  const payload: QueryRequest = {
    query: normalizedQuery,
    top_k: request.top_k ?? 5,
    include_superseded: request.include_superseded ?? false,
    collection_id: request.collection_id || 'sebi_cscrf',
  };

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(`${API_BASE_URL}/api/v1/query`, {
      method: 'POST',
      headers: buildHeaders(token, { 'Content-Type': 'application/json' }),
      body: JSON.stringify(payload),
      signal: controller.signal,
    });

    clearTimeout(timer);

    if (!response.ok) {
      let detailMsg = `HTTP ${response.status}: ${response.statusText}`;
      try {
        const errorData = await response.json();
        if (typeof errorData?.detail === 'string') {
          detailMsg = errorData.detail;
        } else if (Array.isArray(errorData?.detail)) {
          detailMsg = errorData.detail
            .map((d: { msg?: string; loc?: string[] }) => d.msg || JSON.stringify(d))
            .join('; ');
        }
      } catch {
        // Fall back to HTTP status string
      }

      const error: QueryError = {
        message:
          response.status === 401
            ? 'Authentication required to query this private collection.'
            : response.status === 404
            ? 'The requested collection was not found or is not accessible.'
            : response.status === 422
            ? `Validation Error: ${detailMsg}`
            : response.status === 500
            ? `Server Error: ${detailMsg}`
            : `Request Failed: ${detailMsg}`,
        status: response.status,
        detail: detailMsg,
        isValidationError: response.status === 422,
      };
      throw error;
    }

    const data = await response.json();

    if (!data || typeof data !== 'object' || !data.disposition || !data.summary_narrative) {
      const error: QueryError = {
        message: 'Malformed response structure received from backend service.',
      };
      throw error;
    }

    return data as QueryResponse;
  } catch (err: unknown) {
    clearTimeout(timer);

    if ((err as QueryError).message && (err as QueryError).status !== undefined) {
      throw err;
    }

    if (err instanceof DOMException && err.name === 'AbortError') {
      const error: QueryError = {
        message: `Query timed out after ${timeoutMs / 1000}s. Please verify the backend is running.`,
        isNetworkError: true,
      };
      throw error;
    }

    const error: QueryError = {
      message: err instanceof Error ? err.message : 'Unable to connect to the backend query service.',
      isNetworkError: true,
    };
    throw error;
  }
}

// ============================================================================
// 5. Query History APIs
// ============================================================================

export async function fetchQueryHistory(
  token: string,
  options: {
    limit?: number;
    offset?: number;
    workspaceId?: string | null;
    collectionId?: string | null;
  } = {}
): Promise<QueryHistoryListResponse> {
  const params = new URLSearchParams();
  if (options.limit !== undefined) params.set('limit', String(options.limit));
  if (options.offset !== undefined) params.set('offset', String(options.offset));
  if (options.workspaceId) params.set('workspace_id', options.workspaceId);
  if (options.collectionId) params.set('collection_id', options.collectionId);

  const url = `${API_BASE_URL}/api/v1/query-history${params.toString() ? `?${params.toString()}` : ''}`;
  const response = await fetch(url, {
    method: 'GET',
    headers: buildHeaders(token),
  });

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Failed to fetch query history.');
  }

  return response.json();
}

export async function fetchQueryHistoryDetail(
  historyId: string,
  token: string
): Promise<QueryHistoryDetail> {
  const response = await fetch(`${API_BASE_URL}/api/v1/query-history/${historyId}`, {
    method: 'GET',
    headers: buildHeaders(token),
  });

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Query history record not found.');
  }

  return response.json();
}

// ============================================================================
// 6. Ephemeral Guest Session APIs (Milestone 11)
// ============================================================================

export async function initiateGuestSession(): Promise<GuestSessionResponse> {
  const response = await fetch(`${API_BASE_URL}/api/v1/guest/session`, {
    method: 'POST',
    headers: buildHeaders(),
  });

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Failed to initiate guest session.');
  }

  return response.json();
}

export async function fetchGuestSessionStatus(token: string): Promise<GuestSessionStatusResponse> {
  const response = await fetch(`${API_BASE_URL}/api/v1/guest/session`, {
    method: 'GET',
    headers: buildHeaders(token),
  });

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Guest session is expired or invalid.');
  }

  return response.json();
}

export async function terminateGuestSession(token: string): Promise<GuestSessionTerminationResponse> {
  const response = await fetch(`${API_BASE_URL}/api/v1/guest/session/terminate`, {
    method: 'POST',
    headers: buildHeaders(token),
  });

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Failed to terminate guest session.');
  }

  return response.json();
}

export async function createGuestCollection(
  name: string,
  description: string | undefined,
  token: string
): Promise<GuestCollection> {
  const response = await fetch(`${API_BASE_URL}/api/v1/guest/collections`, {
    method: 'POST',
    headers: buildHeaders(token, { 'Content-Type': 'application/json' }),
    body: JSON.stringify({ name, description }),
  });

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Failed to create guest collection.');
  }

  return response.json();
}

export async function fetchGuestCollections(token: string): Promise<GuestCollection[]> {
  const response = await fetch(`${API_BASE_URL}/api/v1/guest/collections`, {
    method: 'GET',
    headers: buildHeaders(token),
  });

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Failed to fetch guest collections.');
  }

  return response.json();
}

export async function uploadGuestDocument(
  collectionId: string,
  file: File,
  title: string | undefined,
  token: string
): Promise<GuestUploadResponse> {
  const formData = new FormData();
  formData.append('collection_id', collectionId);
  formData.append('file', file);
  if (title && title.trim()) {
    formData.append('title', title.trim());
  }

  const response = await fetch(`${API_BASE_URL}/api/v1/guest/upload`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token.trim()}`,
      Accept: 'application/json',
    },
    body: formData,
  });

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Failed to upload guest document.');
  }

  return response.json();
}

export async function fetchGuestDocuments(token: string): Promise<GuestDocument[]> {
  const response = await fetch(`${API_BASE_URL}/api/v1/guest/documents`, {
    method: 'GET',
    headers: buildHeaders(token),
  });

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Failed to fetch guest documents.');
  }

  return response.json();
}

export async function fetchGuestJobs(token: string): Promise<GuestJob[]> {
  const response = await fetch(`${API_BASE_URL}/api/v1/guest/jobs`, {
    method: 'GET',
    headers: buildHeaders(token),
  });

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Failed to fetch guest jobs.');
  }

  return response.json();
}

export async function fetchGuestHistory(token: string): Promise<{ items: GuestHistoryRecord[]; total: number }> {
  const response = await fetch(`${API_BASE_URL}/api/v1/guest/history`, {
    method: 'GET',
    headers: buildHeaders(token),
  });

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Failed to fetch guest history.');
  }

  return response.json();
}

export async function recordGuestQuery(
  queryText: string,
  collectionId: string,
  disposition: string,
  answerText: string | undefined,
  citationsCount: number,
  token: string
): Promise<{ status: string; id: string }> {
  const response = await fetch(`${API_BASE_URL}/api/v1/guest/history/record`, {
    method: 'POST',
    headers: buildHeaders(token, { 'Content-Type': 'application/json' }),
    body: JSON.stringify({
      query_text: queryText,
      collection_id: collectionId,
      disposition,
      answer_text: answerText || null,
      citations_count: citationsCount,
    }),
  });

  if (!response.ok) {
    throw await parseErrorResponse(response, 'Failed to record guest query history.');
  }

  return response.json();
}
