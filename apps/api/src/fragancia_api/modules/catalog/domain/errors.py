from dataclasses import dataclass

from fragancia_api.shared.kernel import ConflictError, InvalidValueError


@dataclass(frozen=True)
class BrandNameInvalid(InvalidValueError):
    code: str = "CATALOG_BRAND_NAME_INVALID"
    message: str = "A brand name needs 2 to 80 characters, including letters or digits"


@dataclass(frozen=True)
class BrandAlreadyExists(ConflictError):
    code: str = "CATALOG_BRAND_ALREADY_EXISTS"
    message: str = "A brand with this name already exists"
