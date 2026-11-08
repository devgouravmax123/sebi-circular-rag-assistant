import { useState, useEffect } from 'react';
import { useAuth } from '../context/AuthContext';
import type { GuestSessionTerminationResponse } from '../types/guest';

interface SessionSettingsViewProps {
  onOpenAuthModal?: (mode: 'login' | 'signup') => void;
  onSessionEnded?: () => void;
}

export function SessionSettingsView({
  onOpenAuthModal,
  onSessionEnded,
}: SessionSettingsViewProps) {
  const {
    isGuest,
    guestSession,
    endGuestSession,
    refreshGuestStatus,
    isAuthenticated,
    user,
    logout,
  } = useAuth();

  const [isEnding, setIsEnding] = useState(false);
  const [showConfirmModal, setShowConfirmModal] = useState(false);
  const [terminationResult, setTerminationResult] = useState<GuestSessionTerminationResponse | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // Time remaining countdown formatter
  const [secondsRemaining, setSecondsRemaining] = useState<number>(
    guestSession?.time_remaining_seconds || 86400
  );

  useEffect(() => {
    if (guestSession?.time_remaining_seconds !== undefined) {
      setSecondsRemaining(guestSession.time_remaining_seconds);
    }
  }, [guestSession?.time_remaining_seconds]);

  useEffect(() => {
    if (!isGuest) return;
    const interval = setInterval(() => {
      setSecondsRemaining((prev) => (prev > 0 ? prev - 1 : 0));
    }, 1000);
    return () => clearInterval(interval);
  }, [isGuest]);

  const formatCountdown = (totalSeconds: number) => {
    if (totalSeconds <= 0) return 'Expired';
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    return `${hours.toString().padStart(2, '0')}h ${minutes.toString().padStart(2, '0')}m ${seconds.toString().padStart(2, '0')}s`;
  };

  const handleEndSession = async () => {
    setIsEnding(true);
    setErrorMessage(null);
    try {
      const result = await endGuestSession();
      if (result) {
        setTerminationResult(result);
      }
      setShowConfirmModal(false);
      if (onSessionEnded) {
        onSessionEnded();
      }
    } catch (err) {
      setErrorMessage(err instanceof Error ? err.message : 'Failed to terminate guest session.');
    } finally {
      setIsEnding(false);
    }
  };

  // If user is a Registered User
  if (isAuthenticated && user) {
    return (
      <div className="space-y-8 animate-fadeIn max-w-4xl mx-auto">
        {/* Header */}
        <section className="border-b border-slate-800/80 pb-6">
          <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-cyan-950/80 border border-cyan-800/60 text-xs font-medium text-cyan-400 mb-2">
            <span>👤</span>
            <span>Account Profile &amp; Settings</span>
          </div>
          <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-white">
            Saved Account Settings
          </h1>
          <p className="text-sm text-slate-300 mt-1">
            Manage your personal profile, authenticated session tokens, and workspace association.
          </p>
        </section>

        {/* Account Details Card */}
        <div className="bg-slate-900/60 border border-slate-800 rounded-2xl p-6 space-y-6">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
            <div>
              <label className="text-xs font-medium text-slate-400">Full Name</label>
              <div className="mt-1 text-sm font-semibold text-white">
                {user.full_name || 'Not provided'}
              </div>
            </div>

            <div>
              <label className="text-xs font-medium text-slate-400">Email Address</label>
              <div className="mt-1 text-sm font-semibold text-white font-mono">
                {user.email}
              </div>
            </div>

            <div>
              <label className="text-xs font-medium text-slate-400">Global Role</label>
              <div className="mt-1">
                <span className="px-2.5 py-0.5 rounded text-xs font-semibold uppercase tracking-wider bg-cyan-950 text-cyan-400 border border-cyan-800/60">
                  {user.role || 'Member'}
                </span>
              </div>
            </div>

            <div>
              <label className="text-xs font-medium text-slate-400">Account Status</label>
              <div className="mt-1 flex items-center gap-2">
                <span className="w-2 h-2 rounded-full bg-emerald-400" />
                <span className="text-sm text-emerald-400 font-medium">Active &amp; Persistent</span>
              </div>
            </div>
          </div>

          <div className="pt-6 border-t border-slate-800/80 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
            <div>
              <h3 className="text-sm font-semibold text-slate-200">Sign Out</h3>
              <p className="text-xs text-slate-400 mt-0.5">
                End your authenticated browser session. All workspace data remains safely preserved.
              </p>
            </div>
            <button
              onClick={() => logout()}
              className="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-200 hover:text-white text-xs font-semibold rounded-lg transition border border-slate-700 cursor-pointer"
            >
              Sign Out
            </button>
          </div>
        </div>
      </div>
    );
  }

  // If user is an Ephemeral Guest
  const quotas = guestSession?.quotas || {
    max_collections: 1,
    max_documents: 2,
    max_upload_bytes: 10485760,
    max_queries: 10,
    ttl_hours: 24,
  };

  const collectionsUsed = guestSession?.collections_count || 0;
  const docsUsed = guestSession?.documents_count || 0;
  const bytesUsed = guestSession?.bytes_uploaded || 0;
  const queriesUsed = guestSession?.queries_count || 0;

  const mbUsed = (bytesUsed / (1024 * 1024)).toFixed(1);
  const maxMb = (quotas.max_upload_bytes / (1024 * 1024)).toFixed(0);

  return (
    <div className="space-y-8 animate-fadeIn max-w-4xl mx-auto">
      {/* Header */}
      <section className="border-b border-slate-800/80 pb-6">
        <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-amber-950/80 border border-amber-800/60 text-xs font-medium text-amber-400 mb-2">
          <span className="w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
          <span>Ephemeral Sandbox (24-Hour TTL)</span>
        </div>
        <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-white">
          Guest Session &amp; Data Deletion
        </h1>
        <p className="text-sm text-slate-300 mt-1 leading-relaxed">
          You are currently using an isolated, ephemeral guest session. Your uploaded documents and queries
          are kept temporary and will be permanently deleted when the timer expires or when you end your session.
        </p>
      </section>

      {terminationResult && (
        <div className="p-4 bg-emerald-950/80 border border-emerald-800 rounded-xl text-emerald-300 text-xs">
          {terminationResult.message} ({terminationResult.deleted_documents_count} documents removed)
        </div>
      )}

      {/* Session Expiration & Timer Card */}
      <div className="bg-slate-900/60 border border-slate-800 rounded-2xl p-6 space-y-6">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
          <div>
            <span className="text-xs font-medium text-slate-400 uppercase tracking-wider">
              Time Remaining Before Automatic Purge
            </span>
            <div className="text-3xl sm:text-4xl font-mono font-bold text-amber-400 mt-1">
              {formatCountdown(secondsRemaining)}
            </div>
            <p className="text-xs text-slate-400 mt-1">
              Session expires at: <span className="font-mono text-slate-300">{guestSession?.expires_at ? new Date(guestSession.expires_at).toLocaleTimeString() : 'In 24 hours'}</span>
            </p>
          </div>

          <button
            onClick={() => refreshGuestStatus()}
            className="px-3.5 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs rounded-lg transition border border-slate-700 cursor-pointer self-start sm:self-auto"
            title="Refresh session quotas from server"
          >
            ↻ Refresh Status
          </button>
        </div>

        {/* Quota Progress Grid */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 pt-6 border-t border-slate-800/80">
          {/* Quota 1: Collections */}
          <div className="bg-slate-950/70 border border-slate-800/80 rounded-xl p-4">
            <div className="flex items-center justify-between text-xs text-slate-400 mb-1">
              <span>Collections</span>
              <span>📁</span>
            </div>
            <div className="text-xl font-bold text-white">
              {collectionsUsed} / {quotas.max_collections}
            </div>
            <div className="w-full bg-slate-800 h-1.5 rounded-full mt-2.5 overflow-hidden">
              <div
                className="bg-cyan-400 h-full rounded-full transition-all"
                style={{ width: `${Math.min(100, (collectionsUsed / quotas.max_collections) * 100)}%` }}
              />
            </div>
          </div>

          {/* Quota 2: Documents */}
          <div className="bg-slate-950/70 border border-slate-800/80 rounded-xl p-4">
            <div className="flex items-center justify-between text-xs text-slate-400 mb-1">
              <span>PDF Documents</span>
              <span>📄</span>
            </div>
            <div className="text-xl font-bold text-white">
              {docsUsed} / {quotas.max_documents}
            </div>
            <div className="w-full bg-slate-800 h-1.5 rounded-full mt-2.5 overflow-hidden">
              <div
                className="bg-teal-400 h-full rounded-full transition-all"
                style={{ width: `${Math.min(100, (docsUsed / quotas.max_documents) * 100)}%` }}
              />
            </div>
          </div>

          {/* Quota 3: Storage */}
          <div className="bg-slate-950/70 border border-slate-800/80 rounded-xl p-4">
            <div className="flex items-center justify-between text-xs text-slate-400 mb-1">
              <span>Upload Capacity</span>
              <span>💾</span>
            </div>
            <div className="text-xl font-bold text-white">
              {mbUsed} / {maxMb} MB
            </div>
            <div className="w-full bg-slate-800 h-1.5 rounded-full mt-2.5 overflow-hidden">
              <div
                className="bg-indigo-400 h-full rounded-full transition-all"
                style={{
                  width: `${Math.min(100, (bytesUsed / quotas.max_upload_bytes) * 100)}%`,
                }}
              />
            </div>
          </div>

          {/* Quota 4: Research Queries */}
          <div className="bg-slate-950/70 border border-slate-800/80 rounded-xl p-4">
            <div className="flex items-center justify-between text-xs text-slate-400 mb-1">
              <span>Queries</span>
              <span>🔍</span>
            </div>
            <div className="text-xl font-bold text-white">
              {queriesUsed} / {quotas.max_queries}
            </div>
            <div className="w-full bg-slate-800 h-1.5 rounded-full mt-2.5 overflow-hidden">
              <div
                className="bg-amber-400 h-full rounded-full transition-all"
                style={{ width: `${Math.min(100, (queriesUsed / quotas.max_queries) * 100)}%` }}
              />
            </div>
          </div>
        </div>

        {/* Security & Data Erasure Guarantee */}
        <div className="p-4 bg-slate-950/90 border border-slate-800 rounded-xl flex items-start gap-3">
          <div className="text-xl text-teal-400 shrink-0">🔒</div>
          <div className="space-y-1">
            <h4 className="text-xs font-semibold text-slate-200">Ephemeral Session Policy</h4>
            <p className="text-xs text-slate-400 leading-relaxed">
              All documents, metadata, indexes, and queries generated during this guest session are kept in an isolated temporary partition. Ending your session triggers server-side deletion of all temporary session artifacts.
            </p>
          </div>
        </div>

        {/* Action: End Session & Delete Data */}
        <div className="pt-6 border-t border-slate-800/80 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
          <div>
            <h3 className="text-sm font-semibold text-rose-300 flex items-center gap-1.5">
              <span>🗑️</span>
              <span>End Session &amp; Delete Data</span>
            </h3>
            <p className="text-xs text-slate-400 mt-0.5">
              Permanently delete all uploaded documents, temporary indices, and return to the public landing page.
            </p>
          </div>

          <button
            onClick={() => setShowConfirmModal(true)}
            disabled={isEnding}
            className="px-4 py-2 bg-rose-950/60 hover:bg-rose-900/80 text-rose-300 hover:text-white border border-rose-800/80 rounded-xl text-xs font-semibold transition cursor-pointer flex items-center gap-1.5"
          >
            <span>End Session &amp; Purge Data</span>
          </button>
        </div>
      </div>

      {/* Upgrade CTA */}
      <section className="rounded-2xl border border-cyan-900/40 bg-gradient-to-r from-cyan-950/30 via-slate-900/40 to-teal-950/30 p-6 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
        <div className="space-y-1 max-w-xl">
          <h3 className="text-base font-semibold text-cyan-300">
            Want to retain your documents and research permanently?
          </h3>
          <p className="text-xs text-slate-400 leading-relaxed">
            Create a free registered account to unlock persistent multi-document workspaces, team role collaboration, and unlimited query history.
          </p>
        </div>
        {onOpenAuthModal && (
          <div className="flex items-center gap-2 shrink-0">
            <button
              onClick={() => onOpenAuthModal('signup')}
              className="px-4 py-2 text-xs font-semibold text-slate-950 bg-gradient-to-r from-teal-400 to-cyan-500 hover:from-teal-300 hover:to-cyan-400 rounded-lg transition shadow-sm cursor-pointer"
            >
              Sign Up Free
            </button>
          </div>
        )}
      </section>

      {/* Confirmation Modal */}
      {showConfirmModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-950/80 backdrop-blur-sm animate-fadeIn">
          <div className="bg-slate-900 border border-slate-800 rounded-2xl max-w-md w-full p-6 shadow-2xl space-y-4">
            <div className="flex items-center gap-3 text-rose-400">
              <span className="text-2xl">⚠️</span>
              <h3 className="text-lg font-bold text-white">Permanently Delete Session Data?</h3>
            </div>

            <p className="text-xs text-slate-300 leading-relaxed">
              This action cannot be undone. Ending your session will permanently delete:
            </p>
            <ul className="text-xs text-slate-400 list-disc list-inside space-y-1">
              <li>Delete all {docsUsed} uploaded PDF document(s) from server storage.</li>
              <li>Purge all temporary index structures and ingestion jobs.</li>
              <li>Invalidate your ephemeral session token.</li>
            </ul>

            {errorMessage && (
              <div className="p-3 bg-rose-950/80 border border-rose-800 rounded-lg text-rose-300 text-xs">
                {errorMessage}
              </div>
            )}

            <div className="pt-4 flex items-center justify-end gap-3 border-t border-slate-800">
              <button
                onClick={() => setShowConfirmModal(false)}
                disabled={isEnding}
                className="px-3.5 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium rounded-lg transition cursor-pointer"
              >
                Cancel
              </button>
              <button
                onClick={handleEndSession}
                disabled={isEnding}
                className="px-4 py-1.5 bg-rose-600 hover:bg-rose-500 text-white text-xs font-semibold rounded-lg transition shadow-md cursor-pointer flex items-center gap-1.5"
              >
                <span>{isEnding ? 'Purging...' : 'Yes, Delete Everything'}</span>
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
