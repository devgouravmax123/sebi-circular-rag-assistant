import { useState, useEffect } from 'react';
import type { QueryRequest, QueryResponse, QueryError } from './types/query';
import { queryRegulatoryAssistant, probeHealth } from './api/client';
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

  // Health check probing
  const checkHealth = async () => {
    setApiStatus('checking');
    const result = await probeHealth(4000);
    setApiStatus(result.ok ? 'connected' : 'disconnected');
  };

  useEffect(() => {
    let ignore = false;
    probeHealth(4000).then((result) => {
      if (!ignore) {
        setApiStatus(result.ok ? 'connected' : 'disconnected');
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

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col font-sans selection:bg-cyan-500 selection:text-slate-950">
      {/* Header with connection indicator */}
      <Header apiStatus={apiStatus} onRetryHealth={checkHealth} />

      {/* Main Container */}
      <main className="flex-1 max-w-5xl w-full mx-auto px-4 sm:px-6 py-8 flex flex-col gap-6">
        {/* Scope & Grounding Disclosure Banner */}
        <DisclosureBanner />

        {/* Main Search and Configuration Form */}
        <QueryForm
          isLoading={isLoading}
          onSubmit={handleQuerySubmit}
          initialQuery={lastSubmittedRequest?.query || ''}
          initialTopK={lastSubmittedRequest?.top_k || 5}
          initialIncludeSuperseded={lastSubmittedRequest?.include_superseded || false}
        />

        {/* Results / Disposition Panel */}
        <ResultsPanel
          isLoading={isLoading}
          error={error}
          response={response}
          onRetry={lastSubmittedRequest ? handleRetry : undefined}
        />

        {/* Mechanical vs Legal Technical Validation Explainer */}
        <TechnicalInfoDrawer />
      </main>

      {/* Footer */}
      <footer className="border-t border-slate-900 bg-slate-950/80 px-6 py-6 text-center text-xs text-slate-500">
        <div className="max-w-4xl mx-auto space-y-1">
          <p>
            SEBI CSCRF Regulatory Intelligence Assistant — Educational Research Prototype.
          </p>
          <p className="text-[11px] text-slate-600">
            Operating strictly over the 5-document pilot corpus. Not affiliated with, sponsored, or endorsed by the Securities and Exchange Board of India (SEBI). Excerpts represent mechanical text grounding only.
          </p>
        </div>
      </footer>
    </div>
  );
}
