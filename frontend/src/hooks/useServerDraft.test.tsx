import "@testing-library/jest-dom/vitest";

import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { useServerDraft } from "./useServerDraft";

function Harness({ server, signature }: { server: string; signature?: unknown }) {
  const [draft, setDraft] = useServerDraft(server, signature ?? server);
  return (
    <div>
      <output>{draft}</output>
      <button onClick={() => setDraft("edited")} type="button">edit</button>
    </div>
  );
}

describe("useServerDraft", () => {
  it("keeps local edits until the server value changes", () => {
    const view = render(<Harness server="a" />);
    fireEvent.click(screen.getByRole("button", { name: "edit" }));
    expect(screen.getByRole("status", { hidden: true }) ?? true).toBeTruthy();
    expect(screen.getByText("edited")).toBeInTheDocument();
    view.rerender(<Harness server="a" />);
    expect(screen.getByText("edited")).toBeInTheDocument();
    view.rerender(<Harness server="b" />);
    expect(screen.getByText("b")).toBeInTheDocument();
  });

  it("resets only on the signature when one is provided", () => {
    const view = render(<Harness server="a" signature={1} />);
    fireEvent.click(screen.getByRole("button", { name: "edit" }));
    view.rerender(<Harness server="changed" signature={1} />);
    expect(screen.getByText("edited")).toBeInTheDocument();
    view.rerender(<Harness server="changed" signature={2} />);
    expect(screen.getByText("changed")).toBeInTheDocument();
  });

  it("tolerates unstable object server values with a stable signature", () => {
    function Wrapper() {
      const [tick, setTick] = useState(0);
      const [draft] = useServerDraft({ tick }, "stable");
      return <button onClick={() => setTick(tick + 1)} type="button">t{draft.tick}</button>;
    }
    render(<Wrapper />);
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByRole("button")).toHaveTextContent("t0");
  });
});
