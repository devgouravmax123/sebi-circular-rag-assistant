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
    collection_id: 'cisa_playbooks',
    name: 'CISA Incident Response Playbooks',
    description: 'Federal cybersecurity incident response guidelines',
    domain: 'CYBERSECURITY',
    regulator: 'CISA',
    policy_type: 'general_static',
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
      selectedCollectionId: 'cisa_playbooks',
    })
  );

  assert.match(html, /Target Collection:/);
  assert.match(html, /CISA Incident Response Playbooks \(cisa_playbooks\)/);
  assert.match(html, /Acme Technical Handbook \(general_synthetic\)/);
});

test('2. QueryForm adapts placeholders and labels for general collections', () => {
  const html = renderToStaticMarkup(
    React.createElement(QueryForm, {
      isLoading: false,
      onSubmit: () => {},
      collections: testCollections,
      selectedCollectionId: 'general_synthetic',
    })
  );

  assert.match(html, /Documentation Question \/ Search Topic/);
  assert.match(html, /Include Archived \/ Superseded Versions:/);
  assert.match(html, /Sample Queries \(Click to Populate\):/);
});

test('3. DisclosureBanner renders general_static disclosure without regulatory legal overclaims', () => {
  const html = renderToStaticMarkup(
    React.createElement(DisclosureBanner, {
      selectedCollection: testCollections[1],
    })
  );

  assert.match(html, /Documentation Disclosure — Acme Technical Handbook/);
  assert.match(html, /general_synthetic/);
  assert.match(html, /Excerpts are mechanically validated for verbatim textual provenance/);
  assert.doesNotMatch(html, /CIR\/2024\/113/);
  assert.doesNotMatch(html, /SEBI/);
});

