import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StarRating } from "./StarRating";

describe("StarRating", () => {
  it("renders the correct number of filled and empty stars", () => {
    render(<StarRating rating={3} />);
    const stars = screen.getByLabelText("3 out of 5 stars").querySelectorAll(".star");
    expect(stars).toHaveLength(5);
    expect(document.querySelectorAll(".star.filled")).toHaveLength(3);
    expect(document.querySelectorAll(".star.empty")).toHaveLength(2);
  });

  it("shows an explicit unrated state instead of zero stars", () => {
    render(<StarRating rating={null} />);
    expect(screen.getByLabelText("Not rated")).toBeInTheDocument();
    expect(document.querySelectorAll(".star")).toHaveLength(0);
  });

  it("clamps out-of-range ratings into the valid 1-5 display window", () => {
    render(<StarRating rating={7} />);
    expect(screen.getByLabelText("5 out of 5 stars")).toBeInTheDocument();
  });
});
