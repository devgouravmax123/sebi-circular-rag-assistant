import { createContext, useContext, useState, useEffect, type ReactNode } from 'react';
import type { User, LoginCredentials, SignupCredentials } from '../types/auth';
import type { GuestSessionStatusResponse, GuestSessionTerminationResponse } from '../types/guest';
import {
  authLogin,
  authSignup,
  authLogout,
  authGetMe,
  authLinkLegacy,
  initiateGuestSession,
  fetchGuestSessionStatus,
  terminateGuestSession,
} from '../api/client';
import {
  isFirebaseClientConfigured,
  firebaseRegisterUser,
  firebaseSignInUser,
  firebaseSendVerificationEmail,
  firebaseCheckEmailVerification,
  firebaseSendPasswordReset,
  firebaseSignOutUser,
  firebaseGetIdToken,
  onAuthStateSubscription,
} from '../api/firebase';

const TOKEN_STORAGE_KEY = 'citenza_access_token';
const LEGACY_TOKEN_STORAGE_KEY = 'sebi_rag_access_token';
const GUEST_TOKEN_STORAGE_KEY = 'citenza_guest_token';
const LEGACY_GUEST_TOKEN_STORAGE_KEY = 'doc_intel_guest_token';

interface AuthContextValue {
  user: User | null;
  token: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  login: (credentials: LoginCredentials) => Promise<void>;
  signup: (credentials: SignupCredentials) => Promise<void>;
  logout: () => Promise<void>;
  refreshUser: () => Promise<void>;

  // Firebase Email Verification & Recovery (Milestone 18)
  isEmailVerified: boolean;
  isVerificationPending: boolean;
  pendingVerificationEmail: string | null;
  checkEmailVerification: () => Promise<boolean>;
  resendVerificationEmail: () => Promise<void>;
  sendPasswordReset: (email: string) => Promise<void>;
  linkLegacyAccount: (password: string) => Promise<void>;
  pendingLegacyLinkToken: string | null;

  // Ephemeral Guest Mode (Milestone 11)
  isGuest: boolean;
  guestToken: string | null;
  guestSession: GuestSessionStatusResponse | null;
  startGuestSession: () => Promise<void>;
  endGuestSession: () => Promise<GuestSessionTerminationResponse | null>;
  refreshGuestStatus: () => Promise<void>;
}

const defaultAuthValue: AuthContextValue = {
  user: null,
  token: null,
  isAuthenticated: false,
  isLoading: false,
  login: async () => {},
  signup: async () => {},
  logout: async () => {},
  refreshUser: async () => {},

  isEmailVerified: false,
  isVerificationPending: false,
  pendingVerificationEmail: null,
  checkEmailVerification: async () => false,
  resendVerificationEmail: async () => {},
  sendPasswordReset: async () => {},
  linkLegacyAccount: async () => {},
  pendingLegacyLinkToken: null,

  isGuest: false,
  guestToken: null,
  guestSession: null,
  startGuestSession: async () => {},
  endGuestSession: async () => null,
  refreshGuestStatus: async () => {},
};

