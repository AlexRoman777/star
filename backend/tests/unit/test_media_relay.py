"""How the coordinator decides a photo is usable."""

from __future__ import annotations


def test_a_photo_is_judged_by_whether_it_decodes_not_by_its_label() -> None:
    """A library shot on several cameras must not lose photographs to a header.

    Immich labels previews in more ways than three, and refusing the rest threw
    away images that open perfectly. The byte and pixel ceilings still apply,
    and anything positively claiming not to be an image is still refused before
    the decoder sees it.
    """

    from star.services.media_relay import media_type_is_decodable

    for accepted in (
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/avif",
        "image/tiff",
        "image/heic",
        "",
        "application/octet-stream",
    ):
        assert media_type_is_decodable(accepted), accepted

    for refused in ("text/html", "application/json", "video/mp4", "text/plain"):
        assert not media_type_is_decodable(refused), refused
