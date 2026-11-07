import { createContext, useContext, useState, useEffect, type ReactNode } from 'react';
import type { User, LoginCredentials, SignupCredentials } from '../types/auth';
import { authLogin, authSignup, authLogout, authGetMe } from '../api/client';

const TOKEN_STORAGE_KEY = 'sebi_rag_access_token';

interface AuthContextValue {
  user: User | null;
  token: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  login: (credentials: LoginCredentials) => Promise<void>;
  signup: (credentials: SignupCredentials) => Promise<void>;
  logout: () => Promise<void>;
  refreshUser: () => Promise<void>;
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
  const [isLoading, setIsLoading] = useState<boolean>(true);

  // Restore session from token on startup
  useEffect(() => {
    let ignore = false;

    async function initSession() {
      if (!token) {
        setIsLoading(false);
        return;
      }

      try {
        const currentUser = await authGetMe(token);
        if (!ignore) {
          setUser(currentUser);
        }
      } catch {
        // Token invalid, expired, or user deactivated: clear stored session
        if (!ignore) {
          setUser(null);
          setToken(null);
          try {
            localStorage.removeItem(TOKEN_STORAGE_KEY);
          } catch {
            // Storage unavailable
          }
        }
      } finally {
        if (!ignore) {
          setIsLoading(false);
        }
      }
    }

    initSession();

    return () => {
      ignore = true;
    };
  }, [token]);

  const login = async (credentials: LoginCredentials) => {
    const res = await authLogin(credentials);
    setToken(res.access_token);
    setUser(res.user);
    try {
      localStorage.setItem(TOKEN_STORAGE_KEY, res.access_token);
    } catch {
      // Storage unavailable
    }
  };

  const signup = async (credentials: SignupCredentials) => {
    const res = await authSignup(credentials);
    setToken(res.access_token);
    setUser(res.user);
    try {
      localStorage.setItem(TOKEN_STORAGE_KEY, res.access_token);
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
