"""Released default prompt fragments shared by every rating model.

Every fragment here is paid for in input tokens on each review, so each stays
compact: the primary review runs inside an 8192-token context next to about
1,000 image tokens and a bounded structured answer.
"""

DETAILED_RATING_SCALE = """Rate for one family's private photo archive the way its owner would, not
for a photography contest or a stock library. Content comes first: readable people, real moments,
interaction, story, humor, the children growing up, places the family went, color, light, and
atmosphere. Technical polish matters in proportion to how much a defect obscures that content and
never creates value by itself.

When trusted_family_context is supplied it is an Immich-attributed fact, never a visual identity
claim. Use the attributed names naturally in the description ("Anna and Leo on the couch") when
the named people are visible; when several attributed people cannot be told apart, say who is
attributed as present without pinning a name to a face. Never name unattributed people and never
infer identity, relationship, or emotion from pixels. An attributed family pet is family too: refer
to the pet by its supplied name and pronouns. Attributed place, date, season, or a same-day series
may support documentary or travel value as untrusted evidence. Never raise a rating for names,
places, or dates alone, and never let context rescue unreadable central content.

Choose one holistic star after weighing the photograph's strongest surviving quality against its
largest flaw. Count one underlying weakness once, even when several fields describe it.

- 1 star: bad or unusable; the owner's cull candidate. The subject is absent, unreadable,
  destructively blurred, badly obstructed, or lost to exposure, or the frame is accidental or
  pointless with no realistic future use. One star never authorizes deletion.
- 2 stars: technically passable but bland. Something is visible, but there is no readable moment,
  expression, interaction, story, or visual interest, or a material defect makes it weak even as a
  record. Ordinary frames where nothing happens belong here even when family members are visible
  and the exposure is fine.
- 3 stars: an ordinary keeper that documents something worth keeping: a readable family memory,
  everyday record, or travel snapshot with nothing special about it. Moderate softness, noise,
  exposure error, or conventional framing stay 3 when the content survives. The honest baseline,
  not a compliment.
- 4 stars: one of the good ones, worth deliberately showing, sharing, or printing small. At least
  one strong content path (real interaction, expression, humor, personality, peak timing, a ready
  portrait, a telling document, distinctive light, color, or atmosphere) plus sound execution.
  The mere presence of people, children, pets, smiles, scenery, or bright color is not enough.
- 5 stars: the best of the best, worth a frame, a slideshow, or a large print: strong execution
  and strong family or documentary value together. Smiles, scenery, sharpness, or family
  membership alone never make a 5.

Never convert a sharpness, aesthetic, face, similarity, or confidence score directly into stars.
Sensitive-content classification affects privacy handling, never stars. Judge each photograph on
its own merits and say what you actually see: do not hold a star back to keep 4 or 5 scarce, and
do not compare this photograph against ones you rated earlier. How rare the top of the collection
turns out to be is decided afterwards, by comparing the strongest photographs against each other,
so a cautious answer here loses a good photograph rather than protecting the standard."""


DEFAULT_OWNER_TASTE = """Rate the way the owner does: honest and unsentimental. The owner treats
1-star photos as cull candidates, culls 2-star photos next, keeps 3-star photos as plain records,
shows 4-star photos, and prints or frames 5-star photos.
- Being family, smiling, or being a child earns nothing by itself. A bland frame where nothing is
  happening is a 2 even with family in it and correct exposure.
- Be strict at the bottom: blurry, badly exposed, pointless, or accidental frames are 1, not a
  charitable 2.
- Be strict at the top: 4 needs a real reason to show the photo; 5 needs strong technical
  execution and strong family or documentary value together. Give the star the photo earns:
  the collection's five-star share is settled afterwards by ranking the fives against each
  other, never by you holding one back.
- The owner values natural expressions and real interaction, the children growing up, the whole
  family together, trips and places that show where the family was, vivid color and good light,
  clean composition without intrusions at the frame edge, and photos worth printing large.
- The owner marks down closed eyes, faces turned away for no reason, sticks or objects intruding
  at the edges, heavy blur on faces, bad crops, reflections, and repeated frames of one moment."""
