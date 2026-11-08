import { useState } from 'react';
import { useAuth } from '../context/AuthContext';
import { AuthModal } from './AuthModal';

interface LandingPageProps {
  onStartGuest?: () => void;
  onOpenAuth?: (mode: 'login' | 'signup') => void;
}

export function LandingPage({ onStartGuest, onOpenAuth }: LandingPageProps) {
  const { startGuestSession, isLoading: isAuthLoading } = useAuth();
  const [isAuthModalOpen, setIsAuthModalOpen] = useState(false);
  const [authMode, setAuthMode] = useState<'login' | 'signup'>('login');
  const [isStartingGuest, setIsStartingGuest] = useState(false);
  const [guestError, setGuestError] = useState<string | null>(null);

  const handleOpenAuth = (mode: 'login' | 'signup') => {
    if (onOpenAuth) {
      onOpenAuth(mode);
    } else {
      setAuthMode(mode);
      setIsAuthModalOpen(true);
    }
  };

  const handleStartGuest = async () => {
    setIsStartingGuest(true);
    setGuestError(null);
    try {
      if (onStartGuest) {
        onStartGuest();
      } else {
        await startGuestSession();
      }
    } catch (err) {
      setGuestError(err instanceof Error ? err.message : 'Failed to initialize guest session.');
    } finally {
      setIsStartingGuest(false);
    }
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col font-sans selection:bg-cyan-500 selection:text-slate-950">
      {/* Public Landing Navigation Bar */}
      <header className="border-b border-slate-800/80 bg-slate-950/80 backdrop-blur-md sticky top-0 z-30 px-4 sm:px-8 py-3.5 flex items-center justify-between">
        <div className="flex items-center space-x-3">
          <div className="h-9 w-9 rounded-xl bg-gradient-to-tr from-teal-500 to-cyan-600 flex items-center justify-center font-bold text-slate-950 shadow-md shadow-teal-500/20 text-base tracking-tight select-none">
            DI
          </div>
          <div>
            <span className="text-base font-bold tracking-tight text-white">Document Intelligence</span>
            <span className="hidden sm:inline-block ml-2 px-2 py-0.5 rounded text-[10px] font-semibold bg-cyan-950 text-cyan-400 border border-cyan-800/60">
              AI Research
            </span>
          </div>
        </div>

        <div className="flex items-center space-x-3">
          <button
            onClick={() => handleOpenAuth('login')}
            className="px-3.5 py-1.5 text-xs sm:text-sm font-medium text-slate-300 hover:text-white bg-slate-900 hover:bg-slate-800 rounded-lg transition border border-slate-700 cursor-pointer"
          >
            Sign In
          </button>
          <button
            onClick={() => handleOpenAuth('signup')}
            className="hidden sm:inline-flex px-3.5 py-1.5 text-xs sm:text-sm font-semibold text-slate-950 bg-gradient-to-r from-teal-400 to-cyan-500 hover:from-teal-300 hover:to-cyan-400 rounded-lg transition shadow-sm cursor-pointer"
          >
            Sign Up
          </button>
          <button
            onClick={handleStartGuest}
            disabled={isStartingGuest || isAuthLoading}
            className="px-3.5 py-1.5 text-xs sm:text-sm font-semibold text-cyan-300 hover:text-white bg-cyan-950/70 hover:bg-cyan-900 border border-cyan-700/60 rounded-lg transition cursor-pointer flex items-center gap-1.5"
          >
            <span>⚡</span>
            <span>{isStartingGuest ? 'Starting...' : 'Try Guest Mode'}</span>
          </button>
        </div>
      </header>

      {/* Hero Section */}
      <main className="flex-1 max-w-6xl w-full mx-auto px-4 sm:px-6 py-12 sm:py-20 flex flex-col items-center text-center">
        {/* Badge */}
        <div className="inline-flex items-center gap-2 px-3.5 py-1.5 rounded-full bg-cyan-950/80 border border-cyan-800/60 text-xs sm:text-sm font-medium text-cyan-400 mb-6 shadow-sm">
          <span className="w-2 h-2 rounded-full bg-cyan-400 animate-pulse" />
          <span>General-Purpose AI Document Intelligence Platform</span>
        </div>

        {/* Main Headline */}
        <h1 className="text-4xl sm:text-6xl font-extrabold tracking-tight text-white max-w-4xl leading-tight sm:leading-none">
          Turn your documents <br className="hidden sm:inline" />
          <span className="bg-gradient-to-r from-teal-400 via-cyan-400 to-sky-400 bg-clip-text text-transparent">
            into trusted answers.
          </span>
        </h1>

        {/* Subtitle */}
        <p className="mt-6 text-base sm:text-xl text-slate-300 max-w-3xl leading-relaxed">
          Evidence-grounded AI research for business, technical, legal, and operational documents.
          Every answer is backed by exact source citations and verified quotes—with zero hallucinations.
        </p>

        {/* CTAs */}
        <div className="mt-10 flex flex-col sm:flex-row items-center justify-center gap-4 w-full max-w-md">
          <button
            onClick={handleStartGuest}
            disabled={isStartingGuest || isAuthLoading}
            className="w-full sm:w-auto px-7 py-3.5 bg-gradient-to-r from-teal-400 to-cyan-500 hover:from-teal-300 hover:to-cyan-400 text-slate-950 font-bold text-sm sm:text-base rounded-xl transition shadow-lg shadow-teal-500/25 cursor-pointer flex items-center justify-center gap-2"
          >
            <span>⚡</span>
            <span>{isStartingGuest ? 'Initializing Sandbox...' : 'Continue as Guest'}</span>
          </button>

          <button
            onClick={() => handleOpenAuth('signup')}
            className="w-full sm:w-auto px-7 py-3.5 bg-slate-900 hover:bg-slate-800 text-white font-semibold text-sm sm:text-base rounded-xl transition border border-slate-700 cursor-pointer flex items-center justify-center gap-2"
          >
            <span>Create Free Account</span>
            <span>&rarr;</span>
          </button>
        </div>

        {/* Guest Mode Callout */}
        <div className="mt-4 text-xs text-slate-400 max-w-lg">
          <span className="font-semibold text-slate-300">Guest Mode:</span> Instant 24-hour ephemeral sandbox. Upload up to 2 PDFs and run 10 queries. All documents are deleted automatically upon session termination.
        </div>

        {guestError && (
          <div className="mt-4 p-3 bg-rose-950/60 border border-rose-800/80 rounded-lg text-rose-300 text-xs max-w-md">
            {guestError}
          </div>
        )}

        {/* Feature Cards Grid */}
        <section className="mt-20 w-full grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 text-left">
          {/* Card 1: Evidence Grounding */}
          <div className="bg-slate-900/50 border border-slate-800 rounded-2xl p-6 flex flex-col justify-between hover:border-cyan-500/40 transition">
            <div>
              <div className="w-12 h-12 rounded-xl bg-cyan-950/80 border border-cyan-800/60 flex items-center justify-center text-2xl mb-4 text-cyan-400">
                🛡️
              </div>
              <h3 className="text-lg font-semibold text-white">Citation Grounding</h3>
              <p className="text-sm text-slate-400 mt-2 leading-relaxed">
                Every factual claim is mechanically cross-referenced against exact excerpts and page numbers in your indexed corpus.
              </p>
            </div>
            <div className="mt-6 pt-4 border-t border-slate-800/80 text-xs font-medium text-cyan-400">
              Zero Unverified Claims
            </div>
          </div>

          {/* Card 2: Controlled Abstention */}
          <div className="bg-slate-900/50 border border-slate-800 rounded-2xl p-6 flex flex-col justify-between hover:border-teal-500/40 transition">
            <div>
              <div className="w-12 h-12 rounded-xl bg-teal-950/80 border border-teal-800/60 flex items-center justify-center text-2xl mb-4 text-teal-400">
                ⚖️
              </div>
              <h3 className="text-lg font-semibold text-white">Controlled Abstention</h3>
              <p className="text-sm text-slate-400 mt-2 leading-relaxed">
                If the evidence is missing or ambiguous, our safety gate openly abstains instead of inventing convincing but inaccurate answers.
              </p>
            </div>
            <div className="mt-6 pt-4 border-t border-slate-800/80 text-xs font-medium text-teal-400">
              Hallucination Defense
            </div>
          </div>

          {/* Card 3: Guest Sandbox */}
          <div className="bg-slate-900/50 border border-slate-800 rounded-2xl p-6 flex flex-col justify-between hover:border-indigo-500/40 transition">
            <div>
              <div className="w-12 h-12 rounded-xl bg-indigo-950/80 border border-indigo-800/60 flex items-center justify-center text-2xl mb-4 text-indigo-400">
                ⏳
              </div>
              <h3 className="text-lg font-semibold text-white">Ephemeral Guest Mode</h3>
              <p className="text-sm text-slate-400 mt-2 leading-relaxed">
                Instant access with zero sign-up friction. Test your documents in an isolated sandbox with automated 24-hour server data cleanup.
              </p>
            </div>
            <div className="mt-6 pt-4 border-t border-slate-800/80 text-xs font-medium text-indigo-400">
              Ephemeral Session Isolation
            </div>
          </div>

          {/* Card 4: Multi-Tenant Workspaces */}
          <div className="bg-slate-900/50 border border-slate-800 rounded-2xl p-6 flex flex-col justify-between hover:border-amber-500/40 transition">
            <div>
              <div className="w-12 h-12 rounded-xl bg-amber-950/80 border border-amber-800/60 flex items-center justify-center text-2xl mb-4 text-amber-400">
                🏢
              </div>
              <h3 className="text-lg font-semibold text-white">Persistent Workspaces</h3>
              <p className="text-sm text-slate-400 mt-2 leading-relaxed">
                Registered users receive persistent workspaces, team RBAC collaboration, audit history ledgers, and unlimited document collections.
              </p>
            </div>
            <div className="mt-6 pt-4 border-t border-slate-800/80 text-xs font-medium text-amber-400">
              Team RBAC &amp; Audit Logs
            </div>
          </div>
        </section>

        {/* How It Works Section */}
        <section className="mt-24 w-full text-center max-w-4xl">
          <h2 className="text-2xl sm:text-3xl font-bold text-white tracking-tight">
            How Document Intelligence Works
          </h2>
          <p className="text-sm sm:text-base text-slate-400 mt-2">
            Three simple steps to transform static documents into verifiable knowledge
          </p>

          <div className="mt-10 grid grid-cols-1 md:grid-cols-3 gap-8">
            <div className="bg-slate-900/30 border border-slate-800/80 rounded-xl p-6 flex flex-col items-center">
              <div className="w-10 h-10 rounded-full bg-cyan-950 border border-cyan-800 flex items-center justify-center font-bold text-cyan-400 text-sm mb-4">
                1
              </div>
              <h4 className="font-semibold text-white">Create or Select Collection</h4>
              <p className="text-xs text-slate-400 mt-2 leading-relaxed">
                Group related documents by department, project, or regulation into dedicated searchable partitions.
              </p>
            </div>

            <div className="bg-slate-900/30 border border-slate-800/80 rounded-xl p-6 flex flex-col items-center">
              <div className="w-10 h-10 rounded-full bg-teal-950 border border-teal-800 flex items-center justify-center font-bold text-teal-400 text-sm mb-4">
                2
              </div>
              <h4 className="font-semibold text-white">Upload PDFs</h4>
              <p className="text-xs text-slate-400 mt-2 leading-relaxed">
                Upload your source documents. Documents are chunked and indexed into verifiable retrieval snapshots.
              </p>
            </div>

            <div className="bg-slate-900/30 border border-slate-800/80 rounded-xl p-6 flex flex-col items-center">
              <div className="w-10 h-10 rounded-full bg-indigo-950 border border-indigo-800 flex items-center justify-center font-bold text-indigo-400 text-sm mb-4">
                3
              </div>
              <h4 className="font-semibold text-white">Ask &amp; Verify</h4>
              <p className="text-xs text-slate-400 mt-2 leading-relaxed">
                Pose natural questions. Inspect exact verbatim citations and confidence metrics side by side.
              </p>
            </div>
          </div>
        </section>

        {/* Versatility Showcase */}
        <section className="mt-20 w-full bg-gradient-to-r from-slate-900/60 via-slate-900/90 to-slate-900/60 border border-slate-800 rounded-2xl p-8 text-left">
          <div className="flex flex-col md:flex-row md:items-center justify-between gap-6">
            <div className="space-y-2 max-w-xl">
              <h3 className="text-xl font-bold text-white">Built for Any Document-Heavy Domain</h3>
              <p className="text-sm text-slate-400 leading-relaxed">
                From financial compliance and legal discovery to technical specifications and standard operating procedures, our safety gate ensures strict factual compliance.
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <span className="px-3 py-1.5 rounded-lg bg-slate-800/90 text-xs text-slate-300 border border-slate-700">Financial Reports</span>
              <span className="px-3 py-1.5 rounded-lg bg-slate-800/90 text-xs text-slate-300 border border-slate-700">Legal Contracts</span>
              <span className="px-3 py-1.5 rounded-lg bg-slate-800/90 text-xs text-slate-300 border border-slate-700">Engineering Specs</span>
              <span className="px-3 py-1.5 rounded-lg bg-slate-800/90 text-xs text-slate-300 border border-slate-700">Regulatory Policies</span>
            </div>
          </div>
        </section>
      </main>

      {/* Footer */}
      <footer className="border-t border-slate-900 bg-slate-950/80 px-6 py-6 text-center text-xs text-slate-500">
        <div className="max-w-4xl mx-auto space-y-1">
          <p>
            Document Intelligence &amp; Multi-Tenant AI Research Platform.
          </p>
          <p className="text-[11px] text-slate-600">
            Evidence-grounded synthesis with deterministic citation validation. Public regulatory materials are provided for demonstration purposes.
          </p>
        </div>
      </footer>

      {/* Auth Modal for Sign In / Sign Up */}
      <AuthModal
        isOpen={isAuthModalOpen}
        onClose={() => setIsAuthModalOpen(false)}
        defaultMode={authMode}
      />
    </div>
  );
}

