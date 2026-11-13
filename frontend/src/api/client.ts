import { validateApiResponse } from "./generated/openapiContract";

interface FailurePayload {
  detail?: unknown;
  message?: unknown;
  errors?: unknown;
  error_class?: unknown;
}

interface FailureDetail {
  detail: string;
  validationErrors?: Array<Record<string, unknown>>;
  errorClass?: string;
}

class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly method: string,
    public readonly path: string,
    public readonly requestId: string | null = null,
    public readonly requestBody: string | null = null,
    public readonly validationErrors: Array<Record<string, unknown>> | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export const API_ACTIVITY_EVENT = "star:api-activity";
export const API_PENDING_EVENT = "star:api-pending";
export const API_AUTH_EXPIRED_EVENT = "star:auth-expired";

export interface ApiPendingState {
  count: number;
}

export interface ApiActivity {
  id: string;
  method: string;
  path: string;
  label: string;
  phase: "started" | "confirmed" | "failed" | "refresh_failed";
  detail: string;
  requestId: string | null;
  status: number | null;
  errorClass: string | null;
  retryCount: number;
}

let activitySequence = 0;
let pendingRequests = 0;
let authExpiryNoticeActive = false;

interface ApiAuthExpired {
  path: string;
  requestId: string | null;
}

export function clearApiAuthExpiryNotice(): void {
  authExpiryNoticeActive = false;
}

function reportAuthExpiry(path: string, requestId: string | null): void {
  if (typeof window === "undefined" || authExpiryNoticeActive) return;
  authExpiryNoticeActive = true;
  window.dispatchEvent(new CustomEvent<ApiAuthExpired>(API_AUTH_EXPIRED_EVENT, {
    detail: { path, requestId },
  }));
  console.warn("star.auth_session_expired", { path, requestId });
}

function dispatchPendingRequestCount(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent<ApiPendingState>(API_PENDING_EVENT, {
    detail: { count: pendingRequests },
  }));
}

function beginPendingRequest(): void {
  pendingRequests += 1;
  dispatchPendingRequestCount();
}

function finishPendingRequest(): void {
  pendingRequests = Math.max(0, pendingRequests - 1);
  dispatchPendingRequestCount();
}

function nextActivityId(): string {
  activitySequence += 1;
  return `request-${Date.now()}-${activitySequence}`;
}

function actionLabel(method: string, path: string): string {
  const rules: Array<[RegExp, string]> = [
    [/^\/auth\/password\/login$/, "Signing in"],
    [/^\/auth\/password\/setup$/, "Saving administrator access"],
    [/^\/library-program\/start$/, "Starting library work"],
    [/^\/library-program\/pause$/, "Pausing new work"],
    [/^\/library-program\/resume$/, "Resuming library work"],
    [/^\/library-program\/drain$/, "Stopping after queued work"],
    [/^\/library-program\/queue\/clear$/, "Clearing waiting work"],
    [/^\/library-reset\/preview$/, "Preparing reset preview"],
    [/^\/library-reset\/execute$/, "Resetting Star photo data"],
    [/^\/reanalysis$/, "Starting immutable reanalysis"],
    [/^\/reanalysis\/[^/]+\/actions\//, "Controlling reanalysis"],
    [/^\/workers\/[^/]+\/control$/, "Sending worker command"],
    [/^\/workers\/[^/]+\/schedule$/, "Saving worker schedule"],
    [/^\/workers\/control-all$/, "Pausing or resuming every machine"],
    [/^\/workers\/[^/]+\/remove$/, "Removing offline worker"],
    [/^\/assets\/[^/]+\/human-rating$/, "Saving your rating"],
    [/^\/owner-rating-sessions\/[^/]+\/advance$/, "Saving your blind rating"],
    [/^\/assets\/[^/]+\/curation$/, "Saving curation priority"],
    [/^\/assets\/[^/]+\/immich-context\/refresh$/, "Refreshing Immich context"],
    [/^\/assets\/[^/]+\/retry-from-immich$/, "Requesting source recovery"],
    [/^\/assets\/[^/]+\/remove-from-star$/, "Removing local Star record"],
    [/^\/assets\/[^/]+\/(reanalyze|reanalyze-technical|reanalyze-quality|reanalyze-complete)/, "Queueing photo analysis"],
    [/^\/writeback-batches\/prepare$/, "Preparing immutable write-back batch"],
    [/^\/writeback-batches\/[^/]+\/approve$/, "Approving Immich write-back"],
    [/^\/writeback-batches\/[^/]+\/cancel$/, "Cancelling write-back batch"],
    [/^\/models\/[^/]+\/benchmark$/, "Saving benchmark result"],
    [/^\/library-program\/inference-settings$/, "Saving inference settings"],
    [/^\/models\/[^/]+\/packages$/, "Registering model package"],
    [/^\/models$/, "Registering model"],
    [/^\/immich\/connections\/[^/]+\/credential$/, "Validating Immich credential"],
    [/^\/immich\/connections\/[^/]+\/remove$/, "Removing Immich connection"],
    [/^\/immich\/connections$/, "Connecting Immich account"],
    [/^\/immich\/instances$/, "Registering Immich server"],
  ];
  return rules.find(([pattern]) => pattern.test(path))?.[1] ?? `${method} request`;
}

function dispatchActivity(activity: ApiActivity): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent<ApiActivity>(API_ACTIVITY_EVENT, { detail: activity }));
}

