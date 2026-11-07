import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

import { Header } from '../src/components/Header.tsx';
import { DashboardView } from '../src/components/DashboardView.tsx';
import { CollectionsView } from '../src/components/CollectionsView.tsx';
import { QueryHistoryView } from '../src/components/QueryHistoryView.tsx';
import { WorkspaceSettingsView } from '../src/components/WorkspaceSettingsView.tsx';
import { AuthProvider } from '../src/context/AuthContext.tsx';
import { WorkspaceProvider } from '../src/context/WorkspaceContext.tsx';

const sampleCollections = [
  {
    collection_id: 'sebi_cscrf',
    name: 'SEBI CSCRF Regulatory Intelligence',
    description: 'Master circulars and cybersecurity framework',
    domain: 'CSCRF',
    regulator: 'SEBI',
    policy_type: 'sebi_cscrf_strict',
    status: 'active',
    is_private: false,
  },
  {
    collection_id: 'research_papers',
    name: 'AI & Systems Research Papers',
    description: 'Scientific preprints and foundational systems research',
    domain: 'RESEARCH',
    regulator: null,
    policy_type: 'general_static',
    status: 'active',
    is_private: true,
  },
];

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
// Test 1: Header renders navigation tabs
// ----------------------------------------------------------------------------
test('1. Header: renders navigation tabs for Dashboard, AI Research, Collections, Query History, and Workspace', () => {
  const html = renderToStaticMarkup(
    React.createElement(Header, {
      apiStatus: 'connected',
      selectedCollection: sampleCollections[1],
      activeTab: 'dashboard',
      onSelectTab: () => {},
    })
  );

  assert.match(html, /Dashboard/);
  assert.match(html, /AI Research/);
  assert.match(html, /Collections &amp; Ingestion/);
  assert.match(html, /Query History/);
  assert.match(html, /Workspace Settings/);
  assert.match(html, /aria-current="page"/);
});

// ----------------------------------------------------------------------------
// Test 2: DashboardView renders platform overview, quick actions, and collections
// ----------------------------------------------------------------------------
test('2. DashboardView: renders platform banner, quick actions, and collection showcase', () => {
  const html = renderWithProviders(
    React.createElement(DashboardView, {
      collections: sampleCollections,
      onNavigate: () => {},
      onSelectCollectionAndResearch: () => {},
    })
  );

  assert.match(html, /Document Intelligence &amp; Research Platform/);
  assert.match(html, /AI-Powered Evidence Synthesis &amp; Multi-Tenant Research/);
  assert.match(html, /Quick Actions/);
  assert.match(html, /Ask Questions/);
  assert.match(html, /Create Collection/);
  assert.match(html, /Upload PDF Documents/);
  assert.match(html, /Workspace &amp; Team/);
  assert.match(html, /SEBI CSCRF Regulatory Intelligence/);
  assert.match(html, /AI &amp; Systems Research Papers/);
});

// ----------------------------------------------------------------------------
// Test 3: CollectionsView renders domain templates and asynchronous ingestion notices
// ----------------------------------------------------------------------------
test('3. CollectionsView: renders domain templates, upload notices, and asynchronous ingestion architecture', () => {
  const html = renderWithProviders(
    React.createElement(CollectionsView, {
      onNavigateToResearch: () => {},
    })
  );

  assert.match(html, /Document Collections &amp; Ingestion Pipeline/);
  // Domain templates
  assert.match(html, /Research/);
  assert.match(html, /Financial/);
  assert.match(html, /Legal/);
  assert.match(html, /Tech Docs/);
  // Ingestion guidance
  assert.match(html, /Asynchronous Ingestion Pipeline Notice/);
  assert.match(html, /Snapshot Readiness Rule/);
  assert.match(html, /Backblaze B2 \/ S3/);
});

// ----------------------------------------------------------------------------
// Test 4: QueryHistoryView renders ledger header and unauthenticated/empty states
// ----------------------------------------------------------------------------
test('4. QueryHistoryView: renders ledger header and persistent research notice', () => {
  const html = renderWithProviders(
    React.createElement(QueryHistoryView, {
      onReplayQuery: () => {},
    })
  );

  assert.match(html, /Persistent Query History &amp; Citations/);
  assert.match(html, /Audit &amp; Research History Ledger/);
  assert.match(html, /Sign In Required for Persistent Query History/);
});

// ----------------------------------------------------------------------------
// Test 5: WorkspaceSettingsView renders workspace administration and RBAC role guide
// ----------------------------------------------------------------------------
test('5. WorkspaceSettingsView: renders RBAC guide and multi-tenant admin structure', () => {
  const html = renderWithProviders(
    React.createElement(WorkspaceSettingsView, {})
  );

  assert.match(html, /Workspace Settings &amp; RBAC Team Management/);
  assert.match(html, /Role-Based Access Control \(RBAC\) Reference/);
  assert.match(html, /Owner/);
  assert.match(html, /Admin/);
  assert.match(html, /Member/);
  assert.match(html, /Viewer/);
});

