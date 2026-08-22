import json
import unittest
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from models import OrderData, calculate_order_totals


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = tuple(
    ROOT / "samples" / f"synthetic_order_{index:02}.expected.json"
    for index in range(1, 6)
)


def load_fixture(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


class OrderDataTests(unittest.TestCase):
    def test_expected_fixtures_load_and_validate(self) -> None:
        for fixture in FIXTURES:
            with self.subTest(fixture=fixture.name):
                self.assertIsInstance(OrderData.model_validate(load_fixture(fixture)), OrderData)

    def test_decimals_serialize_as_strings_and_dates_as_iso_strings(self) -> None:
        for fixture in FIXTURES:
            with self.subTest(fixture=fixture.name):
                serialized = json.loads(
                    OrderData.model_validate(load_fixture(fixture)).model_dump_json()
                )
                self.assertRegex(serialized["order_date"], r"^\d{4}-\d{2}-\d{2}$")
                self.assertRegex(
                    serialized["payment"]["payment_date"], r"^\d{4}-\d{2}-\d{2}$"
                )
                for item in serialized["items"]:
                    for field in (
                        "quantity",
                        "unit_net",
                        "discount_percent",
                        "vat_percent",
                        "source_line_net",
                    ):
                        self.assertIsInstance(item[field], str)
                for value in serialized["totals"].values():
                    self.assertIsInstance(value, str)

    def test_incorrect_source_line_net_is_rejected(self) -> None:
        data = deepcopy(load_fixture(FIXTURES[0]))
        data["items"][0]["source_line_net"] = "449.99"

        with self.assertRaisesRegex(
            ValidationError,
            r"source_line_net mismatch: extracted=449\.99 calculated=450\.00",
        ):
            OrderData.model_validate(data)

    def test_incorrect_gross_total_is_rejected(self) -> None:
        data = deepcopy(load_fixture(FIXTURES[0]))
        data["totals"]["gross"] = "678.31"

        with self.assertRaisesRegex(
            ValidationError,
            r"totals\.gross mismatch: extracted=678\.31 calculated=678\.30",
        ):
            OrderData.model_validate(data)

    def test_totals_use_unrounded_fakturama_line_values(self) -> None:
        data = deepcopy(load_fixture(FIXTURES[4]))
        data["items"][0]["unit_net"] = "499.95"
        data["items"][0]["source_line_net"] = "424.96"
        data["totals"] = {
            "net": "588.46",
            "vat": "111.81",
            "gross": "700.26",
        }

        order = OrderData.model_validate(data)

        self.assertEqual(order.totals.net, Decimal("588.46"))
        self.assertEqual(order.totals.vat, Decimal("111.81"))
        self.assertEqual(order.totals.gross, Decimal("700.26"))

    def test_rounded_visible_totals_cannot_override_fakturama_gross(self) -> None:
        data = deepcopy(load_fixture(FIXTURES[4]))
        data["items"][0]["unit_net"] = "499.95"
        data["items"][0]["source_line_net"] = "424.96"
        data["totals"] = {
            "net": "588.46",
            "vat": "111.81",
            "gross": "700.27",
        }

        with self.assertRaisesRegex(
            ValidationError,
            r"totals\.gross mismatch: extracted=700\.27 calculated=700\.26",
        ):
            OrderData.model_validate(data)

    def test_cumulative_net_uses_raw_values_before_rounding(self) -> None:
        data = deepcopy(load_fixture(FIXTURES[0]))
        for item in data["items"]:
            item.update(
                {
                    "quantity": "1",
                    "unit_net": "0.01",
                    "discount_percent": "60",
                    "vat_percent": "0",
                    "source_line_net": "0.00",
                }
            )
        data["totals"] = {"net": "0.01", "vat": "0.00", "gross": "0.01"}

        order = OrderData.model_validate(data)

        self.assertEqual(calculate_order_totals(order.items).net, Decimal("0.01"))

    def test_paid_without_payment_date_is_rejected(self) -> None:
        data = deepcopy(load_fixture(FIXTURES[0]))
        data["payment"]["payment_date"] = None

        with self.assertRaisesRegex(
            ValidationError,
            r"payment\.payment_date must be non-null when status is PAID",
        ):
            OrderData.model_validate(data)


if __name__ == "__main__":
    unittest.main()
