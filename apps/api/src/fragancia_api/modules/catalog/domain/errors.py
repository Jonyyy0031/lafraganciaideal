from dataclasses import dataclass

from fragancia_api.shared.kernel import ConflictError, InvalidValueError, NotFoundError


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
