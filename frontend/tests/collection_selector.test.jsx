import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { QueryForm } from '../src/components/QueryForm.tsx';
import { DisclosureBanner } from '../src/components/DisclosureBanner.tsx';
import { ResultsPanel } from '../src/components/ResultsPanel.tsx';
import { Header } from '../src/components/Header.tsx';
import { TechnicalInfoDrawer } from '../src/components/TechnicalInfoDrawer.tsx';

const testCollections = [
  {
    collection_id: 'sebi_cscrf',
    name: 'SEBI CSCRF Regulatory Intelligence',
    description: 'Master circulars and guidelines',
    domain: 'CSCRF',
    regulator: 'SEBI',
    policy_type: 'sebi_cscrf_strict',
    status: 'active',
  },
  {
    collection_id: 'general_synthetic',
    name: 'Acme Technical Handbook',
    description: 'Synthetic systems documentation',
    domain: 'TECH',
    regulator: null,
    policy_type: 'general_static',
    status: 'active',
  },
];

test('1. QueryForm renders collection dropdown with populated collections', () => {
  const html = renderToStaticMarkup(
    React.createElement(QueryForm, {
      isLoading: false,
      onSubmit: () => {},
      collections: testCollections,
      selectedCollectionId: 'sebi_cscrf',
    })
  );

  assert.match(html, /Target Collection:/);
  assert.match(html, /SEBI CSCRF Regulatory Intelligence \(sebi_cscrf\)/);
  assert.match(html, /Acme Technical Handbook \(general_synthetic\)/);
});

test('2. QueryForm adapts placeholders and labels based on general_static collection selection', () => {
  const html = renderToStaticMarkup(
    React.createElement(QueryForm, {
      isLoading: false,
      onSubmit: () => {},
      collections: testCollections,
      selectedCollectionId: 'general_synthetic',
    })
  );

  assert.match(html, /Documentation Question \/ Search Topic/);
  assert.match(html, /Include Archived Versions:/);
  assert.match(html, /Sample Technical Documentation Queries/);
});

test('3. DisclosureBanner renders general_static disclosure without regulatory legal overclaims', () => {
  const html = renderToStaticMarkup(
    React.createElement(DisclosureBanner, {
      selectedCollection: testCollections[1],
    })
  );

  assert.match(html, /Static Collection Disclosure — Acme Technical Handbook/);
  assert.match(html, /general_synthetic/);
  assert.match(html, /General technical documentation does not represent regulatory circulars or official legal advice/);
  assert.doesNotMatch(html, /CIR\/2024\/113/);
});

test('4. DisclosureBanner preserves SEBI CSCRF disclosure when sebi_cscrf is selected', () => {
  const html = renderToStaticMarkup(
    React.createElement(DisclosureBanner, {
      selectedCollection: testCollections[0],
    })
  );

  assert.match(html, /Authoritative Pilot Scope &amp; Non-Legal-Advice Disclosure/);
  assert.match(html, /CIR\/2024\/113/);
  assert.match(html, /CIR\/2025\/119/);
});

