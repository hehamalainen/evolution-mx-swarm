import { FormEvent, useEffect, useState } from "react";
import { Switch, Route, Router as WouterRouter } from "wouter";
import { queryClient } from "./lib/queryClient";
import { QueryClientProvider } from "@tanstack/react-query";
import { Toaster } from "@/components/ui/toaster";
import { TooltipProvider } from "@/components/ui/tooltip";
import {
  AUTH_REQUIRED_EVENT,
  loginDashboardSession,
  useDashboardAuthStatusQuery,
} from "./lib/api_adapter";
import Home from "@/pages/Home";
import NotFound from "@/pages/not-found";

function AppRoutes() {
  return (
    <Switch>
      <Route path="/" component={Home} />
      <Route component={NotFound} />
    </Switch>
  );
}

function DashboardAuthGate() {
  const authQuery = useDashboardAuthStatusQuery();
  const [tokenInput, setTokenInput] = useState("");
  const [authError, setAuthError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [sessionExpired, setSessionExpired] = useState(false);

  useEffect(() => {
    const handleAuthRequired = () => {
      setSessionExpired(true);
      setAuthError("Dashboard session expired or unauthorized. Please sign in again.");
      void authQuery.refetch();
    };
    window.addEventListener(AUTH_REQUIRED_EVENT, handleAuthRequired);
    return () => window.removeEventListener(AUTH_REQUIRED_EVENT, handleAuthRequired);
  }, [authQuery]);

  if (authQuery.isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background text-foreground font-mono text-sm">
        Checking dashboard session...
      </div>
    );
  }

  const isAuthenticated = Boolean(authQuery.data?.authenticated) && !sessionExpired;
  if (!isAuthenticated) {
    const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      setIsSubmitting(true);
      setAuthError(null);
      try {
        const ok = await loginDashboardSession(tokenInput);
        if (!ok) {
          setAuthError("Invalid MX_DASHBOARD_TOKEN. Please verify your token and try again.");
          return;
        }
        setTokenInput("");
        setSessionExpired(false);
        await authQuery.refetch();
        await queryClient.invalidateQueries();
      } finally {
        setIsSubmitting(false);
      }
    };

    return (
      <div className="flex min-h-screen items-center justify-center bg-background p-6 text-foreground">
        <div className="hud-panel w-full max-w-md p-6">
          <h1 className="text-lg font-bold uppercase tracking-wider text-primary mb-2">
            Dashboard Sign In
          </h1>
          <p className="text-xs text-muted-foreground mb-4" id="v2-auth-message">
            {authError || "Enter MX_DASHBOARD_TOKEN to establish an authenticated session."}
          </p>
          <form id="v2-dashboard-auth-form" onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label
                htmlFor="v2-dashboard-token-input"
                className="block text-xs uppercase tracking-wider text-muted-foreground mb-1"
              >
                Access Token
              </label>
              <input
                id="v2-dashboard-token-input"
                type="password"
                value={tokenInput}
                onChange={(e) => setTokenInput(e.target.value)}
                placeholder="Enter MX_DASHBOARD_TOKEN..."
                autoComplete="current-password"
                required
                className="w-full rounded border border-white/10 bg-black/40 px-3 py-2 text-sm text-foreground focus:border-primary focus:outline-none"
              />
            </div>
            <button
              id="v2-dashboard-auth-submit"
              type="submit"
              disabled={isSubmitting}
              className="w-full rounded bg-primary px-4 py-2 text-xs font-bold uppercase tracking-wider text-primary-foreground hover:opacity-90 disabled:opacity-50"
            >
              {isSubmitting ? "Signing In..." : "Sign In"}
            </button>
          </form>
        </div>
      </div>
    );
  }

  return (
    <WouterRouter base="/v2">
      <AppRoutes />
    </WouterRouter>
  );
}

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <Toaster />
        <DashboardAuthGate />
      </TooltipProvider>
    </QueryClientProvider>
  );
}

export default App;