function countLabel(value: number, singular: string, plural = `${singular}s`): string {
  return `${value.toLocaleString()} ${value === 1 ? singular : plural}`;
}

function recordResponseDetail(record: Record<string, unknown>): string | null {
  const message = typeof record.message === "string" ? record.message.trim() : "";
  if (message) return message;
  if (Array.isArray(record.steps)) return `${countLabel(record.steps.length, "analysis step")} accepted.`;
  if (typeof record.cancelled_tasks === "number") return `${countLabel(record.cancelled_tasks, "waiting task")} cancelled.`;
  if (typeof record.eligible_count === "number") return `${countLabel(record.eligible_count, "eligible rating")} recorded in the batch.`;
  if (typeof record.created === "boolean" && typeof record.state === "string") {
    const state = record.state.replaceAll("_", " ");
    return record.created ? `A task was created in ${state} state.` : `The existing task remains ${state}.`;
  }
  if (typeof record.state === "string") return `Server state: ${record.state.replaceAll("_", " ")}.`;
  if (typeof record.queued_tasks === "number") return `${countLabel(record.queued_tasks, "task")} queued.`;
  return null;
}

function responseDetail(payload: unknown): string {
  if (Array.isArray(payload)) return `${countLabel(payload.length, "item")} accepted.`;
  if (payload && typeof payload === "object") {
    return recordResponseDetail(payload as Record<string, unknown>) ?? "The server confirmed the request.";
  }
  return "The server confirmed the request.";
}

function normalizeValidationErrors(payload: unknown): Array<Record<string, unknown>> | null {
  if (!Array.isArray(payload)) return null;
  const filtered = payload
    .map((item) => (item && typeof item === "object" ? (item as Record<string, unknown>) : null))
    .filter((item): item is Record<string, unknown> => Boolean(item));
  return filtered.length ? filtered : null;
}

function formatValidationSummary(errors: Array<Record<string, unknown>>): string {
  return errors
    .slice(0, 2)
    .map((item) => {
      const message = typeof item.msg === "string" ? item.msg : "invalid value";
      const loc = Array.isArray(item.loc)
        ? item.loc
            .map((entry) => String(entry))
            .filter(Boolean)
            .join(".")
        : "request";
      return `${loc}: ${message}`;
    })
    .join("; ");
}

function parseFailurePayload(payload: FailurePayload): { detail: string; validationErrors?: Array<Record<string, unknown>> } {
  const candidate = normalizeValidationErrors(payload.detail)
    ?? normalizeValidationErrors(payload.errors);
  if (candidate) {
    return {
      detail: formatValidationSummary(candidate),
      validationErrors: candidate,
    };
  }
  if (typeof payload.detail === "string" && payload.detail.trim()) {
    return { detail: payload.detail.trim() };
  }
  if (typeof payload.message === "string" && payload.message.trim()) {
    return { detail: payload.message.trim() };
  }
  return { detail: "Request failed without details." };
}

