/**
 * Robust API Client for SEBI CSCRF Regulatory Intelligence Assistant.
 * Connects to POST /api/v1/query and GET /health.
 */

import type { QueryRequest, QueryResponse, QueryError, CollectionSummary } from '../types/query';

export const API_BASE_URL: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/+$/, '') ||
  'http://127.0.0.1:8000';

const DEFAULT_TIMEOUT_MS = 35000;

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

/**
 * Fetches available queryable collections from GET /api/v1/collections.
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
 * Submits query to POST /api/v1/query with full client-side timeout and validation safety.
 */
export async function queryRegulatoryAssistant(
  request: QueryRequest,
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
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/json',
      },
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
          // Pydantic validation error format
          detailMsg = errorData.detail.map((d: { msg?: string; loc?: string[] }) => d.msg || JSON.stringify(d)).join('; ');
        }
      } catch {
        // Fall back to HTTP status string
      }

      const error: QueryError = {
        message: response.status === 422
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

    // Basic shape validation to protect UI from unexpected payloads
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
