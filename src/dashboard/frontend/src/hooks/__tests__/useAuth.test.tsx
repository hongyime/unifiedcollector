// useAuth wiring tests: login → token storage, failed login → error + no
// storage, logout → clear, session restore via GET /auth/me (pre-existing
// token, invalid token, and the auth-disabled default user).
//
// The api module is mocked — these tests assert hook behavior, not fetch.
// Backend contract (src/dashboard/api/auth.py):
//   POST /auth/login → {token, username, role} | 401 {detail}
//   GET  /auth/me    → {username, role} (200 with no token when
//                       DASHBOARD_AUTH_DISABLED=true)
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { useState } from "react";
import { MemoryRouter, Routes, Route } from "react-router";

vi.mock("../../services/api", () => ({
  api: {
    login: vi.fn(),
    me: vi.fn(),
  },
}));

import { api } from "../../services/api";
import { useAuthProvider, useAuth, AuthContext } from "../useAuth";
import { LoginPage } from "../../features/auth/LoginPage";

const mockLogin = vi.mocked(api.login);
const mockMe = vi.mocked(api.me);

function unauthorized() {
  return Object.assign(new Error("401 Unauthorized"), { status: 401 });
}

// Mounts a real AuthProvider value around children so useAuth() consumers see
// the live state (mirrors the AuthProvider wiring in App.tsx).
function Harness({ children }: { children: React.ReactNode }) {
  const auth = useAuthProvider();
  return <AuthContext.Provider value={auth}>{children}</AuthContext.Provider>;
}

// Renders the hook state and exposes login/logout through buttons so tests
// drive the hook exactly like a component would.
function Probe() {
  const { user, loading, login, logout } = useAuth();
  const [err, setErr] = useState("");
  return (
    <div>
      <div data-testid="loading">{String(loading)}</div>
      <div data-testid="user">{user ? `${user.username}:${user.role}` : "none"}</div>
      {err && <div data-testid="error">{err}</div>}
      <button onClick={() => login("admin", "pw").catch((e: Error) => setErr(e.message))}>
        do-login
      </button>
      <button onClick={logout}>do-logout</button>
    </div>
  );
}

function renderProbe() {
  return render(
    <Harness>
      <Probe />
    </Harness>,
  );
}

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
});

afterEach(() => {
  cleanup();
});

describe("useAuth", () => {
  it("successful login stores the token and renders the user", async () => {
    // No token + auth enabled → mount /auth/me probe 401s, user stays null.
    mockMe.mockRejectedValue(unauthorized());
    mockLogin.mockResolvedValue({ token: "tok-123", username: "admin", role: "admin" });

    renderProbe();
    await waitFor(() => expect(screen.getByTestId("loading")).toHaveTextContent("false"));
    expect(screen.getByTestId("user")).toHaveTextContent("none");

    fireEvent.click(screen.getByText("do-login"));
    expect(await screen.findByText("admin:admin")).toBeInTheDocument();
    expect(localStorage.getItem("auth_token")).toBe("tok-123");
  });

  it("failed login shows an error and stores nothing", async () => {
    mockMe.mockRejectedValue(unauthorized());
    mockLogin.mockRejectedValue(
      Object.assign(new Error("401 Unauthorized"), { status: 401, detail: "Invalid credentials" }),
    );

    renderProbe();
    await waitFor(() => expect(screen.getByTestId("loading")).toHaveTextContent("false"));

    fireEvent.click(screen.getByText("do-login"));
    expect(await screen.findByText("Invalid credentials")).toBeInTheDocument();
    expect(screen.getByTestId("user")).toHaveTextContent("none");
    expect(localStorage.getItem("auth_token")).toBeNull();
  });

  it("logout clears the user and the stored token", async () => {
    mockMe.mockRejectedValue(unauthorized());
    mockLogin.mockResolvedValue({ token: "tok-123", username: "admin", role: "admin" });

    renderProbe();
    await waitFor(() => expect(screen.getByTestId("loading")).toHaveTextContent("false"));
    fireEvent.click(screen.getByText("do-login"));
    await screen.findByText("admin:admin");

    fireEvent.click(screen.getByText("do-logout"));
    expect(screen.getByTestId("user")).toHaveTextContent("none");
    expect(localStorage.getItem("auth_token")).toBeNull();
  });

  it("pre-existing token restores the session via /auth/me", async () => {
    localStorage.setItem("auth_token", "tok-old");
    mockMe.mockResolvedValue({ username: "bob", role: "viewer" });

    renderProbe();
    expect(await screen.findByText("bob:viewer")).toBeInTheDocument();
    expect(mockMe).toHaveBeenCalledTimes(1);
  });

  it("invalid token is cleared when /auth/me returns 401", async () => {
    localStorage.setItem("auth_token", "tok-expired");
    mockMe.mockRejectedValue(unauthorized());

    renderProbe();
    await waitFor(() => expect(screen.getByTestId("loading")).toHaveTextContent("false"));
    expect(screen.getByTestId("user")).toHaveTextContent("none");
    expect(localStorage.getItem("auth_token")).toBeNull();
  });

  it("auth-disabled backend restores the default admin user without a token", async () => {
    // DASHBOARD_AUTH_DISABLED=true → /auth/me answers 200 with no token.
    mockMe.mockResolvedValue({ username: "localhost", role: "admin" });

    renderProbe();
    expect(await screen.findByText("localhost:admin")).toBeInTheDocument();
    expect(localStorage.getItem("auth_token")).toBeNull();
  });
});

describe("LoginPage", () => {
  it("surfaces the login error in the UI", async () => {
    mockMe.mockRejectedValue(unauthorized());
    mockLogin.mockRejectedValue(
      Object.assign(new Error("401 Unauthorized"), { status: 401, detail: "Invalid credentials" }),
    );

    render(
      <Harness>
        <MemoryRouter initialEntries={["/login"]}>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/" element={<div data-testid="home">home</div>} />
          </Routes>
        </MemoryRouter>
      </Harness>,
    );

    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "admin" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "wrong" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign In" }));

    expect(await screen.findByText("Invalid credentials")).toBeInTheDocument();
    expect(screen.queryByTestId("home")).not.toBeInTheDocument();
    expect(localStorage.getItem("auth_token")).toBeNull();
  });
});
