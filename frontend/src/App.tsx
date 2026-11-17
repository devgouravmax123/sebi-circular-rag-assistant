import { useState, useEffect, useMemo } from 'react';
import type { QueryRequest, QueryResponse, QueryError, CollectionSummary } from './types/query';
import type { GuestCollection } from './types/guest';
import {
  queryRegulatoryAssistant,
  probeHealth,
  fetchCollections,
  fetchGuestCollections,
  recordGuestQuery,
} from './api/client';
import { AuthProvider, useAuth } from './context/AuthContext';
import { WorkspaceProvider, useWorkspace } from './context/WorkspaceContext';
import { Header, type NavTab } from './components/Header';
import { DisclosureBanner } from './components/DisclosureBanner';
import { QueryForm } from './components/QueryForm';
import { ResultsPanel } from './components/ResultsPanel';
import { TechnicalInfoDrawer } from './components/TechnicalInfoDrawer';
import { QueryHistoryDrawer } from './components/QueryHistoryDrawer';
import { DashboardView } from './components/DashboardView';
import { CollectionsView } from './components/CollectionsView';
import { QueryHistoryView } from './components/QueryHistoryView';
import { WorkspaceSettingsView } from './components/WorkspaceSettingsView';
import { SessionSettingsView } from './components/SessionSettingsView';
import { LandingPage } from './components/LandingPage';
import { AuthModal } from './components/AuthModal';

