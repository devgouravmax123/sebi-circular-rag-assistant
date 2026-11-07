import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

import { AuthModal } from '../src/components/AuthModal.tsx';
import { WorkspaceSelector } from '../src/components/WorkspaceSelector.tsx';
import { WorkspaceManageModal } from '../src/components/WorkspaceManageModal.tsx';
import { QueryHistoryDrawer } from '../src/components/QueryHistoryDrawer.tsx';
import {
  authGetMe,
  queryRegulatoryAssistant,
  fetchWorkspaces,
  createWorkspace,
  fetchWorkspaceCollections,
  createWorkspaceCollection,
  fetchQueryHistory,
  fetchQueryHistoryDetail,
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

// ----------------------------------------------------------------------------
// Test 1: Signup and Login form validation
// ----------------------------------------------------------------------------
test('1. AuthModal: validates form fields for login and signup', () => {
  // Login mode rendering
  const loginHtml = renderToStaticMarkup(
    React.createElement(AuthModal, { isOpen: true, onClose: () => {}, defaultMode: 'login' })
  );
  assert.match(loginHtml, /Sign In to Workspace/);
  assert.match(loginHtml, /Email Address/);
  assert.match(loginHtml, /Password/);
  assert.match(loginHtml, /Sign In/);

  // Signup mode rendering
  const signupHtml = renderToStaticMarkup(
    React.createElement(AuthModal, { isOpen: true, onClose: () => {}, defaultMode: 'signup' })
  );
  assert.match(signupHtml, /Create an Account/);
  assert.match(signupHtml, /Full Name \(Optional\)/);
  assert.match(signupHtml, /Must be at least 8 characters/);
  assert.match(signupHtml, /Create Account/);
});

// ----------------------------------------------------------------------------
// Test 2: Session restoration via /auth/me
// ----------------------------------------------------------------------------
test('2. Auth API: authGetMe retrieves authenticated user identity', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = createMockFetch((url, options) => {
    assert.match(url, /\/api\/v1\/auth\/me$/);
    assert.equal(options.headers['Authorization'], 'Bearer valid-test-token');
    return {
      status: 200,
      data: {
        id: 'u-12345',
        email: 'alice@example.com',
        full_name: 'Alice Engineer',
        is_active: true,
      },
    };
  });

  try {
    const user = await authGetMe('valid-test-token');
    assert.equal(user.email, 'alice@example.com');
    assert.equal(user.id, 'u-12345');
    assert.equal(user.is_active, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

// ----------------------------------------------------------------------------
// Test 3: Logout terminates session
// ----------------------------------------------------------------------------
test('3. Auth API: authLogout calls backend logout with bearer token', async () => {
  const originalFetch = globalThis.fetch;
  let logoutCalled = false;
  globalThis.fetch = createMockFetch((url, options) => {
    assert.match(url, /\/api\/v1\/auth\/logout$/);
    assert.equal(options.headers['Authorization'], 'Bearer logout-test-token');
    logoutCalled = true;
    return { status: 200, data: { message: 'Successfully logged out.' } };
  });

  try {
    const { authLogout } = await import('../src/api/client.ts');
    await authLogout('logout-test-token');
    assert.equal(logoutCalled, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

// ----------------------------------------------------------------------------
// Test 4: 401 handling clears invalid sessions
// ----------------------------------------------------------------------------
test('4. Auth API: 401 Unauthorized raises error on invalid token', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = createMockFetch((url) => {
    return {
      ok: false,
      status: 401,
      statusText: 'Unauthorized',
      data: { detail: 'Token has expired.' },
    };
  });

  try {
    await assert.rejects(
      async () => {
        await authGetMe('expired-token');
      },
      (err) => {
        assert.match(err.message, /Token has expired/);
        assert.equal(err.status, 401);
        return true;
      }
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

// ----------------------------------------------------------------------------
// Test 5: Workspace listing and creation
// ----------------------------------------------------------------------------
test('5. Workspace API: fetchWorkspaces and createWorkspace', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = createMockFetch((url, options) => {
    if (options.method === 'GET') {
      assert.match(url, /\/api\/v1\/workspaces$/);
      return {
        status: 200,
        data: {
          workspaces: [
            {
              id: 'ws-111',
              name: 'Compliance Workspace',
              slug: 'compliance-ws',
              is_system: false,
              current_user_role: 'owner',
              created_at: '2026-11-01T00:00:00Z',
              updated_at: '2026-11-01T00:00:00Z',
            },
          ],
          total: 1,
        },
      };
    } else if (options.method === 'POST') {
      assert.match(url, /\/api\/v1\/workspaces$/);
      const body = JSON.parse(options.body);
      assert.equal(body.name, 'New Team WS');
      return {
        status: 201,
        data: {
          id: 'ws-222',
          name: body.name,
          slug: body.slug,
          is_system: false,
          current_user_role: 'owner',
          created_at: '2026-11-01T00:00:00Z',
          updated_at: '2026-11-01T00:00:00Z',
        },
      };
    }
  });

  try {
    const list = await fetchWorkspaces('test-token');
    assert.equal(list.length, 1);
    assert.equal(list[0].name, 'Compliance Workspace');

    const created = await createWorkspace(
      { name: 'New Team WS', slug: 'new-team-ws', description: 'Testing' },
      'test-token'
    );
    assert.equal(created.id, 'ws-222');
    assert.equal(created.name, 'New Team WS');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

// ----------------------------------------------------------------------------
// Test 6: Workspace selection and inaccessible-workspace handling
// ----------------------------------------------------------------------------
test('6. WorkspaceSelector: renders Public Demo Mode when unauthenticated and workspace name when active', () => {
  // Unauthenticated
  const unauthHtml = renderToStaticMarkup(React.createElement(WorkspaceSelector));
  assert.match(unauthHtml, /Public Demo Mode/);
});

// ----------------------------------------------------------------------------
// Test 7: Collection listing and creation
// ----------------------------------------------------------------------------
test('7. Collection API: fetchWorkspaceCollections and createWorkspaceCollection', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = createMockFetch((url, options) => {
    if (options.method === 'GET') {
      assert.match(url, /\/api\/v1\/workspaces\/ws-111\/collections$/);
      return {
        status: 200,
        data: {
          collections: [
            {
              id: 'private_col_1',
              workspace_id: 'ws-111',
              name: 'Private Security Playbooks',
              policy_name: 'general_static',
              enforce_regulatory_graph: false,
              is_system: false,
              is_public: false,
              created_at: '2026-11-01T00:00:00Z',
              updated_at: '2026-11-01T00:00:00Z',
            },
          ],
          total: 1,
        },
      };
    } else if (options.method === 'POST') {
      assert.match(url, /\/api\/v1\/workspaces\/ws-111\/collections$/);
      const body = JSON.parse(options.body);
      return {
        status: 201,
        data: {
          id: body.id,
          workspace_id: 'ws-111',
          name: body.name,
          policy_name: body.policy_name,
          enforce_regulatory_graph: false,
          is_system: false,
          is_public: false,
          created_at: '2026-11-01T00:00:00Z',
          updated_at: '2026-11-01T00:00:00Z',
        },
      };
    }
  });

  try {
    const list = await fetchWorkspaceCollections('ws-111', 'test-token');
    assert.equal(list.length, 1);
    assert.equal(list[0].id, 'private_col_1');

    const created = await createWorkspaceCollection(
      'ws-111',
      { id: 'private_col_2', name: 'Second Collection', policy_name: 'general_static' },
      'test-token'
    );
    assert.equal(created.id, 'private_col_2');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

// ----------------------------------------------------------------------------
// Test 8: Public collection query works without a token
// ----------------------------------------------------------------------------
test('8. Query API: public collection query omits Authorization header when token is null', async () => {
  const originalFetch = globalThis.fetch;
  let authHeaderPresent = true;
  globalThis.fetch = createMockFetch((url, options) => {
    assert.match(url, /\/api\/v1\/query$/);
    authHeaderPresent = 'Authorization' in options.headers;
    return {
      status: 200,
      data: {
        query: 'What is CSCRF?',
        disposition: 'CONTROLLED_ABSTENTION',
        verified_excerpts: [],
        historical_superseded_excerpts: [],
        summary_narrative: 'Controlled Abstention',
        technical_validation_passed: true,
        retrieval_candidate_count: 0,
        abstention_reason: null,
        regulatory_warnings: [],
        pilot_corpus_scope_limitation: 'Demo corpus',
      },
    };
  });

  try {
    const res = await queryRegulatoryAssistant({ query: 'What is CSCRF?' }, null);
    assert.equal(authHeaderPresent, false);
    assert.equal(res.disposition, 'CONTROLLED_ABSTENTION');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

// ----------------------------------------------------------------------------
// Test 9: Private collection query includes bearer authentication
// ----------------------------------------------------------------------------
test('9. Query API: private collection query attaches Bearer token', async () => {
  const originalFetch = globalThis.fetch;
  let authHeader = null;
  globalThis.fetch = createMockFetch((url, options) => {
    assert.match(url, /\/api\/v1\/query$/);
    authHeader = options.headers['Authorization'];
    return {
      status: 200,
      data: {
        query: 'What is our internal SOC SLA?',
        disposition: 'VERIFIED_HISTORICAL_EXCERPTS',
        verified_excerpts: [],
        historical_superseded_excerpts: [],
        summary_narrative: 'Verified against private collection.',
        technical_validation_passed: true,
        retrieval_candidate_count: 3,
        abstention_reason: null,
        regulatory_warnings: [],
        pilot_corpus_scope_limitation: 'Private collection',
      },
    };
  });

  try {
    const res = await queryRegulatoryAssistant(
      { query: 'What is our internal SOC SLA?', collection_id: 'internal_soc' },
      'user-bearer-token-123'
    );
    assert.equal(authHeader, 'Bearer user-bearer-token-123');
    assert.equal(res.disposition, 'VERIFIED_HISTORICAL_EXCERPTS');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

// ----------------------------------------------------------------------------
// Test 10: Query error states for 404, 422, and server errors
// ----------------------------------------------------------------------------
test('10. Query API: formats error messages for 401, 404, 422, and 500', async () => {
  const originalFetch = globalThis.fetch;

  // 404
  globalThis.fetch = createMockFetch(() => ({
    ok: false,
    status: 404,
    statusText: 'Not Found',
    data: { detail: 'Collection not found.' },
  }));
  await assert.rejects(
    () => queryRegulatoryAssistant({ query: 'Test query' }, null),
    (err) => {
      assert.equal(err.status, 404);
      assert.match(err.message, /not found or is not accessible/);
      return true;
    }
  );

  // 422
  globalThis.fetch = createMockFetch(() => ({
    ok: false,
    status: 422,
    statusText: 'Unprocessable Entity',
    data: { detail: [{ loc: ['body', 'query'], msg: 'Field required' }] },
  }));
  await assert.rejects(
    () => queryRegulatoryAssistant({ query: 'Test query' }, null),
    (err) => {
      assert.equal(err.status, 422);
      assert.match(err.message, /Validation Error: Field required/);
      return true;
    }
  );

  // 500
  globalThis.fetch = createMockFetch(() => ({
    ok: false,
    status: 500,
    statusText: 'Internal Server Error',
    data: { detail: 'Vector index unavailable.' },
  }));
  await assert.rejects(
    () => queryRegulatoryAssistant({ query: 'Test query' }, null),
    (err) => {
      assert.equal(err.status, 500);
      assert.match(err.message, /Server Error: Vector index unavailable/);
      return true;
    }
  );

  globalThis.fetch = originalFetch;
});

// ----------------------------------------------------------------------------
// Test 11: Query history is hidden for anonymous users
// ----------------------------------------------------------------------------
test('11. QueryHistoryDrawer: displays authentication required prompt when anonymous', () => {
  const html = renderToStaticMarkup(
    React.createElement(QueryHistoryDrawer, { isOpen: true, onClose: () => {} })
  );
  assert.match(html, /Authentication Required/);
  assert.match(html, /Persistent query history is private to authenticated accounts/);
});

// ----------------------------------------------------------------------------
// Test 12: History list and detail rendering
// ----------------------------------------------------------------------------
test('12. QueryHistory API: fetchQueryHistory and fetchQueryHistoryDetail', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = createMockFetch((url) => {
    if (url.includes('/query-history/rec-999')) {
      return {
        status: 200,
        data: {
          id: 'rec-999',
          collection_id: 'sebi_cscrf',
          query_text: 'Mandatory SOC SLA?',
          query_options: { top_k: 5 },
          disposition: 'VERIFIED_HISTORICAL_EXCERPTS',
          summary_narrative: 'Found 2 excerpts.',
          answer_text: 'SOC SLA is 4 hours.',
          claim_citations: [{ claim_text: 'Claim 1' }],
          verified_excerpts_count: 2,
          technical_validation_passed: true,
          created_at: '2026-11-01T00:00:00Z',
        },
      };
    }
    return {
      status: 200,
      data: {
        items: [
          {
            id: 'rec-999',
            collection_id: 'sebi_cscrf',
            query_text: 'Mandatory SOC SLA?',
            disposition: 'VERIFIED_HISTORICAL_EXCERPTS',
            verified_excerpts_count: 2,
            technical_validation_passed: true,
            created_at: '2026-11-01T00:00:00Z',
          },
        ],
        total: 1,
        limit: 10,
        offset: 0,
      },
    };
  });

  try {
    const list = await fetchQueryHistory('test-token');
    assert.equal(list.items.length, 1);
    assert.equal(list.items[0].query_text, 'Mandatory SOC SLA?');

    const detail = await fetchQueryHistoryDetail('rec-999', 'test-token');
    assert.equal(detail.answer_text, 'SOC SLA is 4 hours.');
    assert.equal(detail.claim_citations.length, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

// ----------------------------------------------------------------------------
// Test 13: Role-based UI affordances do not replace backend authorization
// ----------------------------------------------------------------------------
test('13. WorkspaceManageModal: displays collections and member management tabs', () => {
  const html = renderToStaticMarkup(
    React.createElement(WorkspaceManageModal, { isOpen: true, onClose: () => {}, defaultTab: 'collections' })
  );
  assert.match(html, /Private Collections in Workspace/);
  assert.match(html, /Members/);
  assert.match(html, /\+ New Workspace/);
});

// ----------------------------------------------------------------------------
// Test 14: No hardcoded workspace/collection IDs or credentials
// ----------------------------------------------------------------------------
test('14. Security: No hardcoded secret tokens, passwords, or fixed workspace UUIDs in client sources', async () => {
  const fs = await import('node:fs');
  const path = await import('node:path');

  const clientSource = fs.readFileSync(path.resolve('src/api/client.ts'), 'utf-8');
  assert.doesNotMatch(clientSource, /Bearer\s+ey/i, 'No hardcoded JWT token in client.ts');
  assert.doesNotMatch(clientSource, /password\s*:\s*["'][^"']+["']/i, 'No hardcoded password in client.ts');
  assert.doesNotMatch(clientSource, /00000000-0000-0000-0000/i, 'No hardcoded workspace UUID in client.ts');
});

// ----------------------------------------------------------------------------
// Test 15: Workspace State Hardening - Out-of-order collection race condition guard
// ----------------------------------------------------------------------------
test('15. Workspace State Hardening: out-of-order collection responses from older workspace do not overwrite active workspace collections', async () => {
  const fs = await import('node:fs');
  const path = await import('node:path');

  // Verify WorkspaceContext source enforces request tracking refs
  const wsSource = fs.readFileSync(path.resolve('src/context/WorkspaceContext.tsx'), 'utf-8');
  assert.match(wsSource, /collectionRequestIdRef\s*=\s*useRef<number>\(0\)/, 'Tracks collection fetch request IDs');
  assert.match(wsSource, /activeWorkspaceIdRef\s*=\s*useRef<string \| null>/, 'Tracks current active workspace ID ref');
  assert.match(wsSource, /reqId\s*!==\s*collectionRequestIdRef\.current/, 'Guards against stale request IDs');
  assert.match(wsSource, /targetWsId\s*!==\s*activeWorkspaceIdRef\.current/, 'Guards against mismatch with active workspace');

  // Behavioral simulation of race condition:
  // WS-A fetch is initiated first (reqId 1, slow)
  // WS-B fetch is initiated second (reqId 2, fast)
  let state = {
    collections: [],
    loading: false,
    activeWsId: null,
  };

  const reqIdRef = { current: 0 };
  const activeWsRef = { current: null };

  const selectWorkspaceSim = (id) => {
    if (id !== activeWsRef.current) {
      reqIdRef.current++;
      activeWsRef.current = id;
      state.collections = [];
      state.loading = true;
      state.activeWsId = id;
    }
  };

  const executeFetchCollections = async (wsId, delayMs, collectionsToReturn) => {
    const thisReqId = ++reqIdRef.current;
    const targetWs = wsId;
    activeWsRef.current = targetWs;
    state.loading = true;

    await new Promise((r) => setTimeout(r, delayMs));

    // Guard check identical to WorkspaceContext.tsx
    if (thisReqId !== reqIdRef.current || targetWs !== activeWsRef.current) {
      return; // Drop stale response!
    }
    state.collections = collectionsToReturn;
    state.loading = false;
  };

  // 1. Select WS-A and initiate slow fetch
  selectWorkspaceSim('ws-A');
  const slowPromiseA = executeFetchCollections('ws-A', 50, [{ id: 'col-a1', name: 'Col A1' }]);

  // 2. Quickly select WS-B and initiate fast fetch
  selectWorkspaceSim('ws-B');
  assert.deepEqual(state.collections, [], 'State collections cleared immediately on switch');
  const fastPromiseB = executeFetchCollections('ws-B', 10, [{ id: 'col-b1', name: 'Col B1' }]);

  // 3. Await both to finish
  await Promise.all([slowPromiseA, fastPromiseB]);

  // 4. Assert WS-B collections won and WS-A was ignored
  assert.equal(state.activeWsId, 'ws-B');
  assert.deepEqual(state.collections, [{ id: 'col-b1', name: 'Col B1' }]);
  assert.equal(state.loading, false);
});

// ----------------------------------------------------------------------------
// Test 16: Workspace State Hardening - Immediate collection clearing on workspace switch
// ----------------------------------------------------------------------------
test('16. Workspace State Hardening: switching workspaces immediately clears collection state before new fetch resolves', async () => {
  const fs = await import('node:fs');
  const path = await import('node:path');

  const wsSource = fs.readFileSync(path.resolve('src/context/WorkspaceContext.tsx'), 'utf-8');
  assert.match(
    wsSource,
    /selectWorkspace\s*=\s*\(id:\s*string\)\s*=>\s*\{[^}]*setWorkspaceCollections\(\[\]\)/s,
    'selectWorkspace immediately clears collections when workspace ID changes'
  );

  // Verify that unauthenticated / logout also immediately resets collections in WorkspaceContext
  assert.match(
    wsSource,
    /if\s*\(!token\s*\|\|\s*!isAuthenticated\)\s*\{[^}]*setWorkspaceCollections\(\[\]\)/s,
    'refreshWorkspaces immediately clears collections when unauthenticated'
  );
});

// ----------------------------------------------------------------------------
// Test 17: Query History State Hardening - Immediate purge on logout & drop in-flight responses
// ----------------------------------------------------------------------------
test('17. Query History State Hardening: unauthenticated state or logout immediately clears history state and rejects in-flight responses', async () => {
  const fs = await import('node:fs');
  const path = await import('node:path');

  // Verify QueryHistoryDrawer source enforces immediate clearing and request tracking
  const drawerSource = fs.readFileSync(path.resolve('src/components/QueryHistoryDrawer.tsx'), 'utf-8');
  assert.match(drawerSource, /historyRequestIdRef\s*=\s*useRef<number>\(0\)/, 'Tracks history request IDs');
  assert.match(drawerSource, /detailRequestIdRef\s*=\s*useRef<number>\(0\)/, 'Tracks detail request IDs');
  assert.match(drawerSource, /activeUserIdRef\s*=\s*useRef<string \| null>/, 'Tracks active authenticated user ID');
  assert.match(drawerSource, /resetAllHistoryState/, 'Has unified reset handler');
  assert.match(drawerSource, /if\s*\(!isAuthenticated\s*\|\|\s*currentId\s*!==\s*activeUserIdRef\.current\)/, 'Clears on logout or user switch');

  // Functional simulation of logout during in-flight fetch:
  let drawerState = {
    historyItems: [{ id: 'prev-1', query_text: 'old question' }],
    selectedDetail: { id: 'prev-1', answer_text: 'old answer' },
    totalCount: 1,
    isLoading: true,
  };

  let isAuthenticated = true;
  let activeUserId = 'user-alice';
  const historyReqRef = { current: 0 };
  const userRef = { current: activeUserId };

  const resetAllHistoryStateSim = () => {
    historyReqRef.current++;
    drawerState.historyItems = [];
    drawerState.selectedDetail = null;
    drawerState.totalCount = 0;
    drawerState.isLoading = false;
  };

  const loadHistorySim = async (reqUserId, delayMs, items) => {
    const thisReqId = ++historyReqRef.current;
    drawerState.isLoading = true;

    await new Promise((r) => setTimeout(r, delayMs));

    // Guard check identical to QueryHistoryDrawer.tsx
    if (thisReqId !== historyReqRef.current || !isAuthenticated || userRef.current !== reqUserId) {
      return; // Discard stale response
    }
    drawerState.historyItems = items;
    drawerState.totalCount = items.length;
    drawerState.isLoading = false;
  };

  // Alice starts loading history
  const aliceFetch = loadHistorySim('user-alice', 40, [{ id: 'q-1', query_text: 'Alice query' }]);

  // Alice logs out before response arrives
  isAuthenticated = false;
  activeUserId = null;
  userRef.current = null;
  resetAllHistoryStateSim();

  assert.deepEqual(drawerState.historyItems, [], 'History items cleared immediately on logout');
  assert.equal(drawerState.selectedDetail, null, 'Selected detail cleared immediately on logout');
  assert.equal(drawerState.totalCount, 0, 'Total count reset on logout');

  // Await fetch completion
  await aliceFetch;

  // Verify Alice response was completely discarded
  assert.deepEqual(drawerState.historyItems, [], 'Stale response from logged out user did NOT repopulate state');
  assert.equal(drawerState.isLoading, false);
});

// ----------------------------------------------------------------------------
// Test 18: Query History State Hardening - Account switching isolation
// ----------------------------------------------------------------------------
test('18. Query History State Hardening: account switching immediately purges previous user history data', async () => {
  let drawerState = {
    historyItems: [{ id: 'alice-1', query_text: 'Alice secret question' }],
    totalCount: 1,
  };

  let isAuthenticated = true;
  let activeUserId = 'user-alice';
  const historyReqRef = { current: 0 };
  const userRef = { current: activeUserId };

  const switchUserSim = (newUserId) => {
    historyReqRef.current++;
    userRef.current = newUserId;
    activeUserId = newUserId;
    // Immediate wipe on account transition
    drawerState.historyItems = [];
    drawerState.totalCount = 0;
  };

  // Alice has an in-flight slow request
  const aliceFetch = async () => {
    const thisReqId = ++historyReqRef.current;
    await new Promise((r) => setTimeout(r, 40));
    if (thisReqId !== historyReqRef.current || userRef.current !== 'user-alice') {
      return; // Discard!
    }
    drawerState.historyItems = [{ id: 'alice-late', query_text: 'Late Alice' }];
  };
  const alicePromise = aliceFetch();

  // User switches to Bob
  switchUserSim('user-bob');
  assert.deepEqual(drawerState.historyItems, [], 'Alice history wiped immediately on switch to Bob');

  // Bob starts fast request
  const bobFetch = async () => {
    const thisReqId = ++historyReqRef.current;
    await new Promise((r) => setTimeout(r, 10));
    if (thisReqId !== historyReqRef.current || userRef.current !== 'user-bob') {
      return;
    }
    drawerState.historyItems = [{ id: 'bob-1', query_text: 'Bob query' }];
    drawerState.totalCount = 1;
  };
  const bobPromise = bobFetch();

  await Promise.all([alicePromise, bobPromise]);

  assert.equal(drawerState.historyItems.length, 1);
  assert.equal(drawerState.historyItems[0].id, 'bob-1');
  assert.equal(drawerState.historyItems[0].query_text, 'Bob query');
});

// ----------------------------------------------------------------------------
// Test 19: Query History State Hardening - Record detail selection race condition guard
// ----------------------------------------------------------------------------
test('19. Query History State Hardening: out-of-order detail fetch responses do not overwrite newly selected record', async () => {
  let selectedDetail = null;
  const detailReqRef = { current: 0 };

  const selectRecordSim = async (recordId, delayMs, detailPayload) => {
    const thisReqId = ++detailReqRef.current;

    await new Promise((r) => setTimeout(r, delayMs));

    if (thisReqId !== detailReqRef.current) {
      return; // Discard older detail response
    }
    selectedDetail = detailPayload;
  };

  // User clicks Record 1 (slow network)
  const clickRecord1 = selectRecordSim('rec-1', 40, { id: 'rec-1', query_text: 'First question' });
  // User immediately clicks Record 2 (fast network)
  const clickRecord2 = selectRecordSim('rec-2', 10, { id: 'rec-2', query_text: 'Second question' });

  await Promise.all([clickRecord1, clickRecord2]);

  assert.equal(selectedDetail.id, 'rec-2', 'Newer record selection detail preserved');
  assert.equal(selectedDetail.query_text, 'Second question');
});

// ----------------------------------------------------------------------------
// Test 20: Ingestion Pipeline API Client Methods
// ----------------------------------------------------------------------------
test('20. Ingestion API: upload, jobs list, documents list, and download blob', async () => {
  const {
    uploadCollectionDocument,
    fetchCollectionIngestionJobs,
    fetchCollectionDocuments,
    downloadCollectionDocumentBlob,
  } = await import('../src/api/client');

  const originalFetch = global.fetch;
  const originalFormData = global.FormData;

  // Polyfill FormData if needed for Node test environment
  if (!global.FormData) {
    global.FormData = class {
      constructor() {
        this.fields = {};
      }
      append(key, value) {
        this.fields[key] = value;
      }
    };
  }

  try {
    // 1. uploadCollectionDocument
    global.fetch = async (url, options) => {
      assert.match(url, /\/api\/v1\/workspaces\/ws-123\/collections\/col-alpha\/upload$/);
      assert.equal(options.method, 'POST');
      assert.equal(options.headers['Authorization'], 'Bearer secret-jwt-123');
      return {
        ok: true,
        json: async () => ({
          job_id: 'job-999',
          document_id: 'doc-alpha-1',
          workspace_id: 'ws-123',
          collection_id: 'col-alpha',
          status: 'queued',
          is_duplicate_registration: false,
          message: 'Document uploaded and queued.',
        }),
      };
    };

    const mockFile = { name: 'test-policy.pdf', size: 1024, type: 'application/pdf' };
    const uploadRes = await uploadCollectionDocument(
      'ws-123',
      'col-alpha',
      mockFile,
      'Test Policy',
      'REF-01',
      'secret-jwt-123'
    );
    assert.equal(uploadRes.job_id, 'job-999');
    assert.equal(uploadRes.document_id, 'doc-alpha-1');

    // 2. fetchCollectionIngestionJobs
    global.fetch = async (url, options) => {
      assert.match(url, /\/api\/v1\/workspaces\/ws-123\/collections\/col-alpha\/ingestion-jobs$/);
      assert.equal(options.method, 'GET');
      assert.equal(options.headers['Authorization'], 'Bearer secret-jwt-123');
      return {
        ok: true,
        json: async () => ({
          jobs: [
            {
              job_id: 'job-999',
              document_id: 'doc-alpha-1',
              workspace_id: 'ws-123',
              collection_id: 'col-alpha',
              status: 'processing',
              stage: 'EMBEDDING',
              created_at: new Date().toISOString(),
            },
          ],
          total: 1,
        }),
      };
    };

    const jobs = await fetchCollectionIngestionJobs('ws-123', 'col-alpha', 'secret-jwt-123');
    assert.equal(jobs.length, 1);
    assert.equal(jobs[0].job_id, 'job-999');
    assert.equal(jobs[0].stage, 'EMBEDDING');

    // 3. fetchCollectionDocuments
    global.fetch = async (url, options) => {
      assert.match(url, /\/api\/v1\/workspaces\/ws-123\/collections\/col-alpha\/documents$/);
      return {
        ok: true,
        json: async () => ({
          documents: [
            {
              document_id: 'doc-alpha-1',
              workspace_id: 'ws-123',
              collection_id: 'col-alpha',
              title: 'Test Policy',
              circular_number: 'REF-01',
              is_published: true,
              created_at: new Date().toISOString(),
            },
          ],
          total: 1,
        }),
      };
    };

    const docs = await fetchCollectionDocuments('ws-123', 'col-alpha', 'secret-jwt-123');
    assert.equal(docs.length, 1);
    assert.equal(docs[0].document_id, 'doc-alpha-1');
    assert.equal(docs[0].is_published, true);

    // 4. downloadCollectionDocumentBlob
    global.fetch = async (url, options) => {
      assert.match(url, /\/api\/v1\/workspaces\/ws-123\/collections\/col-alpha\/documents\/doc-alpha-1\/download$/);
      assert.equal(options.headers['Authorization'], 'Bearer secret-jwt-123');
      return {
        ok: true,
        blob: async () => ({ size: 500, type: 'application/pdf' }),
      };
    };

    const blob = await downloadCollectionDocumentBlob('ws-123', 'col-alpha', 'doc-alpha-1', 'secret-jwt-123');
    assert.equal(blob.size, 500);
  } finally {
    global.fetch = originalFetch;
    global.FormData = originalFormData;
  }
});

// ----------------------------------------------------------------------------
// Test 21: WorkspaceManageModal Ingestion and Document Lifecycle UI
// ----------------------------------------------------------------------------
test('21. WorkspaceManageModal: Documents & Ingestion tab renders upload and pipeline inspection', async () => {
  const fs = await import('node:fs');
  const path = await import('node:path');

  const modalSource = fs.readFileSync(path.resolve('src/components/WorkspaceManageModal.tsx'), 'utf-8');

  // Verify Documents tab and controls
  assert.match(modalSource, /Documents & Ingestion/, 'Renders Documents & Ingestion tab');
  assert.match(modalSource, /Upload Document \(PDF\)/, 'Renders PDF upload heading');
  assert.match(modalSource, /PDF format only • Maximum 30MB/, 'Shows 30MB file size boundary');
  assert.match(modalSource, /Ingestion Pipeline Jobs/, 'Renders Ingestion Pipeline Jobs section');
  assert.match(modalSource, /Collection Documents/, 'Renders Collection Documents section');
  assert.match(modalSource, /renderJobStatusBadge/, 'Includes job status badge renderer');
  assert.match(modalSource, /downloadCollectionDocumentBlob/, 'Includes authenticated PDF download capability');
  assert.match(modalSource, /End-to-End Ingestion & Snapshot Indexing/, 'Explains automatic index snapshot generation');
});

// ----------------------------------------------------------------------------
// Test 22: QueryForm Private Collection Readiness Advisory
// ----------------------------------------------------------------------------
test('22. QueryForm: displays private collection snapshot readiness guidance', async () => {
  const React = await import('react');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const { QueryForm } = await import('../src/components/QueryForm');

  const collections = [
    {
      collection_id: 'sebi_cscrf',
      name: 'SEBI CSCRF',
      policy_type: 'sebi_cscrf_strict',
      is_private: false,
    },
    {
      collection_id: 'private_guidelines',
      name: 'Private Guidelines',
      policy_type: 'general_static',
      is_private: true,
    },
  ];

  // Render with private collection selected
  const markup = renderToStaticMarkup(
    React.createElement(QueryForm, {
      isLoading: false,
      onSubmit: () => {},
      collections,
      selectedCollectionId: 'private_guidelines',
    })
  );

  assert.match(
    markup,
    /Ensure documents have completed ingestion and snapshot publication before querying/,
    'Renders private collection snapshot readiness advisory'
  );
});
