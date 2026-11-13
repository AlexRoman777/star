import { MutationCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { lazy, Suspense } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";

import { AuthGate } from "./components/AuthGate";
import { ApiActivityCenter } from "./components/ApiActivityCenter";
import { Shell } from "./components/Shell";
import { RouteErrorBoundary } from "./components/RouteErrorBoundary";
import { SystemEventsProvider } from "./features/live/SystemEventsProvider";
import { ROUTE_ALIASES } from "./navigation";

const AssetInspectorPage = lazy(() => import("./pages/AssetInspectorPage").then((module) => ({ default: module.AssetInspectorPage })));
const BurstPage = lazy(() => import("./pages/BurstPage").then((module) => ({ default: module.BurstPage })));
const HomePage = lazy(() => import("./pages/HomePage").then((module) => ({ default: module.HomePage })));
const CommandPage = lazy(() => import("./pages/CommandPage").then((module) => ({ default: module.CommandPage })));
const LibraryPage = lazy(() => import("./pages/LibraryPage").then((module) => ({ default: module.LibraryPage })));
const MachinesPage = lazy(() => import("./pages/MachinesPage").then((module) => ({ default: module.MachinesPage })));
const MachineDashboardPage = lazy(() => import("./pages/MachineDashboardPage").then((module) => ({ default: module.MachineDashboardPage })));
const MyStarRatingsPage = lazy(() => import("./pages/MyStarRatingsPage").then((module) => ({ default: module.MyStarRatingsPage })));
const ActivityPage = lazy(() => import("./pages/ActivityPage").then((module) => ({ default: module.ActivityPage })));
const WorkPage = lazy(() => import("./pages/WorkPage").then((module) => ({ default: module.WorkPage })));
const SettingsPage = lazy(() => import("./pages/SettingsPage").then((module) => ({ default: module.SettingsPage })));

function LegacyRedirect({ target }: { target: string }) {
  const location = useLocation();
  if (!location.search) return <Navigate to={target} replace />;
  const separator = target.includes("?") ? "&" : "?";
  return <Navigate to={`${target}${separator}${location.search.slice(1)}`} replace />;
}

function refreshAfterMutation(
  _data: unknown,
  _variables: unknown,
  _context: unknown,
  mutation: { meta?: Record<string, unknown> },
): Promise<void> {
  // A mutation may declare the query-key prefixes it affects via
  // meta.invalidates; only those active queries refetch. Mutations without a
  // declaration keep the conservative refresh-everything behavior so nothing
  // silently goes stale while call sites migrate.
  const declared = mutation.meta?.invalidates;
  if (Array.isArray(declared) && declared.length > 0) {
    return Promise.all(
      declared.map((queryKey) =>
        queryClient.invalidateQueries({ queryKey: queryKey as readonly unknown[], type: "active" }),
      ),
    ).then(() => undefined);
  }
  return queryClient.invalidateQueries({ type: "active" });
}

const mutationCache = new MutationCache({
  onSuccess: refreshAfterMutation,
});
const queryClient: QueryClient = new QueryClient({
  mutationCache,
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      retry: 1,
      refetchOnReconnect: "always",
      refetchOnWindowFocus: "always",
    },
    mutations: { retry: false },
  },
});

export function App() {
  return <QueryClientProvider client={queryClient}><ApiActivityCenter /><BrowserRouter><AuthGate><SystemEventsProvider><Shell><RouteErrorBoundary><Suspense fallback={<div className="panel empty-state large"><strong>Loading page…</strong></div>}><Routes>
    <Route path="/" element={<HomePage />} />
    <Route path="/progress" element={<Navigate to="/" replace />} />
    <Route path="/home" element={<Navigate to="/" replace />} />
    <Route path="/activity" element={<ActivityPage />} />
    <Route path="/command" element={<CommandPage />} />
    <Route path="/advanced" element={<Navigate to="/settings" replace />} />
    {ROUTE_ALIASES.map((alias) => <Route key={alias.path} path={alias.path} element={<Navigate to={alias.target} replace />} />)}
    <Route path="/photos" element={<LibraryPage />} />
    <Route path="/people" element={<LegacyRedirect target="/photos?section=people" />} />
    <Route path="/sequences" element={<Navigate to="/photos" replace />} />
    <Route path="/showcase" element={<LegacyRedirect target="/photos?section=highlights" />} />
    <Route path="/review" element={<Navigate to="/photos" replace />} />
    <Route path="/my-star-ratings" element={<MyStarRatingsPage />} />
    <Route path="/work" element={<WorkPage />} />
    <Route path="/machines" element={<MachinesPage />} />
    <Route path="/machines/:machineKey" element={<MachineDashboardPage />} />
    <Route path="/advanced/machines" element={<MachinesPage />} />
    <Route path="/settings" element={<SettingsPage />} />
    <Route path="/assets/:assetId" element={<AssetInspectorPage />} />
    <Route path="/bursts/:burstId" element={<BurstPage />} />
    <Route path="/immich" element={<LegacyRedirect target="/settings?section=import" />} />
    <Route path="/write" element={<LegacyRedirect target="/settings?section=write" />} />
    <Route path="/processing" element={<LegacyRedirect target="/work?view=run" />} />
    <Route path="/queue" element={<LegacyRedirect target="/work?view=queue" />} />
    <Route path="/operations" element={<LegacyRedirect target="/activity" />} />
    <Route path="/metrics" element={<LegacyRedirect target="/machines?section=metrics" />} />
    <Route path="/system" element={<LegacyRedirect target="/settings?section=health" />} />
    <Route path="/storage" element={<LegacyRedirect target="/settings?section=storage" />} />
    <Route path="/tech" element={<LegacyRedirect target="/machines?section=models" />} />
    <Route path="/statistics" element={<Navigate to="/work" replace />} />
    <Route path="/audit" element={<LegacyRedirect target="/settings?section=audit" />} />
    <Route path="/shortcuts" element={<Navigate to="/settings" replace />} />
    <Route path="*" element={<Navigate to="/" replace />} />
  </Routes></Suspense></RouteErrorBoundary></Shell></SystemEventsProvider></AuthGate></BrowserRouter></QueryClientProvider>;
}
