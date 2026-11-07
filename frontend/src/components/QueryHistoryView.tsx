import { useState, useEffect } from 'react';
import { useAuth } from '../context/AuthContext';
import { fetchQueryHistory, fetchQueryHistoryDetail } from '../api/client';
import type { QueryHistoryItem, QueryHistoryDetail } from '../types/history';

interface QueryHistoryViewProps {
  onReplayQuery: (queryText: string, collectionId: string) => void;
  onOpenAuthModal?: (mode: 'login' | 'signup') => void;
}

export function QueryHistoryView({
  onReplayQuery,
  onOpenAuthModal,
}: QueryHistoryViewProps) {
  const { token, isAuthenticated } = useAuth();

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
    if (!token) return;
    setIsLoading(true);
    setError(null);

    try {
      const res = await fetchQueryHistory(token, { limit: 50 });
      setHistoryItems(res.items);
      setTotalCount(res.total);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load query history.');
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    if (isAuthenticated && token) {
      loadHistory();
    } else {
      setHistoryItems([]);
      setTotalCount(0);
      setSelectedDetail(null);
    }
  }, [isAuthenticated, token]);

  const handleInspectDetail = async (id: string) => {
    if (!token) return;
    setIsDetailLoading(true);
    setDetailError(null);
    try {
      const detail = await fetchQueryHistoryDetail(id, token);
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
              <span>Audit &amp; Research History Ledger</span>
            </div>
            <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-white">
              Persistent Query History &amp; Citations
            </h1>
            <p className="text-sm text-slate-300 mt-1 max-w-3xl leading-relaxed">
              Review past inquiries, synthesis dispositions, verified citations, and evidence passages.
              Re-run previous queries or inspect granular citation boundaries.
            </p>
          </div>

          {isAuthenticated && (
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

      {/* Unauthenticated Prompt */}
      {!isAuthenticated && (
        <div className="rounded-xl border border-cyan-900/60 bg-gradient-to-r from-cyan-950/30 via-slate-900/40 to-slate-950 p-6 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
          <div className="space-y-1">
            <h3 className="text-sm font-semibold text-cyan-300 flex items-center gap-2">
              <span>🔒</span>
              <span>Sign In Required for Persistent Query History</span>
            </h3>
            <p className="text-xs text-slate-400">
              Query history and cryptographic citation verification records are stored per authenticated user account. Sign in to access your persistent research records.
            </p>
          </div>
          {onOpenAuthModal && (
            <div className="flex items-center gap-2 shrink-0">
              <button
                onClick={() => onOpenAuthModal('login')}
                className="px-4 py-2 text-xs font-semibold text-slate-950 bg-gradient-to-r from-teal-400 to-cyan-500 hover:from-teal-300 hover:to-cyan-400 rounded-lg transition cursor-pointer"
              >
                Sign In
              </button>
            </div>
          )}
        </div>
      )}

      {isAuthenticated && (
        <div className="space-y-6">
          {/* Search / Filter Bar */}
          <div className="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-3">
            <div className="relative flex-1 max-w-md">
              <span className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none text-slate-400 text-xs">
                🔍
              </span>
              <input
                type="text"
                value={searchFilter}
                onChange={(e) => setSearchFilter(e.target.value)}
                placeholder="Filter by question, collection, or keyword..."
                className="w-full pl-8 pr-3 py-2 bg-slate-900/80 border border-slate-800 rounded-xl text-xs text-white placeholder-slate-500 focus:border-cyan-500 focus:outline-none"
              />
            </div>
            <div className="text-xs text-slate-400 self-center">
              Showing {filteredItems.length} of {totalCount} records
            </div>
          </div>

          {error && (
            <div className="p-4 bg-rose-950/60 border border-rose-800/80 rounded-xl text-xs text-rose-300">
              {error}
            </div>
          )}

          {isLoading ? (
            <div className="p-12 text-center text-sm text-slate-400 animate-pulse bg-slate-900/30 rounded-xl border border-slate-800/80">
              Loading query history ledger...
            </div>
          ) : filteredItems.length === 0 ? (
            <div className="p-12 text-center rounded-xl border border-dashed border-slate-800 text-slate-400 bg-slate-900/20">
              <div className="text-3xl mb-2">📜</div>
              <p className="text-sm font-semibold text-slate-300">No Query History Records Found</p>
              <p className="text-xs text-slate-500 mt-1">
                {searchFilter ? 'No inquiries match your filter criteria.' : 'Ask questions in the AI Research tab to establish your citations ledger.'}
              </p>
            </div>
          ) : (
            <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
              {/* History Items List (7 cols) */}
              <div className="lg:col-span-7 space-y-3">
                {filteredItems.map((item) => {
                  const isSelected = selectedDetail?.id === item.id;
                  const isSebi = item.collection_id === 'sebi_cscrf';
                  return (
                    <div
                      key={item.id}
                      className={`p-4 rounded-xl border transition flex flex-col justify-between gap-3 ${
                        isSelected
                          ? 'bg-slate-900/90 border-cyan-600/80 shadow-md shadow-cyan-950/30'
                          : 'bg-slate-900/50 border-slate-800/80 hover:border-slate-700'
                      }`}
                    >
                      <div className="space-y-1.5">
                        <div className="flex items-center justify-between gap-2">
                          <span
                            className={`px-2 py-0.5 rounded text-[10px] font-semibold uppercase font-mono border ${
                              isSebi
                                ? 'bg-rose-950/60 text-rose-300 border-rose-800/60'
                                : 'bg-cyan-950/60 text-cyan-300 border-cyan-800/60'
                            }`}
                          >
                            {item.collection_id}
                          </span>
                          <span className="text-[11px] text-slate-500">
                            {new Date(item.created_at).toLocaleDateString()} &bull;{' '}
                            {new Date(item.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                          </span>
                        </div>

                        <h3 className="font-semibold text-white text-sm leading-snug">
                          {item.query_text}
                        </h3>

                        <p className="text-xs text-slate-400">
                          Disposition: <span className="font-medium text-slate-200">{item.disposition.replace(/_/g, ' ')}</span>
                        </p>
                      </div>

                      <div className="pt-3 border-t border-slate-800/60 flex items-center justify-between gap-2">
                        <div className="flex items-center gap-2">
                          <span className="px-2 py-0.5 rounded text-[10px] font-medium bg-slate-800 text-slate-300 border border-slate-700">
                            {item.disposition.replace(/_/g, ' ')}
                          </span>
                          <span className="text-[11px] text-slate-400">
                            {item.verified_excerpts_count} citations
                          </span>
                        </div>

                        <div className="flex items-center gap-1.5">
                          <button
                            type="button"
                            onClick={() => handleInspectDetail(item.id)}
                            className="px-2.5 py-1 text-xs text-slate-300 hover:text-white bg-slate-800 hover:bg-slate-700 rounded-lg transition border border-slate-700 cursor-pointer"
                          >
                            Inspect
                          </button>
                          <button
                            type="button"
                            onClick={() => onReplayQuery(item.query_text, item.collection_id)}
                            className="px-2.5 py-1 text-xs font-semibold text-cyan-300 hover:text-white bg-cyan-950/60 hover:bg-cyan-900/60 border border-cyan-800/60 rounded-lg transition cursor-pointer flex items-center gap-1"
                          >
                            <span>Re-run</span>
                            <span>&rarr;</span>
                          </button>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>

              {/* Detail Inspector Pane (5 cols) */}
              <div className="lg:col-span-5">
                <div className="sticky top-20 bg-slate-900/80 border border-slate-800 rounded-xl p-5 space-y-4">
                  <div className="flex items-center justify-between border-b border-slate-800/80 pb-3">
                    <h3 className="font-semibold text-white text-sm flex items-center gap-2">
                      <span>🔍</span>
                      <span>Citation &amp; Evidence Inspector</span>
                    </h3>
                    {selectedDetail && (
                      <button
                        onClick={() => setSelectedDetail(null)}
                        className="text-xs text-slate-400 hover:text-slate-200 cursor-pointer"
                      >
                        ✕ Close
                      </button>
                    )}
                  </div>

                  {isDetailLoading ? (
                    <div className="p-8 text-center text-xs text-slate-400 animate-pulse">
                      Retrieving verified citations and metadata...
                    </div>
                  ) : detailError ? (
                    <div className="p-3 bg-rose-950/60 border border-rose-800/80 rounded-lg text-xs text-rose-300">
                      {detailError}
                    </div>
                  ) : selectedDetail ? (
                    <div className="space-y-4 max-h-[70vh] overflow-y-auto pr-1">
                      <div>
                        <span className="text-[11px] font-medium text-slate-400 uppercase tracking-wider block">
                          Question
                        </span>
                        <p className="text-xs font-semibold text-white mt-0.5">
                          {selectedDetail.query_text}
                        </p>
                      </div>

                      <div>
                        <span className="text-[11px] font-medium text-slate-400 uppercase tracking-wider block">
                          Grounded Answer
                        </span>
                        <p className="text-xs text-slate-200 mt-0.5 leading-relaxed bg-slate-950/60 p-3 rounded-lg border border-slate-800">
                          {selectedDetail.answer_text || selectedDetail.summary_narrative || 'No synthesis produced.'}
                        </p>
                      </div>

                      {selectedDetail.claim_citations && selectedDetail.claim_citations.length > 0 && (
                        <div>
                          <span className="text-[11px] font-medium text-slate-400 uppercase tracking-wider block mb-2">
                            Claim Citations ({selectedDetail.claim_citations.length})
                          </span>
                          <div className="space-y-2">
                            {selectedDetail.claim_citations.map((cite, idx) => (
                              <div
                                key={idx}
                                className="p-2.5 rounded-lg bg-slate-950/80 border border-slate-800/80 text-xs space-y-1.5"
                              >
                                <div className="flex items-center justify-between text-[11px] text-cyan-400">
                                  <span className="font-semibold">{String(cite.document_id || cite.claim_text || `Citation ${idx + 1}`)}</span>
                                </div>
                                {cite.excerpt_text ? (
                                  <p className="text-slate-300 text-[11px] leading-relaxed italic">
                                    &ldquo;{String(cite.excerpt_text)}&rdquo;
                                  </p>
                                ) : null}
                              </div>
                            ))}
                          </div>
                        </div>
                      )}

                      <div className="pt-3 border-t border-slate-800 flex items-center justify-between">
                        <button
                          type="button"
                          onClick={() => onReplayQuery(selectedDetail.query_text, selectedDetail.collection_id)}
                          className="w-full py-2 bg-gradient-to-r from-teal-500 to-cyan-600 hover:from-teal-400 hover:to-cyan-500 text-slate-950 font-semibold text-xs rounded-lg transition shadow cursor-pointer flex items-center justify-center gap-1.5"
                        >
                          <span>🔍 Load &amp; Re-run in AI Research</span>
                        </button>
                      </div>
                    </div>
                  ) : (
                    <div className="p-8 text-center text-xs text-slate-500">
                      Select a query from the ledger to inspect verified excerpts, page citations, and synthesis metadata.
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