test('5. ResultsPanel: GENERAL_STATIC supported answer has no SEBI-only corpus wording and displays collection boundary', () => {
  const resp = {
    query_text: 'What are the FinOps retention rules?',
    retrieval_candidate_count: 3,
    verified_excerpts: [
      {
        excerpt_id: 'finops_01',
        document_id: 'finops_guide',
        circular_number: 'FINOPS-2025',
        issue_date: '2025-01-15',
        title: 'Acme Cloud FinOps Handbook',
        page_start: 4,
        page_end: 4,
        source_chunk_id: 'finops_chunk_01',
        match_confidence: 1.0,
        mechanical_match_verified: true,
        regulatory_status: 'ACTIVE',
        status_reason: 'Verified verbatim against registered document snapshot.',
        acting_document_ids: ['finops_guide'],
        applicable_edge_ids: [],
        excerpt_text: 'Cloud FinOps retention policy requires daily snapshot lifecycle management.',
      },
    ],
    historical_superseded_excerpts: [],
    disposition: 'VERIFIED_HISTORICAL_EXCERPTS',
    summary_narrative: 'Found 1 verified excerpt in Acme Cloud FinOps.',
    regulatory_warnings: [],
    pilot_corpus_scope_limitation: 'Educational & Pilot Assistant: Operating strictly over the 5-document SEBI CSCRF circular corpus (CIR/2024/113, CIR/2025/045).',
    abstention_reason: null,
    answer_text: 'Daily snapshots must be managed via automated lifecycle policies.',
    synthesis_status: 'FULLY_ANSWERED',
  };

  const finopsCollection = {
    collection_id: 'demo_finops',
    name: 'Acme Cloud FinOps',
    description: 'Cloud financial operations guidelines',
    domain: 'FINOPS',
    regulator: null,
    policy_type: 'general_static',
    status: 'active',
  };

  const html = renderToStaticMarkup(
    React.createElement(ResultsPanel, {
      isLoading: false,
      error: null,
      response: resp,
      selectedCollection: finopsCollection,
    })
  );

  // Checks for generic and collection-aware descriptions
  assert.match(html, /Verified Document Excerpts/);
  assert.match(html, /Corpus Boundary:/);
  assert.match(html, /Static document collection: excerpts verified verbatim from Acme Cloud FinOps \(demo_finops\)\./);

  // Ensures SEBI-only wording is absent
  assert.doesNotMatch(html, /5-document SEBI CSCRF/);
  assert.doesNotMatch(html, /CIR\/2024\/113/);
  assert.doesNotMatch(html, /official SEBI CSCRF corpus/);
});

test('6. ResultsPanel: GENERAL_STATIC controlled abstention replaces SEBI-specific abstention explanation', () => {
  const resp = {
    query_text: 'What are the international tax rules?',
    retrieval_candidate_count: 0,
    verified_excerpts: [],
    historical_superseded_excerpts: [],
    disposition: 'CONTROLLED_ABSTENTION',
    summary_narrative: 'Controlled Abstention: Zero candidate passages found.',
    regulatory_warnings: [],
    pilot_corpus_scope_limitation: 'Educational & Pilot Assistant: Operating strictly over the 5-document SEBI CSCRF circular corpus.',
    abstention_reason: 'No relevant passages identified in Acme Cloud FinOps.',
    answer_text: 'Controlled Abstention: Zero candidate passages found.',
    synthesis_status: 'ABSTAINED',
  };

  const finopsCollection = {
    collection_id: 'demo_finops',
    name: 'Acme Cloud FinOps',
    description: 'Cloud financial operations guidelines',
    domain: 'FINOPS',
    regulator: null,
    policy_type: 'general_static',
    status: 'active',
  };

  const html = renderToStaticMarkup(
    React.createElement(ResultsPanel, {
      isLoading: false,
      error: null,
      response: resp,
      selectedCollection: finopsCollection,
    })
  );

  assert.match(html, /Abstention Reason:/);
  assert.match(html, /Note: Abstention indicates no sufficiently relevant passages with verified textual provenance exist in Acme Cloud FinOps\./);
  assert.match(html, /Static document collection: excerpts verified verbatim from Acme Cloud FinOps \(demo_finops\)\./);

  // Must not claim SEBI circular or 5-document indexed pilot corpus
  assert.doesNotMatch(html, /no relevant SEBI circular exists globally/);
  assert.doesNotMatch(html, /within the 5-document indexed pilot corpus/);
  assert.doesNotMatch(html, /5-document SEBI CSCRF circular corpus/);
});

