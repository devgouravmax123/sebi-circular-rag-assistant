import { useState, useEffect } from 'react';
import type { QueryRequest, QueryResponse, QueryError, CollectionSummary } from './types/query';
import { queryRegulatoryAssistant, probeHealth, fetchCollections } from './api/client';
import { Header } from './components/Header';
import { DisclosureBanner } from './components/DisclosureBanner';
import { QueryForm } from './components/QueryForm';
import { ResultsPanel } from './components/ResultsPanel';
import { TechnicalInfoDrawer } from './components/TechnicalInfoDrawer';

export default function App() {
  const [apiStatus, setApiStatus] = useState<'checking' | 'connected' | 'disconnected'>('checking');
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<QueryError | null>(null);
  const [response, setResponse] = useState<QueryResponse | null>(null);
  const [lastSubmittedRequest, setLastSubmittedRequest] = useState<QueryRequest | null>(null);

  // Collections state
  const [collections, setCollections] = useState<CollectionSummary[]>([]);
  const [selectedCollectionId, setSelectedCollectionId] = useState<string>('sebi_cscrf');
  const [isCollectionsLoading, setIsCollectionsLoading] = useState<boolean>(true);
  const [collectionsError, setCollectionsError] = useState<string | null>(null);

  // Health check and collections loading
  const checkHealth = async () => {
    setApiStatus('checking');
    const result = await probeHealth(4000);
    setApiStatus(result.ok ? 'connected' : 'disconnected');
  };

  useEffect(() => {
    let ignore = false;

    // Check health
    probeHealth(4000).then((result) => {
      if (!ignore) {
        setApiStatus(result.ok ? 'connected' : 'disconnected');
      }
    });

    // Fetch collection catalog
    setIsCollectionsLoading(true);
    fetchCollections(5000)
      .then((colls) => {
        if (!ignore) {
          setCollections(colls);
          setCollectionsError(null);
          // If sebi_cscrf is available, default to it; otherwise select the first active collection
          if (colls.length > 0) {
            const hasSebi = colls.some((c) => c.collection_id === 'sebi_cscrf');
            if (hasSebi) {
              setSelectedCollectionId('sebi_cscrf');
            } else {
              setSelectedCollectionId(colls[0].collection_id);
            }
          }
        }
      })
      .catch((err: unknown) => {
        if (!ignore) {
          const msg = err instanceof Error ? err.message : 'Failed to load collections catalog';
          setCollectionsError(msg);
          // Safe default fallback
          setSelectedCollectionId('sebi_cscrf');
        }
      })
      .finally(() => {
        if (!ignore) {
          setIsCollectionsLoading(false);
        }
      });

    return () => {
      ignore = true;
    };
  }, []);

  // Query submission handler
  const handleQuerySubmit = async (request: QueryRequest) => {
    setIsLoading(true);
    setError(null);
    setLastSubmittedRequest(request);

    try {
      const res = await queryRegulatoryAssistant(request);
      setResponse(res);
      setApiStatus('connected');
    } catch (err: unknown) {
      const queryErr = err as QueryError;
      setError(queryErr);
      if (queryErr.isNetworkError) {
        setApiStatus('disconnected');
      }
    } finally {
      setIsLoading(false);
    }
  };

  const handleRetry = () => {
    if (lastSubmittedRequest) {
      handleQuerySubmit(lastSubmittedRequest);
    }
  };

  const activeCollection = collections.find((c) => c.collection_id === selectedCollectionId) || null;

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col font-sans selection:bg-cyan-500 selection:text-slate-950">
      {/* Header with connection indicator */}
      <Header
        apiStatus={apiStatus}
        selectedCollection={activeCollection}
        onRetryHealth={checkHealth}
      />

      {/* Main Container */}
      <main className="flex-1 max-w-5xl w-full mx-auto px-4 sm:px-6 py-8 flex flex-col gap-6">
        {/* Scope & Grounding Disclosure Banner */}
        <DisclosureBanner selectedCollection={activeCollection} />

        {/* Main Search and Configuration Form */}
        <QueryForm
          isLoading={isLoading}
          onSubmit={handleQuerySubmit}
          collections={collections}
          selectedCollectionId={selectedCollectionId}
          onSelectCollection={(id) => setSelectedCollectionId(id)}
          isCollectionsLoading={isCollectionsLoading}
          collectionsError={collectionsError}
          initialQuery={lastSubmittedRequest?.query || ''}
          initialTopK={lastSubmittedRequest?.top_k || 5}
          initialIncludeSuperseded={lastSubmittedRequest?.include_superseded || false}
        />

        {/* Results / Disposition Panel */}
        <ResultsPanel
          isLoading={isLoading}
          error={error}
          response={response}
          selectedCollection={activeCollection}
          onRetry={lastSubmittedRequest ? handleRetry : undefined}
        />

        {/* Mechanical vs Legal Technical Validation Explainer */}
        <TechnicalInfoDrawer selectedCollection={activeCollection} />
      </main>

      {/* Footer */}
      <footer className="border-t border-slate-900 bg-slate-950/80 px-6 py-6 text-center text-xs text-slate-500">
        <div className="max-w-4xl mx-auto space-y-1">
          <p>
            Multi-Collection Document Intelligence & Regulatory Research Platform — Prototype.
          </p>
          <p className="text-[11px] text-slate-600">
            {activeCollection?.policy_type === 'general_static'
              ? `Active collection: ${activeCollection.name} (${activeCollection.collection_id}). Static documents are mechanically verified for verbatim textual provenance.`
              : 'Operating strictly over the 5-document SEBI CSCRF pilot corpus. Not affiliated with, sponsored, or endorsed by SEBI. Excerpts represent mechanical text grounding only.'}
          </p>
        </div>
      </footer>
    </div>
  );
}
