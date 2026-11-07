import type { CollectionSummary } from '../types/query';
import { WorkspaceSelector } from './WorkspaceSelector';
import { UserNav } from './UserNav';

export type NavTab = 'dashboard' | 'research' | 'collections' | 'history' | 'workspace';

interface HeaderProps {
  apiStatus: 'checking' | 'connected' | 'disconnected';
  selectedCollection?: CollectionSummary | null;
  activeTab?: NavTab;
  onSelectTab?: (tab: NavTab) => void;
  onRetryHealth?: () => void;
  onOpenHistory?: () => void;
}

export const Header = ({
  apiStatus,
  selectedCollection,
  activeTab = 'research',
  onSelectTab,
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

  const navItems: { id: NavTab; label: string; icon: string }[] = [
    { id: 'dashboard', label: 'Dashboard', icon: '📊' },
    { id: 'research', label: 'AI Research', icon: '🔍' },
    { id: 'collections', label: 'Collections & Ingestion', icon: '📁' },
    { id: 'history', label: 'Query History', icon: '📜' },
    { id: 'workspace', label: 'Workspace Settings', icon: '⚙️' },
  ];

  return (
    <header className="border-b border-slate-800/80 bg-slate-900/90 backdrop-blur-md sticky top-0 z-30 shadow-md shadow-slate-950/40">
      {/* Top Bar: Branding, System State, Auth */}
      <div className="px-4 sm:px-6 py-2.5 flex flex-col md:flex-row items-start md:items-center justify-between gap-3">
        {/* Left: Branding & Title */}
        <div
          className="flex items-center space-x-3.5 cursor-pointer"
          onClick={() => onSelectTab && onSelectTab('dashboard')}
          title="Go to Dashboard"
        >
          <div className="h-9 w-9 rounded-xl bg-gradient-to-tr from-teal-500 to-cyan-600 flex items-center justify-center font-bold text-slate-950 shadow-md shadow-teal-500/20 text-base tracking-tight select-none shrink-0">
            {avatarText}
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-sm sm:text-base font-semibold tracking-tight text-white">
                {headerTitle}
              </h1>
              <span className="hidden sm:inline-flex items-center px-2 py-0.5 rounded text-[11px] font-medium bg-cyan-950/80 text-cyan-400 border border-cyan-800/60">
                {badgeLabel}
              </span>
            </div>
            <p className="text-[11px] text-slate-400">
              Evidence-First Retrieval &amp; Citation-Validation Interface
            </p>
          </div>
        </div>

        {/* Right: Workspace, History, Auth, and Connection */}
        <div className="flex flex-wrap items-center gap-2 self-stretch md:self-auto justify-end">
          {/* Workspace Selector */}
          <WorkspaceSelector />

          {/* Quick History Drawer Trigger if handler exists */}
          {onOpenHistory && (
            <button
              onClick={onOpenHistory}
              className="px-2.5 py-1.5 text-xs font-medium text-slate-300 hover:text-white bg-slate-950/80 hover:bg-slate-900 border border-slate-800 rounded-lg transition cursor-pointer flex items-center gap-1.5"
              title="View persistent query history drawer"
            >
              <span>📜</span>
              <span className="hidden sm:inline">Drawer</span>
            </button>
          )}

          {/* User Auth Navigation */}
          <UserNav />

          {/* Backend health status badge */}
          <div className="hidden lg:flex items-center space-x-2 bg-slate-950/80 border border-slate-800 px-2.5 py-1.5 rounded-lg text-xs font-medium">
            <span className="text-slate-400">Backend:</span>
            {apiStatus === 'checking' && (
              <span className="flex items-center space-x-1.5 text-amber-400">
                <span className="h-2 w-2 rounded-full bg-amber-400 animate-pulse" />
                <span>Probing</span>
              </span>
            )}
            {apiStatus === 'connected' && (
              <span className="flex items-center space-x-1.5 text-emerald-400">
                <span className="h-2 w-2 rounded-full bg-emerald-400" />
                <span>Live</span>
              </span>
            )}
            {apiStatus === 'disconnected' && (
              <div className="flex items-center space-x-1.5">
                <span className="flex items-center space-x-1 text-rose-400">
                  <span className="h-2 w-2 rounded-full bg-rose-400" />
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
      </div>

      {/* Navigation Tab Bar */}
      {onSelectTab && (
        <div className="border-t border-slate-800/60 px-4 sm:px-6 bg-slate-950/40">
          <nav className="flex space-x-1 sm:space-x-2 overflow-x-auto py-1.5 no-scrollbar" aria-label="Tabs">
            {navItems.map((tab) => {
              const isActive = activeTab === tab.id;
              return (
                <button
                  key={tab.id}
                  onClick={() => onSelectTab(tab.id)}
                  className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition cursor-pointer whitespace-nowrap ${
                    isActive
                      ? 'bg-slate-800 text-cyan-300 shadow-sm border border-slate-700'
                      : 'text-slate-400 hover:text-slate-200 hover:bg-slate-900/60 border border-transparent'
                  }`}
                  aria-current={isActive ? 'page' : undefined}
                >
                  <span className="text-xs">{tab.icon}</span>
                  <span>{tab.label}</span>
                </button>
              );
            })}
          </nav>
        </div>
      )}
    </header>
  );
};
