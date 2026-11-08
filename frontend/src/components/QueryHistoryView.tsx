import { useState, useEffect } from 'react';
import { useAuth } from '../context/AuthContext';
import { fetchQueryHistory, fetchQueryHistoryDetail, fetchGuestHistory } from '../api/client';
import type { QueryHistoryItem, QueryHistoryDetail } from '../types/history';

interface QueryHistoryViewProps {
  onReplayQuery: (queryText: string, collectionId: string) => void;
  onOpenAuthModal?: (mode: 'login' | 'signup') => void;
}

export function QueryHistoryView({
  onReplayQuery,
  onOpenAuthModal,
}: QueryHistoryViewProps) {
  const { token, isAuthenticated, isGuest, guestToken } = useAuth();

  const [historyItems, setHistoryItems] = useState<QueryHistoryItem[]>([]);
  const [totalCount, setTotalCount] = useState<number>(0);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // Search filter
  const [searchFilter, setSearchFilter] = useState('');

  // Selected detail state
  const [selectedDetail, setSelectedDetail] = useState<QueryHistoryDetail | null>(null);
  const [isDetailLoading, setIsDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const loadHistory = async () => {
    setIsLoading(true);
    setError(null);

    try {
      if (isGuest && guestToken) {
        const res = await fetchGuestHistory(guestToken);
        const mappedItems: QueryHistoryItem[] = res.items.map((item) => ({
          id: item.id,
          workspace_id: 'guest',
          collection_id: item.collection_id,
          query_text: item.query_text,
          disposition: item.disposition,
          verified_excerpts_count: item.citations_count,
          technical_validation_passed: true,
          created_at: item.created_at,
          answer_preview: item.answer_text ? item.answer_text.slice(0, 120) : null,
          citation_count: item.citations_count,
        }));
        setHistoryItems(mappedItems);
        setTotalCount(res.total);
      } else if (isAuthenticated && token) {
        const res = await fetchQueryHistory(token, { limit: 50 });
        setHistoryItems(res.items);
        setTotalCount(res.total);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load query history.');
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    if ((isAuthenticated && token) || (isGuest && guestToken)) {
      loadHistory();
    } else {
      setHistoryItems([]);
      setTotalCount(0);
      setSelectedDetail(null);
    }
  }, [isAuthenticated, token, isGuest, guestToken]);

  const handleInspectDetail = async (item: QueryHistoryItem) => {
    if (isGuest) {
      setSelectedDetail({
        id: item.id,
        workspace_id: 'guest',
        collection_id: item.collection_id,
        query_text: item.query_text,
        disposition: item.disposition,
        verified_excerpts_count: item.verified_excerpts_count,
        technical_validation_passed: item.technical_validation_passed,
        created_at: item.created_at,
        query_options: {},
        summary_narrative: item.answer_preview || null,
        answer_text: item.answer_preview || null,
        claim_citations: [],
      });
      return;
    }

    if (!token) return;
    setIsDetailLoading(true);
    setDetailError(null);
    try {
      const detail = await fetchQueryHistoryDetail(item.id, token);
      setSelectedDetail(detail);
    } catch (err) {
      setDetailError(err instanceof Error ? err.message : 'Failed to load query record details.');
    } finally {
      setIsDetailLoading(false);
    }
  };

  const filteredItems = historyItems.filter((item) => {
    if (!searchFilter.trim()) return true;
    const q = searchFilter.toLowerCase();
    return (
      item.query_text.toLowerCase().includes(q) ||
      item.collection_id.toLowerCase().includes(q) ||
      item.disposition.toLowerCase().includes(q)
    );
  });

  return (
    <div className="space-y-8 animate-fadeIn">
      {/* Header section */}
      <section className="border-b border-slate-800/80 pb-6">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
          <div>
            <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-cyan-950/80 border border-cyan-800/60 text-xs font-medium text-cyan-400 mb-2">
              <span>📜</span>
              <span>{isGuest ? 'Ephemeral Guest Query Ledger' : 'Audit & Research History Ledger'}</span>
            </div>
            <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-white">
              {isGuest ? 'Guest Query History' : 'Persistent Query History & Citations'}
            </h1>
            <p className="text-sm text-slate-300 mt-1 max-w-3xl leading-relaxed">
              Review past inquiries, synthesis dispositions, verified citations, and evidence passages.
              Re-run previous queries or inspect granular citation boundaries.
            </p>
          </div>

          {(isAuthenticated || isGuest) && (
            <button
              onClick={loadHistory}
              disabled={isLoading}
              className="px-3.5 py-1.5 bg-slate-800/90 hover:bg-slate-700 text-slate-200 text-xs font-medium rounded-lg transition border border-slate-700 cursor-pointer flex items-center gap-1.5 self-start sm:self-auto shrink-0"
            >
              <span className={isLoading ? 'animate-spin' : ''}>🔄</span>
              <span>Refresh Ledger</span>
            </button>
          )}
        </div>
      </section>

      {/* Guest Mode Notice */}
      {isGuest && (
        <div className="rounded-xl border border-amber-800/80 bg-amber-950/40 p-4 space-y-1">
          <div className="flex items-center gap-2 text-xs font-semibold text-amber-300">
            <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
            <span>Ephemeral History Ledger (24h TTL)</span>
          </div>
          <p className="text-xs text-slate-300 leading-relaxed">
            Your query history is scoped to this temporary session. Ending your session will purge all recorded queries.
          </p>
        </div>
      )}

      {/* Unauthenticated Prompt */}
      {!isAuthenticated && !isGuest && (
        <div className="rounded-xl border border-cyan-900/60 bg-gradient-to-r from-cyan-950/30 via-slate-900/40 to-slate-950 p-6 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
          <div className="space-y-1">
            <h3 className="text-sm font-semibold text-cyan-300 flex items-center gap-2">
              <span>🔒</span>
              <span>Sign In Required for Persistent Query History</span>
            </h3>
            <p className="text-xs text-slate-400">
              In anonymous mode, queries are not recorded. Sign in or continue as guest to maintain an active research ledger.
            </p>
          </div>
          {onOpenAuthModal && (
            <div className="flex items-center gap-2 shrink-0">
              <button
                onClick={() => onOpenAuthModal('login')}
                className="px-3.5 py-1.5 text-xs font-medium text-slate-300 hover:text-white bg-slate-800 hover:bg-slate-700 rounded-lg transition border border-slate-700 cursor-pointer"
              >
                Sign In
              </button>
              <button
                onClick={() => onOpenAuthModal('signup')}
                className="px-3.5 py-1.5 text-xs font-semibold text-slate-950 bg-gradient-to-r from-teal-400 to-cyan-500 hover:from-teal-300 hover:to-cyan-400 rounded-lg transition shadow-sm cursor-pointer"
              >
                Sign Up
              </button>
            </div>
          )}
        </div>
      )}

      {/* Main Ledger Content */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-8">
        {/* Left Column: Filterable History List (7 cols) */}
        <div className="lg:col-span-7 space-y-4">
          {/* Search Filter Bar */}
          <div className="flex items-center justify-between gap-3">
            <div className="relative flex-1">
              <input
                type="text"
                value={searchFilter}
                onChange={(e) => setSearchFilter(e.target.value)}
                placeholder="Filter by question, collection, or disposition..."
                className="w-full px-3.5 py-2 pl-9 bg-slate-900/80 border border-slate-800 rounded-xl text-xs text-white placeholder-slate-500 focus:border-cyan-500 focus:outline-none transition"
              />
              <span className="absolute left-3 top-2.5 text-slate-500 text-xs">🔍</span>
            </div>
            <span className="text-xs text-slate-400 shrink-0">
              {filteredItems.length} of {totalCount} records
            </span>
          </div>

          {error && (
            <div className="p-3 bg-rose-950/60 border border-rose-800/80 rounded-xl text-xs text-rose-300">
              {error}
            </div>
          )}

          {isLoading ? (
            <div className="p-12 text-center text-slate-400 text-xs animate-pulse">
              Loading research history...
            </div>
          ) : filteredItems.length === 0 ? (
            <div className="p-12 text-center rounded-2xl border border-dashed border-slate-800 text-xs text-slate-400 space-y-2">
              <div className="text-2xl">📜</div>
              <p className="font-semibold text-slate-300">No Query History Records Found</p>
              <p className="text-slate-500 max-w-sm mx-auto">
                {searchFilter
                  ? 'No past inquiries matched your search criteria.'
                  : 'Start asking questions in the Ask AI tab to build your research ledger.'}
              </p>
            </div>
          ) : (
            <div className="space-y-3">
              {filteredItems.map((item) => {
                const isSelected = selectedDetail?.id === item.id;
                const isAbstention = item.disposition.includes('abstention');
                const isVerified = item.disposition.includes('verified');

                return (
                  <div
                    key={item.id}
                    onClick={() => handleInspectDetail(item)}
                    className={`p-4 rounded-xl border transition cursor-pointer flex flex-col justify-between gap-3 ${
                      isSelected
                        ? 'bg-slate-900 border-cyan-500/80 shadow-lg shadow-cyan-950/20'
                        : 'bg-slate-900/50 border-slate-800/80 hover:border-slate-700 hover:bg-slate-900/80'
                    }`}
                  >
                    <div>
                      <div className="flex items-start justify-between gap-3 mb-1.5">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="px-2 py-0.5 rounded text-[10px] font-mono text-cyan-400 bg-cyan-950/80 border border-cyan-800/60">
                            {item.collection_id}
                          </span>
                          <span
                            className={`px-2 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider border ${
                              isVerified
                                ? 'bg-emerald-950/80 text-emerald-300 border-emerald-800/60'
                                : isAbstention
                                ? 'bg-amber-950/80 text-amber-300 border-amber-800/60'
                                : 'bg-slate-800 text-slate-300 border-slate-700'
                            }`}
                          >
                            {item.disposition.replace(/_/g, ' ')}
                          </span>
                        </div>
                        <span className="text-[11px] text-slate-500 font-mono shrink-0">
                          {new Date(item.created_at).toLocaleTimeString([], {
                            hour: '2-digit',
                            minute: '2-digit',
                          })}
                        </span>
                      </div>

                      <h3 className="text-xs sm:text-sm font-semibold text-slate-100 leading-snug">
                        {item.query_text}
                      </h3>

                      {item.answer_preview && (
                        <p className="text-xs text-slate-400 mt-2 line-clamp-2 leading-relaxed">
                          {item.answer_preview}
                        </p>
                      )}
                    </div>

                    <div className="pt-2.5 border-t border-slate-800/80 flex items-center justify-between text-xs text-slate-400">
                      <span>
                        {(item.citation_count ?? item.verified_excerpts_count) > 0
                          ? `📑 ${item.citation_count ?? item.verified_excerpts_count} verified citation(s)`
                          : 'Zero verified citations'}
                      </span>
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          onReplayQuery(item.query_text, item.collection_id);
                        }}
                        className="px-2 py-1 bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white rounded text-[11px] font-medium transition cursor-pointer flex items-center gap-1 border border-slate-700"
                        title="Replay query in Ask AI"
                      >
                        <span>Replay</span>
                        <span>&rarr;</span>
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Right Column: Selected Record Detail & Evidence Inspector (5 cols) */}
        <div className="lg:col-span-5">
          <div className="bg-slate-900/60 border border-slate-800 rounded-2xl p-5 sticky top-24 space-y-4">
            <h3 className="text-sm font-semibold text-white flex items-center gap-2 pb-3 border-b border-slate-800">
              <span>🔍</span>
              <span>Evidence &amp; Citation Inspector</span>
            </h3>

            {detailError && (
              <div className="p-3 bg-rose-950/60 border border-rose-800/80 rounded-lg text-xs text-rose-300">
                {detailError}
              </div>
            )}

            {isDetailLoading ? (
              <div className="p-8 text-center text-xs text-slate-400 animate-pulse">
                Loading query details...
              </div>
            ) : !selectedDetail ? (
              <div className="p-8 text-center text-xs text-slate-400 space-y-1">
                <p className="font-semibold text-slate-300">No Record Selected</p>
                <p className="text-slate-500">
                  Click any query record on the left to inspect its complete verified evidence and citations.
                </p>
              </div>
            ) : (
              <div className="space-y-4 text-xs">
                <div>
                  <label className="text-[11px] font-medium text-slate-400 uppercase tracking-wider block">
                    Query Inquired
                  </label>
                  <p className="mt-1 font-semibold text-slate-200 bg-slate-950/80 p-3 rounded-xl border border-slate-800/80 leading-relaxed">
                    {selectedDetail.query_text}
                  </p>
                </div>

                <div className="grid grid-cols-2 gap-3 text-slate-300">
                  <div className="bg-slate-950/50 p-2.5 rounded-lg border border-slate-800/80">
                    <span className="text-[10px] text-slate-500 uppercase block">Collection</span>
                    <span className="font-mono text-cyan-400 font-semibold">{selectedDetail.collection_id}</span>
                  </div>
                  <div className="bg-slate-950/50 p-2.5 rounded-lg border border-slate-800/80">
                    <span className="text-[10px] text-slate-500 uppercase block">Disposition</span>
                    <span className="font-mono text-teal-400 font-semibold truncate block">
                      {selectedDetail.disposition}
                    </span>
                  </div>
                </div>

                {/* Replay CTA */}
                <button
                  onClick={() => onReplayQuery(selectedDetail.query_text, selectedDetail.collection_id)}
                  className="w-full py-2 bg-gradient-to-r from-teal-500 to-cyan-600 hover:from-teal-400 hover:to-cyan-500 text-slate-950 font-bold rounded-xl transition shadow cursor-pointer flex items-center justify-center gap-1.5"
                >
                  <span>Re-execute in Ask AI</span>
                  <span>&rarr;</span>
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
