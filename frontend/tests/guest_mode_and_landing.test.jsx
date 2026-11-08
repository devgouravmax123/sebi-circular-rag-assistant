import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

import { LandingPage } from '../src/components/LandingPage.tsx';
import { SessionSettingsView } from '../src/components/SessionSettingsView.tsx';
import { CollectionsView } from '../src/components/CollectionsView.tsx';
import { AuthProvider } from '../src/context/AuthContext.tsx';
import { WorkspaceProvider } from '../src/context/WorkspaceContext.tsx';
import {
  initiateGuestSession,
  fetchGuestSessionStatus,
  terminateGuestSession,
  createGuestCollection,
  uploadGuestDocument,
  fetchGuestHistory,
  recordGuestQuery,
} from '../src/api/client.ts';

// Helper to mock global fetch
function createMockFetch(resolver) {
  return async (url, options) => {
    const res = await resolver(url, options);
    return {
      ok: res.ok !== undefined ? res.ok : true,
      status: res.status || 200,
      statusText: res.statusText || 'OK',
      json: async () => res.data,
      text: async () => JSON.stringify(res.data),
    };
  };
}

function renderWithProviders(element) {
  return renderToStaticMarkup(
    React.createElement(
      AuthProvider,
      null,
      React.createElement(WorkspaceProvider, null, element)
    )
  );
}

// ----------------------------------------------------------------------------
// Test 1: LandingPage renders neutral SaaS branding, headline, and 3 primary CTAs
// ----------------------------------------------------------------------------
test('1. LandingPage: renders neutral branding, headline, and Sign In / Sign Up / Guest CTAs', () => {
  const html = renderWithProviders(React.createElement(LandingPage, {}));

  // Neutral product identity
  assert.match(html, /Document Intelligence/);
  assert.match(html, /General-Purpose AI Document Intelligence Platform/);
  assert.match(html, /Turn your documents/);
  assert.match(html, /into trusted answers\./);
  assert.match(html, /Evidence-grounded AI research for business, technical, legal, and operational documents\./);

  // 3 Primary Entry CTAs
  assert.match(html, /Sign In/);
  assert.match(html, /Sign Up/);
  assert.match(html, /Continue as Guest/);

  // Benefit Cards
  assert.match(html, /Citation Grounding/);
  assert.match(html, /Controlled Abstention/);
  assert.match(html, /Ephemeral Guest Mode/);
  assert.match(html, /Persistent Workspaces/);

  // How it works 3 steps
  assert.match(html, /Create or Select Collection/);
  assert.match(html, /Upload PDFs/);
  assert.match(html, /Ask &amp; Verify/);

  // Domains
  assert.match(html, /Financial Reports/);
  assert.match(html, /Legal Contracts/);
  assert.match(html, /Engineering Specs/);
  assert.match(html, /Regulatory Policies/);

  // Must NOT show internal dashboard tables or public collections automatically
  assert.doesNotMatch(html, /Active Workspace/);
  assert.doesNotMatch(html, /sebi_cscrf/);
});

