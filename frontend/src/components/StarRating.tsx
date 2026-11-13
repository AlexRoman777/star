const MAX_STARS = 5;

/**
 * Renders a 1-5 rating as five star glyphs (filled vs empty) instead of a
 * literal "4★" text label. Purely presentational; callers decide when a
 * rating is unset (pass `null`).
 */
export function StarRating({
  rating,
  size = "medium",
}: {
  rating: number | null;
  size?: "small" | "medium" | "large";
}) {
  if (rating === null) {
    return <span className="star-rating unrated" aria-label="Not rated">Unrated</span>;
  }
  const bounded = Math.max(1, Math.min(MAX_STARS, Math.round(rating)));
  return (
    <span
      className={`star-rating ${size === "small" ? "star-rating-small" : size === "large" ? "star-rating-large" : ""}`}
      aria-label={`${bounded} out of ${MAX_STARS} stars`}
      title={`${bounded} / ${MAX_STARS} stars`}
    >
      {Array.from({ length: MAX_STARS }, (_, index) => (
        <span
          key={index}
          className={index < bounded ? "star filled" : "star empty"}
          aria-hidden="true"
        >
          ★
        </span>
      ))}
    </span>
  );
}
