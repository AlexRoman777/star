/**
 * The blur cover for NSFW and uncertain-sensitive photos in tile views.
 * A photo stays covered until this explicit per-photo reveal; revealing is a
 * privacy action and never changes any rating or classification.
 */
export function SensitiveCover({ onReveal }: { onReveal: () => void }) {
  return (
    <span className="photo-sensitive-cover">
      <strong>NSFW</strong>
      <button
        type="button"
        onClick={(event) => {
          event.preventDefault();
          event.stopPropagation();
          onReveal();
        }}
      >
        Reveal photo
      </button>
    </span>
  );
}
