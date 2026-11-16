import { useState, useEffect, type FormEvent } from 'react';
import { useAuth } from '../context/AuthContext';

export type AuthModalMode = 'login' | 'signup' | 'verify-email' | 'forgot-password' | 'link-legacy';

interface AuthModalProps {
  isOpen: boolean;
  onClose: () => void;
  defaultMode?: 'login' | 'signup' | 'verify-email' | 'forgot-password' | 'link-legacy';
}

export function AuthModal({ isOpen, onClose, defaultMode = 'login' }: AuthModalProps) {
  const {
    login,
    signup,
    checkEmailVerification,
    resendVerificationEmail,
    sendPasswordReset,
    linkLegacyAccount,
    pendingVerificationEmail,
    isVerificationPending,
  } = useAuth();

  const [mode, setMode] = useState<AuthModalMode>(defaultMode);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [fullName, setFullName] = useState('');
  const [legacyPassword, setLegacyPassword] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);

  // Verification email resend cooldown (60 seconds)
  const [resendCooldown, setResendCooldown] = useState(0);

  // Sync mode with defaultMode or verification pending state
  useEffect(() => {
    if (isOpen) {
      if (isVerificationPending) {
        setMode('verify-email');
      } else {
        setMode(defaultMode);
      }
      setErrorMessage(null);
      setSuccessMessage(null);
    }
  }, [isOpen, defaultMode, isVerificationPending]);

  // Resend cooldown timer
  useEffect(() => {
    if (resendCooldown <= 0) return;
    const timer = setTimeout(() => setResendCooldown((prev) => prev - 1), 1000);
    return () => clearTimeout(timer);
  }, [resendCooldown]);

  if (!isOpen) return null;

  const targetEmail = pendingVerificationEmail || email;

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setErrorMessage(null);
    setSuccessMessage(null);

    const trimmedEmail = email.trim();

    if (mode === 'forgot-password') {
      if (!trimmedEmail || !trimmedEmail.includes('@')) {
        setErrorMessage('Please provide a valid email address.');
        return;
      }
      setIsSubmitting(true);
      try {
        await sendPasswordReset(trimmedEmail);
        setSuccessMessage('Password reset link sent! Please check your email inbox.');
      } catch (err: unknown) {
        setErrorMessage(err instanceof Error ? err.message : 'Failed to send password reset email.');
      } finally {
        setIsSubmitting(false);
      }
      return;
    }

    if (mode === 'link-legacy') {
      if (!legacyPassword) {
        setErrorMessage('Please enter your previous password.');
        return;
      }
      setIsSubmitting(true);
      try {
        await linkLegacyAccount(legacyPassword);
        onClose();
      } catch (err: unknown) {
        setErrorMessage(err instanceof Error ? err.message : 'Failed to link account.');
      } finally {
        setIsSubmitting(false);
      }
      return;
    }

    if (!trimmedEmail || !trimmedEmail.includes('@')) {
      setErrorMessage('Please provide a valid email address.');
      return;
    }

    if (password.length < 8) {
      setErrorMessage('Password must be at least 8 characters long.');
      return;
    }

    setIsSubmitting(true);
    try {
      if (mode === 'login') {
        await login({ email: trimmedEmail, password });
        onClose();
      } else if (mode === 'signup') {
        await signup({
          email: trimmedEmail,
          password,
          full_name: fullName.trim() || undefined,
        });
        // Transition to email verification view
        setMode('verify-email');
        setResendCooldown(60);
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Authentication failed.';
      if (msg === 'EMAIL_VERIFICATION_REQUIRED') {
        setMode('verify-email');
        setResendCooldown(60);
      } else if (msg === 'LEGACY_ACCOUNT_NEEDS_LINKING') {
        setMode('link-legacy');
      } else {
        setErrorMessage(msg);
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleCheckVerification = async () => {
    setIsSubmitting(true);
    setErrorMessage(null);
    try {
      const verified = await checkEmailVerification();
      if (verified) {
        onClose();
      } else {
        setErrorMessage(
          'Email not yet verified. Please click the verification link sent to your inbox, then click here to verify.'
        );
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to verify email.';
      if (msg === 'LEGACY_ACCOUNT_NEEDS_LINKING') {
        setMode('link-legacy');
      } else {
        setErrorMessage(msg);
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleResendVerification = async () => {
    if (resendCooldown > 0 || isSubmitting) return;
    setIsSubmitting(true);
    setErrorMessage(null);
    try {
      await resendVerificationEmail();
      setSuccessMessage('A fresh verification link has been sent to your email.');
      setResendCooldown(60);
    } catch (err: unknown) {
      setErrorMessage(err instanceof Error ? err.message : 'Failed to resend verification email.');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-950/80 backdrop-blur-sm animate-fadeIn">
      <div
        className="w-full max-w-md bg-slate-900 border border-slate-800 rounded-2xl shadow-2xl p-6 flex flex-col gap-5 text-slate-100 max-h-[90vh] overflow-y-auto"
        role="dialog"
        aria-modal="true"
        aria-labelledby="auth-modal-title"
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-slate-800 pb-4">
          <h2 id="auth-modal-title" className="text-lg font-semibold tracking-tight text-white">
            {mode === 'login' && 'Sign In to Workspace'}
            {mode === 'signup' && 'Create an Account'}
            {mode === 'verify-email' && 'Verify Your Email'}
            {mode === 'forgot-password' && 'Reset Your Password'}
            {mode === 'link-legacy' && 'Link Existing Workspace'}
          </h2>
          <button
            onClick={onClose}
            className="text-slate-400 hover:text-slate-200 transition p-1 rounded-lg cursor-pointer"
            aria-label="Close authentication modal"
          >
            ✕
          </button>
        </div>

        {/* Alerts */}
        {errorMessage && (
          <div className="p-3 bg-rose-950/70 border border-rose-800/80 rounded-xl text-rose-300 text-xs">
            {errorMessage}
          </div>
        )}
        {successMessage && (
          <div className="p-3 bg-emerald-950/70 border border-emerald-800/80 rounded-xl text-emerald-300 text-xs">
            {successMessage}
          </div>
        )}

        {/* View: Verify Email */}
        {mode === 'verify-email' && (
          <div className="flex flex-col gap-4 text-center py-2">
            <div className="w-14 h-14 mx-auto rounded-full bg-cyan-950/80 border border-cyan-800 flex items-center justify-center text-2xl text-cyan-400">
              ✉️
            </div>
            <div>
              <h3 className="text-base font-semibold text-white">Check Your Inbox</h3>
              <p className="text-xs text-slate-300 mt-1 leading-relaxed">
                We sent a verification link to{' '}
                <span className="font-semibold text-cyan-400">{targetEmail || 'your email'}</span>.
              </p>
              <p className="text-[11px] text-slate-400 mt-2 bg-slate-950/60 p-2.5 rounded-lg border border-slate-800/80 text-left">
                💡 <span className="font-semibold text-slate-300">Important:</span> Click the verification link in your email to activate your account. Once clicked, return here and click the button below.
              </p>
            </div>

            <div className="flex flex-col gap-2 mt-2">
              <button
                type="button"
                onClick={handleCheckVerification}
                disabled={isSubmitting}
                className="w-full py-2.5 px-4 bg-gradient-to-r from-teal-500 to-cyan-600 hover:from-teal-400 hover:to-cyan-500 text-slate-950 font-semibold rounded-xl text-sm transition shadow-md shadow-cyan-900/30 disabled:opacity-50 cursor-pointer"
              >
                {isSubmitting ? 'Checking Status...' : "I've Verified My Email"}
              </button>

              <button
                type="button"
                onClick={handleResendVerification}
                disabled={resendCooldown > 0 || isSubmitting}
                className="w-full py-2 px-4 bg-slate-950 hover:bg-slate-800 text-slate-300 hover:text-white border border-slate-800 rounded-xl text-xs font-medium transition disabled:opacity-50 cursor-pointer"
              >
                {resendCooldown > 0 ? `Resend Link in ${resendCooldown}s` : 'Resend Verification Link'}
              </button>
            </div>

            <div className="text-center pt-2 text-xs text-slate-400 border-t border-slate-800/80">
              <button
                type="button"
                onClick={() => {
                  setMode('login');
                  setErrorMessage(null);
                  setSuccessMessage(null);
                }}
                className="text-cyan-400 hover:text-cyan-300 font-medium cursor-pointer"
              >
                Back to Sign In
              </button>
            </div>
          </div>
        )}

        {/* View: Link Legacy Account */}
        {mode === 'link-legacy' && (
          <form onSubmit={handleSubmit} className="flex flex-col gap-4">
            <div className="p-3 bg-amber-950/60 border border-amber-800/80 rounded-xl text-amber-300 text-xs">
              An existing workspace was found for <span className="font-semibold">{targetEmail}</span>. Enter your previous password to securely link your documents, collections, and query history to your verified account.
            </div>

            <div className="flex flex-col gap-1.5">
              <label className="text-xs font-medium text-slate-300" htmlFor="legacy-password">
                Previous Account Password <span className="text-cyan-400">*</span>
              </label>
              <input
                id="legacy-password"
                type="password"
                placeholder="••••••••••••"
                required
                value={legacyPassword}
                onChange={(e) => setLegacyPassword(e.target.value)}
                className="w-full px-3.5 py-2 bg-slate-950 border border-slate-800 rounded-xl text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-cyan-500"
                disabled={isSubmitting}
              />
            </div>

            <button
              type="submit"
              disabled={isSubmitting}
              className="w-full mt-2 py-2.5 px-4 bg-gradient-to-r from-teal-500 to-cyan-600 hover:from-teal-400 hover:to-cyan-500 text-slate-950 font-semibold rounded-xl text-sm transition shadow-md shadow-cyan-900/30 disabled:opacity-50 cursor-pointer"
            >
              {isSubmitting ? 'Linking Workspace...' : 'Link Workspace & Enter'}
            </button>

            <div className="text-center pt-2 text-xs text-slate-400 border-t border-slate-800/80">
              <button
                type="button"
                onClick={() => {
                  setMode('login');
                  setErrorMessage(null);
                  setSuccessMessage(null);
                }}
                className="text-cyan-400 hover:text-cyan-300 font-medium cursor-pointer"
              >
                Cancel and Return to Sign In
              </button>
            </div>
          </form>
        )}

        {/* View: Forgot Password */}
        {mode === 'forgot-password' && (
          <form onSubmit={handleSubmit} className="flex flex-col gap-4">
            <p className="text-xs text-slate-300 leading-relaxed">
              Enter your email address and we'll send you a secure link to reset your password.
            </p>

            <div className="flex flex-col gap-1.5">
              <label className="text-xs font-medium text-slate-300" htmlFor="reset-email">
                Email Address <span className="text-cyan-400">*</span>
              </label>
              <input
                id="reset-email"
                type="email"
                placeholder="user@example.com"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="w-full px-3.5 py-2 bg-slate-950 border border-slate-800 rounded-xl text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-cyan-500"
                disabled={isSubmitting}
              />
            </div>

            <button
              type="submit"
              disabled={isSubmitting}
              className="w-full mt-2 py-2.5 px-4 bg-gradient-to-r from-teal-500 to-cyan-600 hover:from-teal-400 hover:to-cyan-500 text-slate-950 font-semibold rounded-xl text-sm transition shadow-md shadow-cyan-900/30 disabled:opacity-50 cursor-pointer"
            >
              {isSubmitting ? 'Sending Link...' : 'Send Password Reset Link'}
            </button>

            <div className="text-center pt-2 text-xs text-slate-400 border-t border-slate-800/80">
              <button
                type="button"
                onClick={() => {
                  setMode('login');
                  setErrorMessage(null);
                  setSuccessMessage(null);
                }}
                className="text-cyan-400 hover:text-cyan-300 font-medium cursor-pointer"
              >
                Back to Sign In
              </button>
            </div>
          </form>
        )}

        {/* View: Sign In & Sign Up */}
        {(mode === 'login' || mode === 'signup') && (
          <form onSubmit={handleSubmit} className="flex flex-col gap-4">
            {mode === 'signup' && (
              <div className="flex flex-col gap-1.5">
                <label className="text-xs font-medium text-slate-300" htmlFor="auth-fullname">
                  Full Name (Optional)
                </label>
                <input
                  id="auth-fullname"
                  type="text"
                  placeholder="Ada Lovelace"
                  value={fullName}
                  onChange={(e) => setFullName(e.target.value)}
                  className="w-full px-3.5 py-2 bg-slate-950 border border-slate-800 rounded-xl text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-cyan-500"
                  disabled={isSubmitting}
                />
              </div>
            )}

            <div className="flex flex-col gap-1.5">
              <label className="text-xs font-medium text-slate-300" htmlFor="auth-email">
                Email Address <span className="text-cyan-400">*</span>
              </label>
              <input
                id="auth-email"
                type="email"
                placeholder="user@example.com"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="w-full px-3.5 py-2 bg-slate-950 border border-slate-800 rounded-xl text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-cyan-500"
                disabled={isSubmitting}
              />
            </div>

            <div className="flex flex-col gap-1.5">
              <div className="flex items-center justify-between">
                <label className="text-xs font-medium text-slate-300" htmlFor="auth-password">
                  Password <span className="text-cyan-400">*</span>
                </label>
                {mode === 'login' && (
                  <button
                    type="button"
                    onClick={() => {
                      setMode('forgot-password');
                      setErrorMessage(null);
                      setSuccessMessage(null);
                    }}
                    className="text-[11px] text-cyan-400 hover:text-cyan-300 cursor-pointer"
                  >
                    Forgot password?
                  </button>
                )}
              </div>
              <input
                id="auth-password"
                type="password"
                placeholder="••••••••••••"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full px-3.5 py-2 bg-slate-950 border border-slate-800 rounded-xl text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-cyan-500"
                disabled={isSubmitting}
              />
              <span className="text-[11px] text-slate-500">
                Must be at least 8 characters.
              </span>
            </div>

            <button
              type="submit"
              disabled={isSubmitting}
              className="w-full mt-2 py-2.5 px-4 bg-gradient-to-r from-teal-500 to-cyan-600 hover:from-teal-400 hover:to-cyan-500 text-slate-950 font-semibold rounded-xl text-sm transition shadow-md shadow-cyan-900/30 disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
            >
              {isSubmitting
                ? 'Processing...'
                : mode === 'login'
                ? 'Sign In'
                : 'Create Account'}
            </button>
          </form>
        )}

        {/* Footer Toggle */}
        {(mode === 'login' || mode === 'signup') && (
          <div className="text-center pt-2 text-xs text-slate-400 border-t border-slate-800/80">
            {mode === 'login' ? (
              <p>
                Don't have an account?{' '}
                <button
                  type="button"
                  onClick={() => {
                    setMode('signup');
                    setErrorMessage(null);
                    setSuccessMessage(null);
                  }}
                  className="text-cyan-400 hover:text-cyan-300 font-medium cursor-pointer"
                >
                  Create one now
                </button>
              </p>
            ) : (
              <p>
                Already have an account?{' '}
                <button
                  type="button"
                  onClick={() => {
                    setMode('login');
                    setErrorMessage(null);
                    setSuccessMessage(null);
                  }}
                  className="text-cyan-400 hover:text-cyan-300 font-medium cursor-pointer"
                >
                  Sign in
                </button>
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