// ----------------------------------------------------------------------------
// Test 2: Ephemeral Guest Session API Client calls
// ----------------------------------------------------------------------------
test('2. Guest API Client: initiate, status, terminate, collections, upload, and history', async () => {
  const originalFetch = global.fetch;

  let capturedRequests = [];
  global.fetch = createMockFetch((url, options) => {
    capturedRequests.push({ url, options });
    if (url.includes('/api/v1/guest/session/terminate')) {
      return {
        ok: true,
        data: {
          session_id: 'test-guest-session',
          is_active: false,
          cleaned_up: true,
          deleted_documents_count: 2,
          deleted_bytes: 524288,
          message: 'Guest session terminated and all ephemeral storage purged.',
        },
      };
    }
    if (url.includes('/api/v1/guest/session') && options?.method === 'GET') {
      return {
        ok: true,
        data: {
          session_id: 'test-guest-session',
          is_active: true,
          expires_at: '2026-11-09T00:00:00Z',
          time_remaining_seconds: 86400,
          collections_count: 1,
          documents_count: 2,
          bytes_uploaded: 524288,
          queries_count: 3,
          quotas: {
            max_collections: 1,
            max_documents: 2,
            max_upload_bytes: 10485760,
            max_queries: 10,
            ttl_hours: 24,
          },
        },
      };
    }
    if (url.includes('/api/v1/guest/session')) {
      return {
        ok: true,
        data: {
          session_id: 'test-guest-session',
          token: 'guest-jwt-token-12345',
          expires_at: '2026-11-09T00:00:00Z',
          quotas: {
            max_collections: 1,
            max_documents: 2,
            max_upload_bytes: 10485760,
            max_queries: 10,
            ttl_hours: 24,
          },
        },
      };
    }
    if (url.includes('/api/v1/guest/collections')) {
      return {
        ok: true,
        data: {
          id: 'guest-col-1',
          name: 'Temporary FinOps',
          description: 'Guest sandbox collection',
          policy_name: 'general_static',
          created_at: '2026-11-08T00:00:00Z',
          is_temporary: true,
        },
      };
    }
    if (url.includes('/api/v1/guest/history/record')) {
      return {
        ok: true,
        data: { status: 'recorded', id: 'rec-123' },
      };
    }
    if (url.includes('/api/v1/guest/history')) {
      return {
        ok: true,
        data: {
          items: [
            {
              id: 'rec-123',
              query_text: 'What are the FinOps rules?',
              collection_id: 'guest-col-1',
              disposition: 'VERIFIED_HISTORICAL_EXCERPTS',
              answer_text: 'Cloud FinOps retention requires daily snapshot lifecycle.',
              created_at: '2026-11-08T00:00:00Z',
              citations_count: 1,
            },
          ],
          total: 1,
        },
      };
    }
    return { ok: true, data: {} };
  });

  try {
    // 1. Initiate session
    const initRes = await initiateGuestSession();
    assert.strictEqual(initRes.session_id, 'test-guest-session');
    assert.strictEqual(initRes.token, 'guest-jwt-token-12345');
    assert.strictEqual(initRes.quotas.max_collections, 1);
    assert.strictEqual(initRes.quotas.max_documents, 2);

    // 2. Fetch session status
    const statusRes = await fetchGuestSessionStatus('guest-jwt-token-12345');
    assert.strictEqual(statusRes.is_active, true);
    assert.strictEqual(statusRes.queries_count, 3);

    // 3. Create collection
    const colRes = await createGuestCollection('Temporary FinOps', 'Guest sandbox', 'guest-jwt-token-12345');
    assert.strictEqual(colRes.id, 'guest-col-1');
    assert.strictEqual(colRes.is_temporary, true);

    // 4. Record query history
    const recRes = await recordGuestQuery('What are the FinOps rules?', 'guest-col-1', 'VERIFIED_HISTORICAL_EXCERPTS', 'Answer text', 1, 'guest-jwt-token-12345');
    assert.strictEqual(recRes.status, 'recorded');

    // 5. Fetch guest history
    const histRes = await fetchGuestHistory('guest-jwt-token-12345');
    assert.strictEqual(histRes.total, 1);
    assert.strictEqual(histRes.items[0].query_text, 'What are the FinOps rules?');

    // 6. Terminate session
    const termRes = await terminateGuestSession('guest-jwt-token-12345');
    assert.strictEqual(termRes.cleaned_up, true);
    assert.strictEqual(termRes.deleted_documents_count, 2);
  } finally {
    global.fetch = originalFetch;
  }
});

// ----------------------------------------------------------------------------
// Test 3: SessionSettingsView renders quotas and deletion controls in Guest Mode
// ----------------------------------------------------------------------------
test('3. SessionSettingsView: renders quotas, time remaining, and purge action in Guest Mode', () => {
  const html = renderWithProviders(React.createElement(SessionSettingsView, {}));

  // Guest Mode Ephemeral notice
  assert.match(html, /Ephemeral Sandbox \(24-Hour TTL\)/);
  assert.match(html, /Guest Session &amp; Data Deletion/);
  assert.match(html, /Time Remaining Before Automatic Purge/);

  // Quotas meters
  assert.match(html, /Collections/);
  assert.match(html, /PDF Documents/);
  assert.match(html, /Upload Capacity/);
  assert.match(html, /Queries/);

  // Zero Permanent Storage Guarantee
  assert.match(html, /Ephemeral Session Policy/);
  assert.match(html, /All documents, metadata, indexes, and queries generated during this guest session are kept in an isolated temporary partition\./);

  // Purge Action
  assert.match(html, /End Session &amp; Purge Data/);
  assert.match(html, /Want to retain your documents and research permanently\?/);
});

// ----------------------------------------------------------------------------
// Test 4: Ingestion Reality Guard on CollectionsView
// ----------------------------------------------------------------------------
test('4. CollectionsView: renders Free-Tier Ingestion Reality Guard and accurate queue status', () => {
  const html = renderWithProviders(React.createElement(CollectionsView, {
    onNavigateToResearch: () => {},
  }));

  // Ingestion Reality Guard Notice
  assert.match(html, /Asynchronous Ingestion Pipeline Notice/);
  assert.match(html, /Upload received\. Document is queued for ingestion\. On the free tier, background indexing requires worker execution\./);
  assert.match(html, /Snapshot Readiness Rule:/);
  assert.match(html, /Pre-indexed collections remain active and ready to query immediately\./);
});