function AssistantMain() {
  const { token, isAuthenticated, isGuest, guestToken, refreshGuestStatus } = useAuth();
  const { workspaceCollections, isCollectionsLoading: isWsColsLoading } = useWorkspace();

  // Active view tab
  const [activeTab, setActiveTab] = useState<NavTab>('dashboard');

  // Backend connection status
  const [apiStatus, setApiStatus] = useState<'checking' | 'connected' | 'disconnected'>('checking');
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<QueryError | null>(null);
  const [response, setResponse] = useState<QueryResponse | null>(null);
  const [lastSubmittedRequest, setLastSubmittedRequest] = useState<QueryRequest | null>(null);

  // Collections state
  const [publicCollections, setPublicCollections] = useState<CollectionSummary[]>([]);
  const [guestCollections, setGuestCollections] = useState<GuestCollection[]>([]);
  const [selectedCollectionId, setSelectedCollectionId] = useState<string>('');
  const [isCollectionsLoading, setIsCollectionsLoading] = useState<boolean>(true);
  const [collectionsError, setCollectionsError] = useState<string | null>(null);

  // Auth modal triggered from anywhere
  const [isAuthModalOpen, setIsAuthModalOpen] = useState(false);
  const [authModalMode, setAuthModalMode] = useState<'login' | 'signup'>('login');

  // History Drawer state
  const [isHistoryDrawerOpen, setIsHistoryDrawerOpen] = useState<boolean>(false);

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

  // Fetch guest collections when in guest mode
  useEffect(() => {
    let ignore = false;
    if (isGuest && guestToken) {
      fetchGuestCollections(guestToken)
        .then((cols) => {
          if (!ignore) setGuestCollections(cols);
        })
        .catch(() => {
          if (!ignore) setGuestCollections([]);
        });
    } else {
      setGuestCollections([]);
    }
    return () => {
      ignore = true;
    };
  }, [isGuest, guestToken]);

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
          if (colls.length > 0 && !selectedCollectionId) {
            setSelectedCollectionId(colls[0].collection_id);
          }
        }
      })
      .catch((err: unknown) => {
        if (!ignore) {
          const msg = err instanceof Error ? err.message : 'Failed to load collections catalog';
          setCollectionsError(msg);
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

  // Compute active collections based on session context
  const combinedCollections: CollectionSummary[] = useMemo(() => {
    if (isAuthenticated) {
      // Authenticated users strictly work with their workspace collections
      return workspaceCollections.map((wc) => ({
        collection_id: wc.id,
        name: wc.name,
        description: wc.description,
        policy_type: wc.policy_name,
        status: 'active',
        is_private: true,
      }));
    }
    if (isGuest) {
      return guestCollections.map((gc) => ({
        collection_id: gc.id,
        name: gc.name,
        description: gc.description,
        policy_type: 'general_static',
        status: 'active',
        is_private: true,
      }));
    }
    return publicCollections;
  }, [isAuthenticated, isGuest, workspaceCollections, guestCollections, publicCollections]);

  // Validate that selected collection still exists in combined collections
  useEffect(() => {
    if (combinedCollections.length > 0) {
      const exists = combinedCollections.some((c) => c.collection_id === selectedCollectionId);
      if (!exists) {
        setSelectedCollectionId(combinedCollections[0].collection_id);
      }
    } else {
      setSelectedCollectionId('');
    }
  }, [combinedCollections, selectedCollectionId]);

  // Query submission handler
  const handleQuerySubmit = async (request: QueryRequest) => {
    setIsLoading(true);
    setError(null);
    setLastSubmittedRequest(request);

    const authToken = token || guestToken;

    try {
      const res = await queryRegulatoryAssistant(request, authToken);
      setResponse(res);
      setApiStatus('connected');

      // Record query history in guest session if in guest mode
      if (isGuest && guestToken) {
        try {
          await recordGuestQuery(
            request.query,
            request.collection_id || '',
            res.disposition,
            res.answer_text || undefined,
            res.verified_excerpts?.length || 0,
            guestToken
          );
          await refreshGuestStatus();
        } catch (recErr) {
          console.warn('Failed to record guest query history:', recErr);
        }
      }
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
    setActiveTab('research');
    handleQuerySubmit({
      query: queryText,
      collection_id: collectionId,
    });
  };

  const handleSelectCollectionAndResearch = (collectionId: string) => {
    setSelectedCollectionId(collectionId);
    setActiveTab('research');
  };

  const handleOpenAuthModal = (mode: 'login' | 'signup') => {
    setAuthModalMode(mode);
    setIsAuthModalOpen(true);
  };

  const activeCollection =
    combinedCollections.find((c) => c.collection_id === selectedCollectionId) || null;

  // 1. PRODUCT ENTRY EXPERIENCE: If unauthenticated and NOT in guest mode, show the dedicated Landing Page!
  if (!isAuthenticated && !isGuest) {
    return (
      <>
        <LandingPage
          onOpenAuth={handleOpenAuthModal}
        />
        <AuthModal
          isOpen={isAuthModalOpen}
          onClose={() => setIsAuthModalOpen(false)}
          defaultMode={authModalMode}
        />
      </>
    );
  }

  // 2. ACTIVE APPLICATION: When authenticated or in active guest mode
  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col font-sans selection:bg-cyan-500 selection:text-slate-950">
      {/* Header with connection indicator, workspace switcher, and user auth */}
      <Header
        apiStatus={apiStatus}
        selectedCollection={activeTab === 'research' ? activeCollection : null}
        activeTab={activeTab}
        onSelectTab={(tab) => {
          if (tab === 'workspace' && isGuest) {
            setActiveTab('session');
          } else {
            setActiveTab(tab);
          }
        }}
        onRetryHealth={checkHealth}
        onOpenHistory={() => setIsHistoryDrawerOpen(true)}
      />

      {/* Main Container */}
      <main className="flex-1 max-w-6xl w-full mx-auto px-4 sm:px-6 py-6 flex flex-col gap-6">
        {/* Tab 1: Dashboard View */}
        {activeTab === 'dashboard' && (
          <DashboardView
            collections={combinedCollections}
            onNavigate={(tab) => setActiveTab(tab)}
            onSelectCollectionAndResearch={handleSelectCollectionAndResearch}
            onOpenAuthModal={handleOpenAuthModal}
          />
        )}

        {/* Tab 2: Collections / My Documents View */}
        {activeTab === 'collections' && (
          <CollectionsView
            onNavigateToResearch={handleSelectCollectionAndResearch}
            onOpenAuthModal={handleOpenAuthModal}
          />
        )}

        {/* Tab 3: Ask AI View */}
        {activeTab === 'research' && (
          <div className="space-y-6 animate-fadeIn">
            {/* Scope & Grounding Disclosure Banner (Only when SEBI is selected) */}
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
          </div>
        )}

        {/* Tab 4: Research History View */}
        {activeTab === 'history' && (
          <QueryHistoryView
            onReplayQuery={handleReplayHistoryQuery}
            onOpenAuthModal={handleOpenAuthModal}
          />
        )}

        {/* Tab 5: Workspace Settings View (Registered Users Only) */}
        {activeTab === 'workspace' && !isGuest && (
          <WorkspaceSettingsView onOpenAuthModal={handleOpenAuthModal} />
        )}

        {/* Tab 6: Session Settings / Account Settings View */}
        {activeTab === 'session' && (
          <SessionSettingsView
            onOpenAuthModal={handleOpenAuthModal}
            onSessionEnded={() => setActiveTab('dashboard')}
          />
        )}
      </main>

      {/* Side Query History Drawer */}
      <QueryHistoryDrawer
        isOpen={isHistoryDrawerOpen}
        onClose={() => setIsHistoryDrawerOpen(false)}
        onReplayQuery={handleReplayHistoryQuery}
      />

      {/* Global Auth Modal */}
      <AuthModal
        isOpen={isAuthModalOpen}
        onClose={() => setIsAuthModalOpen(false)}
        defaultMode={authModalMode}
      />

      {/* Footer */}
      <footer className="border-t border-slate-900 bg-slate-950/80 px-6 py-6 text-center text-xs text-slate-500">
        <div className="max-w-4xl mx-auto space-y-1">
          <p>
            Citenza — Multi-Tenant AI Document Intelligence &amp; Research Platform.
          </p>
          <p className="text-[11px] text-slate-600">
            Answers are strictly limited to the selected indexed collection and verified evidence citations.
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
