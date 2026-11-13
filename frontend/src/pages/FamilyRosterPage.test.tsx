import "@testing-library/jest-dom/vitest";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FamilyRosterPage } from "./FamilyRosterPage";

const { apiFetchMock } = vi.hoisted(() => ({ apiFetchMock: vi.fn() }));

vi.mock("../api/client", () => ({
  apiFetch: apiFetchMock,
}));

const cookie = {
  id: "member-cookie",
  name: "Biscuit",
  kind: "pet",
  role: "pet",
  aliases: ["cookie"],
  species: "dog",
  breed: "Border collie",
  pronouns: "she/her",
  active: true,
};

const ida = {
  id: "member-ida",
  name: "Mia",
  kind: "person",
  role: "child",
  aliases: ["ida"],
  species: null,
  breed: null,
  pronouns: null,
  active: true,
};

function renderPage() {
  apiFetchMock.mockImplementation((path: string, init?: RequestInit) => {
    if (typeof path !== "string") return Promise.resolve({});
    if (path === "/family-roster" && (!init?.method || init.method === "GET")) {
      return Promise.resolve({ members: [ida, cookie] });
    }
    if (path === "/family-roster" && init?.method === "POST") {
      return Promise.resolve({ ...ida, id: "member-new", name: "Vera" });
    }
    if (path.startsWith("/family-roster/") && init?.method === "DELETE") {
      return Promise.resolve(undefined);
    }
    throw new Error(`Unexpected API request: ${path}`);
  });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <FamilyRosterPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("FamilyRosterPage", () => {
  beforeEach(() => apiFetchMock.mockReset());
  afterEach(() => vi.restoreAllMocks());

  it("lists people and pets with their attributed identity facts", async () => {
    renderPage();

    expect(await screen.findByText("Biscuit")).toBeInTheDocument();
    expect(screen.getByText("Mia")).toBeInTheDocument();
    expect(screen.getByText("Border collie · she/her")).toBeInTheDocument();
    expect(screen.getByText(/never guess identity from pixels/)).toBeInTheDocument();
  });

  it("creates a person with normalized aliases", async () => {
    renderPage();
    await screen.findByText("Biscuit");

    fireEvent.click(screen.getByRole("button", { name: "Add member" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Grandma Vera" } });
    fireEvent.change(screen.getByLabelText("Immich names"), {
      target: { value: "vera, Grandma , vera" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      const create = apiFetchMock.mock.calls.find(
        ([, init]) => (init as RequestInit | undefined)?.method === "POST",
      );
      expect(create).toBeDefined();
      const body = JSON.parse(String((create?.[1] as RequestInit).body));
      expect(body.name).toBe("Grandma Vera");
      expect(body.aliases).toEqual(["vera", "Grandma"]);
      expect(body.kind).toBe("person");
    });
  });

  it("removes a member only after an explicit confirmation", async () => {
    renderPage();
    await screen.findByText("Biscuit");

    fireEvent.click(screen.getAllByRole("button", { name: "Remove" })[0]!);
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/Photos and ratings are unchanged/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove" }));

    await waitFor(() => {
      expect(
        apiFetchMock.mock.calls.some(
          ([path, init]) =>
            path === "/family-roster/member-ida" &&
            (init as RequestInit | undefined)?.method === "DELETE",
        ),
      ).toBe(true);
    });
  });
});