const AuthContext = createContext<AuthContextValue>(defaultAuthValue);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [token, setToken] = useState<string | null>(() => {
    try {
      return localStorage.getItem(TOKEN_STORAGE_KEY) || localStorage.getItem(LEGACY_TOKEN_STORAGE_KEY);
    } catch {
      return null;
    }
  });

  const [guestToken, setGuestToken] = useState<string | null>(() => {
    try {
      return localStorage.getItem(GUEST_TOKEN_STORAGE_KEY) || localStorage.getItem(LEGACY_GUEST_TOKEN_STORAGE_KEY);
    } catch {
      return null;
    }
  });
  const [guestSession, setGuestSession] = useState<GuestSessionStatusResponse | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);

  // Email verification state
  const [isVerificationPending, setIsVerificationPending] = useState<boolean>(false);
  const [pendingVerificationEmail, setPendingVerificationEmail] = useState<string | null>(null);
  const [pendingLegacyLinkToken, setPendingLegacyLinkToken] = useState<string | null>(null);

  // Restore registered user session or guest session on startup
  useEffect(() => {
    let ignore = false;

    // 1. If Firebase is configured, subscribe to Firebase auth state
    if (isFirebaseClientConfigured()) {
      const unsubscribe = onAuthStateSubscription(async (fbUser) => {
        if (ignore) return;

        if (!fbUser) {
          // If no Firebase user, check if we have a valid guest token
          if (guestToken) {
            try {
              const status = await fetchGuestSessionStatus(guestToken);
              if (!ignore && status.is_active && status.time_remaining_seconds > 0) {
                setGuestSession(status);
              } else {
                setGuestToken(null);
                setGuestSession(null);
                try {
                  localStorage.removeItem(GUEST_TOKEN_STORAGE_KEY);
                } catch {}
              }
            } catch {
              if (!ignore) {
                setGuestToken(null);
                setGuestSession(null);
              }
            }
          }
          if (!ignore) {
            setUser(null);
            setToken(null);
            setIsLoading(false);
          }
          return;
        }

        if (fbUser.emailVerified) {
          try {
            const idToken = await fbUser.getIdToken();
            const currentUser = await authGetMe(idToken);
            if (!ignore) {
              setUser(currentUser);
              setToken(idToken);
              setIsVerificationPending(false);
              setPendingVerificationEmail(null);
              try {
                localStorage.setItem(TOKEN_STORAGE_KEY, idToken);
                localStorage.removeItem(GUEST_TOKEN_STORAGE_KEY);
              } catch {}
            }
          } catch {
            if (!ignore) {
              setUser(null);
              setToken(null);
            }
          }
        } else {
          if (!ignore) {
            setIsVerificationPending(true);
            setPendingVerificationEmail(fbUser.email);
            setUser(null);
            setToken(null);
          }
        }

        if (!ignore) {
          setIsLoading(false);
        }
      });

      return () => {
        ignore = true;
        unsubscribe();
      };
    }

    // 2. Direct backend auth fallback (offline / tests)
    async function initFallbackSessions() {
      if (token) {
        try {
          const currentUser = await authGetMe(token);
          if (!ignore) {
            setUser(currentUser);
            setIsLoading(false);
          }
          return;
        } catch {
          if (!ignore) {
            setUser(null);
            setToken(null);
            try {
              localStorage.removeItem(TOKEN_STORAGE_KEY);
            } catch {}
          }
        }
      }

      if (guestToken) {
        try {
          const status = await fetchGuestSessionStatus(guestToken);
          if (!ignore) {
            if (status.is_active && status.time_remaining_seconds > 0) {
              setGuestSession(status);
            } else {
              setGuestToken(null);
              setGuestSession(null);
              try {
                localStorage.removeItem(GUEST_TOKEN_STORAGE_KEY);
              } catch {}
            }
          }
        } catch {
          if (!ignore) {
            setGuestToken(null);
            setGuestSession(null);
            try {
              localStorage.removeItem(GUEST_TOKEN_STORAGE_KEY);
            } catch {}
          }
        }
      }

      if (!ignore) {
        setIsLoading(false);
      }
    }

    initFallbackSessions();

    return () => {
      ignore = true;
    };
  }, [token, guestToken]);

  // Periodic guest session TTL countdown check (every 30 seconds)
  useEffect(() => {
    if (!guestToken || !guestSession) return;

    const interval = setInterval(async () => {
      try {
        const updated = await fetchGuestSessionStatus(guestToken);
        if (!updated.is_active || updated.time_remaining_seconds <= 0) {
          setGuestToken(null);
          setGuestSession(null);
          try {
            localStorage.removeItem(GUEST_TOKEN_STORAGE_KEY);
          } catch {}
        } else {
          setGuestSession(updated);
        }
      } catch {
        setGuestToken(null);
        setGuestSession(null);
        try {
          localStorage.removeItem(GUEST_TOKEN_STORAGE_KEY);
        } catch {}
      }
    }, 30000);

    return () => clearInterval(interval);
  }, [guestToken, guestSession]);

  const login = async (credentials: LoginCredentials) => {
    if (isFirebaseClientConfigured()) {
      const fbUser = await firebaseSignInUser(credentials.email, credentials.password);
      if (!fbUser.emailVerified) {
        setPendingVerificationEmail(credentials.email);
        setIsVerificationPending(true);
        throw new Error('EMAIL_VERIFICATION_REQUIRED');
      }

      const idToken = await fbUser.getIdToken();
      try {
        const currentUser = await authGetMe(idToken);
        setToken(idToken);
        setUser(currentUser);
        setIsVerificationPending(false);
        setPendingVerificationEmail(null);
        setPendingLegacyLinkToken(null);
        setGuestToken(null);
        setGuestSession(null);
        try {
          localStorage.setItem(TOKEN_STORAGE_KEY, idToken);
          localStorage.removeItem(GUEST_TOKEN_STORAGE_KEY);
        } catch {}
      } catch (err: unknown) {
        const errStatus = (err as unknown as { status?: number })?.status;
        if (errStatus === 409) {
          setPendingLegacyLinkToken(idToken);
          setPendingVerificationEmail(credentials.email);
          throw new Error('LEGACY_ACCOUNT_NEEDS_LINKING');
        }
        throw err;
      }
      return;
    }

    // Direct backend fallback
    const res = await authLogin(credentials);
    setToken(res.access_token);
    setUser(res.user);
    setGuestToken(null);
    setGuestSession(null);
    try {
      localStorage.setItem(TOKEN_STORAGE_KEY, res.access_token);
      localStorage.removeItem(GUEST_TOKEN_STORAGE_KEY);
    } catch {}
  };

  const signup = async (credentials: SignupCredentials) => {
    if (isFirebaseClientConfigured()) {
      await firebaseRegisterUser(
        credentials.email,
        credentials.password,
        credentials.full_name
      );
      setPendingVerificationEmail(credentials.email);
      setIsVerificationPending(true);
      return;
    }

    // Direct backend fallback
    const res = await authSignup(credentials);
    setToken(res.access_token);
    setUser(res.user);
    setGuestToken(null);
    setGuestSession(null);
    try {
      localStorage.setItem(TOKEN_STORAGE_KEY, res.access_token);
      localStorage.removeItem(GUEST_TOKEN_STORAGE_KEY);
    } catch {}
  };

  const checkEmailVerification = async (): Promise<boolean> => {
    if (!isFirebaseClientConfigured()) return true;

    const isVerified = await firebaseCheckEmailVerification();
    if (isVerified) {
      const idToken = await firebaseGetIdToken(true);
      if (idToken) {
        try {
          const currentUser = await authGetMe(idToken);
          setToken(idToken);
          setUser(currentUser);
          setIsVerificationPending(false);
          setPendingVerificationEmail(null);
          setGuestToken(null);
          setGuestSession(null);
          try {
            localStorage.setItem(TOKEN_STORAGE_KEY, idToken);
            localStorage.removeItem(GUEST_TOKEN_STORAGE_KEY);
          } catch {}
          return true;
        } catch (err: unknown) {
          const errStatus = (err as unknown as { status?: number })?.status;
          if (errStatus === 409) {
            setPendingLegacyLinkToken(idToken);
            throw new Error('LEGACY_ACCOUNT_NEEDS_LINKING');
          }
          throw err;
        }
      }
    }
    return false;
  };

  const resendVerificationEmail = async () => {
    if (isFirebaseClientConfigured()) {
      await firebaseSendVerificationEmail();
    }
  };

  const sendPasswordReset = async (email: string) => {
    if (isFirebaseClientConfigured()) {
      await firebaseSendPasswordReset(email);
    }
  };

  const linkLegacyAccount = async (password: string) => {
    if (!pendingLegacyLinkToken) {
      throw new Error('No pending authentication to link.');
    }
    const linkedUser = await authLinkLegacy(password, pendingLegacyLinkToken);
    setToken(pendingLegacyLinkToken);
    setUser(linkedUser);
    setIsVerificationPending(false);
    setPendingVerificationEmail(null);
    setPendingLegacyLinkToken(null);
    setGuestToken(null);
    setGuestSession(null);
    try {
      localStorage.setItem(TOKEN_STORAGE_KEY, pendingLegacyLinkToken);
      localStorage.removeItem(GUEST_TOKEN_STORAGE_KEY);
    } catch {}
  };

  const logout = async () => {
    if (isFirebaseClientConfigured()) {
      await firebaseSignOutUser();
    } else if (token) {
      await authLogout(token);
    }
    setToken(null);
    setUser(null);
    setIsVerificationPending(false);
    setPendingVerificationEmail(null);
    setPendingLegacyLinkToken(null);
    try {
      localStorage.removeItem(TOKEN_STORAGE_KEY);
      localStorage.removeItem(LEGACY_TOKEN_STORAGE_KEY);
    } catch {}
  };

  const refreshUser = async () => {
    if (!token) return;
    try {
      const refreshed = await authGetMe(token);
      setUser(refreshed);
    } catch {
      await logout();
    }
  };

  const startGuestSession = async () => {
    setIsLoading(true);
    try {
      const res = await initiateGuestSession();
      setGuestToken(res.token);
      try {
        localStorage.setItem(GUEST_TOKEN_STORAGE_KEY, res.token);
      } catch {}
      const status = await fetchGuestSessionStatus(res.token);
      setGuestSession(status);
    } finally {
      setIsLoading(false);
    }
  };

  const endGuestSession = async (): Promise<GuestSessionTerminationResponse | null> => {
    if (!guestToken) return null;
    try {
      const result = await terminateGuestSession(guestToken);
      setGuestToken(null);
      setGuestSession(null);
      try {
        localStorage.removeItem(GUEST_TOKEN_STORAGE_KEY);
        localStorage.removeItem(LEGACY_GUEST_TOKEN_STORAGE_KEY);
      } catch {}
      return result;
    } catch {
      setGuestToken(null);
      setGuestSession(null);
      try {
        localStorage.removeItem(GUEST_TOKEN_STORAGE_KEY);
        localStorage.removeItem(LEGACY_GUEST_TOKEN_STORAGE_KEY);
      } catch {}
      return null;
    }
  };

  const refreshGuestStatus = async () => {
    if (!guestToken) return;
    try {
      const status = await fetchGuestSessionStatus(guestToken);
      if (status.is_active && status.time_remaining_seconds > 0) {
        setGuestSession(status);
      } else {
        setGuestToken(null);
        setGuestSession(null);
        try {
          localStorage.removeItem(GUEST_TOKEN_STORAGE_KEY);
        } catch {}
      }
    } catch {
      setGuestToken(null);
      setGuestSession(null);
      try {
        localStorage.removeItem(GUEST_TOKEN_STORAGE_KEY);
      } catch {}
    }
  };

  return (
    <AuthContext.Provider
      value={{
        user,
        token,
        isAuthenticated: !!token && !!user,
        isLoading,
        login,
        signup,
        logout,
        refreshUser,

        isEmailVerified: !!user?.email_verified,
        isVerificationPending,
        pendingVerificationEmail,
        checkEmailVerification,
        resendVerificationEmail,
        sendPasswordReset,
        linkLegacyAccount,
        pendingLegacyLinkToken,

        isGuest: !!guestToken,
        guestToken,
        guestSession,
        startGuestSession,
        endGuestSession,
        refreshGuestStatus,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}
