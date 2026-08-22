import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import win32con
from PIL import Image, ImageDraw

from fakturama import (
    AutomationResult,
    DateEntryTrace,
    FakturamaConfig,
    GridCellTrace,
    _expected_item_row,
    _absolute_screen_coordinates,
    _detailed_trace_enabled,
    _documents_result_rows,
    _matching_debtor_id,
    _matching_product_id,
    _money_from_ui,
    _copy_text_to_clipboard,
    _item_cell_geometry,
    _paste_full_date,
    _persistence_rows,
    _rendered_currency_glyph,
    _selector_first_row_point,
    _selector_result_rows,
    _save_document_once,
    _set_date_edit,
    _verify_address_text,
    _verify_document_receivers,
    apply_invoice_payment,
    automate_order,
    connect_or_launch,
    detect_item_grid,
    populate_item,
    read_persistence,
    select_existing_product,
    verify_document_charges,
    verify_item_row,
    wait_for,
)
from main import load_order_json


class FakturamaRuntimeTests(unittest.TestCase):
    def test_set_date_edit_pastes_complete_date_and_validates_readback(self) -> None:
        root = Path(__file__).resolve().parents[1]
        payment_date = load_order_json(
            root / "samples" / "synthetic_order_03.expected.json"
        ).payment.payment_date
        date_edit = MagicMock()
        date_edit.get_value.side_effect = ["Aug 22, 2026", "Aug 5, 2026"]

        def observe_committed_date(predicate, *_):
            self.assertTrue(predicate())
            return True

        with patch("fakturama._paste_full_date") as paste, patch(
            "fakturama.wait_for", side_effect=observe_committed_date
        ) as wait:
            result = _set_date_edit(
                date_edit,
                payment_date,
                description="payment date",
            )

        self.assertEqual(result, "Aug 5, 2026")
        paste.assert_called_once_with(date_edit, "Aug 5, 2026", None)
        date_edit.type_keys.assert_not_called()
        self.assertIn("committed payment date Aug 5, 2026", wait.call_args.args)

    def test_set_date_edit_does_not_rewrite_correct_date(self) -> None:
        root = Path(__file__).resolve().parents[1]
        order_date = load_order_json(
            root / "samples" / "synthetic_order_03.expected.json"
        ).order_date
        date_edit = MagicMock()
        date_edit.get_value.return_value = "Aug 3, 2026"

        with patch("fakturama._paste_full_date") as paste:
            result = _set_date_edit(date_edit, order_date, description="Order date")

        self.assertEqual(result, "Aug 3, 2026")
        paste.assert_not_called()
        date_edit.type_keys.assert_not_called()

    def test_auto_selected_product_accepts_master_price_before_order_override(self) -> None:
        root = Path(__file__).resolve().parents[1]
        item = load_order_json(
            root / "samples" / "synthetic_order_02.expected.json"
        ).items[0]
        config = FakturamaConfig(
            Path("fakturama.exe"), Path("workspace"), Path("diagnostics")
        )
        total_net = MagicMock()
        total_net.get_value.side_effect = ["$0.00", "$250.00"]
        search = MagicMock()
        dialog = MagicMock()
        editor = MagicMock()
        editor.top_level_parent.return_value.descendants.return_value = []

        def observe_total_change(predicate, *_):
            self.assertTrue(predicate())
            return True

        with patch("fakturama.read_persistence", return_value="persistence"), patch(
            "fakturama._matching_product_id", return_value=1
        ), patch(
            "fakturama._unique", side_effect=[total_net, search]
        ), patch(
            "fakturama.open_selector", return_value=dialog
        ), patch(
            "fakturama.send_swt_text"
        ), patch(
            "fakturama._stable_descendants", return_value=True
        ), patch(
            "fakturama.wait_for", side_effect=observe_total_change
        ) as wait:
            select_existing_product(editor, item, "USD", config)

        wait.assert_called_once()
        self.assertEqual(item.unit_net, Decimal("300.00"))

    def test_detailed_instrumentation_is_opt_in(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(_detailed_trace_enabled())
        for enabled in ("1", "true", "YES", "on"):
            with self.subTest(enabled=enabled), patch.dict(
                "os.environ", {"FAKTURAMA_DETAILED_TRACE": enabled}, clear=True
            ):
                self.assertTrue(_detailed_trace_enabled())

    def test_grid_cell_trace_serializes_monotonic_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "fakturama.time.monotonic", side_effect=[200.0, 200.4]
        ):
            trace = GridCellTrace(Path(directory))
            attempt = trace.next_attempt(1, "Discount")
            trace.record(
                "grid_cell_attempt",
                attempt=attempt,
                coordinates=[1245, 48],
            )
            record = json.loads(trace.path.read_text(encoding="utf-8"))

        self.assertIn("attempt-001-row-02-discount", attempt)
        self.assertEqual(record["event"], "grid_cell_attempt")
        self.assertEqual(record["monotonic_seconds"], 200.4)
        self.assertAlmostEqual(record["elapsed_seconds"], 0.4)
        self.assertEqual(record["coordinates"], [1245, 48])

    def test_grid_cell_trace_failure_does_not_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            blocked_parent = Path(directory) / "not-a-directory"
            blocked_parent.write_text("blocked", encoding="utf-8")
            trace = GridCellTrace(blocked_parent)
            trace.record("diagnostic-write-attempt")
            self.assertIsNone(
                trace.capture(MagicMock(), "attempt-001", "before-double-click")
            )

    def test_item_cell_geometry_uses_detected_boundaries(self) -> None:
        columns = [(0, 10), (10, 30), (30, 70)]
        rows = [(20, 40), (41, 61)]

        boundaries, coordinates = _item_cell_geometry(columns, rows, 1, 2)

        self.assertEqual(boundaries, (30, 41, 70, 61))
        self.assertEqual(coordinates, (50, 51))
        table_rectangle = SimpleNamespace(left=421, top=373)
        self.assertEqual(
            _absolute_screen_coordinates(table_rectangle, coordinates),
            (471, 424),
        )

    def test_copy_text_to_clipboard_uses_unicode_and_always_closes(self) -> None:
        with patch("fakturama.win32clipboard.OpenClipboard") as open_clipboard, patch(
            "fakturama.win32clipboard.EmptyClipboard"
        ) as empty_clipboard, patch(
            "fakturama.win32clipboard.SetClipboardText"
        ) as set_text, patch(
            "fakturama.win32clipboard.CloseClipboard"
        ) as close_clipboard:
            _copy_text_to_clipboard("Jul 14, 2026")

        open_clipboard.assert_called_once_with()
        empty_clipboard.assert_called_once_with()
        set_text.assert_called_once_with("Jul 14, 2026", win32con.CF_UNICODETEXT)
        close_clipboard.assert_called_once_with()

        with patch("fakturama.win32clipboard.OpenClipboard"), patch(
            "fakturama.win32clipboard.EmptyClipboard"
        ), patch(
            "fakturama.win32clipboard.SetClipboardText",
            side_effect=RuntimeError("clipboard failure"),
        ), patch("fakturama.win32clipboard.CloseClipboard") as close_clipboard:
            with self.assertRaisesRegex(RuntimeError, "clipboard failure"):
                _copy_text_to_clipboard("Jul 14, 2026")
        close_clipboard.assert_called_once_with()

    def test_paste_full_date_clicks_once_and_pastes_complete_value(self) -> None:
        rectangle = MagicMock()
        rectangle.left = 889
        rectangle.top = 156
        rectangle.width.return_value = 129
        rectangle.height.return_value = 17
        date_edit = MagicMock()
        date_edit.rectangle.return_value = rectangle

        with patch("fakturama._copy_text_to_clipboard") as copy_text:
            _paste_full_date(date_edit, "Jul 14, 2026")

        date_edit.click_input.assert_called_once_with(coords=(64, 8))
        copy_text.assert_called_once_with("Jul 14, 2026")
        self.assertEqual(
            date_edit.type_keys.call_args_list,
            [call("^v"), call("{TAB}")],
        )

    def test_date_entry_trace_writes_monotonic_json_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "fakturama.time.monotonic", side_effect=[100.0, 100.25, 100.75]
        ):
            trace = DateEntryTrace(Path(directory))
            trace.record("first", value="Jul 22, 2026")
            trace.record("second", focused=True)

            records = [
                json.loads(line)
                for line in trace.path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(
            records,
            [
                {
                    "event": "first",
                    "monotonic_seconds": 100.25,
                    "elapsed_seconds": 0.25,
                    "value": "Jul 22, 2026",
                },
                {
                    "event": "second",
                    "monotonic_seconds": 100.75,
                    "elapsed_seconds": 0.75,
                    "focused": True,
                },
            ],
        )

    def test_date_entry_trace_write_failure_does_not_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            blocked_parent = Path(directory) / "not-a-directory"
            blocked_parent.write_text("blocked", encoding="utf-8")
            trace = DateEntryTrace(blocked_parent)
            trace.record("diagnostic-write-attempt")

    def test_launch_passes_the_exact_runtime_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "Fakturama.exe"
            executable.write_bytes(b"")
            workspace = root / "runtime workspace"
            workspace.mkdir()
            config = FakturamaConfig(executable, workspace, root / "diagnostics")
            window = object()
            trace = MagicMock()

            with patch(
                "fakturama._configured_main_windows", side_effect=[[], [window]]
            ), patch("fakturama.subprocess.Popen") as launch, patch(
                "fakturama.wait_for", side_effect=lambda predicate, *_: predicate()
            ):
                launch.return_value.pid = 1234
                result = connect_or_launch(config, trace)

            self.assertIs(result, window)
            launch.assert_called_once_with(
                [
                    str(executable),
                    "--workspace",
                    str(workspace.resolve()),
                ],
                cwd=str(executable.parent),
            )
            self.assertEqual(
                trace.record.call_args_list,
                [
                    call("fakturama_process_launched", process_id=1234),
                    call("main_window_first_detected", launch_required=True),
                ],
            )

    def test_documents_rows_ignore_empty_grid_bands(self) -> None:
        image = Image.new("RGB", (403, 321), (245, 245, 245))
        drawing = ImageDraw.Draw(image)
        for y in range(25, 301, 25):
            drawing.line((0, y, 402, y), fill=(180, 180, 180))
        drawing.rectangle((30, 32, 60, 40), fill=(20, 20, 20))

        rows = _documents_result_rows(image)

        self.assertEqual(len(rows), 1)
        self.assertLessEqual(rows[0][0], 35)
        self.assertGreaterEqual(rows[0][1], 35)

    def test_automate_order_uses_inherited_invoice_addresses_without_rewrite(self) -> None:
        root = Path(__file__).resolve().parents[1]
        order = load_order_json(root / "samples" / "synthetic_order_01.expected.json")
        config = FakturamaConfig(Path("fakturama.exe"), Path("workspace"), Path("diagnostics"))
        main_window = object()
        order_editor = object()
        saved_order = object()
        invoice_editor = object()
        with patch("fakturama.DateEntryTrace"), patch(
            "fakturama.GridCellTrace"
        ), patch(
            "fakturama.connect_or_launch", return_value=main_window
        ), patch(
            "fakturama.open_new_order", return_value=order_editor
        ), patch("fakturama.verify_document_currency") as verify_currency, patch(
            "fakturama.set_order_header", return_value="PO000003"
        ), patch(
            "fakturama.select_existing_debtor"
        ) as select_debtor, patch("fakturama.verify_order_addresses"), patch(
            "fakturama.populate_item"
        ) as populate, patch("fakturama.verify_document_charges"), patch(
            "fakturama.verify_order_totals"
        ), patch(
            "fakturama.save_order", return_value="PO000003"
        ), patch("fakturama.verify_order_in_documents"), patch(
            "fakturama._rebind_saved_document", return_value=saved_order
        ), patch(
            "fakturama.create_followup_invoice", return_value=invoice_editor
        ), patch(
            "fakturama.verify_copied_invoice", return_value="INV000003"
        ) as verify_invoice, patch("fakturama.apply_invoice_payment"), patch(
            "fakturama.save_invoice", return_value="INV000003"
        ), patch("fakturama.verify_final_documents"):
            result = automate_order(order, config)
        self.assertEqual(result, AutomationResult("PO000003", "INV000003"))
        verify_currency.assert_called_once_with(order_editor, "USD")
        select_debtor.assert_called_once_with(order_editor, order, config)
        verify_invoice.assert_called_once_with(invoice_editor, order)
        self.assertEqual(
            populate.call_args_list,
            [
                call(
                    order_editor,
                    0,
                    order.items[0],
                    order.currency,
                    config,
                    Decimal("450.00"),
                ),
                call(
                    order_editor,
                    1,
                    order.items[1],
                    order.currency,
                    config,
                    Decimal("570.00"),
                ),
            ],
        )

    def test_populate_item_checks_expected_cumulative_raw_net(self) -> None:
        root = Path(__file__).resolve().parents[1]
        item = load_order_json(
            root / "samples" / "synthetic_order_05.expected.json"
        ).items[1]
        config = FakturamaConfig(
            Path("fakturama.exe"), Path("workspace"), Path("diagnostics")
        )
        total_net = MagicMock()
        total_net.window_text.return_value = "Total Net"
        total_net.get_value.return_value = "$588.47"
        editor = MagicMock()
        editor.descendants.return_value = [total_net]
        table = MagicMock()

        with patch("fakturama.select_existing_product"), patch(
            "fakturama.find_items_table", return_value=table
        ), patch("fakturama.edit_item_cell"), patch(
            "fakturama.verify_item_row"
        ), patch("fakturama.render_control"):
            populate_item(
                editor,
                1,
                item,
                "USD",
                config,
                Decimal("588.47"),
            )

        self.assertEqual(total_net.get_value.call_count, 1)

    def test_money_from_ui_requires_usd_marker(self) -> None:
        self.assertEqual(_money_from_ui("$678.30", "USD"), Decimal("678.30"))
        with self.assertRaisesRegex(RuntimeError, "expected USD marker"):
            _money_from_ui("678.30", "USD")

    def test_invoice_paid_checkbox_uses_native_toggle_once(self) -> None:
        root = Path(__file__).resolve().parents[1]
        order = load_order_json(root / "samples" / "synthetic_order_01.expected.json")
        paid = MagicMock()
        paid.window_text.return_value = "paid"
        paid.is_visible.return_value = True
        paid.rectangle.return_value = SimpleNamespace(
            left=386, right=437, top=839, bottom=859
        )
        paid.get_toggle_state.side_effect = [0, 1, 1]
        payment_combo = MagicMock()
        payment_combo.is_visible.return_value = True
        payment_combo.rectangle.return_value = SimpleNamespace(
            left=450, right=560, top=835, bottom=863
        )
        payment_combo.selected_text.return_value = order.payment.method
        invoice_editor = MagicMock()
        invoice_editor.descendants.side_effect = lambda control_type: {
            "CheckBox": [paid],
            "ComboBox": [payment_combo],
        }[control_type]
        date_edit = MagicMock()
        date_edit.get_value.return_value = "Jul 18, 2026"
        value_edit = MagicMock()
        value_edit.get_value.return_value = "$678.30"

        with patch(
            "fakturama._edit_immediately_right_of_label",
            side_effect=[date_edit, value_edit],
        ), patch("fakturama._set_date_edit"):
            apply_invoice_payment(
                invoice_editor,
                order.payment,
                order.totals.gross,
                order.currency,
            )

        paid.toggle.assert_called_once_with()
        paid.click_input.assert_not_called()

    def test_automation_failure_captures_diagnostics_without_masking(self) -> None:
        root = Path(__file__).resolve().parents[1]
        order = load_order_json(
            root / "samples" / "synthetic_order_01.expected.json"
        )
        config = FakturamaConfig(
            Path("fakturama.exe"), Path("workspace"), Path("diagnostics")
        )
        main_window = object()
        original = RuntimeError("original failure")
        with patch("fakturama.DateEntryTrace"), patch(
            "fakturama.GridCellTrace"
        ), patch(
            "fakturama.connect_or_launch", return_value=main_window
        ), patch("fakturama.open_new_order", side_effect=original), patch(
            "fakturama.capture_diagnostics"
        ) as capture, self.assertRaisesRegex(RuntimeError, "original failure"):
            automate_order(order, config)
        capture.assert_called_once_with(
            main_window, "automation-failure", config
        )

        with patch("fakturama.DateEntryTrace"), patch(
            "fakturama.GridCellTrace"
        ), patch(
            "fakturama.connect_or_launch", return_value=main_window
        ), patch("fakturama.open_new_order", side_effect=original), patch(
            "fakturama.capture_diagnostics",
            side_effect=RuntimeError("diagnostic failure"),
        ), self.assertRaisesRegex(RuntimeError, "original failure"):
            automate_order(order, config)

        with patch("fakturama.DateEntryTrace"), patch(
            "fakturama.GridCellTrace"
        ), patch(
            "fakturama.connect_or_launch", side_effect=original
        ), patch("fakturama.capture_diagnostics") as capture, self.assertRaisesRegex(
            RuntimeError, "original failure"
        ):
            automate_order(order, config)
        capture.assert_not_called()

    def test_save_document_once_normal_and_ambiguous_paths(self) -> None:
        save = MagicMock()
        save.is_enabled.return_value = True
        save.window_text.return_value = "Save the current contents"
        main_window = MagicMock()
        editor = object()
        workspace = Path("workspace")

        with patch("fakturama._unique", return_value=save), patch(
            "fakturama.wait_for", return_value=object()
        ):
            result = _save_document_once(
                main_window,
                editor,
                "PO000003",
                "Order",
                "REF-1",
                workspace,
            )
        self.assertEqual(result, "PO000003")
        save.invoke.assert_called_once_with()

        row = ["NULL"] * 18
        row[0] = "3"
        row[1] = "Order"
        row[5] = "REF-1"
        row[7] = "FALSE"
        row[17] = "PO000003"
        save.reset_mock()
        with patch("fakturama._unique", return_value=save), patch(
            "fakturama.wait_for", side_effect=TimeoutError("save timeout")
        ), patch("fakturama.read_persistence", return_value="persistence"), patch(
            "fakturama._persistence_rows", return_value=[row]
        ), patch(
            "fakturama._filter_documents",
            return_value=(object(), Image.new("RGB", (10, 10)), [(1, 2)]),
        ), patch(
            "fakturama._rebind_saved_document", return_value=object()
        ):
            result = _save_document_once(
                main_window,
                editor,
                "PO000003",
                "Order",
                "REF-1",
                workspace,
            )
        self.assertEqual(result, "PO000003")
        save.invoke.assert_called_once_with()

        for persisted_rows, rendered_rows, rebound in (
            ([], [(1, 2)], object()),
            ([row], [], object()),
            ([row], [(1, 2)], None),
        ):
            with self.subTest(
                persisted=bool(persisted_rows),
                rendered=bool(rendered_rows),
                rebound=rebound is not None,
            ):
                save.reset_mock()
                with patch("fakturama._unique", return_value=save), patch(
                    "fakturama.wait_for",
                    side_effect=TimeoutError("save timeout"),
                ), patch(
                    "fakturama.read_persistence", return_value="persistence"
                ), patch(
                    "fakturama._persistence_rows",
                    return_value=persisted_rows,
                ), patch(
                    "fakturama._filter_documents",
                    return_value=(
                        object(),
                        Image.new("RGB", (10, 10)),
                        rendered_rows,
                    ),
                ), patch(
                    "fakturama._rebind_saved_document",
                    return_value=rebound,
                ), self.assertRaisesRegex(
                    RuntimeError, "Save is ambiguous; Save was not retried"
                ):
                    _save_document_once(
                        main_window,
                        editor,
                        "PO000003",
                        "Order",
                        "REF-1",
                        workspace,
                    )
                save.invoke.assert_called_once_with()

    def test_save_fails_if_please_verify_dialog_appears(self) -> None:
        save = MagicMock()
        save.is_enabled.return_value = True
        save.window_text.return_value = "Save the current contents"
        dialog = MagicMock()
        dialog.window_text.return_value = "Please verify"
        dialog.is_visible.return_value = True
        main_window = MagicMock()

        def descendants(*, control_type):
            return [save] if control_type == "Button" else [dialog]

        main_window.descendants.side_effect = descendants
        with self.assertRaisesRegex(
            RuntimeError, "unexpected Please verify dialog"
        ):
            _save_document_once(
                main_window,
                object(),
                "INV000003",
                "Invoice",
                "REF-1",
                Path("workspace"),
            )
        save.invoke.assert_called_once_with()

    def test_source_address_components_are_required(self) -> None:
        root = Path(__file__).resolve().parents[1]
        order = load_order_json(root / "samples" / "synthetic_order_01.expected.json")
        billing = """Northstar Office Inc.
Marta Klein
100 Market Street
US-10001 New York"""
        delivery = """Northstar Office Inc.
100 Market Street
US-10001 New York"""
        _verify_address_text(billing, order.debtor.billing_address, "billing")
        _verify_address_text(delivery, order.debtor.delivery_address, "delivery")

        wrong_recipient = delivery.replace("Northstar Office Inc.", "Wrong Recipient")
        with self.assertRaisesRegex(RuntimeError, "delivery address mismatch"):
            _verify_address_text(
                wrong_recipient,
                order.debtor.delivery_address,
                "delivery",
            )

    def test_identical_billing_and_delivery_receivers_require_two_exact_rows(self) -> None:
        root = Path(__file__).resolve().parents[1]
        order = load_order_json(
            root / "samples" / "synthetic_order_01.expected.json"
        )

        def receiver(
            receiver_id: str,
            recipient: str,
            street: str,
            postal_code: str,
            country_code: str = "US",
        ) -> str:
            row = ["NULL"] * 35
            for index, value in {
                0: receiver_id,
                2: "New York",
                4: r"Northstar Office Inc.\u0009",
                6: country_code,
                9: "FALSE",
                25: street,
                32: postal_code,
                33: "7",
                34: recipient,
            }.items():
                row[index] = value
            encoded = ",".join(
                value
                if value in {"NULL", "FALSE"} or value.isdigit()
                else f"'{value}'"
                for value in row
            )
            return f'INSERT INTO "FKT_DOCUMENTRECEIVER" VALUES({encoded})'

        billing = receiver(
            "10",
            "Northstar Office Inc.",
            "100 Market Street",
            "10001",
        )
        delivery = receiver(
            "9",
            "Northstar Office Inc.",
            "100 Market Street",
            "10001",
        )
        _verify_document_receivers(
            billing + "\n" + delivery, "7", order
        )
        with self.assertRaisesRegex(
            RuntimeError, "receiver persistence mismatch"
        ):
            _verify_document_receivers(
                billing
                + "\n"
                + delivery.replace("100 Market Street", "Wrong Street 1"),
                "7",
                order,
            )
        wrong_country = receiver(
            "9",
            "Northstar Office Inc.",
            "100 Market Street",
            "10001",
            "CA",
        )
        with self.assertRaisesRegex(
            RuntimeError, "receiver persistence mismatch"
        ):
            _verify_document_receivers(
                billing + "\n" + wrong_country,
                "7",
                order,
            )

    def test_expected_item_row_uses_negative_display_discount(self) -> None:
        root = Path(__file__).resolve().parents[1]
        item = load_order_json(
            root / "samples" / "synthetic_order_01.expected.json"
        ).items[0]
        self.assertEqual(
            _expected_item_row(item),
            {
                "sku": "CHR-ERG-01",
                "name": "Ergonomic Desk Chair",
                "quantity": Decimal("2"),
                "unit_net": Decimal("250.00"),
                "vat": Decimal("19"),
                "discount": Decimal("-10"),
                "price": Decimal("450.00"),
            },
        )

    def test_item_row_mismatch_stops_verification(self) -> None:
        root = Path(__file__).resolve().parents[1]
        item = load_order_json(
            root / "samples" / "synthetic_order_01.expected.json"
        ).items[0]
        actual = _expected_item_row(item)
        actual["sku"] = "WRONG-SKU"
        actual["price"] = "$450.00"
        with patch("fakturama._read_item_row", return_value=actual), self.assertRaisesRegex(
            RuntimeError, "item row 1 mismatch"
        ):
            verify_item_row(object(), 0, item, "USD")

    def test_rendered_currency_glyph_is_scale_local(self) -> None:
        image = Image.new("RGB", (30, 12), (0, 120, 212))
        glyph = ((0, 0), (0, 1), (1, 1), (0, 2))
        for offset in (0, 15):
            for x, y in glyph:
                image.putpixel((offset + 3 + x, 3 + y), (255, 255, 255))
            image.putpixel((offset + 9, 6), (255, 255, 255))
            image.putpixel((offset + 9, 7), (255, 255, 255))
        first = _rendered_currency_glyph(image, (0, 0, 15, 12))
        second = _rendered_currency_glyph(image, (15, 0, 30, 12))
        self.assertEqual(first, second)

    def test_document_charges_require_zero_and_free_shipping(self) -> None:
        root = Path(__file__).resolve().parents[1]
        order = load_order_json(
            root / "samples" / "synthetic_order_01.expected.json"
        )
        rectangle = lambda left, right, top: SimpleNamespace(
            left=left, right=right, top=top, bottom=top + 20
        )
        discount = MagicMock()
        discount.window_text.return_value = "Discount"
        discount.is_visible.return_value = True
        discount.get_value.return_value = "0%"
        discount.rectangle.return_value = rectangle(200, 300, 70)
        shipping = MagicMock()
        shipping.window_text.return_value = "Shipping"
        shipping.is_visible.return_value = True
        shipping.get_value.return_value = "Free of shipping costs"
        shipping.rectangle.return_value = rectangle(0, 100, 100)
        shipping_value = MagicMock()
        shipping_value.window_text.return_value = ""
        shipping_value.is_visible.return_value = True
        shipping_value.get_value.return_value = "$0.00"
        shipping_value.rectangle.return_value = rectangle(100, 200, 100)
        editor = MagicMock()
        editor.descendants.return_value = [discount, shipping, shipping_value]

        verify_document_charges(editor, order)

        discount.get_value.return_value = "1%"
        with self.assertRaisesRegex(RuntimeError, "document charges mismatch"):
            verify_document_charges(editor, order)

    def test_detect_item_grid_from_fixture_at_supported_scales(self) -> None:
        path = (
            Path(__file__).resolve().parents[1]
            / "tests"
            / "fixtures"
            / "item-grid.png"
        )
        with Image.open(path) as source:
            for scale in (1, 1.25, 1.5):
                with self.subTest(scale=scale):
                    image = source.resize(
                        (
                            round(source.width * scale),
                            round(source.height * scale),
                        ),
                        Image.Resampling.LANCZOS,
                    )
                    columns, rows = detect_item_grid(image)
                    self.assertEqual(len(columns), 10)
                    self.assertGreaterEqual(len(rows), 1)
                    self.assertTrue(
                        all(0 <= left < right <= image.width for left, right in columns)
                    )
                    self.assertTrue(
                        all(0 <= top < bottom <= image.height for top, bottom in rows)
                    )

    def test_selector_render_requires_exactly_one_row(self) -> None:
        path = (
            Path(__file__).resolve().parents[1]
            / "tests"
            / "fixtures"
            / "selector-one-row.png"
        )
        with Image.open(path) as source:
            for scale in (1, 1.25, 1.5):
                with self.subTest(scale=scale):
                    image = source.resize(
                        (
                            round(source.width * scale),
                            round(source.height * scale),
                        ),
                        Image.Resampling.LANCZOS,
                    )
                    self.assertEqual(len(_selector_result_rows(image)), 1)
                    x, y = _selector_first_row_point(image)
                    self.assertTrue(0 <= x < image.width)
                    self.assertTrue(0 <= y < image.height)

            zero = source.copy()
            for y in range(26, 52):
                for x in range(zero.width):
                    zero.putpixel((x, y), (255, 255, 255))
            self.assertEqual(_selector_result_rows(zero), [])
            with self.assertRaisesRegex(RuntimeError, "found 0"):
                _selector_first_row_point(zero)

            multiple = source.copy()
            multiple.paste(source.crop((0, 26, source.width, 50)), (0, 51))
            self.assertEqual(len(_selector_result_rows(multiple)), 2)
            with self.assertRaisesRegex(RuntimeError, "found 2"):
                _selector_first_row_point(multiple)

    def test_exact_master_persistence_identity_and_ambiguity(self) -> None:
        root = Path(__file__).resolve().parents[1]
        order = load_order_json(
            root / "samples" / "synthetic_order_01.expected.json"
        )

        def insert(table: str, values: list[str]) -> str:
            encoded = ",".join(
                value
                if value in {"NULL", "FALSE", "TRUE"}
                or value.replace(".", "", 1).isdigit()
                else f"'{value}'"
                for value in values
            )
            return f"INSERT INTO {table} VALUES({encoded})"

        contact = ["NULL"] * 33
        for index, value in {
            0: "2",
            1: "Debitor",
            3: r"Northstar Office Inc.\u0009",
            6: "FALSE",
            8: "Marta",
            14: "Klein",
            19: "NORTHSTAR-US",
        }.items():
            contact[index] = value

        def address(
            address_id: str, name: str, street: str, postal_code: str
        ) -> list[str]:
            row = ["NULL"] * 21
            for index, value in {
                0: address_id,
                2: name,
                3: "New York",
                5: "US",
                7: "FALSE",
                16: street,
                19: postal_code,
                20: "2",
            }.items():
                row[index] = value
            return row

        vat = ["NULL"] * 13
        for index, value in {
            0: "2",
            2: "FALSE",
            6: "VAT 19%",
            8: "0.19",
        }.items():
            vat[index] = value
        product = ["NULL"] * 36
        for index, value in {
            0: "2",
            11: "FALSE",
            14: "CHR-ERG-01",
            17: "Ergonomic Desk Chair",
            32: "2",
        }.items():
            product[index] = value
        persistence = "\n".join(
            [
                insert("FKT_CONTACT", contact),
                insert(
                    "FKT_ADDRESS",
                    address(
                        "2",
                        "Northstar Office Inc.",
                        "100 Market Street",
                        "10001",
                    ),
                ),
                "INSERT INTO FKT_ADDRESS_CONTACTTYPES VALUES(2,'BILLING')",
                "INSERT INTO FKT_ADDRESS_CONTACTTYPES VALUES(2,'DELIVERY')",
                insert('"FKT_VAT"', vat),
                insert('"FKT_PRODUCT"', product),
            ]
        )
        self.assertEqual(_matching_debtor_id(order, persistence), 2)
        with self.assertRaisesRegex(RuntimeError, r"found IDs \[\]"):
            _matching_debtor_id(
                order,
                persistence.replace("'US'", "'CA'", 1),
            )
        self.assertEqual(
            _matching_product_id(order.items[0], persistence), 2
        )

        ambiguous_product = product.copy()
        ambiguous_product[0] = "3"
        with self.assertRaisesRegex(RuntimeError, r"found IDs \['2', '3'\]"):
            _matching_product_id(
                order.items[0],
                persistence
                + "\n"
                + insert('"FKT_PRODUCT"', ambiguous_product),
            )

    def test_repository_workspace_fixture_has_exact_usd_happy_path_masters(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workspace = root / "samples" / "fakturama_workspace"
        order = load_order_json(root / "samples" / "synthetic_order_01.expected.json")
        persistence = (workspace / "Database" / "Database.script").read_text(
            encoding="latin-1"
        )

        self.assertEqual(_matching_debtor_id(order, persistence), 1)
        self.assertEqual(
            {_matching_product_id(item, persistence) for item in order.items},
            {2, 3},
        )
        payment_rows = [
            row
            for row in _persistence_rows(persistence, "FKT_PAYMENT")
            if len(row) > 10
            and row[3] == "FALSE"
            and row[10] == order.payment.method
        ]
        self.assertEqual(len(payment_rows), 1)
        currency_rows = [
            row
            for row in _persistence_rows(persistence, "FKT_USERPROPERTY")
            if len(row) > 11 and row[7] == "PREFERENCE_CURRENCY_LOCALE"
        ]
        self.assertEqual(len(currency_rows), 1)
        self.assertEqual(currency_rows[0][11], "es/US")
        address_format_rows = [
            row
            for row in _persistence_rows(persistence, "FKT_USERPROPERTY")
            if len(row) > 11 and row[7] == "CONTACT_FORMAT_ADDRESS"
        ]
        self.assertEqual(len(address_format_rows), 1)
        self.assertEqual(
            address_format_rows[0][11],
            "{company}<br>{title} {firstname} {lastname}<br>{street}<br>"
            "{countrycode}{zip} {city}<br>{country}",
        )
        self.assertEqual(_persistence_rows(persistence, "FKT_DOCUMENT"), [])
        self.assertEqual(_persistence_rows(persistence, "FKT_DOCUMENTRECEIVER"), [])
        self.assertNotIn("Warehouse", persistence)
        self.assertEqual(
            [
                path
                for pattern in ("*.log", "*.lck", "*.lock")
                for path in workspace.rglob(pattern)
            ],
            [],
        )

    def test_read_persistence_reads_script_and_log_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            database = workspace / "Database"
            database.mkdir()
            script = database / "Database.script"
            log = database / "Database.log"
            script.write_bytes("script Northstar\n".encode("latin-1"))
            log.write_bytes("log MAT-DESK-02\n".encode("latin-1"))
            before = (script.read_bytes(), log.read_bytes())

            combined = read_persistence(workspace)

            self.assertIn("script Northstar", combined)
            self.assertIn("log MAT-DESK-02", combined)
            self.assertEqual((script.read_bytes(), log.read_bytes()), before)

    def test_wait_for_reports_last_observed_value(self) -> None:
        with self.assertRaisesRegex(TimeoutError, "last observed value: False"):
            wait_for(lambda: False, 0.01, "an impossible condition")


if __name__ == "__main__":
    unittest.main()
