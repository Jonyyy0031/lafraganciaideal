"""Plan 006: the photos of a perfume (a child entity of the aggregate) and `photo_keys`."""

from uuid import UUID

import pytest

from fragancia_api.modules.catalog.domain.perfume import (
    CARD_SIDE,
    DETAIL_SIDE,
    PHOTO_MIN_SIDE,
    PHOTOS_MAX,
    Perfume,
    photo_keys,
)
from fragancia_api.shared.kernel import Ok, new_id
from tests.unit.catalog.perfume_support import (
    LATER,
    NOW,
    add_presentation,
    error_code,
    error_details,
    make_brand,
    make_concentration,
    make_family,
    make_perfume,
    unwrap_ok,
)

UNKNOWN = UUID(int=404)


def _perfume() -> Perfume:
    return make_perfume(make_brand(), make_family(), make_concentration())


def _add(perfume: Perfume, *, width: int = 1200, height: int = 900) -> UUID:
    photo_id = new_id()
    card_key, detail_key = photo_keys(perfume.id, photo_id)
    unwrap_ok(perfume.add_photo(photo_id, card_key, detail_key, width, height, created_at=NOW))
    return photo_id


def _with_photos(count: int) -> tuple[Perfume, list[UUID]]:
    perfume = _perfume()
    return perfume, [_add(perfume) for _ in range(count)]


def _archived(count: int) -> tuple[Perfume, list[UUID]]:
    perfume, ids = _with_photos(count)
    perfume.archive()
    return perfume, ids


def _order(perfume: Perfume) -> list[UUID]:
    return [photo.id for photo in perfume.photos]


def _positions(perfume: Perfume) -> list[int]:
    return [photo.position for photo in perfume.photos]


# --- constants and keys -----------------------------------------------------------------------


def test_the_photo_rules_are_the_ones_fixed_by_the_plan() -> None:
    assert (PHOTOS_MAX, PHOTO_MIN_SIDE, CARD_SIDE, DETAIL_SIDE) == (3, 600, 400, 1200)


def test_photo_keys_live_under_the_perfume_prefix_and_name_both_renditions() -> None:
    perfume_id, photo_id = new_id(), new_id()

    assert photo_keys(perfume_id, photo_id) == (
        f"perfumes/{perfume_id}/{photo_id}-card.webp",
        f"perfumes/{perfume_id}/{photo_id}-detail.webp",
    )


def test_a_new_perfume_has_no_photos_and_nothing_removed() -> None:
    perfume = _perfume()

    assert perfume.photos == []
    assert perfume.removed_photo_ids == []


# --- add --------------------------------------------------------------------------------------


def test_a_new_photo_takes_the_next_position_and_keeps_its_fields() -> None:
    perfume = _perfume()
    first_id, second_id = new_id(), new_id()
    first_keys, second_keys = photo_keys(perfume.id, first_id), photo_keys(perfume.id, second_id)

    assert isinstance(perfume.add_photo(first_id, *first_keys, 1200, 900, created_at=NOW), Ok)
    assert isinstance(perfume.add_photo(second_id, *second_keys, 800, 1200, created_at=LATER), Ok)

    first, second = perfume.photos
    assert (first.id, first.position, first.width, first.height) == (first_id, 1, 1200, 900)
    assert (first.card_key, first.detail_key, first.created_at) == (*first_keys, NOW)
    assert (second.id, second.position, second.width, second.height) == (second_id, 2, 800, 1200)
    assert second.created_at == LATER


def test_rejects_a_fourth_photo_with_the_limit_in_the_details() -> None:
    perfume, ids = _with_photos(3)

    result = perfume.add_photo(new_id(), "c", "d", 1200, 900, created_at=NOW)

    assert error_code(result) == "CATALOG_PHOTO_LIMIT_REACHED"
    assert error_details(result) == {"max": 3}
    assert _order(perfume) == ids


def test_rejects_adding_a_photo_to_an_archived_perfume() -> None:
    perfume, _ = _archived(1)

    result = perfume.add_photo(new_id(), "c", "d", 1200, 900, created_at=NOW)

    assert error_code(result) == "CATALOG_PERFUME_ARCHIVED"
    assert len(perfume.photos) == 1


def test_an_archived_perfume_at_the_limit_answers_archived_first() -> None:
    perfume, _ = _archived(3)

    result = perfume.add_photo(new_id(), "c", "d", 1200, 900, created_at=NOW)

    assert error_code(result) == "CATALOG_PERFUME_ARCHIVED"


# --- remove -----------------------------------------------------------------------------------


