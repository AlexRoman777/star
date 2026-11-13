import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { apiFetch } from "../api/client";
import type { FamilyRoster, FamilyRosterMember, FamilyRosterMemberInput } from "../api/types";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PageHeader } from "../components/PageHeader";

const EMPTY_FORM: FamilyRosterMemberInput = {
  name: "",
  kind: "person",
  role: "child",
  aliases: [],
  species: null,
  breed: null,
  pronouns: null,
  active: true,
};

function parseAliases(value: string): string[] {
  return [...new Set(value.split(",").map((alias) => alias.trim()).filter(Boolean))].slice(0, 8);
}

function memberToForm(member: FamilyRosterMember): FamilyRosterMemberInput {
  return {
    name: member.name,
    kind: member.kind,
    role: member.role,
    aliases: member.aliases,
    species: member.species,
    breed: member.breed,
    pronouns: member.pronouns,
    active: member.active,
  };
}

function MemberForm({
  initial,
  saving,
  onSave,
  onCancel,
}: {
  initial: FamilyRosterMemberInput;
  saving: boolean;
  onSave: (value: FamilyRosterMemberInput) => void;
  onCancel?: () => void;
}) {
  const [form, setForm] = useState(initial);
  const [aliasText, setAliasText] = useState(initial.aliases.join(", "));
  const isPet = form.kind === "pet";
  const aliases = parseAliases(aliasText);
  const valid = form.name.trim().length > 0 && aliases.length > 0;
  return (
    <form
      className="family-roster-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (!valid) return;
        onSave({
          ...form,
          name: form.name.trim(),
          role: isPet ? "pet" : form.role === "pet" ? "child" : form.role,
          aliases,
          species: isPet ? form.species || null : null,
          breed: isPet ? form.breed || null : null,
          pronouns: form.pronouns || null,
        });
      }}
    >
      <label>
        Name
        <input
          maxLength={64}
          onChange={(event) => setForm({ ...form, name: event.target.value })}
          required
          value={form.name}
        />
      </label>
      <label>
        Kind
        <select
          onChange={(event) =>
            setForm({
              ...form,
              kind: event.target.value,
              role: event.target.value === "pet" ? "pet" : "child",
            })
          }
          value={form.kind}
        >
          <option value="person">Person</option>
          <option value="pet">Pet</option>
        </select>
      </label>
      {!isPet && (
        <label>
          Role
          <select
            onChange={(event) => setForm({ ...form, role: event.target.value })}
            value={form.role}
          >
            <option value="parent">Parent</option>
            <option value="child">Child</option>
          </select>
        </label>
      )}
      <label>
        Immich names
        <input
          onChange={(event) => setAliasText(event.target.value)}
          placeholder="comma-separated, exact Immich person names"
          required
          value={aliasText}
        />
      </label>
      {isPet && (
        <>
          <label>
            Species
            <input
              maxLength={64}
              onChange={(event) => setForm({ ...form, species: event.target.value })}
              placeholder="dog"
              value={form.species ?? ""}
            />
          </label>
          <label>
            Breed
            <input
              maxLength={64}
              onChange={(event) => setForm({ ...form, breed: event.target.value })}
              placeholder="Border collie"
              value={form.breed ?? ""}
            />
          </label>
        </>
      )}
      <label>
        Pronouns
        <input
          maxLength={32}
          onChange={(event) => setForm({ ...form, pronouns: event.target.value })}
          placeholder="she/her"
          value={form.pronouns ?? ""}
        />
      </label>
      <label className="family-roster-active">
        <input
          checked={form.active ?? true}
          onChange={(event) => setForm({ ...form, active: event.target.checked })}
          type="checkbox"
        />
        Active
      </label>
      <div className="family-roster-form-actions">
        <button disabled={!valid || saving} type="submit">
          {saving ? "Saving…" : "Save"}
        </button>
        {onCancel && (
          <button className="secondary" onClick={onCancel} type="button">
            Cancel
          </button>
        )}
      </div>
    </form>
  );
}

