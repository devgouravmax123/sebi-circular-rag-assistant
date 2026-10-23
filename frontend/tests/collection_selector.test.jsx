import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { QueryForm } from '../src/components/QueryForm.tsx';
import { DisclosureBanner } from '../src/components/DisclosureBanner.tsx';

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
