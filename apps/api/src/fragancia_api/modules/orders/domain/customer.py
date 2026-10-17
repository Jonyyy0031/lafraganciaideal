"""Guest customer data: contact, Mexican address (decision 28), phone (decision 29) and the
invoice data checked against the SAT catalogs (decision 27)."""

import re
from dataclasses import dataclass
from typing import Self

from fragancia_api.modules.orders.domain.delivery import PostalCode
from fragancia_api.modules.orders.domain.errors import CustomerDataInvalid
from fragancia_api.shared.kernel import Err, Ok, Result

NAME_MIN = 2
NAME_MAX = 120
EMAIL_MAX = 254
PHONE_DIGITS = 10
STREET_MAX = 120
EXTERIOR_MAX = 20
INTERIOR_MAX = 20
NEIGHBORHOOD_MAX = 120
MUNICIPALITY_MAX = 120
REFERENCES_MAX = 300
BUSINESS_NAME_MAX = 254

_RFC = re.compile(r"^[A-ZÑ&]{3,4}[0-9]{6}[A-Z0-9]{3}$")
_PHONE_NOISE = str.maketrans("", "", " -.()")


def _invalid(field: str) -> CustomerDataInvalid:
    return CustomerDataInvalid(details={"field": field})


def _collapse(raw: str) -> str:
    return " ".join(raw.split())


@dataclass(frozen=True, slots=True)
class CustomerName:
    value: str

    @classmethod
    def create(cls, raw: str) -> Result[Self, CustomerDataInvalid]:
        value = _collapse(raw)
        if not NAME_MIN <= len(value) <= NAME_MAX:
            return Err(_invalid("contact.name"))
        return Ok(cls(value))


@dataclass(frozen=True, slots=True)
class CustomerEmail:
    value: str

    @classmethod
    def create(cls, raw: str) -> Result[Self, CustomerDataInvalid]:
        value = raw.strip().lower()
        local, at, domain = value.partition("@")
        valid = (
            len(value) <= EMAIL_MAX
            and not any(char.isspace() for char in value)
            and value.count("@") == 1
            and bool(at)
            and bool(local)
            and "." in domain[1:-1]
        )
        if not valid:
            return Err(_invalid("contact.email"))
        return Ok(cls(value))


@dataclass(frozen=True, slots=True)
class Phone:
    """A 10-digit Mexican number; spaces, dashes, dots, parentheses and a leading +52 (or 52
    when 12 digits remain) are ignored."""

    value: str

    @classmethod
    def create(cls, raw: str) -> Result[Self, CustomerDataInvalid]:
        value = raw.strip().translate(_PHONE_NOISE)
        if value.startswith("+52"):
            value = value[3:]
        elif value.startswith("52") and len(value) == PHONE_DIGITS + 2:
            value = value[2:]
        if len(value) != PHONE_DIGITS or not (value.isascii() and value.isdigit()):
            return Err(_invalid("contact.phone"))
        return Ok(cls(value))


MX_STATES: dict[str, str] = {
    "AGU": "Aguascalientes",
    "BCN": "Baja California",
    "BCS": "Baja California Sur",
    "CAM": "Campeche",
    "CHP": "Chiapas",
    "CHH": "Chihuahua",
    "CMX": "Ciudad de México",
    "COA": "Coahuila",
    "COL": "Colima",
    "DUR": "Durango",
    "MEX": "Estado de México",
    "GUA": "Guanajuato",
    "GRO": "Guerrero",
    "HID": "Hidalgo",
    "JAL": "Jalisco",
    "MIC": "Michoacán",
    "MOR": "Morelos",
    "NAY": "Nayarit",
    "NLE": "Nuevo León",
    "OAX": "Oaxaca",
    "PUE": "Puebla",
    "QUE": "Querétaro",
    "ROO": "Quintana Roo",
    "SLP": "San Luis Potosí",
    "SIN": "Sinaloa",
    "SON": "Sonora",
    "TAB": "Tabasco",
    "TAM": "Tamaulipas",
    "TLA": "Tlaxcala",
    "VER": "Veracruz",
    "YUC": "Yucatán",
    "ZAC": "Zacatecas",
}

# SAT c_RegimenFiscal.
TAX_REGIMES: dict[str, str] = {
    "601": "General de Ley Personas Morales",
    "603": "Personas Morales con Fines no Lucrativos",
    "605": "Sueldos y Salarios e Ingresos Asimilados a Salarios",
    "606": "Arrendamiento",
    "607": "Régimen de Enajenación o Adquisición de Bienes",
    "608": "Demás ingresos",
    "610": "Residentes en el Extranjero sin Establecimiento Permanente en México",
    "611": "Ingresos por Dividendos (socios y accionistas)",
    "612": "Personas Físicas con Actividades Empresariales y Profesionales",
    "614": "Ingresos por intereses",
    "615": "Régimen de los ingresos por obtención de premios",
    "616": "Sin obligaciones fiscales",
    "620": "Sociedades Cooperativas de Producción que optan por diferir sus ingresos",
    "621": "Incorporación Fiscal",
    "622": "Actividades Agrícolas, Ganaderas, Silvícolas y Pesqueras",
    "623": "Opcional para Grupos de Sociedades",
    "624": "Coordinados",
    "625": (
        "Régimen de las Actividades Empresariales con ingresos a través de Plataformas Tecnológicas"
    ),
    "626": "Régimen Simplificado de Confianza",
}

