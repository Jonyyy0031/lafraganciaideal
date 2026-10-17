"""Customer data (plan 003 step 1): name, email, phone, address, the SAT catalogs and the
invoice, each at its bounds, with the field path of every refusal."""

import pytest

from fragancia_api.modules.orders.domain.customer import (
    CFDI_USES,
    MX_STATES,
    TAX_REGIMES,
    Address,
    CustomerEmail,
    CustomerName,
    Invoice,
    Phone,
)
from fragancia_api.modules.orders.domain.errors import CustomerDataInvalid
from fragancia_api.shared.kernel import Err, Ok, Result


def value_of[T](result: Result[T, CustomerDataInvalid]) -> T:
    assert isinstance(result, Ok), result
    return result.value


def field_of(result: Result[object, CustomerDataInvalid]) -> str:
    assert isinstance(result, Err), result
    assert result.error.code == "ORDERS_CUSTOMER_DATA_INVALID"
    assert result.error.details is not None
    return str(result.error.details["field"])


# --- name ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "accepted"), [("A", False), ("Al", True)])
def test_a_name_needs_at_least_two_characters(raw: str, accepted: bool) -> None:
    result = CustomerName.create(raw)

    assert isinstance(result, Ok) is accepted
    if not accepted:
        assert field_of(result) == "contact.name"


@pytest.mark.parametrize(("length", "accepted"), [(120, True), (121, False)])
def test_a_name_has_at_most_120_characters(length: int, accepted: bool) -> None:
    assert isinstance(CustomerName.create("a" * length), Ok) is accepted


def test_a_name_collapses_its_whitespace() -> None:
    assert value_of(CustomerName.create("  Ana \t  López\n")).value == "Ana López"


def test_a_blank_name_is_refused() -> None:
    assert field_of(CustomerName.create("    ")) == "contact.name"


# --- email --------------------------------------------------------------------------------------


def test_an_email_is_trimmed_and_lowercased() -> None:
    assert (
        value_of(CustomerEmail.create("  Ana.Lopez@Example.COM ")).value == "ana.lopez@example.com"
    )


@pytest.mark.parametrize(
    "raw",
    ["", "ana", "ana@", "@example.com", "ana@example", "ana@@example.com", "ana@.com", "ana@b.",
     "an a@example.com", "ana@exa mple.com"],
)  # fmt: skip
def test_a_malformed_email_is_refused(raw: str) -> None:
    assert field_of(CustomerEmail.create(raw)) == "contact.email"


def test_an_email_of_254_characters_is_accepted_and_255_is_refused() -> None:
    tail = "@b.co"

    assert isinstance(CustomerEmail.create("a" * (254 - len(tail)) + tail), Ok)
    assert field_of(CustomerEmail.create("a" * (255 - len(tail)) + tail)) == "contact.email"


# --- phone --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "9981234567",
        "+52 998-123-4567",
        "+52 (998) 123-4567",
        "998.123.4567",
        "(998) 123 4567",
        "+529981234567",
        "529981234567",
        "  998 123 4567  ",
    ],
)
def test_every_phone_form_is_normalized_to_ten_digits(raw: str) -> None:
    assert value_of(Phone.create(raw)).value == "9981234567"


def test_a_ten_digit_number_starting_with_52_is_kept_whole() -> None:
    # "52" is only a country code when 12 digits remain.
    assert value_of(Phone.create("5212345678")).value == "5212345678"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "998123456",  # 9 digits
        "99812345678",  # 11 digits
        "52998123456",  # 11 digits starting with 52: not a country code
        "+52998123456",  # 9 digits after +52
        "+5299812345678",  # 11 digits after +52
        "99812345ab",
        "٩٩٨١٢٣٤٥٦٧",  # non-ASCII digits
    ],
)
def test_a_phone_that_is_not_ten_digits_is_refused(raw: str) -> None:
    assert field_of(Phone.create(raw)) == "contact.phone"


# --- states -------------------------------------------------------------------------------------


def test_there_are_32_states_with_three_letter_codes() -> None:
    assert len(MX_STATES) == 32
    assert all(len(code) == 3 and code.isupper() for code in MX_STATES)
    assert MX_STATES["CMX"] == "Ciudad de México"
    assert MX_STATES["MEX"] == "Estado de México"