test('7. ResultsPanel: SEBI_CSCRF_STRICT preserves existing SEBI corpus boundary and disclaimers', () => {
  const resp = {
    query_text: 'What is the mandatory reporting timeline under CSCRF?',
    retrieval_candidate_count: 5,
    verified_excerpts: [
      {
        excerpt_id: 'cscrf_01',
        document_id: 'cscrf_2024',
        circular_number: 'CIR/2024/113',
        issue_date: '2024-08-20',
        title: 'CSCRF Master Circular',
        page_start: 13,
        page_end: 13,
        source_chunk_id: 'cscrf_chunk_01',
        match_confidence: 1.0,
        mechanical_match_verified: true,
        regulatory_status: 'ACTIVE',
        status_reason: 'Active baseline',
        acting_document_ids: ['cscrf_2024'],
        applicable_edge_ids: [],
        excerpt_text: 'Incident reporting within 6 hours of detection.',
      },
    ],
    historical_superseded_excerpts: [],
    disposition: 'VERIFIED_HISTORICAL_EXCERPTS',
    summary_narrative: 'Found 1 verified excerpt in active SEBI CSCRF corpus.',
    regulatory_warnings: ['Pilot Scope: Verification limited to 5 indexed circulars.'],
    pilot_corpus_scope_limitation: 'Operating strictly over the 5-document SEBI CSCRF circular corpus (CIR/2024/113).',
    abstention_reason: null,
    answer_text: 'Incident reporting within 6 hours.',
    synthesis_status: 'FULLY_ANSWERED',
  };

  const html = renderToStaticMarkup(
    React.createElement(ResultsPanel, {
      isLoading: false,
      error: null,
      response: resp,
      selectedCollection: testCollections[0],
    })
  );

  assert.match(html, /Verified Historical Excerpts/);
  assert.match(html, /All returned excerpts mechanically verified against official SEBI CSCRF corpus with resolved status\./);
  assert.match(html, /Operating strictly over the 5-document SEBI CSCRF circular corpus/);
});

test('8. ResultsPanel: Missing collection context does not accidentally display SEBI-specific text', () => {
  const resp = {
    query_text: 'General question with unprovided collection context',
    retrieval_candidate_count: 0,
    verified_excerpts: [],
    historical_superseded_excerpts: [],
    disposition: 'CONTROLLED_ABSTENTION',
    summary_narrative: 'Controlled Abstention: Zero candidate passages found.',
    regulatory_warnings: [],
    pilot_corpus_scope_limitation: 'Educational & Pilot Assistant: Operating strictly over the 5-document SEBI CSCRF circular corpus.',
    abstention_reason: 'No candidate passages retrieved.',
    answer_text: null,
    synthesis_status: 'ABSTAINED',
  };

  const html = renderToStaticMarkup(
    React.createElement(ResultsPanel, {
      isLoading: false,
      error: null,
      response: resp,
      selectedCollection: null,
    })
  );

  // Should use neutral fallback, NOT SEBI circular claims
  assert.match(html, /Document collection: verified verbatim against indexed technical document snapshots\./);
  assert.match(html, /Note: Abstention indicates no sufficiently relevant passages with verified textual provenance exist in the indexed collection\./);
  assert.doesNotMatch(html, /no relevant SEBI circular exists globally/);
  assert.doesNotMatch(html, /5-document SEBI CSCRF circular corpus/);
});

// =========================================================================
// Phase 5.3B Focused Tests: Header and TechnicalInfoDrawer Shell Generalization
// =========================================================================

test('9. Header: GENERAL_STATIC displays DOC, Document Intelligence Platform, and Static Collection badge', () => {
  const html = renderToStaticMarkup(
    React.createElement(Header, {
      apiStatus: 'connected',
      selectedCollection: testCollections[1], // general_synthetic
    })
  );

  assert.match(html, />DOC<\/div>/);
  assert.match(html, /Document Intelligence Platform/);
  assert.match(html, /Static Collection/);
  assert.doesNotMatch(html, />SEBI<\/div>/);
  assert.doesNotMatch(html, /SEBI CSCRF Regulatory Intelligence Assistant/);
  assert.doesNotMatch(html, /5-Doc Pilot/);
});

