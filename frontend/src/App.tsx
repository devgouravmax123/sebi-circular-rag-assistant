import { useState, useEffect, useMemo } from 'react';
import type { QueryRequest, QueryResponse, QueryError, CollectionSummary } from './types/query';
import { queryRegulatoryAssistant, probeHealth, fetchCollections } from './api/client';
import { AuthProvider, useAuth } from './context/AuthContext';
import { WorkspaceProvider, useWorkspace } from './context/WorkspaceContext';
import { Header } from './components/Header';
import { DisclosureBanner } from './components/DisclosureBanner';
import { QueryForm } from './components/QueryForm';
import { ResultsPanel } from './components/ResultsPanel';
import { TechnicalInfoDrawer } from './components/TechnicalInfoDrawer';
import { QueryHistoryDrawer } from './components/QueryHistoryDrawer';

function AssistantMain() {
  const { token } = useAuth();
  const { workspaceCollections, isCollectionsLoading: isWsColsLoading } = useWorkspace();

  const [apiStatus, setApiStatus] = useState<'checking' | 'connected' | 'disconnected'>('checking');
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<QueryError | null>(null);
  const [response, setResponse] = useState<QueryResponse | null>(null);
  const [lastSubmittedRequest, setLastSubmittedRequest] = useState<QueryRequest | null>(null);

  // Collections state
  const [publicCollections, setPublicCollections] = useState<CollectionSummary[]>([]);
  const [selectedCollectionId, setSelectedCollectionId] = useState<string>('sebi_cscrf');
  const [isCollectionsLoading, setIsCollectionsLoading] = useState<boolean>(true);
  const [collectionsError, setCollectionsError] = useState<string | null>(null);

  // History Drawer state
  const [isHistoryOpen, setIsHistoryOpen] = useState<boolean>(false);

  // Health check with retry to absorb Render Free container spin-up delay
  const checkHealth = async () => {
    setApiStatus('checking');
    let result = await probeHealth(10000);
    if (!result.ok) {
      await new Promise((r) => setTimeout(r, 2000));
      result = await probeHealth(15000);
    }
    setApiStatus(result.ok ? 'connected' : 'disconnected');
  };

  useEffect(() => {
    let ignore = false;

    // Check health with retry for Render free cold start
    const runInitialHealthCheck = async () => {
      let result = await probeHealth(10000);
      if (!ignore && !result.ok) {
        await new Promise((r) => setTimeout(r, 2000));
        result = await probeHealth(15000);
      }
      if (!ignore) {
        setApiStatus(result.ok ? 'connected' : 'disconnected');
      }
    };
    runInitialHealthCheck();

    // Fetch public collection catalog
    setIsCollectionsLoading(true);
    fetchCollections(5000)
      .then((colls) => {
        if (!ignore) {
          setPublicCollections(colls);
          setCollectionsError(null);
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

  // Merge public and private workspace collections
  const combinedCollections: CollectionSummary[] = useMemo(
    () => [
      ...publicCollections,
      ...workspaceCollections.map((wc) => ({
        collection_id: wc.id,
        name: wc.name,
        description: wc.description,
        policy_type: wc.policy_name,
        status: 'active',
        is_private: true,
      })),
    ],
    [publicCollections, workspaceCollections]
  );

  // Validate that selected collection still exists in combined collections
  useEffect(() => {
    if (combinedCollections.length > 0) {
      const exists = combinedCollections.some((c) => c.collection_id === selectedCollectionId);
      if (!exists) {
        const hasSebi = combinedCollections.some((c) => c.collection_id === 'sebi_cscrf');
        setSelectedCollectionId(hasSebi ? 'sebi_cscrf' : combinedCollections[0].collection_id);
      }
    }
  }, [combinedCollections, selectedCollectionId]);

  // Query submission handler
  const handleQuerySubmit = async (request: QueryRequest) => {
    setIsLoading(true);
    setError(null);
    setLastSubmittedRequest(request);

    try {
      const res = await queryRegulatoryAssistant(request, token);
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

  const handleReplayHistoryQuery = (queryText: string, collectionId: string) => {
    setSelectedCollectionId(collectionId);
    handleQuerySubmit({
      query: queryText,
      collection_id: collectionId,
    });
  };

  const activeCollection =
    combinedCollections.find((c) => c.collection_id === selectedCollectionId) || null;

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col font-sans selection:bg-cyan-500 selection:text-slate-950">
      {/* Header with connection indicator, workspace switcher, and user auth */}
      <Header
        apiStatus={apiStatus}
        selectedCollection={activeCollection}
        onRetryHealth={checkHealth}
        onOpenHistory={() => setIsHistoryOpen(true)}
      />

      {/* Main Container */}
      <main className="flex-1 max-w-5xl w-full mx-auto px-4 sm:px-6 py-8 flex flex-col gap-6">
        {/* Scope & Grounding Disclosure Banner */}
        <DisclosureBanner selectedCollection={activeCollection} />

        {/* Main Search and Configuration Form */}
        <QueryForm
          isLoading={isLoading}
          onSubmit={handleQuerySubmit}
          collections={combinedCollections}
          selectedCollectionId={selectedCollectionId}
          onSelectCollection={(id) => setSelectedCollectionId(id)}
          isCollectionsLoading={isCollectionsLoading || isWsColsLoading}
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

      {/* Query History Drawer */}
      <QueryHistoryDrawer
        isOpen={isHistoryOpen}
        onClose={() => setIsHistoryOpen(false)}
        onReplayQuery={handleReplayHistoryQuery}
      />

      {/* Footer */}
      <footer className="border-t border-slate-900 bg-slate-950/80 px-6 py-6 text-center text-xs text-slate-500">
        <div className="max-w-4xl mx-auto space-y-1">
          <p>
            Multi-Tenant Document Intelligence & Regulatory Research Platform.
          </p>
          <p className="text-[11px] text-slate-600">
            Answers are strictly limited to the selected indexed collection and verified evidence. SEBI CSCRF materials are not affiliated with or endorsed by SEBI. Excerpts represent mechanical text grounding only.
          </p>
        </div>
      </footer>
    </div>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <WorkspaceProvider>
        <AssistantMain />
      </WorkspaceProvider>
    </AuthProvider>
  );
}
