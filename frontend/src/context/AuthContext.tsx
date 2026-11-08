import { createContext, useContext, useState, useEffect, type ReactNode } from 'react';
import type { User, LoginCredentials, SignupCredentials } from '../types/auth';
import type { GuestSessionStatusResponse, GuestSessionTerminationResponse } from '../types/guest';
import {
  authLogin,
  authSignup,
  authLogout,
  authGetMe,
  initiateGuestSession,
  fetchGuestSessionStatus,
  terminateGuestSession,
} from '../api/client';

const TOKEN_STORAGE_KEY = 'sebi_rag_access_token';
const GUEST_TOKEN_STORAGE_KEY = 'doc_intel_guest_token';

interface AuthContextValue {
  user: User | null;
  token: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  login: (credentials: LoginCredentials) => Promise<void>;
  signup: (credentials: SignupCredentials) => Promise<void>;
  logout: () => Promise<void>;
  refreshUser: () => Promise<void>;

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
      return localStorage.getItem(TOKEN_STORAGE_KEY);
    } catch {
      return null;
    }
  });

  const [guestToken, setGuestToken] = useState<string | null>(() => {
    try {
      return localStorage.getItem(GUEST_TOKEN_STORAGE_KEY);
    } catch {
      return null;
    }
  });
  const [guestSession, setGuestSession] = useState<GuestSessionStatusResponse | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);

  // Restore registered user session or guest session on startup
  useEffect(() => {
    let ignore = false;

    async function initSessions() {
      // 1. If registered token exists, validate it
      if (token) {
        try {
          const currentUser = await authGetMe(token);
          if (!ignore) {
            setUser(currentUser);
            setIsLoading(false);
          }
          return;
        } catch {
          // Token invalid, expired, or user deactivated: clear stored session
          if (!ignore) {
            setUser(null);
            setToken(null);
            try {
              localStorage.removeItem(TOKEN_STORAGE_KEY);
            } catch {
              // Ignore
            }
          }
        }
      }

      // 2. If no registered user, check if active guest token exists
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
              } catch {
                // Ignore
              }
            }
          }
        } catch {
          if (!ignore) {
            setGuestToken(null);
            setGuestSession(null);
            try {
              localStorage.removeItem(GUEST_TOKEN_STORAGE_KEY);
            } catch {
              // Ignore
            }
          }
        }
      }

      if (!ignore) {
        setIsLoading(false);
      }
    }

    initSessions();

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
          } catch {
            // Ignore
          }
        } else {
          setGuestSession(updated);
        }
      } catch {
        // Session expired or cleaned up
        setGuestToken(null);
        setGuestSession(null);
        try {
          localStorage.removeItem(GUEST_TOKEN_STORAGE_KEY);
        } catch {
          // Ignore
        }
      }
    }, 30000);

    return () => clearInterval(interval);
  }, [guestToken, guestSession]);

  const login = async (credentials: LoginCredentials) => {
    const res = await authLogin(credentials);
    setToken(res.access_token);
    setUser(res.user);
    // If transitioning from guest, clear guest session
    setGuestToken(null);
    setGuestSession(null);
    try {
      localStorage.setItem(TOKEN_STORAGE_KEY, res.access_token);
      localStorage.removeItem(GUEST_TOKEN_STORAGE_KEY);
    } catch {
      // Storage unavailable
    }
  };

  const signup = async (credentials: SignupCredentials) => {
    const res = await authSignup(credentials);
    setToken(res.access_token);
    setUser(res.user);
    setGuestToken(null);
    setGuestSession(null);
    try {
      localStorage.setItem(TOKEN_STORAGE_KEY, res.access_token);
      localStorage.removeItem(GUEST_TOKEN_STORAGE_KEY);
    } catch {
      // Storage unavailable
    }
  };

  const logout = async () => {
    if (token) {
      await authLogout(token);
    }
    setToken(null);
    setUser(null);
    try {
      localStorage.removeItem(TOKEN_STORAGE_KEY);
    } catch {
      // Storage unavailable
    }
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
      } catch {
        // Storage unavailable
      }
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
      } catch {
        // Storage unavailable
      }
      return result;
    } catch {
      setGuestToken(null);
      setGuestSession(null);
      try {
        localStorage.removeItem(GUEST_TOKEN_STORAGE_KEY);
      } catch {
        // Storage unavailable
      }
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
        } catch {
          // Ignore
        }
      }
    } catch {
      setGuestToken(null);
      setGuestSession(null);
      try {
        localStorage.removeItem(GUEST_TOKEN_STORAGE_KEY);
      } catch {
        // Ignore
      }
    }
  };

  const isGuest = !user && !!guestToken && !!guestSession;

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
        isGuest,
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
  return context || defaultAuthValue;
}
