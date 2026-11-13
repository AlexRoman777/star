import { useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, ReactNode, useEffect, useState } from "react";

import { API_AUTH_EXPIRED_EVENT, apiFetch, clearApiAuthExpiryNotice } from "../api/client";
import type { AuthenticationStatus, Session } from "../api/types";
import { AuthenticationScreen } from "./AuthenticationScreen";
import type { AuthenticationMode } from "./AuthenticationScreen";

function caughtMessage(caught: unknown, fallback: string): string {
  return caught instanceof Error ? caught.message : fallback;
}

function logAuthFailure(context: string, caught: unknown): void {
  console.error("star.auth_error", {
    context,
    error: caught instanceof Error ? caught.message : String(caught),
  });
}

function setupHeaders(localSetup: boolean, form: FormData): HeadersInit | undefined {
  if (localSetup) return undefined;
  return { "X-Star-Bootstrap-Token": String(form.get("bootstrap_token") ?? "") };
}

interface AuthGateResultProps {
  loading: boolean;
  authenticated: boolean;
  children: ReactNode;
  screen: ReactNode;
}

function AuthGateResult({ loading, authenticated, children, screen }: AuthGateResultProps) {
  if (loading) return <div className="center-screen">Opening the private library…</div>;
  if (authenticated) return children;
  return screen;
}

export function AuthGate({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const session = useQuery({
    queryKey: ["session"],
    queryFn: () => apiFetch<Session>("/auth/session"),
    retry: false,
  });
  const authStatus = useQuery({
    queryKey: ["auth-status"],
    queryFn: () => apiFetch<AuthenticationStatus>("/auth/status"),
    retry: false,
  });
  const [mode, setMode] = useState<AuthenticationMode>("password");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const expireSession = () => {
      queryClient.setQueryData<Session>(["session"], {
        authenticated: false,
        user: null,
      });
      void queryClient.cancelQueries({
        predicate: (query) => query.queryKey[0] !== "auth-status",
      });
      queryClient.removeQueries({
        predicate: (query) => !["session", "auth-status"].includes(String(query.queryKey[0])),
      });
      setError("Your Star session expired. Sign in again.");
    };
    window.addEventListener(API_AUTH_EXPIRED_EVENT, expireSession);
    return () => window.removeEventListener(API_AUTH_EXPIRED_EVENT, expireSession);
  }, [queryClient]);

  const setupAvailable =
    authStatus.data?.initial_password_setup_available ||
    authStatus.data?.administrator_exists === false;

  async function finishAuthentication() {
    clearApiAuthExpiryNotice();
    await queryClient.invalidateQueries({ queryKey: ["session"] });
    await queryClient.invalidateQueries({ queryKey: ["auth-status"] });
  }

  function selectMode(nextMode: AuthenticationMode) {
    setMode(nextMode);
    setError("");
  }

  async function loginWithPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const form = new FormData(event.currentTarget);
    try {
      await apiFetch<Session>("/auth/password/login", {
        method: "POST",
        body: JSON.stringify({
          username: form.get("username"),
          password: form.get("password"),
        }),
      });
      await finishAuthentication();
    } catch (caught) {
      logAuthFailure("login", caught);
      const message = setupAvailable
        ? "No Star password is set yet. Choose Set initial password below."
        : caught instanceof Error
          ? caught.message
          : "Authentication failed";
      setError(message);
    } finally {
      setBusy(false);
    }
  }

  async function setupPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const form = new FormData(event.currentTarget);
    const password = String(form.get("password") ?? "");
    const confirmation = String(form.get("password_confirmation") ?? "");
    if (password !== confirmation) {
      setError("The two passwords do not match.");
      setBusy(false);
      return;
    }
    try {
      await apiFetch<Session>("/auth/password/setup", {
        method: "POST",
        headers: setupHeaders(Boolean(authStatus.data?.local_password_setup_available), form),
        body: JSON.stringify({
          username: form.get("username"),
          display_name: form.get("display_name") || null,
          password,
        }),
      });
      await finishAuthentication();
    } catch (caught) {
      logAuthFailure("initial-password-setup", caught);
      setError(caughtMessage(caught, "Password setup failed"));
    } finally {
      setBusy(false);
    }
  }

  const existingAdministrator = authStatus.data?.administrator_exists !== false;
  const screen = <AuthenticationScreen mode={mode} busy={busy} error={error} setupAvailable={setupAvailable} existingAdministrator={existingAdministrator} localPasswordSetupAvailable={Boolean(authStatus.data?.local_password_setup_available)} selectMode={selectMode} loginWithPassword={(event) => void loginWithPassword(event)} setupPassword={(event) => void setupPassword(event)} />;
  return <AuthGateResult loading={session.isLoading || authStatus.isLoading} authenticated={Boolean(session.data?.authenticated)} screen={screen}>{children}</AuthGateResult>;
}
