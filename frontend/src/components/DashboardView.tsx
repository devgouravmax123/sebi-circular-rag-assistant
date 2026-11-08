import type { CollectionSummary } from '../types/query';
import { useAuth } from '../context/AuthContext';
import { useWorkspace } from '../context/WorkspaceContext';
import type { NavTab } from './Header';

interface DashboardViewProps {
  collections: CollectionSummary[];
  onNavigate: (tab: NavTab) => void;
  onSelectCollectionAndResearch: (collectionId: string) => void;
  onOpenAuthModal?: (mode: 'login' | 'signup') => void;
}

export function DashboardView({
  collections,
  onNavigate,
  onSelectCollectionAndResearch,
  onOpenAuthModal,
}: DashboardViewProps) {
  const { isAuthenticated, isGuest, guestSession } = useAuth();
  const { activeWorkspace, workspaceCollections } = useWorkspace();

  const publicCollections = collections.filter((c) => !c.is_private);

  // Format seconds remaining for guest
  const timeRemainingSeconds = guestSession?.time_remaining_seconds || 86400;
  const hoursLeft = Math.floor(timeRemainingSeconds / 3600);
  const minutesLeft = Math.floor((timeRemainingSeconds % 3600) / 60);

  return (
    <div className="space-y-8 animate-fadeIn">
      {/* Guest Mode Alert Banner (if in guest mode) */}
      {isGuest && (
        <section className="rounded-2xl border border-amber-800/80 bg-gradient-to-r from-amber-950/40 via-slate-900/60 to-amber-950/30 p-5 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4 shadow-lg">
          <div className="space-y-1">
            <div className="flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
              <h3 className="text-sm font-semibold text-amber-300">
                Ephemeral Guest Mode Active ({hoursLeft}h {minutesLeft}m remaining)
              </h3>
            </div>
            <p className="text-xs text-slate-300">
              Your sandbox is temporary. You have used {guestSession?.documents_count || 0}/2 documents and {guestSession?.queries_count || 0}/10 research queries. Data is automatically deleted in 24 hours or on demand.
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button
              onClick={() => onNavigate('session')}
              className="px-3.5 py-1.5 text-xs font-semibold text-amber-300 hover:text-white bg-amber-950/80 hover:bg-amber-900/80 border border-amber-800/80 rounded-lg transition cursor-pointer"
            >
              Session &amp; Quotas
            </button>
            {onOpenAuthModal && (
              <button
                onClick={() => onOpenAuthModal('signup')}
                className="px-3.5 py-1.5 text-xs font-semibold text-slate-950 bg-gradient-to-r from-teal-400 to-cyan-500 hover:from-teal-300 hover:to-cyan-400 rounded-lg transition shadow-sm cursor-pointer"
              >
                Save Free Account
              </button>
            )}
          </div>
        </section>
      )}

      {/* Platform Hero Banner */}
      <section className="relative overflow-hidden rounded-2xl bg-gradient-to-br from-slate-900 via-slate-900/90 to-slate-950 border border-slate-800/80 p-6 sm:p-8 shadow-xl shadow-slate-950/60">
        <div className="absolute top-0 right-0 -mt-8 -mr-8 w-64 h-64 bg-cyan-500/10 rounded-full blur-3xl pointer-events-none" />
        <div className="absolute bottom-0 left-0 -mb-8 -ml-8 w-64 h-64 bg-teal-500/10 rounded-full blur-3xl pointer-events-none" />

        <div className="relative z-10 flex flex-col md:flex-row md:items-center justify-between gap-6">
          <div className="space-y-3 max-w-2xl">
            <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-cyan-950/80 border border-cyan-800/60 text-xs font-medium text-cyan-400">
              <span className="w-1.5 h-1.5 rounded-full bg-cyan-400 animate-pulse" />
              Document Intelligence &amp; Research Platform
            </div>
            <h1 className="text-2xl sm:text-3xl lg:text-4xl font-bold tracking-tight text-white">
              AI-Powered Evidence Synthesis &amp; Multi-Tenant Research
            </h1>
            <p className="text-sm sm:text-base text-slate-300 leading-relaxed">
              Turn your documents into answers. Evidence-grounded document intelligence with exact page citations,
              private document collections, and strict hallucination defense.
            </p>
          </div>

          <div className="flex flex-col sm:flex-row md:flex-col gap-3 shrink-0">
            <button
              onClick={() => onNavigate('research')}
              className="px-5 py-2.5 bg-gradient-to-r from-teal-500 to-cyan-600 hover:from-teal-400 hover:to-cyan-500 text-slate-950 font-semibold text-sm rounded-xl transition shadow-lg shadow-teal-500/20 cursor-pointer flex items-center justify-center gap-2"
            >
              <span>🔍</span>
              <span>Ask AI</span>
            </button>
            <button
              onClick={() => onNavigate('collections')}
              className="px-5 py-2.5 bg-slate-800/90 hover:bg-slate-700/90 text-slate-200 font-medium text-sm rounded-xl transition border border-slate-700 cursor-pointer flex items-center justify-center gap-2"
            >
              <span>📁</span>
              <span>My Documents</span>
            </button>
          </div>
        </div>
      </section>

      {/* Workspace Context & Metrics Grid */}
      <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {/* Workspace Card */}
        <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-4 flex flex-col justify-between">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-medium text-slate-400">Active Workspace</span>
            <span className="text-base">{isGuest ? '⏳' : '🏢'}</span>
          </div>
          <div>
            <div className="text-base font-semibold text-white truncate">
              {isGuest
                ? 'Ephemeral Guest Sandbox'
                : isAuthenticated
                ? activeWorkspace?.name || 'Default Workspace'
                : 'Demo Mode'}
            </div>
            <p className="text-xs text-slate-400 truncate mt-0.5">
              {isGuest
                ? `${hoursLeft}h ${minutesLeft}m remaining`
                : isAuthenticated
                ? activeWorkspace?.slug ? `@${activeWorkspace.slug}` : 'Private Workspace'
                : 'Public Access'}
            </p>
          </div>
          <div className="mt-3 pt-3 border-t border-slate-800/80 flex items-center justify-between">
            <span className="text-[11px] text-slate-400">Status</span>
            <span
              className={`px-2 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider ${
                isGuest
                  ? 'bg-amber-950/80 text-amber-300 border border-amber-800/60'
                  : 'bg-cyan-950/80 text-cyan-400 border border-cyan-800/60'
              }`}
            >
              {isGuest ? 'Temporary' : isAuthenticated ? 'Persistent' : 'Public'}
            </span>
          </div>
        </div>

        {/* Collections Count */}
        <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-4 flex flex-col justify-between">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-medium text-slate-400">Available Collections</span>
            <span className="text-base">📚</span>
          </div>
          <div>
            <div className="text-2xl font-bold text-white">{collections.length}</div>
            <p className="text-xs text-slate-400 mt-0.5">
              {publicCollections.length} public &bull; {workspaceCollections.length} private
            </p>
          </div>
          <div className="mt-3 pt-3 border-t border-slate-800/80 flex items-center justify-between">
            <span className="text-[11px] text-slate-400">Ready to Query</span>
            <span className="text-[11px] font-medium text-emerald-400">Active</span>
          </div>
        </div>

        {/* Verification Engine */}
        <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-4 flex flex-col justify-between">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-medium text-slate-400">Grounded Synthesis</span>
            <span className="text-base">🛡️</span>
          </div>
          <div>
            <div className="text-base font-semibold text-white">Citation Enforced</div>
            <p className="text-xs text-slate-400 mt-0.5">Mechanical quote verification before answering</p>
          </div>
          <div className="mt-3 pt-3 border-t border-slate-800/80 flex items-center justify-between">
            <span className="text-[11px] text-slate-400">Accuracy Gate</span>
            <span className="text-[11px] font-medium text-teal-400">Zero Hallucinations</span>
          </div>
        </div>

        {/* Document Ingestion */}
        <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-4 flex flex-col justify-between">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-medium text-slate-400">Document Processing</span>
            <span className="text-base">📄</span>
          </div>
          <div>
            <div className="text-base font-semibold text-white">PDF Parser &amp; Indexer</div>
            <p className="text-xs text-slate-400 mt-0.5">Asynchronous chunking &amp; embedding</p>
          </div>
          <div className="mt-3 pt-3 border-t border-slate-800/80 flex items-center justify-between">
            <span className="text-[11px] text-slate-400">Queue State</span>
            <span className="text-[11px] font-medium text-amber-400">Free Tier Queued</span>
          </div>
        </div>
      </section>

      {/* Quick Action Navigation Grid */}
      <section className="space-y-4">
        <h2 className="text-lg font-semibold text-white flex items-center gap-2">
          <span>⚡</span>
          <span>Quick Actions</span>
        </h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          {/* Action 1: Ask AI */}
          <div
            onClick={() => onNavigate('research')}
            className="group bg-slate-900/40 hover:bg-slate-900/80 border border-slate-800 hover:border-cyan-500/50 rounded-xl p-5 transition cursor-pointer flex flex-col justify-between"
          >
            <div>
              <div className="w-10 h-10 rounded-lg bg-cyan-950/80 border border-cyan-800/60 flex items-center justify-center text-lg mb-3 group-hover:scale-105 transition">
                🔍
              </div>
              <h3 className="font-semibold text-slate-100 group-hover:text-cyan-300 transition">
                Ask Questions
              </h3>
              <p className="text-xs text-slate-400 mt-1.5 leading-relaxed">
                Query indexed collections using natural language and receive evidence-grounded answers with citations.
              </p>
            </div>
            <div className="mt-4 pt-3 border-t border-slate-800/60 text-xs font-medium text-cyan-400 flex items-center gap-1 group-hover:translate-x-1 transition">
              <span>Start Inquiring</span>
              <span>&rarr;</span>
            </div>
          </div>

          {/* Action 2: Create Collection */}
          <div
            onClick={() => onNavigate('collections')}
            className="group bg-slate-900/40 hover:bg-slate-900/80 border border-slate-800 hover:border-teal-500/50 rounded-xl p-5 transition cursor-pointer flex flex-col justify-between"
          >
            <div>
              <div className="w-10 h-10 rounded-lg bg-teal-950/80 border border-teal-800/60 flex items-center justify-center text-lg mb-3 group-hover:scale-105 transition">
                📁
              </div>
              <h3 className="font-semibold text-slate-100 group-hover:text-teal-300 transition">
                Create Collection
              </h3>
              <p className="text-xs text-slate-400 mt-1.5 leading-relaxed">
                Manage your document collections, organize PDFs, and review ingestion pipelines.
              </p>
            </div>
            <div className="mt-4 pt-3 border-t border-slate-800/60 text-xs font-medium text-teal-400 flex items-center gap-1 group-hover:translate-x-1 transition">
              <span>Browse Documents</span>
              <span>&rarr;</span>
            </div>
          </div>

          {/* Action 3: Upload PDF Documents */}
          <div
            onClick={() => onNavigate('collections')}
            className="group bg-slate-900/40 hover:bg-slate-900/80 border border-slate-800 hover:border-indigo-500/50 rounded-xl p-5 transition cursor-pointer flex flex-col justify-between"
          >
            <div>
              <div className="w-10 h-10 rounded-lg bg-indigo-950/80 border border-indigo-800/60 flex items-center justify-center text-lg mb-3 group-hover:scale-105 transition">
                📄
              </div>
              <h3 className="font-semibold text-slate-100 group-hover:text-indigo-300 transition">
                Upload PDF Documents
              </h3>
              <p className="text-xs text-slate-400 mt-1.5 leading-relaxed">
                Upload PDFs into your collections. On free-tier Render, documents are queued for worker indexing.
              </p>
            </div>
            <div className="mt-4 pt-3 border-t border-slate-800/60 text-xs font-medium text-indigo-400 flex items-center gap-1 group-hover:translate-x-1 transition">
              <span>Upload Document</span>
              <span>&rarr;</span>
            </div>
          </div>

          {/* Action 4: Settings */}
          <div
            onClick={() => onNavigate(isGuest ? 'session' : 'workspace')}
            className="group bg-slate-900/40 hover:bg-slate-900/80 border border-slate-800 hover:border-amber-500/50 rounded-xl p-5 transition cursor-pointer flex flex-col justify-between"
          >
            <div>
              <div className="w-10 h-10 rounded-lg bg-amber-950/80 border border-amber-800/60 flex items-center justify-center text-lg mb-3 group-hover:scale-105 transition">
                {isGuest ? '⏳' : '⚙️'}
              </div>
              <h3 className="font-semibold text-slate-100 group-hover:text-amber-300 transition">
                {isGuest ? 'Guest Session' : 'Workspace & Team'}
              </h3>
              <p className="text-xs text-slate-400 mt-1.5 leading-relaxed">
                {isGuest
                  ? 'Inspect guest quotas, time remaining countdown, or end session to permanently delete data.'
                  : 'Manage team memberships, assign RBAC permissions, and configure workspaces.'}
              </p>
            </div>
            <div className="mt-4 pt-3 border-t border-slate-800/60 text-xs font-medium text-amber-400 flex items-center gap-1 group-hover:translate-x-1 transition">
              <span>{isGuest ? 'View Session' : 'Manage Workspace'}</span>
              <span>&rarr;</span>
            </div>
          </div>
        </div>
      </section>

      {/* Available Collections Showcase */}
      <section className="space-y-4">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
          <div>
            <h2 className="text-lg font-semibold text-white flex items-center gap-2">
              <span>📚</span>
              <span>Document Collections</span>
            </h2>
            <p className="text-xs text-slate-400 mt-0.5">
              Select any collection to immediately inspect evidence or begin natural-language research.
            </p>
          </div>
          <button
            onClick={() => onNavigate('collections')}
            className="text-xs font-medium text-cyan-400 hover:text-cyan-300 flex items-center gap-1 cursor-pointer self-start sm:self-auto"
          >
            <span>+ Add Collection</span>
            <span>&rarr;</span>
          </button>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {collections.map((col) => {
            const isSebi = col.policy_type === 'sebi_cscrf_strict';
            return (
              <div
                key={col.collection_id}
                className="bg-slate-900/50 border border-slate-800 hover:border-slate-700 rounded-xl p-5 flex flex-col justify-between transition"
              >
                <div>
                  <div className="flex items-start justify-between gap-2 mb-2">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="px-2 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider bg-slate-800 text-slate-300 border border-slate-700">
                        {col.is_private ? 'Private' : 'Public'}
                      </span>
                      <span
                        className={`px-2 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider border ${
                          isSebi
                            ? 'bg-rose-950/60 text-rose-300 border-rose-800/60'
                            : 'bg-cyan-950/60 text-cyan-300 border-cyan-800/60'
                        }`}
                      >
                        {isSebi ? 'Regulatory Policy' : 'General / Tech'}
                      </span>
                    </div>
                  </div>

                  <h3 className="font-semibold text-white text-base leading-snug">
                    {col.name}
                  </h3>
                  <p className="text-xs text-slate-400 font-mono mt-0.5">
                    {col.collection_id}
                  </p>
                  <p className="text-xs text-slate-300 mt-2 line-clamp-2 leading-relaxed">
                    {col.description || 'Collection of indexed documents for evidence-grounded research.'}
                  </p>
                </div>

                <div className="mt-4 pt-3 border-t border-slate-800/80 flex items-center justify-between">
                  <span className="text-[11px] text-slate-400">
                    Policy: <span className="font-mono text-slate-300">{col.policy_type}</span>
                  </span>
                  <button
                    onClick={() => onSelectCollectionAndResearch(col.collection_id)}
                    className="px-3 py-1.5 text-xs font-semibold text-cyan-300 hover:text-white bg-cyan-950/60 hover:bg-cyan-900/60 border border-cyan-800/60 rounded-lg transition cursor-pointer flex items-center gap-1"
                  >
                    <span>Ask Questions</span>
                    <span>&rarr;</span>
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      </section>

      {/* Upgrade CTA if anonymous or guest */}
      {!isAuthenticated && (
        <section className="rounded-xl border border-cyan-900/40 bg-gradient-to-r from-cyan-950/20 via-slate-900/40 to-teal-950/20 p-5 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
          <div className="space-y-1">
            <h3 className="text-sm font-semibold text-cyan-300 flex items-center gap-2">
              <span>🚀</span>
              <span>Unlock Persistent Workspaces &amp; Team Collaboration</span>
            </h3>
            <p className="text-xs text-slate-400">
              Create an account or sign in to establish persistent team workspaces, unlimited PDF uploads, and full audit logs.
            </p>
          </div>
          {onOpenAuthModal && (
            <div className="flex items-center gap-2 shrink-0">
              <button
                onClick={() => onOpenAuthModal('login')}
                className="px-3 py-1.5 text-xs font-medium text-slate-300 hover:text-white bg-slate-800 hover:bg-slate-700 rounded-lg transition border border-slate-700 cursor-pointer"
              >
                Sign In
              </button>
              <button
                onClick={() => onOpenAuthModal('signup')}
                className="px-3 py-1.5 text-xs font-semibold text-slate-950 bg-gradient-to-r from-teal-400 to-cyan-500 hover:from-teal-300 hover:to-cyan-400 rounded-lg transition shadow-sm cursor-pointer"
              >
                Create Account
              </button>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