def test_removing_a_photo_returns_it_and_records_its_id() -> None:
    perfume, (_, middle, _) = _with_photos(3)
    removed_photo = perfume.photos[1]

    result = perfume.remove_photo(middle)

    assert unwrap_ok(result) is removed_photo
    assert perfume.removed_photo_ids == [middle]


def test_removing_renumbers_the_rest_keeping_their_order() -> None:
    perfume, (first, middle, last) = _with_photos(3)

    unwrap_ok(perfume.remove_photo(middle))

    assert _order(perfume) == [first, last]
    assert _positions(perfume) == [1, 2]


def test_removing_the_first_photo_promotes_the_next_one_to_position_1() -> None:
    perfume, (first, second, third) = _with_photos(3)

    unwrap_ok(perfume.remove_photo(first))

    assert _order(perfume) == [second, third]
    assert _positions(perfume) == [1, 2]


def test_removed_ids_accumulate_across_removals() -> None:
    perfume, (first, second, third) = _with_photos(3)

    unwrap_ok(perfume.remove_photo(first))
    unwrap_ok(perfume.remove_photo(third))

    assert perfume.removed_photo_ids == [first, third]
    assert _order(perfume) == [second]
    assert _positions(perfume) == [1]


def test_a_removed_slot_can_be_filled_again() -> None:
    perfume, (first, second, third) = _with_photos(3)
    unwrap_ok(perfume.remove_photo(second))

    new = _add(perfume)

    assert _order(perfume) == [first, third, new]
    assert _positions(perfume) == [1, 2, 3]


def test_rejects_removing_an_unknown_photo_without_changing_anything() -> None:
    perfume, ids = _with_photos(2)

    result = perfume.remove_photo(UNKNOWN)

    assert error_code(result) == "CATALOG_PHOTO_NOT_FOUND"
    assert _order(perfume) == ids
    assert perfume.removed_photo_ids == []


def test_rejects_removing_a_photo_of_an_archived_perfume() -> None:
    perfume, (first, _) = _archived(2)

    result = perfume.remove_photo(first)

    assert error_code(result) == "CATALOG_PERFUME_ARCHIVED"
    assert len(perfume.photos) == 2
    assert perfume.removed_photo_ids == []


# --- reorder ----------------------------------------------------------------------------------


def test_reordering_sets_the_order_and_the_positions() -> None:
    perfume, (first, second, third) = _with_photos(3)

    result = perfume.reorder_photos([third, first, second])

    assert isinstance(result, Ok)
    assert _order(perfume) == [third, first, second]
    assert _positions(perfume) == [1, 2, 3]


def test_reordering_to_the_same_order_is_accepted() -> None:
    perfume, ids = _with_photos(3)

    assert isinstance(perfume.reorder_photos(list(ids)), Ok)
    assert _order(perfume) == ids


def test_a_perfume_without_photos_accepts_an_empty_order() -> None:
    assert isinstance(_perfume().reorder_photos([]), Ok)


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda ids: ids[:-1], id="missing"),
        pytest.param(lambda ids: [*ids, UNKNOWN], id="extra"),
        pytest.param(lambda ids: [ids[0], ids[0], ids[1]], id="duplicated"),
        pytest.param(lambda ids: [ids[0], ids[1], UNKNOWN], id="foreign"),
        pytest.param(lambda ids: [], id="empty"),
    ],
)
def test_rejects_an_order_that_is_not_the_current_set(build) -> None:  # type: ignore[no-untyped-def]
    perfume, ids = _with_photos(3)

    result = perfume.reorder_photos(build(ids))

    assert error_code(result) == "CATALOG_PHOTO_ORDER_INVALID"
    assert _order(perfume) == ids
    assert _positions(perfume) == [1, 2, 3]


def test_rejects_an_order_for_a_perfume_without_photos_that_lists_an_id() -> None:
    result = _perfume().reorder_photos([UNKNOWN])

    assert error_code(result) == "CATALOG_PHOTO_ORDER_INVALID"


def test_rejects_reordering_an_archived_perfume() -> None:
    perfume, (first, second) = _archived(2)

    result = perfume.reorder_photos([second, first])

    assert error_code(result) == "CATALOG_PERFUME_ARCHIVED"
    assert _order(perfume) == [first, second]


# --- publishing -------------------------------------------------------------------------------


def test_publishing_does_not_need_a_photo() -> None:
    perfume = _perfume()
    add_presentation(perfume)

    assert isinstance(perfume.publish(NOW), Ok)
    assert perfume.photos == []
