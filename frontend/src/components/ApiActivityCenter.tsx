import { useEffect, useRef, useState } from "react";

import { API_ACTIVITY_EVENT, API_PENDING_EVENT } from "../api/client";
import type { ApiActivity, ApiPendingState } from "../api/client";

interface ActivityNotice extends ApiActivity {
  receivedAt: number;
}

const SUCCESS_LIFETIME_MS = 6_000;
const ERROR_LIFETIME_MS = 12_000;
const REFRESH_ERROR_DEDUPE_MS = 60_000;
const LOADING_REVEAL_MS = 1_500;

function traceLabel(requestId: string | null): string | null {
  if (!requestId) return null;
  return `Request ${requestId}`;
}

function failureContext(notice: ActivityNotice): string | null {
  if (notice.phase !== "failed" && notice.phase !== "refresh_failed") return null;
  const parts = [`${notice.method} /api/v1${notice.path}`];
  if (notice.status) parts.push(`HTTP ${notice.status}`);
  if (notice.errorClass) parts.push(notice.errorClass);
  if (notice.retryCount) parts.push(`${notice.retryCount} automatic retries exhausted`);
  return parts.join(" · ");
}

function actionableDetail(notice: ActivityNotice): string {
  if (notice.detail.toLowerCase() !== "internal server error") return notice.detail;
  const cause = notice.errorClass ? ` (${notice.errorClass})` : "";
  return `The coordinator API could not complete this request${cause}. Existing queue and review state remain saved; use the request ID below to find the matching server log.`;
}

function activityTone(notice: ActivityNotice) {
  return notice.phase === "confirmed" ? "confirmed" : notice.phase === "started" ? "pending" : "failed";
}

function ActivityNoticeCard({ notice, onDismiss }: { notice: ActivityNotice; onDismiss: (id: string) => void }) {
  const tone = activityTone(notice);
  const trace = traceLabel(notice.requestId);
  const context = failureContext(notice);
  return <article className={`api-activity ${tone}`} role={activityRole(tone)}><span className="api-activity-indicator" aria-hidden="true" /><div><strong>{activityTitle(notice)}</strong><span>{actionableDetail(notice)}</span>{context && <small className="api-activity-context">{context}</small>}<ActivityTrace trace={trace} /></div><ActivityDismiss pending={tone === "pending"} label={notice.label} onDismiss={() => onDismiss(notice.id)} /></article>;
}
function activityRole(tone: string) { return tone === "failed" ? "alert" : "status"; }
function activityTitle(notice: ActivityNotice) { return notice.phase === "refresh_failed" ? "Refresh failed" : notice.label; }
function ActivityTrace({ trace }: { trace: string | null }) { return trace ? <small>{trace}</small> : null; }
function ActivityDismiss({ pending, label, onDismiss }: { pending: boolean; label: string; onDismiss: () => void }) { return pending ? null : <button type="button" aria-label={`Dismiss ${label}`} onClick={onDismiss}>×</button>; }

function shouldIgnoreRefreshFailure(activity: ApiActivity, failures: Map<string, number>, now: number) {
  if (activity.phase !== "refresh_failed") return false;
  const recent = now - (failures.get(activity.path) ?? 0) < REFRESH_ERROR_DEDUPE_MS;
  if (!recent) failures.set(activity.path, now);
  return recent;
}

export function ApiActivityCenter() {
  const [notices, setNotices] = useState<ActivityNotice[]>([]);
  const [pendingCount, setPendingCount] = useState(0);
  const [showLoading, setShowLoading] = useState(false);
  const refreshFailures = useRef(new Map<string, number>());
  const loadingTimer = useRef<number | undefined>(undefined);

  useEffect(() => {
    function onActivity(event: Event) {
      const activity = (event as CustomEvent<ApiActivity>).detail;
      const now = Date.now();
      if (shouldIgnoreRefreshFailure(activity, refreshFailures.current, now)) return;
      setNotices((current) => {
        const next = current.filter((notice) => notice.id !== activity.id);
        return [...next, { ...activity, receivedAt: now }].slice(-4);
      });
      if (activity.phase === "started") return;
      const lifetime = activity.phase === "confirmed" ? SUCCESS_LIFETIME_MS : ERROR_LIFETIME_MS;
      window.setTimeout(() => setNotices((current) => current.filter((notice) => notice.id !== activity.id)), lifetime);
    }
    window.addEventListener(API_ACTIVITY_EVENT, onActivity);
    return () => window.removeEventListener(API_ACTIVITY_EVENT, onActivity);
  }, []);

  useEffect(() => {
    function onPending(event: Event) {
      const count = Math.max(0, (event as CustomEvent<ApiPendingState>).detail?.count ?? 0);
      setPendingCount(count);
      if (count === 0) {
        if (loadingTimer.current !== undefined) window.clearTimeout(loadingTimer.current);
        loadingTimer.current = undefined;
        setShowLoading(false);
        return;
      }
      if (loadingTimer.current === undefined) {
        loadingTimer.current = window.setTimeout(() => {
          loadingTimer.current = undefined;
          setShowLoading(true);
        }, LOADING_REVEAL_MS);
      }
    }
    window.addEventListener(API_PENDING_EVENT, onPending);
    return () => {
      window.removeEventListener(API_PENDING_EVENT, onPending);
      if (loadingTimer.current !== undefined) window.clearTimeout(loadingTimer.current);
    };
  }, []);

  return (
    <>
      {showLoading && pendingCount > 0 && <div className="api-loading-indicator" role="status" aria-live="polite"><span className="api-loading-orbit" aria-hidden="true"><i /><i /><i /></span><span><strong>Star is working</strong><small>{pendingCount === 1 ? "Waiting for the coordinator" : `${pendingCount} updates in motion`}</small></span></div>}
      {notices.length > 0 && <aside className="api-activity-center" aria-label="Star activity" aria-live="polite">
        {notices.map((notice) => <ActivityNoticeCard notice={notice} onDismiss={(id) => setNotices((current) => current.filter((item) => item.id !== id))} key={notice.id} />)}
      </aside>}
    </>
  );
}