export function FamilyRosterPage() {
  const queryClient = useQueryClient();
  const roster = useQuery({
    queryKey: ["family-roster"],
    queryFn: () => apiFetch<FamilyRoster>("/family-roster"),
  });
  const [editingId, setEditingId] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<FamilyRosterMember | null>(null);
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["family-roster"] });

  const create = useMutation({
    meta: { invalidates: [["family-roster"]] },
    mutationFn: (value: FamilyRosterMemberInput) =>
      apiFetch<FamilyRosterMember>("/family-roster", {
        method: "POST",
        body: JSON.stringify(value),
      }),
    onSuccess: async () => {
      setAdding(false);
      await refresh();
    },
  });
  const update = useMutation({
    meta: { invalidates: [["family-roster"]] },
    mutationFn: ({ id, value }: { id: string; value: FamilyRosterMemberInput }) =>
      apiFetch<FamilyRosterMember>(`/family-roster/${id}`, {
        method: "PUT",
        body: JSON.stringify(value),
      }),
    onSuccess: async () => {
      setEditingId(null);
      await refresh();
    },
  });
  const remove = useMutation({
    meta: { invalidates: [["family-roster"]] },
    mutationFn: (id: string) => apiFetch<void>(`/family-roster/${id}`, { method: "DELETE" }),
    onSuccess: async () => {
      setRemoving(null);
      await refresh();
    },
  });

  const members = roster.data?.members ?? [];
  return (
    <>
      <PageHeader
        actions={
          <button onClick={() => setAdding(true)} type="button">
            Add member
          </button>
        }
        title="Family"
      />
      <p className="page-lede">
        Named people and pets for photo descriptions and family value. Names stay encrypted and
        apply only through Immich-attributed people; models never guess identity from pixels.
      </p>
      {roster.error && <p className="form-error">Could not load the roster: {roster.error.message}</p>}
      {adding && (
        <MemberForm
          initial={EMPTY_FORM}
          onCancel={() => setAdding(false)}
          onSave={(value) => create.mutate(value)}
          saving={create.isPending}
        />
      )}
      <ul className="family-roster-list">
        {members.map((member) => (
          <li className={member.active ? "" : "inactive"} key={member.id}>
            {editingId === member.id ? (
              <MemberForm
                initial={memberToForm(member)}
                onCancel={() => setEditingId(null)}
                onSave={(value) => update.mutate({ id: member.id, value })}
                saving={update.isPending}
              />
            ) : (
              <div className="family-roster-row">
                <strong>{member.name}</strong>
                <span>
                  {member.kind === "pet"
                    ? [member.breed ?? member.species ?? "pet", member.pronouns]
                        .filter(Boolean)
                        .join(" · ")
                    : member.role}
                </span>
                <span className="family-roster-aliases">{member.aliases.join(", ")}</span>
                {!member.active && <span className="family-roster-muted">inactive</span>}
                <span className="family-roster-actions">
                  <button className="secondary" onClick={() => setEditingId(member.id)} type="button">
                    Edit
                  </button>
                  <button className="secondary" onClick={() => setRemoving(member)} type="button">
                    Remove
                  </button>
                </span>
              </div>
            )}
          </li>
        ))}
      </ul>
      {members.length === 0 && !roster.isLoading && !adding && (
        <p>No roster members yet.</p>
      )}
      <ConfirmDialog
        confirmLabel="Remove"
        description={
          removing
            ? `Remove ${removing.name} from the roster? Photos and ratings are unchanged; only future attributed context stops naming them.`
            : ""
        }
        destructive
        onCancel={() => setRemoving(null)}
        onConfirm={() => removing && remove.mutate(removing.id)}
        open={removing !== null}
        pending={remove.isPending}
        title="Remove roster member"
      />
    </>
  );
}
