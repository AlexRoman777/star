import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "../../api/client";
import type { Session } from "../../api/types";

export function useOwnerSession() {
  const session = useQuery({
    queryKey: ["session"],
    queryFn: () => apiFetch<Session>("/auth/session"),
    retry: false,
  });
  return { session, isOwner: session.data?.user?.role === "administrator" };
}
