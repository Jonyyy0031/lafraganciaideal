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
