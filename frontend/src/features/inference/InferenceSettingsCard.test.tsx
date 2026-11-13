import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { InferenceSettings, InferenceSettingsResponse } from "../../api/types";

const apiFetch = vi.fn();
vi.mock("../../api/client", () => ({ apiFetch: (...args: unknown[]) => apiFetch(...args) }));

import { InferenceSettingsCard } from "./InferenceSettingsCard";
import { inferenceSettingsErrors } from "./inferenceSettings";

const stage = (temperature: number, effort: "off" | "low" | "medium" = "off") => ({
  sampling: { temperature, top_p: 0.75, top_k: 10, min_p: 0, repeat_penalty: 1.05, presence_penalty: 0 },
  reasoning_effort: effort,
  max_completion_tokens: 512,
  max_media_edge: 2_048,
  include_calibration: true,
});
const defaults: InferenceSettings = { primary: stage(0.2), advanced: stage(0.2, "medium"), group: stage(0.2) };
const response = (revision: number, settings: InferenceSettings = defaults): InferenceSettingsResponse => ({ revision, stored: revision > 1, settings, defaults });

function renderCard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><InferenceSettingsCard /></QueryClientProvider>);
}

describe("inferenceSettingsErrors", () => {
  it("flags values outside the worker envelope", () => {
    const broken: InferenceSettings = { ...defaults, advanced: { ...stage(3, "medium"), max_media_edge: 4_096 } };
    expect(inferenceSettingsErrors(broken)).toEqual(["advanced.temperature", "advanced.max_media_edge"]);
    expect(inferenceSettingsErrors(defaults)).toEqual([]);
  });
});

describe("InferenceSettingsCard", () => {
  beforeEach(() => { apiFetch.mockReset(); });

  it("saves under the loaded revision and confirms", async () => {
    apiFetch.mockImplementation((path: string, init?: RequestInit) => {
      if (init?.method === "PUT") return Promise.resolve(response(3, JSON.parse(String(init.body)).settings));
      return Promise.resolve(response(2));
    });
    renderCard();
    const temperature = (await screen.findAllByLabelText(/Temperature/))[0]!;
    fireEvent.change(temperature, { target: { value: "0.35" } });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Saved"));
    const put = apiFetch.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT")!;
    const body = JSON.parse(String((put[1] as RequestInit).body));
    expect(body.expected_revision).toBe(2);
    expect(body.settings.primary.sampling.temperature).toBe(0.35);
  });

  it("reloads on a revision conflict", async () => {
    let revision = 2;
    apiFetch.mockImplementation((_path: string, init?: RequestInit) => {
      if (init?.method === "PUT") return Promise.reject(Object.assign(new Error("stale"), { status: 409 }));
      revision += 1;
      return Promise.resolve(response(revision));
    });
    renderCard();
    fireEvent.change((await screen.findAllByLabelText(/Temperature/))[0]!, { target: { value: "0.9" } });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Reloaded"));
    await waitFor(() => expect(apiFetch.mock.calls.filter(([, init]) => !(init as RequestInit | undefined)?.method).length).toBeGreaterThanOrEqual(2));
  });

  it("offers reset to defaults only when the draft differs from them", async () => {
    apiFetch.mockResolvedValue(response(5, { ...defaults, primary: stage(0.6) }));
    renderCard();
    const reset = await screen.findByRole("button", { name: "Reset to defaults" });
    expect(reset).toBeEnabled();
    fireEvent.click(reset);
    expect(screen.getByRole("button", { name: "Reset to defaults" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save settings" })).toBeEnabled();
  });
});
