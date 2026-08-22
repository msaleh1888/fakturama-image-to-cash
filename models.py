from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator


MONEY_QUANTUM = Decimal("0.01")


def round_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def calculate_raw_line_net(
    quantity: Decimal,
    unit_net: Decimal,
    discount_percent: Decimal,
) -> Decimal:
    return quantity * unit_net * (
        Decimal("1") - discount_percent / Decimal("100")
    )


def _require_nonblank(value: str | None) -> str:
    if value is None or not value.strip():
        raise ValueError("must be non-null and nonblank")
    return value


def _require_value(value: object | None) -> object:
    if value is None:
        raise ValueError("must be non-null")
    return value


class Address(BaseModel):
    name: str | None
    street: str | None
    postal_code: str | None
    city: str | None
    country: str | None

    _nonblank_strings = field_validator(
        "name", "street", "postal_code", "city", "country"
    )(_require_nonblank)


class DebtorData(BaseModel):
    customer_id: str | None
    company: str | None
    alias: str | None
    contact_first_name: str | None
    contact_last_name: str | None
    email: str | None
    phone: str | None
    billing_address: Address
    delivery_address: Address

    _nonblank_strings = field_validator(
        "customer_id",
        "company",
        "alias",
        "contact_first_name",
        "contact_last_name",
        "email",
        "phone",
    )(_require_nonblank)


class PaymentData(BaseModel):
    method: str | None
    status: Literal["PAID", "UNPAID"] | None
    payment_date: date | None

    _nonblank_method = field_validator("method")(_require_nonblank)
    _required_status = field_validator("status")(_require_value)

    @model_validator(mode="after")
    def validate_payment_date(self) -> PaymentData:
        if self.status == "PAID" and self.payment_date is None:
            raise ValueError("payment.payment_date must be non-null when status is PAID")
        if self.status == "UNPAID" and self.payment_date is not None:
            raise ValueError("payment.payment_date must be null when status is UNPAID")
        return self


class LineItem(BaseModel):
    sku: str | None
    description: str | None
    quantity: Decimal | None
    unit: str | None
    unit_net: Decimal | None
    discount_percent: Decimal | None
    vat_percent: Decimal | None
    source_line_net: Decimal | None

    _nonblank_strings = field_validator("sku", "description", "unit")(
        _require_nonblank
    )

    @field_validator(
        "quantity",
        "unit_net",
        "discount_percent",
        "vat_percent",
        "source_line_net",
    )
    @classmethod
    def require_decimal(cls, value: Decimal | None) -> Decimal:
        if value is None:
            raise ValueError("must be non-null")
        return value

    @field_validator("unit_net", "source_line_net")
    @classmethod
    def round_monetary_fields(cls, value: Decimal) -> Decimal:
        return round_money(value)

    @model_validator(mode="after")
    def validate_line(self) -> LineItem:
        assert self.quantity is not None
        assert self.unit_net is not None
        assert self.discount_percent is not None
        assert self.vat_percent is not None
        assert self.source_line_net is not None

        if self.quantity <= 0:
            raise ValueError("quantity must be greater than zero")
        if self.unit_net < 0:
            raise ValueError("unit_net must be nonnegative")
        if self.source_line_net < 0:
            raise ValueError("source_line_net must be nonnegative")
        if not Decimal("0") <= self.discount_percent <= Decimal("100"):
            raise ValueError("discount_percent must be between 0 and 100 inclusive")
        if not Decimal("0") <= self.vat_percent <= Decimal("100"):
            raise ValueError("vat_percent must be between 0 and 100 inclusive")

        calculated = round_money(
            calculate_raw_line_net(
                self.quantity,
                self.unit_net,
                self.discount_percent,
            )
        )
        if self.source_line_net != calculated:
            raise ValueError(
                "source_line_net mismatch: "
                f"extracted={self.source_line_net} calculated={calculated}"
            )
        return self


@dataclass(frozen=True)
class CalculatedTotals:
    net: Decimal
    vat: Decimal
    gross: Decimal


def calculate_order_totals(items: list[LineItem]) -> CalculatedTotals:
    raw_net = Decimal("0")
    raw_vat = Decimal("0")
    for item in items:
        assert item.quantity is not None
        assert item.unit_net is not None
        assert item.discount_percent is not None
        assert item.vat_percent is not None
        line_net = calculate_raw_line_net(
            item.quantity,
            item.unit_net,
            item.discount_percent,
        )
        raw_net += line_net
        raw_vat += line_net * item.vat_percent / Decimal("100")
    return CalculatedTotals(
        net=round_money(raw_net),
        vat=round_money(raw_vat),
        gross=round_money(raw_net + raw_vat),
    )


class Totals(BaseModel):
    net: Decimal | None
    vat: Decimal | None
    gross: Decimal | None

    @field_validator("net", "vat", "gross")
    @classmethod
    def require_and_round_money(cls, value: Decimal | None) -> Decimal:
        if value is None:
            raise ValueError("must be non-null")
        return round_money(value)


class OrderData(BaseModel):
    external_reference: str | None
    order_date: date | None
    currency: str | None
    debtor: DebtorData
    payment: PaymentData
    items: list[LineItem]
    totals: Totals

    @classmethod
    def model_json_schema(cls, *args: object, **kwargs: object) -> dict[str, object]:
        schema = super().model_json_schema(*args, **kwargs)

        def remove_decimal_patterns(value: object) -> None:
            if isinstance(value, dict):
                value.pop("pattern", None)
                for child in value.values():
                    remove_decimal_patterns(child)
            elif isinstance(value, list):
                for child in value:
                    remove_decimal_patterns(child)

        remove_decimal_patterns(schema)
        return schema

    _nonblank_reference = field_validator("external_reference")(_require_nonblank)

    @field_validator("order_date")
    @classmethod
    def require_order_date(cls, value: date | None) -> date:
        if value is None:
            raise ValueError("must be non-null")
        return value

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str | None) -> str:
        value = _require_nonblank(value)
        if len(value) != 3 or not value.isascii() or not value.isalpha() or not value.isupper():
            raise ValueError("currency must be exactly three uppercase letters")
        return value

    @field_validator("items")
    @classmethod
    def require_items(cls, value: list[LineItem]) -> list[LineItem]:
        if not value:
            raise ValueError("at least one item is required")
        return value

    @model_validator(mode="after")
    def validate_dates_and_totals(self) -> OrderData:
        assert self.order_date is not None
        assert self.totals.net is not None
        assert self.totals.vat is not None
        assert self.totals.gross is not None

        if (
            self.payment.payment_date is not None
            and self.payment.payment_date < self.order_date
        ):
            raise ValueError("payment.payment_date cannot precede order_date")

        calculated = calculate_order_totals(self.items)

        comparisons = (
            ("totals.net", self.totals.net, calculated.net),
            ("totals.vat", self.totals.vat, calculated.vat),
            ("totals.gross", self.totals.gross, calculated.gross),
        )
        for field, extracted, calculated in comparisons:
            if extracted != calculated:
                raise ValueError(
                    f"{field} mismatch: extracted={extracted} calculated={calculated}"
                )
        return self
