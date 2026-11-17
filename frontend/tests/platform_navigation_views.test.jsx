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
    collection_id: 'general_synthetic',
    name: 'General Technical Knowledge',
    description: 'Acme Infrastructure and Technical Documentation',
    domain: 'TECHNICAL',
    regulator: null,
    policy_type: 'general_static',
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

  assert.match(html, /Citenza Platform/);
  assert.match(html, /Turn your documents into reliable answers\./);
  assert.match(html, /Your documents\. Verified answers\./);
  assert.match(html, /Quick Actions/);
  assert.match(html, /Ask Questions/);
  assert.match(html, /Create Collection/);
  assert.match(html, /Upload PDF Documents/);
  assert.match(html, /Workspace &amp; Team/);
  assert.match(html, /General Technical Knowledge/);
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

// ----------------------------------------------------------------------------
// Test 6: DashboardView authentic workspace state display
// ----------------------------------------------------------------------------
test('6. DashboardView: authentic workspace state and no fictional default workspace', () => {
  const html = renderWithProviders(
    React.createElement(DashboardView, {
      collections: sampleCollections,
      onNavigate: () => {},
      onSelectCollectionAndResearch: () => {},
    })
  );

  // In unauthenticated context, must display Demo Mode and Public Access, NOT fictional "Default Workspace"
  assert.match(html, /Demo Mode/, 'Displays Demo Mode when unauthenticated');
  assert.match(html, /Public Access/, 'Displays Public Access');
  assert.doesNotMatch(html, /Default Workspace/, 'Never displays fake Default Workspace placeholder');
});

// ----------------------------------------------------------------------------
// Test 7: CollectionsView authentic collection creation guard
// ----------------------------------------------------------------------------
test('7. CollectionsView: shows unauthenticated notice and guards collection creation', () => {
  const html = renderWithProviders(
    React.createElement(CollectionsView, {
      onNavigateToResearch: () => {},
    })
  );

  // When unauthenticated, shows Sign In or Continue as Guest banner
  assert.match(html, /Sign In or Continue as Guest/, 'Renders authentication required advisory');
  assert.match(html, /Sign In to Create Collection/, 'Creation button prompts sign in when unauthenticated');
});

