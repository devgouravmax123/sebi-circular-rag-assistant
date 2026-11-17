import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { ResultsPanel } from '../src/components/ResultsPanel.tsx';
import { EvidenceCard } from '../src/components/EvidenceCard.tsx';

const baseExcerpt = {
  excerpt_id: 'exc_test_001',
  document_id: 'doc_acme_2024',
  circular_number: 'ACME-SEC-2024-001',
  issue_date: '2024-08-20',
  title: 'Acme Information Security Policy',
  page_start: 13,
  page_end: 13,
  source_chunk_id: 'acme_sec_p13_01',
  match_confidence: 1.0,
  mechanical_match_verified: true,
  regulatory_status: 'UNRESOLVED',
  status_reason: 'Corpus scope limitation: provision currency unverified globally.',
  acting_document_ids: ['doc_acme_2024'],
  applicable_edge_ids: [],
  excerpt_text: 'The Information Security Policy has been formulated to provide a structured approach to cybersecurity resilience for systems.',
};

const baseResponse = {
  query_text: 'What is the security policy?',
  retrieval_candidate_count: 5,
  verified_excerpts: [baseExcerpt],
  historical_superseded_excerpts: [],
  disposition: 'QUALIFIED_HISTORICAL_EXCERPTS',
  summary_narrative: 'Found 1 verified excerpt in active corpus.',
  regulatory_warnings: ['Scope Warning: Verification limited to indexed documents.'],
  pilot_corpus_scope_limitation: 'Indexed documents in active snapshot.',
  abstention_reason: null,
};

test('1. ResultsPanel: renders non-empty answer_text in direct grounded answer card', () => {
  const resp = {
    ...baseResponse,
    answer_text: 'The security policy provides a structured approach to cybersecurity resilience across systems.',
    synthesis_status: 'FULLY_ANSWERED',
  };

  const html = renderToStaticMarkup(React.createElement(ResultsPanel, {
    isLoading: false,
    error: null,
    response: resp,
  }));

  assert.match(html, /Direct Grounded Answer/);
  assert.match(html, /The security policy provides a structured approach to cybersecurity resilience across systems\./);
  assert.match(html, /Fully Supported by Evidence/);
});

test('2. ResultsPanel: does not render direct answer card when answer_text is missing or empty', () => {
  const respMissing = {
    ...baseResponse,
    answer_text: undefined,
  };

  const htmlMissing = renderToStaticMarkup(React.createElement(ResultsPanel, {
    isLoading: false,
    error: null,
    response: respMissing,
  }));

  assert.doesNotMatch(htmlMissing, /Direct Grounded Answer/);

  const respEmpty = {
    ...baseResponse,
    answer_text: '   ',
  };

  const htmlEmpty = renderToStaticMarkup(React.createElement(ResultsPanel, {
    isLoading: false,
    error: null,
    response: respEmpty,
  }));

  assert.doesNotMatch(htmlEmpty, /Direct Grounded Answer/);
});

test('3. ResultsPanel: displays distinct synthesis status labels accurately and without legal overclaims', () => {
  const testCases = [
    { status: 'FULLY_ANSWERED', expected: 'Fully Supported by Evidence' },
    { status: 'PARTIALLY_ANSWERED', expected: 'Partially Answered \\(Gaps Disclosed\\)' },
    { status: 'ABSTAINED', expected: 'Controlled Abstention' },
    { status: 'PROVIDER_FAILED', expected: 'Synthesis Service Unavailable' },
  ];

  for (const { status, expected } of testCases) {
    const resp = {
      ...baseResponse,
      answer_text: 'Tested answer for status labeling.',
      synthesis_status: status,
    };

    const html = renderToStaticMarkup(React.createElement(ResultsPanel, {
      isLoading: false,
      error: null,
      response: resp,
    }));

    assert.match(html, new RegExp(expected));
    // Verify FULLY_ANSWERED is never equated with verified current legal status
    assert.doesNotMatch(html, /verified current law/i);
    assert.doesNotMatch(html, /verified current legal status/i);
  }
});

