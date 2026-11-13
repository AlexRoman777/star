const FAMILY_ARCHIVE_RATINGS = [
  { rating: 1, label: "Cull review", description: "A failed or accidental frame with little usable visible record. You still decide what leaves the library." },
  { rating: 2, label: "Passable snapshot", description: "A weak, badly timed, distracted, or low-usefulness photo that may still hold a memory." },
  { rating: 3, label: "Ordinary keeper", description: "A normal usable record worth keeping just in case, without a clearly strong visible reason to revisit it." },
  { rating: 4, label: "Clearly good", description: "Worth revisiting for its execution, moment, people, expression, setting, story, or family value." },
  { rating: 5, label: "Best of the best", description: "A rare, distinctive family highlight across craft, feeling, uniqueness, growth, color, atmosphere, or history." },
] as const;

export function FamilyArchiveRatingGuide() {
  return (
    <section className="family-rating-guide" aria-labelledby="family-rating-guide-title">
      <div className="family-rating-guide-heading">
        <div>
          <p className="eyebrow">Shared rating scale</p>
          <h3 id="family-rating-guide-title">What each star means</h3>
        </div>
        <span>Ratings are proposals; only you cull or write back.</span>
      </div>
      <ol className="family-rating-levels">
        {FAMILY_ARCHIVE_RATINGS.map(({ rating, label, description }) => (
          <li key={rating}>
            <strong><span aria-hidden="true">{rating}★</span><span className="visually-hidden">{rating} stars:</span> {label}</strong>
            <p>{description}</p>
          </li>
        ))}
      </ol>
    </section>
  );
}
