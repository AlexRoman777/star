import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusPill } from "./StatusPill";

describe("StatusPill", () => {
  it("renders readable workflow states", () => {
    render(<StatusPill value="needs_review" />);
    expect(screen.getByText("needs review")).toHaveClass("quiet");
  });

  it("marks failed state as dangerous", () => {
    render(<StatusPill value="failed" />);
    expect(screen.getByText("failed")).toHaveClass("danger");
  });

  it("marks an offline machine as dangerous", () => {
    render(<StatusPill value="offline" />);
    expect(screen.getByText("offline")).toHaveClass("danger");
  });
});
