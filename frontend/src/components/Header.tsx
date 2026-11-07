import type { CollectionSummary } from '../types/query';
import { WorkspaceSelector } from './WorkspaceSelector';
import { UserNav } from './UserNav';

interface HeaderProps {
  apiStatus: 'checking' | 'connected' | 'disconnected';
  selectedCollection?: CollectionSummary | null;
  onRetryHealth?: () => void;
  onOpenHistory?: () => void;
}

export const Header = ({
  apiStatus,
  selectedCollection,
  onRetryHealth,
  onOpenHistory,
}: HeaderProps) => {
  const isSebiStrict = selectedCollection?.policy_type === 'sebi_cscrf_strict';
  const isGeneralStatic = selectedCollection?.policy_type === 'general_static';

  const avatarText = isSebiStrict ? 'SEBI' : 'DOC';
  const headerTitle = isSebiStrict
    ? 'SEBI CSCRF Regulatory Intelligence Assistant'
    : 'Document Intelligence Platform';
  const badgeLabel = isSebiStrict
    ? 'SEBI CSCRF'
    : isGeneralStatic
    ? 'Documentation'
    : 'Multi-Collection';

  return (
    <header className="border-b border-slate-800/80 bg-slate-900/80 backdrop-blur-md sticky top-0 z-20 px-4 sm:px-6 py-3 flex flex-col md:flex-row items-start md:items-center justify-between gap-3 shadow-md shadow-slate-950/40">
      {/* Left: Branding & Title */}
      <div className="flex items-center space-x-3.5">
        <div className="h-10 w-10 rounded-xl bg-gradient-to-tr from-teal-500 to-cyan-600 flex items-center justify-center font-bold text-slate-950 shadow-md shadow-teal-500/20 text-lg tracking-tight select-none">
          {avatarText}
        </div>
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-base sm:text-lg font-semibold tracking-tight text-white">
              {headerTitle}
            </h1>
            <span className="hidden sm:inline-flex items-center px-2 py-0.5 rounded text-[11px] font-medium bg-cyan-950/80 text-cyan-400 border border-cyan-800/60">
              {badgeLabel}
            </span>
          </div>
          <p className="text-xs text-slate-400">
            Evidence-First Retrieval & Citation-Validation Interface
          </p>
        </div>
      </div>

      {/* Right: Workspace, History, Auth, and Connection */}
      <div className="flex flex-wrap items-center gap-2.5 self-stretch md:self-auto justify-end">
        {/* Workspace Selector */}
        <WorkspaceSelector />

        {/* History Trigger */}
        {onOpenHistory && (
          <button
            onClick={onOpenHistory}
            className="px-2.5 py-1.5 text-xs font-medium text-slate-300 hover:text-white bg-slate-950/80 hover:bg-slate-900 border border-slate-800 rounded-lg transition cursor-pointer flex items-center gap-1.5"
            title="View persistent query history"
          >
            <span>📜</span>
            <span className="hidden sm:inline">History</span>
          </button>
        )}

        {/* User Auth Navigation */}
        <UserNav />

        {/* Backend health status badge */}
        <div className="hidden lg:flex items-center space-x-2 bg-slate-950/80 border border-slate-800 px-2.5 py-1.5 rounded-lg text-xs font-medium">
          <span className="text-slate-400">Backend:</span>
          {apiStatus === 'checking' && (
            <span className="flex items-center space-x-1.5 text-amber-400">
              <span className="h-2 w-2 rounded-full bg-amber-400 animate-pulse"></span>
              <span>Probing</span>
            </span>
          )}
          {apiStatus === 'connected' && (
            <span className="flex items-center space-x-1.5 text-emerald-400">
              <span className="h-2 w-2 rounded-full bg-emerald-400"></span>
              <span>Live</span>
            </span>
          )}
          {apiStatus === 'disconnected' && (
            <div className="flex items-center space-x-1.5">
              <span className="flex items-center space-x-1 text-rose-400">
                <span className="h-2 w-2 rounded-full bg-rose-400"></span>
                <span>Offline</span>
              </span>
              {onRetryHealth && (
                <button
                  onClick={onRetryHealth}
                  className="px-1.5 py-0.5 text-[10px] bg-slate-800 hover:bg-slate-700 text-slate-300 rounded transition cursor-pointer border border-slate-700"
                  title="Check backend health again"
                >
                  Retry
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </header>
  );
};
