import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CompletenessBadge } from "./CompletenessBadge";

describe("CompletenessBadge", () => {
  it("shows the lean required fraction while the photo review is missing", () => {
    render(<CompletenessBadge completedChecks={["technical"]} analysisComplete={false} />);
    expect(screen.getByText("0/1 required")).toHaveAttribute(
      "title",
      expect.stringContaining("Primary review"),
    );
  });

  it("uses the evidence plan to exclude conditional deep review", () => {
    render(
      <CompletenessBadge
        completedChecks={["technical", "primary_review"]}
        analysisComplete
        evidencePlan={[
          { key: "technical", label: "Technical screen", analysis_type: "technical_preview", purpose: "Image defects", required: true, applicable: true, status: "completed", reason: null },
          { key: "primary_review", label: "Photo review", analysis_type: "primary_review", purpose: "Photo judgment", required: true, applicable: true, status: "completed", reason: null },
          { key: "deep_review", label: "Conditional deep review", analysis_type: "photo_deep_review", purpose: "Adjudication", required: false, applicable: false, status: "skipped_not_applicable", reason: "Not needed" },
        ]}
      />,
    );
    expect(screen.getByText("2/2 required")).toHaveAttribute(
      "title",
      expect.stringContaining("2 required checks completed"),
    );
  });

  it("marks a deep-reviewed photo distinctly", () => {
    render(<CompletenessBadge completedChecks={["technical", "primary_review", "deep_review"]} analysisComplete deepReviewed />);
    expect(screen.getByText("+deep")).toBeInTheDocument();
  });
});
