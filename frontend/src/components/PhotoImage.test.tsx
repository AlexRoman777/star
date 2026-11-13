import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PhotoImage } from "./PhotoImage";

class IntersectionObserverStub {
  static latest: IntersectionObserverStub | undefined;
  private target: Element | undefined;

  constructor(private readonly callback: IntersectionObserverCallback) {
    IntersectionObserverStub.latest = this;
  }

  observe(target: Element) {
    this.target = target;
  }

  disconnect() {}

  reveal() {
    if (!this.target) return;
    this.callback(
      [{ isIntersecting: true, target: this.target } as IntersectionObserverEntry],
      this as unknown as IntersectionObserver,
    );
  }
}

afterEach(() => {
  vi.unstubAllGlobals();
  IntersectionObserverStub.latest = undefined;
});

describe("PhotoImage", () => {
  it("shows loading, reveals the image, and offers a cache-busting retry after failure", () => {
    render(<PhotoImage src="/preview?id=1" alt="Family photo" allowRetry />);
    const image = screen.getByRole("img", { name: "Family photo" });
    expect(screen.getByRole("status")).toHaveTextContent("Loading photo preview");

    fireEvent.load(image);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    fireEvent.error(image);
    expect(screen.getByRole("alert")).toHaveTextContent("Preview unavailable");
    fireEvent.click(screen.getByRole("button", { name: "Retry preview" }));
    expect(screen.getByRole("img", { name: "Family photo" })).toHaveAttribute("src", "/preview?id=1&preview_retry=1");
    expect(screen.getByRole("status")).toHaveTextContent("Loading photo preview");
  });

  it("does not attach a lazy image URL until the frame approaches the viewport", async () => {
    vi.stubGlobal("IntersectionObserver", IntersectionObserverStub);
    render(<PhotoImage src="/preview?id=2" alt="Deferred family photo" />);
    const image = screen.getByRole("img", { name: "Deferred family photo" });
    expect(image).not.toHaveAttribute("src");
    IntersectionObserverStub.latest?.reveal();
    await waitFor(() => expect(image).toHaveAttribute("src", "/preview?id=2"));
  });

  it("reuses a recently decoded browser image without showing another loading state", () => {
    vi.stubGlobal("IntersectionObserver", IntersectionObserverStub);
    render(<PhotoImage src="/preview?id=decoded" alt="Cached family photo" />);
    IntersectionObserverStub.latest?.reveal();
    const first = screen.getByRole("img", { name: "Cached family photo" });
    fireEvent.load(first);
    cleanup();

    render(<PhotoImage src="/preview?id=decoded" alt="Cached family photo" />);

    expect(screen.getByRole("img", { name: "Cached family photo" })).toHaveAttribute(
      "src",
      "/preview?id=decoded",
    );
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