async function failedResponseDetail(response: Response): Promise<FailureDetail> {
  const fallback = `Request failed (${response.status})`;
  try {
    const payload = (await response.json()) as FailurePayload;
    const parsed = parseFailurePayload(payload);
    return {
      ...parsed,
      detail: parsed.detail || fallback,
      errorClass: typeof payload.error_class === "string" ? payload.error_class : undefined,
    };
  } catch {
    return { detail: fallback };
  }
}

const RETRYABLE_READ_ERRORS = new Set([
  "AssetProjectionBusyRetryable",
  "DatabaseConnectionRetryable",
  "DatabaseDeadlockRetryable",
  "DatabasePoolBusyRetryable",
]);
const RETRYABLE_HTTP_STATUSES = new Set([502, 503, 504]);
// Read-only refreshes can safely outwait a short projection publication burst. The
// backend now serves committed snapshots without taking the writer lock; these retries
// cover rolling upgrades and the small window where an older API still returns 503.
const MAX_TRANSIENT_READ_RETRIES = 5;

function isRetryableIdempotentAction(method: string, path: string): boolean {
  // Human-rating writes carry a server-validated idempotency key. Replaying the
  // identical body is safe after projection contention, a deadlock rollback, or
  // a lost response; other state-changing routes remain single-attempt.
  return (method === "PUT" && /^\/assets\/[^/]+\/human-rating$/.test(path))
    || (method === "POST" && /^\/owner-rating-sessions\/[^/]+\/advance$/.test(path));
}

function canReplayRequest(method: string, path: string): boolean {
  return method === "GET" || method === "HEAD" || isRetryableIdempotentAction(method, path);
}

function retryDelayMs(response: Response): number {
  const seconds = Number(response.headers.get("retry-after"));
  if (!Number.isFinite(seconds) || seconds < 0) return 250;
  return Math.min(2_000, Math.round(seconds * 1_000));
}

function wait(delayMs: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, delayMs));
}

function reportFailure(
  activity: Omit<ApiActivity, "phase" | "detail" | "requestId" | "status" | "errorClass" | "retryCount">,
  detail: string,
  requestId: string | null,
  isAction: boolean,
  status: number,
  method: string,
  path: string,
  requestBody: string | null,
  validationErrors: Array<Record<string, unknown>> | null,
  errorClass: string | null,
  retryCount: number,
): void {
  const bodyPreview = requestBody && requestBody.length > 500 ? `${requestBody.slice(0, 500)}…` : requestBody;
  dispatchActivity({
    ...activity,
    phase: isAction ? "failed" : "refresh_failed",
    detail,
    requestId,
    status,
    errorClass,
    retryCount,
  });
  console.error("star.api_request_failed", {
    status,
    method,
    path,
    requestId,
    validationErrors,
    errorClass,
    retryCount,
    requestBody: bodyPreview,
    detail,
  });
}

type RequestActivity = Omit<ApiActivity, "phase" | "detail" | "requestId" | "status" | "errorClass" | "retryCount">;

type ApiRequestContext = {
  activity: RequestActivity;
  init: RequestInit;
  isAction: boolean;
  method: string;
  path: string;
  requestBody: string | null;
};

async function sendRequest(context: ApiRequestContext, retryCount: number): Promise<Response> {
  try {
    // fallow-ignore-next-line security-sink -- fixed same-origin /api/v1 prefix; input cannot select a host.
    return await fetch("/api/v1" + context.path, {
      ...context.init,
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        ...context.init.headers,
      },
    });
  } catch (caught) {
    if (context.init.signal?.aborted) throw caught;
    if (canReplayRequest(context.method, context.path) && retryCount < MAX_TRANSIENT_READ_RETRIES) {
      await wait(250 * (retryCount + 1));
      return sendRequest(context, retryCount + 1);
    }
    throw caught;
  }
}

