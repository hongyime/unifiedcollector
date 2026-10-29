import { createContext, useContext, useState, useCallback, useEffect } from "react";
import { api } from "../services/api";
import type { AuthUser } from "../services/types";

interface AuthState {
  user: AuthUser | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
}

export const AuthContext = createContext<AuthState>({
  user: null,
  loading: true,
  login: async () => {},
  logout: () => {},
});

export function useAuth() {
  return useContext(AuthContext);
}

export function useAuthProvider(): AuthState {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);

  // Restore the session on mount: with a stored token, /auth/me validates it
  // via authHeaders(); without one it still answers 200 when
  // DASHBOARD_AUTH_DISABLED=true (default admin) and 401 when auth is enforced.
  useEffect(() => {
    let cancelled = false;
    api
      .me()
      .then((me) => {
        if (!cancelled) setUser({ username: me.username, role: me.role as AuthUser["role"] });
      })
      .catch((err) => {
        // Stale/invalid token → drop it. Other failures (network) keep the
        // token so a transient backend blip doesn't log the user out.
        if ((err as { status?: number }).status === 401) {
          localStorage.removeItem("auth_token");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    try {
      const res = await api.login(username, password);
      localStorage.setItem("auth_token", res.token);
      setUser({ username: res.username, role: res.role as AuthUser["role"] });
    } catch (err) {
      // Surface FastAPI's `detail` (e.g. "Invalid credentials") over the raw
      // "401 Unauthorized" status line.
      const e = err as Error & { detail?: unknown };
      throw new Error(typeof e.detail === "string" && e.detail ? e.detail : e.message);
    }
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem("auth_token");
    setUser(null);
  }, []);

  return { user, loading, login, logout };
}
