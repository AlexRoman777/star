import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PhotoZoomViewer } from "./PhotoZoomViewer";

const runtime = vi.hoisted(() => ({ init: vi.fn(), destroy: vi.fn(), options: vi.fn(), zoom: vi.fn(), pan: vi.fn(), callbacks: new Map<string, () => void>() }));
vi.mock("photoswipe", () => ({ default: class {
  element = document.createElement("div");
  currSlide = { currZoomLevel: 1, zoomLevels: { initial: 1, max: 6 }, pan: { x: 0, y: 0 }, zoomTo: runtime.zoom, panTo: runtime.pan };
  constructor(options: unknown) { runtime.options(options); }
  on(name: string, callback: () => void) { runtime.callbacks.set(name, callback); }
  init() { runtime.init(); runtime.callbacks.get("afterInit")?.(); }
  destroy() { runtime.destroy(); runtime.callbacks.get("destroy")?.(); }
} }));

afterEach(() => { runtime.callbacks.clear(); vi.clearAllMocks(); });

function readyPreview() {
  const image = screen.getByAltText("Photo");
  Object.defineProperties(image, { naturalWidth: { value: 1200 }, naturalHeight: { value: 800 } });
}

describe("PhotoZoomViewer", () => {
  it("supports keyboard zoom and pan only while the viewer is open", async () => {
    const view = render(<PhotoZoomViewer src="/photo-preview" alt="Photo" />);
    readyPreview();
    fireEvent.click(screen.getByRole("button", { name: "Open full-screen photo viewer" }));
    await waitFor(() => expect(runtime.init).toHaveBeenCalledOnce());
    fireEvent.keyDown(window, { key: "+" });
    expect(runtime.zoom).toHaveBeenLastCalledWith(1.25, undefined, 0);
    fireEvent.keyDown(window, { key: "0" });
    expect(runtime.zoom).toHaveBeenLastCalledWith(1, undefined, 0);
    fireEvent.keyDown(window, { key: "ArrowLeft", shiftKey: true });
    expect(runtime.pan).toHaveBeenCalledWith(80, 0);
    fireEvent.keyDown(window, { key: "+", ctrlKey: true });
    expect(runtime.zoom).toHaveBeenCalledTimes(2);
    view.unmount();
    fireEvent.keyDown(window, { key: "+" });
    expect(runtime.zoom).toHaveBeenCalledTimes(2);
  });
  it("loads the viewer only on request, with the displayed derivative dimensions", async () => {
    const changed = vi.fn();
    const view = render(<PhotoZoomViewer src="/photo-preview" alt="Photo" onOpenChange={changed} />);
    expect(runtime.init).not.toHaveBeenCalled();
    readyPreview();
    fireEvent.click(screen.getByRole("button", { name: "Open full-screen photo viewer" }));
    await waitFor(() => expect(runtime.init).toHaveBeenCalledOnce());
    expect(runtime.options).toHaveBeenCalledWith(expect.objectContaining({ dataSource: [{ src: "/photo-preview", width: 1200, height: 800, alt: "Photo" }], maxZoomLevel: 6 }));
    expect(changed).toHaveBeenCalledWith(true);
    view.unmount();
    expect(runtime.destroy).toHaveBeenCalled();
    expect(changed).toHaveBeenCalledWith(false);
  });

  it("never opens concealed sensitive media", () => {
    render(<PhotoZoomViewer src="/sensitive-preview" alt="Photo" disabled />);
    const trigger = screen.getByRole("button", { name: "Reveal sensitive photo before zooming" });
    expect(trigger).toBeDisabled();
    fireEvent.click(trigger);
    expect(runtime.init).not.toHaveBeenCalled();
  });

  it("does not invent dimensions or start another fetch when the preview is unavailable", () => {
    render(<PhotoZoomViewer src="/unavailable-preview" alt="Photo" />);
    fireEvent.click(screen.getByRole("button", { name: "Open full-screen photo viewer" }));
    expect(screen.getByText(/The viewer could not open/)).toBeInTheDocument();
    expect(runtime.init).not.toHaveBeenCalled();
  });
});