def address(**overrides: object) -> Result[Address, CustomerDataInvalid]:
    values: dict[str, object] = {
        "street": "Av. Reforma",
        "exterior_number": "100",
        "interior_number": "4B",
        "neighborhood": "Juárez",
        "postal_code": "06600",
        "municipality": "Cuauhtémoc",
        "state": "CMX",
        "references": "Frente al parque",
    }
    values.update(overrides)
    return Address.create(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("code", sorted(MX_STATES))
def test_every_state_code_is_accepted(code: str) -> None:
    assert value_of(address(state=code)).state == code


@pytest.mark.parametrize("state", ["", "XXX", "cmx", "CDMX", "Jalisco", "JA"])
def test_an_unknown_state_is_refused(state: str) -> None:
    assert field_of(address(state=state)) == "address.state"


def test_a_state_code_is_trimmed() -> None:
    assert value_of(address(state=" JAL ")).state == "JAL"


# --- address ------------------------------------------------------------------------------------


def test_a_valid_address_is_built_with_collapsed_whitespace() -> None:
    built = value_of(address(street="  Av.   Reforma ", references="  cerca   del parque "))

    assert built.street == "Av. Reforma"
    assert built.references == "cerca del parque"
    assert built.postal_code.value == "06600"


@pytest.mark.parametrize("field", ["interior_number", "references"])
@pytest.mark.parametrize("empty", [None, "", "   "])
def test_an_empty_optional_field_becomes_none(field: str, empty: str | None) -> None:
    built = value_of(address(**{field: empty}))

    assert getattr(built, field) is None


# field, minimum accepted length (None when it may be empty), maximum accepted length
ADDRESS_BOUNDS = [
    ("street", 1, 120),
    ("exterior_number", 1, 20),
    ("neighborhood", 1, 120),
    ("municipality", 1, 120),
    ("interior_number", None, 20),
    ("references", None, 300),
]


@pytest.mark.parametrize(("field", "minimum", "maximum"), ADDRESS_BOUNDS)
def test_an_address_field_accepts_its_maximum_length(
    field: str, minimum: int | None, maximum: int
) -> None:
    assert isinstance(address(**{field: "a" * maximum}), Ok)


@pytest.mark.parametrize(("field", "minimum", "maximum"), ADDRESS_BOUNDS)
def test_an_address_field_refuses_one_character_over_its_maximum(
    field: str, minimum: int | None, maximum: int
) -> None:
    assert field_of(address(**{field: "a" * (maximum + 1)})) == f"address.{field}"


@pytest.mark.parametrize("field", ["street", "exterior_number", "neighborhood", "municipality"])
def test_a_required_address_field_refuses_blank(field: str) -> None:
    assert field_of(address(**{field: "   "})) == f"address.{field}"


@pytest.mark.parametrize("postal_code", ["", "0660", "066001", "0660a", "٠٦٦٠٠"])
def test_a_bad_postal_code_is_refused_with_the_address_path(postal_code: str) -> None:
    assert field_of(address(postal_code=postal_code)) == "address.postal_code"


def test_the_first_broken_address_field_in_form_order_is_the_error() -> None:
    result = address(street="", neighborhood="", state="XXX")

    assert field_of(result) == "address.street"
    assert field_of(address(neighborhood="", postal_code="x", state="XXX")) == (
        "address.neighborhood"
    )
    assert field_of(address(postal_code="x", state="XXX")) == "address.postal_code"
    assert field_of(address(municipality="", state="XXX")) == "address.municipality"
    assert field_of(address(state="XXX", references="a" * 301)) == "address.state"


# --- SAT catalogs -------------------------------------------------------------------------------


def test_the_tax_regime_catalog_has_the_19_codes_of_the_plan() -> None:
    assert sorted(TAX_REGIMES) == [
        "601", "603", "605", "606", "607", "608", "610", "611", "612", "614",
        "615", "616", "620", "621", "622", "623", "624", "625", "626",
    ]  # fmt: skip
    assert TAX_REGIMES["626"] == "Régimen Simplificado de Confianza"


def test_the_cfdi_use_catalog_has_22_codes_without_payments_and_payroll() -> None:
    assert len(CFDI_USES) == 22
    assert "CP01" not in CFDI_USES
    assert "CN01" not in CFDI_USES
    assert {"G01", "G02", "G03", "I01", "I08", "D01", "D10", "S01"} <= set(CFDI_USES)


# --- invoice ------------------------------------------------------------------------------------


def invoice(**overrides: object) -> Result[Invoice, CustomerDataInvalid]:
    values: dict[str, object] = {
        "rfc": "XAXX010101000",
        "business_name": "Público en General",
        "tax_regime": "601",
        "cfdi_use": "G03",
        "postal_code": "01000",
    }
    values.update(overrides)
    return Invoice.create(**values)  # type: ignore[arg-type]


def test_a_valid_invoice_is_built() -> None:
    built = value_of(invoice())

    assert (built.rfc, built.business_name, built.tax_regime, built.cfdi_use) == (
        "XAXX010101000",
        "Público en General",
        "601",
        "G03",
    )
    assert built.postal_code.value == "01000"


@pytest.mark.parametrize(
    "rfc",
    [
        "XAXX010101000",  # 13: a person
        "XEXX010101000",
        "ABC010101AB1",  # 12: a company
        "ÑAB010101AB1",
        "A&B010101AB1",
        "&AB010101AB1",
    ],
)
def test_an_rfc_of_12_or_13_characters_is_accepted(rfc: str) -> None:
    assert isinstance(invoice(rfc=rfc), Ok)


def test_an_rfc_is_trimmed_and_uppercased() -> None:
    assert value_of(invoice(rfc="  xaxx010101000 ")).rfc == "XAXX010101000"


@pytest.mark.parametrize(
    "rfc",
    [
        "",
        "ABC123",
        "ABC010101AB",  # 11 characters
        "ABCDE010101AB1",  # 14 characters
        "AB0010101AB1",  # two letters only before the date
        "ABCD01010AAB1",  # a letter inside the date digits
        "ABC010101AB-",
    ],
)
def test_a_malformed_rfc_is_refused(rfc: str) -> None:
    assert field_of(invoice(rfc=rfc)) == "invoice.rfc"


@pytest.mark.parametrize(("length", "accepted"), [(1, True), (254, True), (255, False), (0, False)])
def test_a_business_name_has_1_to_254_characters(length: int, accepted: bool) -> None:
    result = invoice(business_name="a" * length)

    assert isinstance(result, Ok) is accepted
    if not accepted:
        assert field_of(result) == "invoice.business_name"


def test_a_business_name_collapses_its_whitespace() -> None:
    assert value_of(invoice(business_name=" Mi   Empresa  SA ")).business_name == "Mi Empresa SA"


@pytest.mark.parametrize("code", sorted(TAX_REGIMES))
def test_every_tax_regime_of_the_catalog_is_accepted(code: str) -> None:
    assert value_of(invoice(tax_regime=code)).tax_regime == code


@pytest.mark.parametrize("code", ["", "999", "604", "60", "6011", "abc"])
def test_a_tax_regime_outside_the_catalog_is_refused(code: str) -> None:
    assert field_of(invoice(tax_regime=code)) == "invoice.tax_regime"


@pytest.mark.parametrize("code", sorted(CFDI_USES))
def test_every_cfdi_use_of_the_catalog_is_accepted(code: str) -> None:
    assert value_of(invoice(cfdi_use=code)).cfdi_use == code


def test_a_cfdi_use_is_uppercased() -> None:
    assert value_of(invoice(cfdi_use=" g03 ")).cfdi_use == "G03"


@pytest.mark.parametrize("code", ["CP01", "CN01", "", "G04", "X01"])
def test_a_cfdi_use_that_does_not_apply_to_a_sale_is_refused(code: str) -> None:
    assert field_of(invoice(cfdi_use=code)) == "invoice.cfdi_use"


def test_a_bad_invoice_postal_code_is_refused_with_the_invoice_path() -> None:
    assert field_of(invoice(postal_code="123")) == "invoice.postal_code"


def test_the_first_broken_invoice_field_in_form_order_is_the_error() -> None:
    assert field_of(invoice(rfc="x", business_name="", tax_regime="9", cfdi_use="x")) == (
        "invoice.rfc"
    )
    assert field_of(invoice(business_name="", tax_regime="9", cfdi_use="x")) == (
        "invoice.business_name"
    )
    assert field_of(invoice(tax_regime="9", cfdi_use="x", postal_code="1")) == (
        "invoice.tax_regime"
    )
    assert field_of(invoice(cfdi_use="x", postal_code="1")) == "invoice.cfdi_use"