test('4. ResultsPanel: displays unresolved subquestions compactly', () => {
  const resp = {
    ...baseResponse,
    answer_text: 'The security policy establishes baseline cyber requirements.',
    synthesis_status: 'PARTIALLY_ANSWERED',
    unresolved_subquestions: [
      {
        subquestion: 'What is the compliance penalty schedule under Section 15HA?',
        reason: 'Penalty schedules are not contained in the indexed document snapshot.',
      },
    ],
  };

  const html = renderToStaticMarkup(React.createElement(ResultsPanel, {
    isLoading: false,
    error: null,
    response: resp,
  }));

  assert.match(html, /Unresolved Sub-questions \(1\)/);
  assert.match(html, /What is the compliance penalty schedule under Section 15HA\?/);
  assert.match(html, /Penalty schedules are not contained in the indexed document snapshot\./);
});

test('5. ResultsPanel: displays synthesis failure reason only when safe and useful', () => {
  const resp = {
    ...baseResponse,
    answer_text: 'Partial text generated before service interruption.',
    synthesis_status: 'PROVIDER_FAILED',
    synthesis_failure_reason: 'Synthesis upstream timeout after 5000ms.',
  };

  const html = renderToStaticMarkup(React.createElement(ResultsPanel, {
    isLoading: false,
    error: null,
    response: resp,
  }));

  assert.match(html, /Synthesis Note:/);
  assert.match(html, /Synthesis upstream timeout after 5000ms\./);
});

test('6. ResultsPanel: preserves safety-gate disposition, warnings, and corpus boundary', () => {
  const resp = {
    ...baseResponse,
    answer_text: 'Security policy details.',
    synthesis_status: 'FULLY_ANSWERED',
  };

  const demoCollection = {
    collection_id: 'general_synthetic',
    name: 'General Technical Knowledge',
    description: 'Master documentation and guidelines',
    domain: 'TECHNICAL',
    regulator: null,
    policy_type: 'general_static',
    status: 'active',
  };

  const html = renderToStaticMarkup(React.createElement(ResultsPanel, {
    isLoading: false,
    error: null,
    response: resp,
    selectedCollection: demoCollection,
  }));

  assert.match(html, /Safety Gate Disposition/);
  assert.match(html, /Document Excerpts — Qualifications Apply/);
  assert.match(html, /Regulatory Warnings &amp; Caveats \(1\)/);
  assert.match(html, /Corpus Boundary:/);
  assert.match(html, /Indexed documents in active snapshot\./);
});

test('7. EvidenceCard: clarifies unresolved currency without implying active law', () => {
  const html = renderToStaticMarkup(React.createElement(EvidenceCard, {
    excerpt: baseExcerpt,
    index: 0,
  }));

  assert.match(html, /CURRENCY NOT RESOLVED/);
  assert.match(html, /Passage verified from collection; active legal or operational currency is not established beyond the indexed collection\./);
  assert.doesNotMatch(html, /verified current law/i);
});

test('8. EvidenceCard: truncates long evidence passages by default and renders accessible toggle', () => {
  const longPassage = 'B'.repeat(350);
  const longExcerpt = {
    ...baseExcerpt,
    excerpt_text: longPassage,
  };

  const html = renderToStaticMarkup(React.createElement(EvidenceCard, {
    excerpt: longExcerpt,
    index: 0,
  }));

  assert.match(html, /\.\.\./);
  assert.match(html, /Show full passage/);
  assert.match(html, /aria-expanded="false"/);
  assert.match(html, /aria-label="Expand full excerpt text"/);
  assert.strictEqual(html.includes(longPassage), false);
});

test('9. EvidenceCard: preserves document metadata, page numbers, and source links', () => {
  const html = renderToStaticMarkup(React.createElement(EvidenceCard, {
    excerpt: baseExcerpt,
    index: 0,
  }));

  assert.match(html, /ACME-SEC-2024-001/);
  assert.match(html, /2024-08-20/);
  assert.match(html, /Page 13/);
  assert.match(html, /acme_sec_p13_01/);
});