test('10. Header: SEBI_CSCRF_STRICT preserves SEBI avatar, title, and 5-Doc Pilot badge', () => {
  const html = renderToStaticMarkup(
    React.createElement(Header, {
      apiStatus: 'connected',
      selectedCollection: testCollections[0], // sebi_cscrf
    })
  );

  assert.match(html, />SEBI<\/div>/);
  assert.match(html, /SEBI CSCRF Regulatory Intelligence Assistant/);
  assert.match(html, /5-Doc Pilot/);
});

test('11. Header: Missing collection context displays neutral branding and Multi-Collection badge', () => {
  const html = renderToStaticMarkup(
    React.createElement(Header, {
      apiStatus: 'connected',
      selectedCollection: null,
    })
  );

  assert.match(html, />DOC<\/div>/);
  assert.match(html, /Document Intelligence Platform/);
  assert.match(html, /Multi-Collection/);
  assert.doesNotMatch(html, />SEBI<\/div>/);
  assert.doesNotMatch(html, /5-Doc Pilot/);
});

test('12. TechnicalInfoDrawer: GENERAL_STATIC displays collection-neutral technical guarantees and limitations', () => {
  const html = renderToStaticMarkup(
    React.createElement(TechnicalInfoDrawer, {
      selectedCollection: testCollections[1], // general_synthetic
      initialOpen: true,
    })
  );

  // Guarantees
  assert.match(html, /100% mechanical character substring match against registered collection document chunks\./);
  assert.match(html, /Deterministic Provenance Tracking:/);
  assert.match(html, /graph supersession checks do not apply to static general documents/);

  // Limitations
  assert.match(html, /Advisory Status:/);
  assert.match(html, /Document excerpts provide technical and operational information only; they do not establish regulatory compliance or legal advice\./);
  assert.match(html, /Collection Boundary:/);
  assert.match(html, /External standards, unindexed manuals, and organization policies outside the active collection snapshot are not covered\./);

  // Negative checks: must NOT mention SEBI, CSCRF, circulars, or 5-document pilot
  assert.doesNotMatch(html, /SEBI CSCRF corpus chunks/);
  assert.doesNotMatch(html, /5-document pilot scope/);
  assert.doesNotMatch(html, /bilateral SEBI directions/);
  assert.doesNotMatch(html, /master circulars/);
});

test('13. TechnicalInfoDrawer: SEBI_CSCRF_STRICT preserves authoritative regulatory explanations and disclaimers', () => {
  const html = renderToStaticMarkup(
    React.createElement(TechnicalInfoDrawer, {
      selectedCollection: testCollections[0], // sebi_cscrf
      initialOpen: true,
    })
  );

  // Guarantees
  assert.match(html, /100% mechanical character substring match against the indexed SEBI CSCRF corpus chunks\./);
  assert.match(html, /Deterministic Graph Annotation:/);
  assert.match(html, /Cross-references verified supersession, partial amendment, and timeline extension edges\./);

  // Limitations
  assert.match(html, /Legal Currency:/);
  assert.match(html, /Does not constitute binding legal counsel or universal currency beyond the 5-document pilot scope\./);
  assert.match(html, /Exhaustive Compliance:/);
  assert.match(html, /Subsequent gazette notifications, master circulars, or bilateral SEBI directions outside the pilot are not indexed\./);
});

test('14. TechnicalInfoDrawer: Missing collection context defaults safely to neutral wording without assuming SEBI policy', () => {
  const html = renderToStaticMarkup(
    React.createElement(TechnicalInfoDrawer, {
      selectedCollection: null,
      initialOpen: true,
    })
  );

  assert.match(html, /100% mechanical character substring match against registered collection document chunks\./);
  assert.match(html, /Advisory Status:/);
  assert.doesNotMatch(html, /SEBI CSCRF corpus chunks/);
  assert.doesNotMatch(html, /5-document pilot scope/);
});
