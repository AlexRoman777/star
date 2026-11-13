import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RouteErrorBoundary } from "./RouteErrorBoundary";

function BrokenPage(): never {
  throw new Error("deliberate route failure");
}

afterEach(() => vi.restoreAllMocks());

describe("RouteErrorBoundary", () => {
  it("keeps a recoverable route-level fallback inside the application shell", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    render(<MemoryRouter><RouteErrorBoundary><BrokenPage /></RouteErrorBoundary></MemoryRouter>);

    expect(screen.getByRole("alert")).toHaveTextContent("This page could not be displayed");
    expect(screen.getByText("deliberate route failure")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload page" })).toBeInTheDocument();
  });
});
