interface HeaderProps {
  apiStatus: 'checking' | 'connected' | 'disconnected';
  onRetryHealth?: () => void;
}

export const Header = ({ apiStatus, onRetryHealth }: HeaderProps) => {
  return (
    <header className="border-b border-slate-800/80 bg-slate-900/80 backdrop-blur-md sticky top-0 z-20 px-4 sm:px-6 py-3.5 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3 shadow-md shadow-slate-950/40">
      <div className="flex items-center space-x-3.5">
        <div className="h-10 w-10 rounded-xl bg-gradient-to-tr from-teal-500 to-cyan-600 flex items-center justify-center font-bold text-slate-950 shadow-md shadow-teal-500/20 text-lg tracking-tight select-none">
          SEBI
        </div>
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-base sm:text-lg font-semibold tracking-tight text-white">
              SEBI CSCRF Regulatory Intelligence Assistant
            </h1>
            <span className="hidden sm:inline-flex items-center px-2 py-0.5 rounded text-[11px] font-medium bg-cyan-950/80 text-cyan-400 border border-cyan-800/60">
              5-Doc Pilot
            </span>
          </div>
          <p className="text-xs text-slate-400">
            Evidence-First Retrieval & Citation-Validation Interface
          </p>
        </div>
      </div>

      <div className="flex items-center gap-2.5 self-end sm:self-auto">
        <span className="inline-flex sm:hidden items-center px-2 py-0.5 rounded text-[10px] font-medium bg-cyan-950/80 text-cyan-400 border border-cyan-800/60">
          5-Doc Pilot
        </span>

        <div className="flex items-center space-x-2.5 bg-slate-950/80 border border-slate-800 px-3 py-1.5 rounded-lg text-xs font-medium">
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
            <div className="flex items-center space-x-2">
              <span className="flex items-center space-x-1.5 text-rose-400">
                <span className="h-2 w-2 rounded-full bg-rose-400"></span>
                <span>Offline</span>
              </span>
              {onRetryHealth && (
                <button
                  onClick={onRetryHealth}
                  className="px-2 py-0.5 text-[11px] bg-slate-800 hover:bg-slate-700 text-slate-300 rounded transition cursor-pointer border border-slate-700"
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
