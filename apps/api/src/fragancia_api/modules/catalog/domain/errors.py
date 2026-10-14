from dataclasses import dataclass

from fragancia_api.shared.kernel import (
    BusinessRuleViolationError,
    ConflictError,
    DomainError,
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


@dataclass(frozen=True)
class PhotoTooLarge(InvalidValueError):
    code: str = "CATALOG_PHOTO_TOO_LARGE"
    message: str = "The photo is too large"


@dataclass(frozen=True)
class PhotoFormatInvalid(InvalidValueError):
    code: str = "CATALOG_PHOTO_FORMAT_INVALID"
    message: str = "The photo must be a JPEG, PNG or WebP image"


@dataclass(frozen=True)
class PhotoTooSmall(InvalidValueError):
    code: str = "CATALOG_PHOTO_TOO_SMALL"
    message: str = "The shorter side of the photo must be at least 600 px"


@dataclass(frozen=True)
class PhotoOrderInvalid(InvalidValueError):
    code: str = "CATALOG_PHOTO_ORDER_INVALID"
    message: str = "The order must list every photo of the perfume exactly once"


@dataclass(frozen=True)
class PhotoLimitReached(ConflictError):
    code: str = "CATALOG_PHOTO_LIMIT_REACHED"
    message: str = "A perfume has at most 3 photos"


@dataclass(frozen=True)
class PhotoNotFound(NotFoundError):
    code: str = "CATALOG_PHOTO_NOT_FOUND"
    message: str = "Photo not found"


# Catalog import: the first five are HTTP-level; the rest only appear in an import report.


@dataclass(frozen=True)
class ImportFormatInvalid(InvalidValueError):
    code: str = "CATALOG_IMPORT_FORMAT_INVALID"
    message: str = "The import must be an .xlsx file"


@dataclass(frozen=True)
class ImportTooLarge(InvalidValueError):
    code: str = "CATALOG_IMPORT_TOO_LARGE"
    message: str = "The import file is too large"


@dataclass(frozen=True)
class ImportNotFound(NotFoundError):
    code: str = "CATALOG_IMPORT_NOT_FOUND"
    message: str = "Import not found"


@dataclass(frozen=True)
class ImportNotReady(ConflictError):
    code: str = "CATALOG_IMPORT_NOT_READY"
    message: str = "Only a ready import that has not expired can be confirmed or discarded"


@dataclass(frozen=True)
class ImportStale(ConflictError):
    """`details` is `{"issues": [{"row", "column", "code", "message"}, ...]}`."""

    code: str = "CATALOG_IMPORT_STALE"
    message: str = "The catalog changed since this import was validated; upload the file again"


@dataclass(frozen=True)
class ImportFileUnreadable(InvalidValueError):
    code: str = "CATALOG_IMPORT_FILE_UNREADABLE"
    message: str = "The file could not be read as an .xlsx workbook"


@dataclass(frozen=True)
class ImportHeaderMissing(InvalidValueError):
    code: str = "CATALOG_IMPORT_HEADER_MISSING"
    message: str = "Required columns are missing from the header row"


@dataclass(frozen=True)
class ImportHeaderDuplicated(InvalidValueError):
    code: str = "CATALOG_IMPORT_HEADER_DUPLICATED"
    message: str = "Columns appear more than once in the header row"


@dataclass(frozen=True)
class ImportEmpty(InvalidValueError):
    code: str = "CATALOG_IMPORT_EMPTY"
    message: str = "The file has no data rows"


@dataclass(frozen=True)
class ImportTooManyRows(InvalidValueError):
    code: str = "CATALOG_IMPORT_TOO_MANY_ROWS"
    message: str = "The file has too many data rows"


@dataclass(frozen=True)
class ImportRequired(InvalidValueError):
    code: str = "CATALOG_IMPORT_REQUIRED"
    message: str = "This cell is required"


@dataclass(frozen=True)
class ImportValueInvalid(InvalidValueError):
    code: str = "CATALOG_IMPORT_VALUE_INVALID"
    message: str = "The value of this cell is not valid"


@dataclass(frozen=True)
class ImportStockMadeToOrder(InvalidValueError):
    code: str = "CATALOG_IMPORT_STOCK_MADE_TO_ORDER"
    message: str = "Made-to-order presentations carry no stock; leave Existencias empty"


@dataclass(frozen=True)
class ImportStockPermissionRequired(DomainError):
    code: str = "CATALOG_IMPORT_STOCK_PERMISSION_REQUIRED"
    message: str = "Confirming a file with Existencias requires inventory:manage"


@dataclass(frozen=True)
class ImportBrandUnknown(InvalidValueError):
    code: str = "CATALOG_IMPORT_BRAND_UNKNOWN"
    message: str = "No brand has this name"


@dataclass(frozen=True)
class ImportFamilyUnknown(InvalidValueError):
    code: str = "CATALOG_IMPORT_FAMILY_UNKNOWN"
    message: str = "No olfactory family has this name"


@dataclass(frozen=True)
class ImportConcentrationUnknown(InvalidValueError):
    code: str = "CATALOG_IMPORT_CONCENTRATION_UNKNOWN"
    message: str = "No concentration has this name or abbreviation"


@dataclass(frozen=True)
class ImportPerfumeMismatch(InvalidValueError):
    code: str = "CATALOG_IMPORT_PERFUME_MISMATCH"
    message: str = "Rows of the same perfume must carry the same perfume data"


@dataclass(frozen=True)
class ImportImageUrlInvalid(InvalidValueError):
    code: str = "CATALOG_IMPORT_IMAGE_URL_INVALID"
    message: str = "An image URL must start with http:// or https://"


@dataclass(frozen=True)
class ImportImageHostBlocked(InvalidValueError):
    code: str = "CATALOG_IMPORT_IMAGE_HOST_BLOCKED"
    message: str = "The image URL points to a private or reserved address"


@dataclass(frozen=True)
class ImportBrandArchived(ConflictError):
    code: str = "CATALOG_IMPORT_BRAND_ARCHIVED"
    message: str = "The brand is archived"


@dataclass(frozen=True)
class ImportFamilyArchived(ConflictError):
    code: str = "CATALOG_IMPORT_FAMILY_ARCHIVED"
    message: str = "The olfactory family is archived"


@dataclass(frozen=True)
class ImportConcentrationArchived(ConflictError):
    code: str = "CATALOG_IMPORT_CONCENTRATION_ARCHIVED"
    message: str = "The concentration is archived"


@dataclass(frozen=True)
class ImportPerfumeArchived(ConflictError):
    code: str = "CATALOG_IMPORT_PERFUME_ARCHIVED"
    message: str = "The perfume is archived; restore it from the panel first"


@dataclass(frozen=True)
class ImportPresentationArchived(ConflictError):
    code: str = "CATALOG_IMPORT_PRESENTATION_ARCHIVED"
    message: str = "The presentation is archived; restore it from the panel first"


@dataclass(frozen=True)
class ImportDuplicateRow(ConflictError):
    code: str = "CATALOG_IMPORT_DUPLICATE_ROW"
    message: str = "Another row already has this perfume and ml"


@dataclass(frozen=True)
class ImportImageUnreachable(DomainError):
    code: str = "CATALOG_IMPORT_IMAGE_UNREACHABLE"
    message: str = "The image could not be downloaded"


@dataclass(frozen=True)
class ImportInterrupted(DomainError):
    code: str = "CATALOG_IMPORT_INTERRUPTED"
    message: str = "The validation was interrupted; upload the file again"


@dataclass(frozen=True)
class ImportFailed(DomainError):
    code: str = "CATALOG_IMPORT_FAILED"
    message: str = "The validation failed unexpectedly; upload the file again"