test('4. DisclosureBanner renders clean general disclosure for all collections', () => {
  const html = renderToStaticMarkup(
    React.createElement(DisclosureBanner, {
      selectedCollection: testCollections[0],
    })
  );

  assert.match(html, /Documentation Disclosure — CISA Incident Response Playbooks/);
  assert.match(html, /cisa_playbooks/);
  assert.doesNotMatch(html, /SEBI CSCRF/);
  assert.doesNotMatch(html, /CIR\//);
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
    pilot_corpus_scope_limitation: 'Operating strictly over Acme Cloud FinOps documents.',
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
    pilot_corpus_scope_limitation: 'Operating strictly over Acme Cloud FinOps.',
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

  // Must not claim SEBI circular or 5-document indexed pilot corpus
  assert.doesNotMatch(html, /no relevant SEBI circular exists globally/);
  assert.doesNotMatch(html, /within the 5-document indexed pilot corpus/);
  assert.doesNotMatch(html, /5-document SEBI CSCRF circular corpus/);
});

test('7. ResultsPanel: General document results render cleanly with verified provenance', () => {
  const resp = {
    query_text: 'What is incident response in CISA playbooks?',
    retrieval_candidate_count: 5,
    verified_excerpts: [
      {
        excerpt_id: 'cisa_01',
        document_id: 'cisa_playbook_2021',
        circular_number: 'EO-14028',
        issue_date: '2021-11-01',
        title: 'Incident Response Playbook',
        page_start: 5,
        page_end: 5,
        source_chunk_id: 'cisa_chunk_01',
        match_confidence: 1.0,
        mechanical_match_verified: true,
        regulatory_status: 'ACTIVE',
        status_reason: 'Active baseline',
        acting_document_ids: ['cisa_playbook_2021'],
        applicable_edge_ids: [],
        excerpt_text: 'Incident response procedures require immediate containment upon detection.',
      },
    ],
    historical_superseded_excerpts: [],
    disposition: 'VERIFIED_HISTORICAL_EXCERPTS',
    summary_narrative: 'Found 1 verified excerpt in CISA collection.',
    regulatory_warnings: [],
    pilot_corpus_scope_limitation: 'Operating strictly over CISA collection.',
    abstention_reason: null,
    answer_text: 'Incident response procedures require immediate containment.',
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

  assert.match(html, /Verified Document Excerpts/);
  assert.match(html, /All returned excerpts mechanically verified for verbatim textual provenance/);
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
    pilot_corpus_scope_limitation: 'Operating strictly over indexed collection.',
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
  assert.match(html, /Note: Abstention indicates no sufficiently relevant passages with verified textual provenance exist in the selected collection\./);
  assert.doesNotMatch(html, /no relevant SEBI circular exists globally/);
  assert.doesNotMatch(html, /5-document SEBI CSCRF circular corpus/);
});

test('9. Header: GENERAL_STATIC displays Citenza avatar C, Citenza title, and collection name badge', () => {
  const html = renderToStaticMarkup(
    React.createElement(Header, {
      apiStatus: 'connected',
      selectedCollection: testCollections[1], // general_synthetic
    })
  );

  assert.match(html, />C<\/div>/);
  assert.match(html, /Citenza/);
  assert.match(html, /Your documents\. Verified answers\./);
  assert.match(html, /Acme Technical Handbook/);
  assert.doesNotMatch(html, />SEBI<\/div>/);
  assert.doesNotMatch(html, /SEBI CSCRF Regulatory Intelligence Assistant/);
  assert.doesNotMatch(html, /5-Doc Pilot/);
});

test('10. Header: Displays Citenza branding and collection badge for general collections', () => {
  const html = renderToStaticMarkup(
    React.createElement(Header, {
      apiStatus: 'connected',
      selectedCollection: testCollections[0], // cisa_playbooks
    })
  );

  assert.match(html, />C<\/div>/);
  assert.match(html, /Citenza/);
  assert.match(html, /Your documents\. Verified answers\./);
  assert.match(html, /CISA Incident Response Playbooks/);
  assert.doesNotMatch(html, />SEBI<\/div>/);
  assert.doesNotMatch(html, /SEBI CSCRF Regulatory Intelligence Assistant/);
  assert.doesNotMatch(html, /5-Doc Pilot/);
});

test('11. Header: Missing collection context displays Citenza branding and default badge', () => {
  const html = renderToStaticMarkup(
    React.createElement(Header, {
      apiStatus: 'connected',
      selectedCollection: null,
    })
  );

  assert.match(html, />C<\/div>/);
  assert.match(html, /Citenza/);
  assert.match(html, /Your documents\. Verified answers\./);
  assert.match(html, /Document Intelligence/);
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

  // Limitations
  assert.match(html, /Advisory Status:/);
  assert.match(html, /Document excerpts provide technical and operational grounding only; they do not constitute formal legal counsel\./);
  assert.match(html, /Collection Boundary:/);
  assert.match(html, /External manuals, unindexed documents, and corporate policies outside the active collection snapshot are not covered\./);

  // Negative checks: must NOT mention SEBI, CSCRF, circulars, or 5-document pilot
  assert.doesNotMatch(html, /SEBI CSCRF corpus chunks/);
  assert.doesNotMatch(html, /5-document pilot scope/);
  assert.doesNotMatch(html, /bilateral SEBI directions/);
  assert.doesNotMatch(html, /master circulars/);
});

test('13. TechnicalInfoDrawer: Displays general technical explanations and disclaimers', () => {
  const html = renderToStaticMarkup(
    React.createElement(TechnicalInfoDrawer, {
      selectedCollection: testCollections[0], // cisa_playbooks
      initialOpen: true,
    })
  );

  // Guarantees
  assert.match(html, /100% mechanical character substring match against registered collection document chunks\./);
  assert.match(html, /Retrieval-Bound Safety:/);

  // Limitations
  assert.match(html, /Advisory Status:/);
  assert.match(html, /Collection Boundary:/);
  assert.doesNotMatch(html, /SEBI/);
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