function shouldRetryResponse(
  context: ApiRequestContext,
  response: Response,
  failure: FailureDetail,
  retryCount: number,
): boolean {
  return canReplayRequest(context.method, context.path)
    && retryCount < MAX_TRANSIENT_READ_RETRIES
    && (RETRYABLE_HTTP_STATUSES.has(response.status)
      || (failure.errorClass != null && RETRYABLE_READ_ERRORS.has(failure.errorClass)));
}

async function throwResponseFailure(
  context: ApiRequestContext,
  response: Response,
  requestId: string | null,
  retryCount: number,
): Promise<never> {
  const failure = await failedResponseDetail(response);
  if (response.status === 401) reportAuthExpiry(context.path, requestId);
  const detail = failure.detail ?? (response.status === 401 ? "Authentication required" : `Request failed (${response.status})`);
  reportFailure(
    context.activity,
    failure.validationErrors ? `${detail}: ${formatValidationSummary(failure.validationErrors)}` : detail,
    requestId,
    context.isAction,
    response.status,
    context.method,
    context.path,
    context.requestBody,
    failure.validationErrors ?? null,
    failure.errorClass ?? null,
    retryCount,
  );
  throw new ApiError(
    detail,
    response.status,
    context.method,
    context.path,
    requestId,
    context.requestBody,
    failure.validationErrors ?? null,
  );
}

async function requestPayload<T>(context: ApiRequestContext): Promise<T> {
  let retryCount = 0;
  while (true) {
    context.init.signal?.throwIfAborted();
    const response = await sendRequest(context, retryCount);
    const requestId = response.headers.get("x-request-id");
    if (!response.ok) {
      const failure = await failedResponseDetail(response.clone());
      if (shouldRetryResponse(context, response, failure, retryCount)) {
        retryCount += 1;
        await wait(retryDelayMs(response));
        continue;
      }
      return throwResponseFailure(context, response, requestId, retryCount);
    }
    const rawPayload: unknown = response.status === 204 ? undefined : await response.json();
    if (response.status !== 204) validateApiResponse(context.method, context.path, rawPayload);
    if (["/auth/session", "/auth/password/login", "/auth/password/setup"].includes(context.path)) {
      clearApiAuthExpiryNotice();
    }
    if (context.isAction) {
      dispatchActivity({
        ...context.activity,
        phase: "confirmed",
        detail: responseDetail(rawPayload),
        requestId,
        status: response.status,
        errorClass: null,
        retryCount,
      });
    }
    return rawPayload as T;
  }
}

// POST endpoints that only compute a preview; they change nothing and stay out of the activity toasts.
const READ_ONLY_POSTS: RegExp[] = [];

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const isAction = method !== "GET" && method !== "HEAD" && !READ_ONLY_POSTS.some((pattern) => pattern.test(path.split("?", 1)[0]!));
  const activityId = nextActivityId();
  const label = actionLabel(method, path);
  const activity = { id: activityId, method, path, label };
  const requestBody = init.body && typeof init.body === "string" ? init.body : null;
  const context = { activity, init, isAction, method, path, requestBody };
  beginPendingRequest();

  if (isAction) {
    dispatchActivity({
      ...activity,
      phase: "started",
      detail: "Sending the request to Star…",
      requestId: null,
      status: null,
      errorClass: null,
      retryCount: 0,
    });
  }
  try {
    return await requestPayload<T>(context);
  } catch (caught) {
    if (caught instanceof ApiError) throw caught;
    // Navigation/filter cancellation is intentional, not an outage to retry
    // or surface in the global activity banner.
    if (init.signal?.aborted) throw caught;
    const detail = caught instanceof Error ? caught.message : "Star could not be reached.";
    reportFailure(
      activity,
      detail,
      null,
      isAction,
      0,
      method,
      path,
      requestBody,
      null,
      caught instanceof Error ? caught.name : "NetworkError",
      0,
    );
    if (caught instanceof Error) {
      throw new ApiError(detail, 0, method, path, null, requestBody);
    }
    throw new ApiError(detail, 0, method, path);
  } finally {
    finishPendingRequest();
  }
}

export function formatBytes(value: number): string {
  if (value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  const amount = value / 1024 ** index;
  const digits = Number.isInteger(amount) ? 0 : index > 2 ? 1 : 0;
  return amount.toFixed(digits) + " " + units[index];
}
