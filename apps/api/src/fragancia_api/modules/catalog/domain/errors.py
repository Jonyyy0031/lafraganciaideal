from dataclasses import dataclass

from fragancia_api.shared.kernel import (
    BusinessRuleViolationError,
    ConflictError,
    InvalidValueError,
    NotFoundError,
)


@dataclass(frozen=True)
class BrandNameInvalid(InvalidValueError):
    code: str = "CATALOG_BRAND_NAME_INVALID"
    message: str = "A brand name needs 2 to 80 characters, including letters or digits"


@dataclass(frozen=True)
class BrandAlreadyExists(ConflictError):
    code: str = "CATALOG_BRAND_ALREADY_EXISTS"
    message: str = "A brand with this name already exists"


@dataclass(frozen=True)
class BrandNotFound(NotFoundError):
    code: str = "CATALOG_BRAND_NOT_FOUND"
    message: str = "Brand not found"


@dataclass(frozen=True)
class FamilyNameInvalid(InvalidValueError):
    code: str = "CATALOG_FAMILY_NAME_INVALID"
    message: str = "An olfactory family name needs 2 to 80 characters, including letters or digits"


@dataclass(frozen=True)
class FamilyAlreadyExists(ConflictError):
    code: str = "CATALOG_FAMILY_ALREADY_EXISTS"
    message: str = "An olfactory family with this name already exists"


@dataclass(frozen=True)
class FamilyNotFound(NotFoundError):
    code: str = "CATALOG_FAMILY_NOT_FOUND"
    message: str = "Olfactory family not found"


@dataclass(frozen=True)
class ConcentrationNameInvalid(InvalidValueError):
    code: str = "CATALOG_CONCENTRATION_NAME_INVALID"
    message: str = "A concentration name needs 2 to 80 characters, including letters or digits"


@dataclass(frozen=True)
class ConcentrationAbbreviationInvalid(InvalidValueError):
    code: str = "CATALOG_CONCENTRATION_ABBREVIATION_INVALID"
    message: str = (
        "A concentration abbreviation needs 2 to 12 characters, including letters or digits"
    )


@dataclass(frozen=True)
class ConcentrationAlreadyExists(ConflictError):
    code: str = "CATALOG_CONCENTRATION_ALREADY_EXISTS"
    message: str = "A concentration with this name already exists"


@dataclass(frozen=True)
class ConcentrationAbbreviationTaken(ConflictError):
    code: str = "CATALOG_CONCENTRATION_ABBREVIATION_TAKEN"
    message: str = "Another concentration already uses this abbreviation"


@dataclass(frozen=True)
class ConcentrationNotFound(NotFoundError):
    code: str = "CATALOG_CONCENTRATION_NOT_FOUND"
    message: str = "Concentration not found"


@dataclass(frozen=True)
class PerfumeNameInvalid(InvalidValueError):
    code: str = "CATALOG_PERFUME_NAME_INVALID"
    message: str = "A perfume name needs 2 to 80 characters, including letters or digits"


@dataclass(frozen=True)
class PerfumeDescriptionTooLong(InvalidValueError):
    code: str = "CATALOG_PERFUME_DESCRIPTION_TOO_LONG"
    message: str = "A perfume description has at most 2000 characters"


@dataclass(frozen=True)
class PerfumeNotesInvalid(InvalidValueError):
    code: str = "CATALOG_PERFUME_NOTES_INVALID"
    message: str = "Each note level holds at most 10 notes of 1 to 40 characters"


@dataclass(frozen=True)
class PresentationMlInvalid(InvalidValueError):
    code: str = "CATALOG_PRESENTATION_ML_INVALID"
    message: str = "A presentation needs a whole number of ml from 1 to 1000"


@dataclass(frozen=True)
class PresentationPriceInvalid(InvalidValueError):
    code: str = "CATALOG_PRESENTATION_PRICE_INVALID"
    message: str = "A price must be greater than 0 and at most 100,000 MXN"


@dataclass(frozen=True)
class PresentationSaleInvalid(InvalidValueError):
    code: str = "CATALOG_PRESENTATION_SALE_INVALID"
    message: str = (
        "A sale needs a price lower than the regular one and, with both dates, an end after its "
        "start"
    )


@dataclass(frozen=True)
class PresentationAvailabilityInvalid(InvalidValueError):
    code: str = "CATALOG_PRESENTATION_AVAILABILITY_INVALID"
    message: str = (
        "In-stock presentations take no lead time; made-to-order ones need 1 to 90 days, min "
        "not above max"
    )


@dataclass(frozen=True)
class PerfumeBrandUnavailable(BusinessRuleViolationError):
    code: str = "CATALOG_PERFUME_BRAND_UNAVAILABLE"
    message: str = "The brand does not exist or is archived"


@dataclass(frozen=True)
class PerfumeFamilyUnavailable(BusinessRuleViolationError):
    code: str = "CATALOG_PERFUME_FAMILY_UNAVAILABLE"
    message: str = "The olfactory family does not exist or is archived"


@dataclass(frozen=True)
class PerfumeConcentrationUnavailable(BusinessRuleViolationError):
    code: str = "CATALOG_PERFUME_CONCENTRATION_UNAVAILABLE"
    message: str = "The concentration does not exist or is archived"


@dataclass(frozen=True)
class PerfumeNothingToSell(BusinessRuleViolationError):
    code: str = "CATALOG_PERFUME_NOTHING_TO_SELL"
    message: str = "A perfume needs at least one active presentation to be published"


@dataclass(frozen=True)
class PerfumeArchived(BusinessRuleViolationError):
    code: str = "CATALOG_PERFUME_ARCHIVED"
    message: str = "An archived perfume is read-only; restore it first"


@dataclass(frozen=True)
class PerfumeAlreadyExists(ConflictError):
    code: str = "CATALOG_PERFUME_ALREADY_EXISTS"
    message: str = "A perfume with this brand, name and concentration already exists"


@dataclass(frozen=True)
class PresentationAlreadyExists(ConflictError):
    code: str = "CATALOG_PRESENTATION_ALREADY_EXISTS"
    message: str = "The perfume already has a presentation with this many ml"


@dataclass(frozen=True)
class PerfumeLastPresentation(ConflictError):
    code: str = "CATALOG_PERFUME_LAST_PRESENTATION"
    message: str = "The last active presentation of a published perfume cannot be archived"


@dataclass(frozen=True)
class PerfumeNotFound(NotFoundError):
    code: str = "CATALOG_PERFUME_NOT_FOUND"
    message: str = "Perfume not found"


@dataclass(frozen=True)
class PresentationNotFound(NotFoundError):
    code: str = "CATALOG_PRESENTATION_NOT_FOUND"
    message: str = "Presentation not found"
