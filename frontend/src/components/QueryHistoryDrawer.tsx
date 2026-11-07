import { useState, useEffect, useCallback, useRef } from 'react';
import { useAuth } from '../context/AuthContext';
import type { QueryHistoryItem, QueryHistoryDetail } from '../types/history';
import { fetchQueryHistory, fetchQueryHistoryDetail } from '../api/client';

interface QueryHistoryDrawerProps {
  isOpen: boolean;
  onClose: () => void;
  onReplayQuery?: (queryText: string, collectionId: string) => void;
}

const PAGE_SIZE = 10;

export function QueryHistoryDrawer({ isOpen, onClose, onReplayQuery }: QueryHistoryDrawerProps) {
  const { token, user, isAuthenticated } = useAuth();

  const historyRequestIdRef = useRef<number>(0);
  const detailRequestIdRef = useRef<number>(0);
  const activeUserIdRef = useRef<string | null>(isAuthenticated && user ? user.id : null);

  const [historyItems, setHistoryItems] = useState<QueryHistoryItem[]>([]);
  const [totalCount, setTotalCount] = useState<number>(0);
  const [offset, setOffset] = useState<number>(0);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // Selected item detail
  const [selectedDetail, setSelectedDetail] = useState<QueryHistoryDetail | null>(null);
  const [isDetailLoading, setIsDetailLoading] = useState<boolean>(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const resetAllHistoryState = useCallback(() => {
    historyRequestIdRef.current++;
    detailRequestIdRef.current++;
    setHistoryItems([]);
    setTotalCount(0);
    setOffset(0);
    setSelectedDetail(null);
    setIsLoading(false);
    setError(null);
    setIsDetailLoading(false);
    setDetailError(null);
  }, []);

  // When authentication ends or the active user changes, immediately clear all history state
  useEffect(() => {
    const currentId = isAuthenticated && user ? user.id : null;
    if (!isAuthenticated || currentId !== activeUserIdRef.current) {
      resetAllHistoryState();
      activeUserIdRef.current = currentId;
    }
  }, [isAuthenticated, user, resetAllHistoryState]);

  const loadHistory = useCallback(
    async (pageOffset: number) => {
      const reqId = ++historyRequestIdRef.current;
      const reqUserId = user?.id;

      if (!token || !isAuthenticated || !reqUserId) {
        resetAllHistoryState();
        return;
      }

      setIsLoading(true);
      setError(null);

      try {
        const res = await fetchQueryHistory(token, {
          limit: PAGE_SIZE,
          offset: pageOffset,
        });
        if (
          reqId !== historyRequestIdRef.current ||
          !isAuthenticated ||
          activeUserIdRef.current !== reqUserId
        ) {
          return;
        }
        setHistoryItems(res.items);
        setTotalCount(res.total);
        setOffset(pageOffset);
      } catch (err: unknown) {
        if (
          reqId !== historyRequestIdRef.current ||
          !isAuthenticated ||
          activeUserIdRef.current !== reqUserId
        ) {
          return;
        }
        setError(err instanceof Error ? err.message : 'Failed to load query history.');
      } finally {
        if (
          reqId === historyRequestIdRef.current &&
          isAuthenticated &&
          activeUserIdRef.current === reqUserId
        ) {
          setIsLoading(false);
        }
      }
    },
    [token, isAuthenticated, user?.id, resetAllHistoryState]
  );

  useEffect(() => {
    if (isOpen && isAuthenticated) {
      loadHistory(0);
      setSelectedDetail(null);
    }
  }, [isOpen, isAuthenticated, loadHistory]);

  const handleSelectRecord = async (item: QueryHistoryItem) => {
    const reqId = ++detailRequestIdRef.current;
    const reqUserId = user?.id;

    if (!token || !isAuthenticated || !reqUserId) {
      setSelectedDetail(null);
      setIsDetailLoading(false);
      return;
    }

    setIsDetailLoading(true);
    setDetailError(null);

    try {
      const detail = await fetchQueryHistoryDetail(item.id, token);
      if (
        reqId !== detailRequestIdRef.current ||
        !isAuthenticated ||
        activeUserIdRef.current !== reqUserId
      ) {
        return;
      }
      setSelectedDetail(detail);
    } catch (err: unknown) {
      if (
        reqId !== detailRequestIdRef.current ||
        !isAuthenticated ||
        activeUserIdRef.current !== reqUserId
      ) {
        return;
      }
      setDetailError(err instanceof Error ? err.message : 'Failed to load record detail.');
    } finally {
      if (
        reqId === detailRequestIdRef.current &&
        isAuthenticated &&
        activeUserIdRef.current === reqUserId
      ) {
        setIsDetailLoading(false);
      }
    }
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-slate-950/70 backdrop-blur-sm animate-fadeIn">
      <div
        className="w-full max-w-lg bg-slate-900 border-l border-slate-800 shadow-2xl flex flex-col h-full text-slate-100"
        role="dialog"
        aria-modal="true"
        aria-labelledby="history-drawer-title"
      >
        {/* Header */}
        <div className="px-5 py-4 border-b border-slate-800 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="text-cyan-400 font-mono text-sm">📜</span>
            <h2 id="history-drawer-title" className="text-base font-semibold text-white">
              Persistent Query History
            </h2>
            {totalCount > 0 && (
              <span className="px-2 py-0.5 rounded text-[11px] font-medium bg-cyan-950 text-cyan-400 border border-cyan-800/60">
                {totalCount} total
              </span>
            )}
          </div>
          <button
            onClick={onClose}
            className="text-slate-400 hover:text-slate-200 transition p-1 rounded-lg cursor-pointer"
            aria-label="Close query history"
          >
            ✕
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-4 flex flex-col gap-3">
          {!isAuthenticated ? (
            <div className="p-6 text-center flex flex-col items-center justify-center gap-3 my-auto">
              <div className="w-12 h-12 rounded-full bg-cyan-950/60 border border-cyan-800/80 flex items-center justify-center text-cyan-400 text-xl">
                🔒
              </div>
              <h3 className="text-sm font-semibold text-slate-200">
                Authentication Required
              </h3>
              <p className="text-xs text-slate-400 max-w-xs leading-relaxed">
                Persistent query history is private to authenticated accounts. Sign in to retrieve your questions, answers, and citations across sessions.
              </p>
            </div>
          ) : isDetailLoading ? (
            <div className="py-8 text-center text-xs text-slate-400">Loading record detail...</div>
          ) : detailError ? (
            <div className="p-3 bg-rose-950/70 border border-rose-800/80 rounded-xl text-rose-300 text-xs">
              {detailError}
            </div>
          ) : selectedDetail ? (
            /* Record Detail View */
            <div className="flex flex-col gap-3 text-xs">
              <button
                onClick={() => setSelectedDetail(null)}
                className="self-start flex items-center gap-1.5 text-cyan-400 hover:text-cyan-300 font-medium py-1 cursor-pointer"
              >
                ← Back to History List
              </button>

              <div className="p-3.5 bg-slate-950 border border-slate-800 rounded-xl flex flex-col gap-2">
                <div className="flex items-center justify-between text-[11px] text-slate-400">
                  <span className="font-mono text-cyan-400">{selectedDetail.collection_id}</span>
                  <span>{new Date(selectedDetail.created_at).toLocaleString()}</span>
                </div>
                <h3 className="text-sm font-medium text-white">{selectedDetail.query_text}</h3>
                <div className="flex items-center gap-2 pt-1 border-t border-slate-900">
                  <span className="px-2 py-0.5 rounded text-[10px] font-semibold uppercase bg-slate-900 border border-slate-800 text-slate-300">
                    {selectedDetail.disposition}
                  </span>
                  <span className="text-[11px] text-slate-400">
                    {selectedDetail.verified_excerpts_count} excerpt(s) verified
                  </span>
                </div>
              </div>

              {selectedDetail.summary_narrative && (
                <div className="p-3 bg-slate-950/70 border border-slate-800/80 rounded-xl">
                  <div className="text-[10px] uppercase font-semibold text-slate-400 mb-1">
                    Summary Narrative
                  </div>
                  <p className="text-slate-200 leading-relaxed">{selectedDetail.summary_narrative}</p>
                </div>
              )}

              {selectedDetail.answer_text ? (
                <div className="p-3.5 bg-teal-950/30 border border-teal-800/50 rounded-xl flex flex-col gap-1.5">
                  <div className="text-[10px] uppercase font-semibold text-teal-400 tracking-wider">
                    Direct Grounded Answer
                  </div>
                  <p className="text-slate-100 leading-relaxed whitespace-pre-wrap">
                    {selectedDetail.answer_text}
                  </p>
                </div>
              ) : (
                <div className="p-3 bg-slate-950/40 border border-slate-800/60 rounded-xl text-slate-400">
                  No direct answer text stored for this disposition.
                </div>
              )}

              {selectedDetail.claim_citations && selectedDetail.claim_citations.length > 0 && (
                <div className="flex flex-col gap-2">
                  <div className="text-[10px] uppercase font-semibold text-slate-400">
                    Citations ({selectedDetail.claim_citations.length})
                  </div>
                  <div className="flex flex-col gap-2">
                    {selectedDetail.claim_citations.map((c, idx) => (
                      <div
                        key={idx}
                        className="p-2.5 bg-slate-950 border border-slate-800/80 rounded-lg text-[11px]"
                      >
                        <p className="text-slate-300 font-medium">{String(c.claim_text || '')}</p>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {onReplayQuery && (
                <button
                  onClick={() => {
                    onReplayQuery(selectedDetail.query_text, selectedDetail.collection_id);
                    onClose();
                  }}
                  className="mt-2 py-2 px-4 bg-slate-800 hover:bg-slate-700 text-slate-200 font-medium rounded-xl text-xs transition cursor-pointer border border-slate-700"
                >
                  Run Query Again
                </button>
              )}
            </div>
          ) : (
            /* History Items List */
            <div className="flex flex-col gap-2.5">
              {isLoading ? (
                <div className="py-8 text-center text-xs text-slate-400">Loading history...</div>
              ) : error ? (
                <div className="p-3 bg-rose-950/70 border border-rose-800/80 rounded-xl text-rose-300 text-xs">
                  {error}
                </div>
              ) : historyItems.length === 0 ? (
                <div className="py-12 text-center text-xs text-slate-500">
                  No historical queries found. Run a query to populate history!
                </div>
              ) : (
                historyItems.map((item) => (
                  <button
                    key={item.id}
                    onClick={() => handleSelectRecord(item)}
                    className="w-full text-left p-3 bg-slate-950 hover:bg-slate-900 border border-slate-800 hover:border-cyan-800/70 rounded-xl transition cursor-pointer flex flex-col gap-1.5"
                  >
                    <div className="flex items-center justify-between text-[10px]">
                      <span className="font-mono text-cyan-400 bg-cyan-950/80 px-1.5 py-0.5 rounded border border-cyan-900/60 truncate max-w-[140px]">
                        {item.collection_id}
                      </span>
                      <span className="text-slate-500">
                        {new Date(item.created_at).toLocaleDateString()}
                      </span>
                    </div>
                    <span className="text-xs font-medium text-slate-200 line-clamp-2">
                      {item.query_text}
                    </span>
                    <div className="flex items-center gap-2 text-[10px] text-slate-400 pt-0.5">
                      <span className="capitalize">{item.disposition.replace(/_/g, ' ').toLowerCase()}</span>
                      <span>•</span>
                      <span>{item.verified_excerpts_count} verified excerpt(s)</span>
                    </div>
                  </button>
                ))
              )}
            </div>
          )}
        </div>

        {/* Footer with Pagination */}
        {isAuthenticated && !selectedDetail && totalCount > PAGE_SIZE && (
          <div className="p-3 border-t border-slate-800 flex items-center justify-between text-xs text-slate-400">
            <button
              onClick={() => loadHistory(Math.max(0, offset - PAGE_SIZE))}
              disabled={offset === 0 || isLoading}
              className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 disabled:opacity-40 disabled:cursor-not-allowed rounded-lg text-slate-200 transition cursor-pointer"
            >
              Previous
            </button>
            <span>
              {offset + 1}–{Math.min(offset + PAGE_SIZE, totalCount)} of {totalCount}
            </span>
            <button
              onClick={() => loadHistory(offset + PAGE_SIZE)}
              disabled={offset + PAGE_SIZE >= totalCount || isLoading}
              className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 disabled:opacity-40 disabled:cursor-not-allowed rounded-lg text-slate-200 transition cursor-pointer"
            >
              Next
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
