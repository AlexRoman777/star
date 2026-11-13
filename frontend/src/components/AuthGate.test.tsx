import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AuthGate } from "./AuthGate";
import { API_AUTH_EXPIRED_EVENT, clearApiAuthExpiryNotice } from "../api/client";

function renderGate() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><AuthGate><div>Private application</div></AuthGate></QueryClientProvider>);
}

afterEach(() => {
  cleanup();
  clearApiAuthExpiryNotice();
  vi.unstubAllGlobals();
});

describe("AuthGate", () => {
  it("shows password sign-in and a separate initial-password action", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path.endsWith("/auth/status")) {
          return new Response(
            JSON.stringify({
              administrator_exists: true,
              initial_password_setup_available: true,
              password_login_enabled: true,
              local_password_setup_available: true,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(JSON.stringify({ detail: "authentication required" }), {
          status: 401,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
    renderGate();

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Star" })).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toHaveAttribute(
      "autocomplete",
      "current-password",
    );
    fireEvent.click(screen.getByRole("button", { name: "Set initial password" }));
    expect(
      await screen.findByRole("heading", { name: "Set initial password" }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Bootstrap token")).not.toBeInTheDocument();
    expect(screen.queryByText("Private application")).not.toBeInTheDocument();
  });

  it("validates and completes local password setup", async () => {
    let authenticated = false;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/auth/status")) return new Response(JSON.stringify({ administrator_exists: true, initial_password_setup_available: true, password_login_enabled: true, local_password_setup_available: true }), { status: 200, headers: { "Content-Type": "application/json" } });
      if (path.endsWith("/auth/password/setup")) {
        authenticated = true;
        return new Response(JSON.stringify({ authenticated: true, user: { id: "user-1", username: "owner", display_name: "Owner", role: "administrator" } }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (path.endsWith("/auth/session") && authenticated) return new Response(JSON.stringify({ authenticated: true, user: { id: "user-1", username: "owner", display_name: "Owner", role: "administrator" } }), { status: 200, headers: { "Content-Type": "application/json" } });
      return new Response(JSON.stringify({ detail: "authentication required" }), { status: 401, headers: { "Content-Type": "application/json" } });
    }));
    renderGate();
    fireEvent.click(await screen.findByRole("button", { name: "Set initial password" }));
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "owner" } });
    fireEvent.change(screen.getByLabelText("New password"), { target: { value: "password1" } });
    fireEvent.change(screen.getByLabelText("Confirm password"), { target: { value: "different" } });
    fireEvent.click(screen.getByRole("button", { name: "Save password and sign in" }));
    expect(await screen.findByText("The two passwords do not match.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Confirm password"), { target: { value: "password1" } });
    fireEvent.click(screen.getByRole("button", { name: "Save password and sign in" }));
    expect(await screen.findByText("Private application")).toBeInTheDocument();
  });

  it("uses the local password sign-in path", async () => {
    let authenticated = false;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/auth/status")) return new Response(JSON.stringify({ administrator_exists: true, initial_password_setup_available: false, password_login_enabled: true, local_password_setup_available: false }), { status: 200, headers: { "Content-Type": "application/json" } });
      if (path.endsWith("/auth/password/login")) {
        authenticated = true;
        return new Response(JSON.stringify({ authenticated: true }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (path.endsWith("/auth/session") && authenticated) return new Response(JSON.stringify({ authenticated: true }), { status: 200, headers: { "Content-Type": "application/json" } });
      return new Response(JSON.stringify({ detail: "authentication required" }), { status: 401, headers: { "Content-Type": "application/json" } });
    }));
    renderGate();
    fireEvent.change(await screen.findByLabelText("Username"), { target: { value: "owner" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "password1" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Private application")).toBeInTheDocument();
  });

  it("immediately closes the private surface when the session expires", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/auth/status")) {
        return new Response(JSON.stringify({
          administrator_exists: true,
          initial_password_setup_available: false,
          password_login_enabled: true,
          local_password_setup_available: false,
        }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      return new Response(JSON.stringify({
        authenticated: true,
        user: { id: "user-1", username: "owner", display_name: "Owner", role: "administrator" },
      }), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    renderGate();
    expect(await screen.findByText("Private application")).toBeInTheDocument();

    window.dispatchEvent(new CustomEvent(API_AUTH_EXPIRED_EVENT, {
      detail: { path: "/dashboard", requestId: "request-401" },
    }));

    await waitFor(() => expect(screen.queryByText("Private application")).not.toBeInTheDocument());
    expect(screen.getByText("Your Star session expired. Sign in again.")).toBeInTheDocument();
  });
});
