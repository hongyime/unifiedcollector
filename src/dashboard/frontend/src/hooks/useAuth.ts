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
  const [user, setUser] = useState<AuthUser | null>({ username: "admin", role: "admin" });
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    // Auth bypassed
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    setUser({ username: "admin", role: "admin" });
  }, []);

  const logout = useCallback(() => {
    // Auth bypassed
  }, []);

  return { user, loading, login, logout };
}
