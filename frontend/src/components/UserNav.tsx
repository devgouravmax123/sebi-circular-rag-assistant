import { useState } from 'react';
import { useAuth } from '../context/AuthContext';
import { AuthModal } from './AuthModal';

export function UserNav() {
  const { user, isAuthenticated, logout } = useAuth();
  const [isAuthModalOpen, setIsAuthModalOpen] = useState(false);
  const [authMode, setAuthMode] = useState<'login' | 'signup'>('login');

  const handleOpenAuth = (mode: 'login' | 'signup') => {
    setAuthMode(mode);
    setIsAuthModalOpen(true);
  };

  if (!isAuthenticated || !user) {
    return (
      <>
        <div className="flex items-center gap-2">
          <button
            onClick={() => handleOpenAuth('login')}
            className="px-3 py-1.5 text-xs font-medium text-slate-300 hover:text-white bg-slate-800 hover:bg-slate-700 rounded-lg transition border border-slate-700 cursor-pointer"
          >
            Sign In
          </button>
          <button
            onClick={() => handleOpenAuth('signup')}
            className="hidden sm:inline-flex px-3 py-1.5 text-xs font-semibold text-slate-950 bg-gradient-to-r from-teal-400 to-cyan-500 hover:from-teal-300 hover:to-cyan-400 rounded-lg transition shadow-sm cursor-pointer"
          >
            Sign Up
          </button>
        </div>
        <AuthModal
          isOpen={isAuthModalOpen}
          onClose={() => setIsAuthModalOpen(false)}
          defaultMode={authMode}
        />
      </>
    );
  }

  const initial = (user.full_name || user.email || 'U').charAt(0).toUpperCase();

  return (
    <div className="flex items-center gap-2.5">
      <div className="flex items-center gap-2 bg-slate-950/80 border border-slate-800 px-2.5 py-1.5 rounded-lg">
        <div className="w-6 h-6 rounded-full bg-cyan-600/30 border border-cyan-500/50 flex items-center justify-center text-xs font-semibold text-cyan-300 select-none">
          {initial}
        </div>
        <div className="flex flex-col text-left">
          <span className="text-xs font-medium text-slate-200 truncate max-w-[120px] sm:max-w-[160px]">
            {user.full_name || user.email}
          </span>
        </div>
      </div>
      <button
        onClick={() => logout()}
        className="px-2.5 py-1.5 text-xs text-slate-400 hover:text-rose-400 hover:bg-rose-950/30 rounded-lg transition border border-transparent hover:border-rose-900/50 cursor-pointer"
        title="Sign out of your session"
      >
        Sign Out
      </button>
    </div>
  );
}