# SAT c_UsoCFDI without CP01 (Pagos) and CN01 (Nómina), which do not apply to a sale.
CFDI_USES: dict[str, str] = {
    "G01": "Adquisición de mercancías",
    "G02": "Devoluciones, descuentos o bonificaciones",
    "G03": "Gastos en general",
    "I01": "Construcciones",
    "I02": "Mobiliario y equipo de oficina por inversiones",
    "I03": "Equipo de transporte",
    "I04": "Equipo de cómputo y accesorios",
    "I05": "Dados, troqueles, moldes, matrices y herramental",
    "I06": "Comunicaciones telefónicas",
    "I07": "Comunicaciones satelitales",
    "I08": "Otra maquinaria y equipo",
    "D01": "Honorarios médicos, dentales y gastos hospitalarios",
    "D02": "Gastos médicos por incapacidad o discapacidad",
    "D03": "Gastos funerales",
    "D04": "Donativos",
    "D05": "Intereses reales efectivamente pagados por créditos hipotecarios (casa habitación)",
    "D06": "Aportaciones voluntarias al SAR",
    "D07": "Primas por seguros de gastos médicos",
    "D08": "Gastos de transportación escolar obligatoria",
    "D09": "Depósitos en cuentas para el ahorro, primas que tengan como base planes de pensiones",
    "D10": "Pagos por servicios educativos (colegiaturas)",
    "S01": "Sin efectos fiscales",
}


def _required(raw: str, field: str, max_length: int) -> Result[str, CustomerDataInvalid]:
    value = _collapse(raw)
    if not 1 <= len(value) <= max_length:
        return Err(_invalid(field))
    return Ok(value)


def _optional(
    raw: str | None, field: str, max_length: int
) -> Result[str | None, CustomerDataInvalid]:
    value = _collapse(raw or "")
    if not value:
        return Ok(None)
    if len(value) > max_length:
        return Err(_invalid(field))
    return Ok(value)


def _postal(raw: str, field: str) -> Result[PostalCode, CustomerDataInvalid]:
    match PostalCode.create(raw):
        case Err():
            return Err(_invalid(field))
        case Ok(code):
            return Ok(code)


@dataclass(frozen=True, slots=True)
class Address:
    street: str
    exterior_number: str
    interior_number: str | None
    neighborhood: str
    postal_code: PostalCode
    municipality: str
    state: str
    references: str | None

    @classmethod
    def create(
        cls,
        *,
        street: str,
        exterior_number: str,
        interior_number: str | None,
        neighborhood: str,
        postal_code: str,
        municipality: str,
        state: str,
        references: str | None,
    ) -> Result[Self, CustomerDataInvalid]:
        """Build the address; the first broken field (in the order of the form) is the error."""
        match _required(street, "address.street", STREET_MAX):
            case Err(error):
                return Err(error)
            case Ok(street_value):
                pass
        match _required(exterior_number, "address.exterior_number", EXTERIOR_MAX):
            case Err(error):
                return Err(error)
            case Ok(exterior_value):
                pass
        match _optional(interior_number, "address.interior_number", INTERIOR_MAX):
            case Err(error):
                return Err(error)
            case Ok(interior_value):
                pass
        match _required(neighborhood, "address.neighborhood", NEIGHBORHOOD_MAX):
            case Err(error):
                return Err(error)
            case Ok(neighborhood_value):
                pass
        match _postal(postal_code, "address.postal_code"):
            case Err(error):
                return Err(error)
            case Ok(postal_value):
                pass
        match _required(municipality, "address.municipality", MUNICIPALITY_MAX):
            case Err(error):
                return Err(error)
            case Ok(municipality_value):
                pass
        state_value = state.strip()
        if state_value not in MX_STATES:
            return Err(_invalid("address.state"))
        match _optional(references, "address.references", REFERENCES_MAX):
            case Err(error):
                return Err(error)
            case Ok(references_value):
                pass
        return Ok(
            cls(
                street=street_value,
                exterior_number=exterior_value,
                interior_number=interior_value,
                neighborhood=neighborhood_value,
                postal_code=postal_value,
                municipality=municipality_value,
                state=state_value,
                references=references_value,
            )
        )


@dataclass(frozen=True, slots=True)
class Invoice:
    rfc: str
    business_name: str
    tax_regime: str
    cfdi_use: str
    postal_code: PostalCode

    @classmethod
    def create(
        cls,
        *,
        rfc: str,
        business_name: str,
        tax_regime: str,
        cfdi_use: str,
        postal_code: str,
    ) -> Result[Self, CustomerDataInvalid]:
        rfc_value = rfc.strip().upper()
        if not _RFC.match(rfc_value):
            return Err(_invalid("invoice.rfc"))
        match _required(business_name, "invoice.business_name", BUSINESS_NAME_MAX):
            case Err(error):
                return Err(error)
            case Ok(name_value):
                pass
        regime = tax_regime.strip()
        if regime not in TAX_REGIMES:
            return Err(_invalid("invoice.tax_regime"))
        use = cfdi_use.strip().upper()
        if use not in CFDI_USES:
            return Err(_invalid("invoice.cfdi_use"))
        match _postal(postal_code, "invoice.postal_code"):
            case Err(error):
                return Err(error)
            case Ok(postal_value):
                pass
        return Ok(cls(rfc_value, name_value, regime, use, postal_value))
