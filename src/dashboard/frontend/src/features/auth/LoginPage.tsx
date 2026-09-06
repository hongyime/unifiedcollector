import { useState } from "react";
import { useNavigate } from "react-router";
import { useAuth } from "../../hooks/useAuth";
import { Button } from "../../components/ui/Button";

export function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await login(username, password);
      navigate("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-background">
      <form onSubmit={handleSubmit} className="bg-surface border border-border rounded-lg p-8 w-full max-w-sm">
        <h1 className="text-xl font-semibold mb-6 text-center">Sign In</h1>
        {error && <p className="text-sm text-error bg-error/10 rounded-md px-3 py-2 mb-4">{error}</p>}
        <div className="mb-4">
          <label className="text-xs text-text-muted block mb-1">Username</label>
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            className="w-full bg-background border border-border rounded-md text-sm px-3 py-2 focus:outline-none focus:ring-2 focus:ring-white/20"
            autoFocus
          />
        </div>
        <div className="mb-6">
          <label className="text-xs text-text-muted block mb-1">Password</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full bg-background border border-border rounded-md text-sm px-3 py-2 focus:outline-none focus:ring-2 focus:ring-white/20"
          />
        </div>
        <Button type="submit" className="w-full" loading={loading} disabled={!username || !password}>
          Sign In
        </Button>
      </form>
    </div>
  );
}
