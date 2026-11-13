import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { AssetAnalysis } from "../../api/types";
import { AnalysisPanel } from "./AssetInspectorPanels";

const analysis: AssetAnalysis = {
  analysis_run_id: "run-1",
  processor_version: "jury-v2",
  status: "complete",
  limitations: ["derivative_not_original"],
  completed_at: "2026-07-19T08:00:00Z",
  metrics: [
    {
      name: "sharpness",
      value: 0.82,
      uncertainty: 0.1,
      explanation: "Multiscale global, local-grid, and central-region detail estimate.",
      details: { source_width: 2048, source_height: 1365 },
    },
  ],
  rating_proposal: null,
  rating_consensus: {
    rating: 4,
    confidence: 0.78,
    agreement: 0.72,
    vote_count: 2,
    explanation: "Two independent sources support a strong keeper.",
    curation: null,
  },
  rating_votes: [
    {
      rating: 4,
      confidence: 0.85,
      explanation: "Strong subject and story, with a distracting edge.",
      reason_codes: ["subject_clear", "edge_distraction"],
      analysis_version: "photo-review-v57",
      source: "primary_photo_review",
      model_version_id: "model-version-27b",
      details: {
        model_id: "qwen/photo-critic-27b",
        runtime: "mlx",
        duration_ms: 2450,
        description: "A warm family portrait by a window.",
        photographic_verdict: "strong_photo",
        harsh_critique: "Worth keeping, but not yet portfolio-level.",
        composition_quality: 0.76,
        artistic_merit: 0.7,
        family_value: 0.92,
        strengths: ["expression", "warm light"],
        weaknesses: ["busy left edge"],
        recoverability: "high",
        recovery_explanation: "A careful crop would strengthen the subject.",
        recovery_actions: ["crop left edge", "lower highlights"],
        preserve_despite_technical_faults: true,
        tone_advice: { highlights: "lower_slightly" },
        crop_advice: "Trim the left edge without cutting the subject.",
        editing_explanation: "Keep the warm color relationship.",
        editability: "safe_adjustments",
        restoration_strategy: "non_generative_first",
        restoration_risk: "low",
      },
    },
    {
      rating: 5,
      confidence: 0.81,
      explanation: "A rare, well-composed highlight with clear emotional value.",
      reason_codes: ["local_deep_review_model", "deep_review_completed"],
      analysis_version: "deep-review-v57",
      source: "deep_photo_adjudication",
      model_version_id: "model-version-27b-deep",
      details: {
        model_id: "qwen3.5:27b",
        machine: "mac-48gb",
        runtime: "mlx",
        duration_ms: 8200,
        description: "A candid highlight moment escalated for a second opinion.",
        roast_text: "The timing works, but the left edge barges in like an uninvited guest.",
        what_this_records: "A visibly candid gathering in a domestic interior.",
        why_it_matters: "The interaction and environmental details make it worth revisiting.",
        why_it_may_not: "Without neighboring frames its uniqueness remains unknown.",
        rescue_and_role: "A modest crop could strengthen it as a family highlight.",
        collection_role: "family_highlight",
        why_five: "The visible moment and execution survive close scrutiny.",
        content_interest: 0.88,
        revisit_value: 0.92,
        evidence_density: 0.75,
        uniqueness_potential: 0.7,
      },
    },
    {
      rating: 2,
      confidence: 0.55,
      explanation: "A soft, forgettable frame with no clear subject.",
      reason_codes: ["local_roaster", "roasted_as_actually_trash"],
      analysis_version: "asset-vlm-review-v2:roast",
      source: "local_roaster",
      model_version_id: "model-version-4b-roast",
      details: {
        model_id: "qwen3-vl:4b",
        machine: "nvidia-8gb",
        runtime: "ollama",
        duration_ms: 1900,
        roast_text: "This photo looks like the camera sneezed. Nothing is in focus.",
        roast_reason: "Total blur with no resolved subject anywhere in frame.",
        is_actually_trash: true,
      },
    },
  ],
  detection_findings: [],
};

describe("AnalysisPanel", () => {
  it("shows every available vote identity, verdict, evidence, and recovery recommendation", () => {
    render(<AnalysisPanel analysis={analysis} />);

    expect(screen.getByText(/2 sources/)).toBeInTheDocument();
    expect(screen.getByText(/qwen\/photo-critic-27b · 85%/)).toBeInTheDocument();
    expect(screen.getByText("A warm family portrait by a window.")).toBeInTheDocument();
    expect(screen.getByText("Strong photo")).toBeInTheDocument();
    expect(screen.getByText("Worth keeping, but not yet portfolio-level.")).toBeInTheDocument();
    expect(screen.getByText("Strong subject and story, with a distracting edge.")).toBeInTheDocument();
    expect(screen.getByText(/Good: expression · warm light/)).toBeInTheDocument();
    expect(screen.getByText(/Weak: busy left edge/)).toBeInTheDocument();
    expect(screen.getByText("high recovery potential")).toBeInTheDocument();
    expect(screen.getByText("A careful crop would strengthen the subject.")).toBeInTheDocument();
    expect(screen.getByText(/Suggested: crop left edge · lower highlights/)).toBeInTheDocument();
    expect(screen.getByText(/Restoration: non generative first/)).toBeInTheDocument();
    expect(screen.getByText("model-version-27b")).toBeInTheDocument();
    expect(screen.getAllByText("Complete stored model result")).toHaveLength(3);
    expect(screen.getAllByText(/The complete normalized result Star accepted/)).toHaveLength(3);
  });

  it("shows the review pipeline version and completion time", () => {
    render(<AnalysisPanel analysis={analysis} />);

    expect(screen.getByText("jury-v2")).toBeInTheDocument();
    expect(screen.getByText(/completed Sunday, 19 July 2026, \d{2}:\d{2}/)).toBeInTheDocument();
  });

  it("labels a Mac deep-review vote clearly and shows which machine ran it", () => {
    render(<AnalysisPanel analysis={analysis} />);

    expect(screen.getByText("Escalated review")).toBeInTheDocument();
    expect(screen.getByText(/Conditional large-model adjudication/)).toBeInTheDocument();
    expect(screen.getByText("Apple Silicon Mac")).toBeInTheDocument();
    expect(screen.getByText("8.2 s")).toBeInTheDocument();
    expect(
      screen.getByText("A candid highlight moment escalated for a second opinion."),
    ).toBeInTheDocument();
    expect(screen.getByText("What this records")).toBeInTheDocument();
    expect(screen.getByText("Why it matters—or does not")).toBeInTheDocument();
    expect(screen.getByText("Rescue and collection role")).toBeInTheDocument();
    expect(screen.getByText("Independent verdict")).toBeInTheDocument();
    expect(screen.getAllByText(/The left edge barges in/i)).toHaveLength(2);
  });

  it("shows a roast vote's text and marks a cleanup candidate distinctly", () => {
    render(<AnalysisPanel analysis={analysis} />);

    expect(screen.getByText("Cleanup candidate")).toBeInTheDocument();
    expect(
      screen.getByText("This photo looks like the camera sneezed. Nothing is in focus."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Total blur with no resolved subject anywhere in frame."),
    ).toBeInTheDocument();
    expect(screen.getByText(/Opt-in, honest-but-funny critique/)).toBeInTheDocument();
  });

});
