from __future__ import annotations

import ctypes
import csv
import json
import os
import re
import subprocess
import tempfile
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar

import win32con
import win32clipboard
import win32gui
import win32ui
from PIL import Image, ImageChops, ImageDraw
from pywinauto import Desktop

from decimal import Decimal

from models import (
    Address,
    LineItem,
    OrderData,
    PaymentData,
    calculate_order_totals,
    round_money,
)


T = TypeVar("T")


@dataclass
class FakturamaConfig:
    executable: Path
    workspace: Path
    diagnostics_dir: Path


@dataclass
class AutomationResult:
    order_number: str
    invoice_number: str


class DateEntryTrace:
    def __init__(self, diagnostics_dir: Path) -> None:
        self.diagnostics_dir = diagnostics_dir
        self.path = diagnostics_dir / "date-entry-trace.jsonl"
        self.started = time.monotonic()
        try:
            diagnostics_dir.mkdir(parents=True, exist_ok=True)
            self.path.write_text("", encoding="utf-8")
        except Exception:
            pass

    def record(self, event: str, **facts: object) -> None:
        try:
            timestamp = time.monotonic()
            record = {
                "event": event,
                "monotonic_seconds": timestamp,
                "elapsed_seconds": timestamp - self.started,
                **facts,
            }
            with self.path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass


class GridCellTrace:
    def __init__(self, diagnostics_dir: Path) -> None:
        self.path = diagnostics_dir / "grid-cell-trace.jsonl"
        self.images_dir = diagnostics_dir / "grid-cell-trace"
        self.started = time.monotonic()
        self.run_id = str(round(self.started * 1_000_000))
        self.attempt_count = 0
        try:
            diagnostics_dir.mkdir(parents=True, exist_ok=True)
            self.images_dir.mkdir(parents=True, exist_ok=True)
            self.path.write_text("", encoding="utf-8")
        except Exception:
            pass

    def next_attempt(self, row_index: int, column_name: str) -> str:
        self.attempt_count += 1
        safe_column = re.sub(r"[^a-z0-9]+", "-", column_name.lower()).strip("-")
        return (
            f"{self.run_id}-attempt-{self.attempt_count:03d}-"
            f"row-{row_index + 1:02d}-{safe_column}"
        )

    def record(self, event: str, **facts: object) -> None:
        try:
            timestamp = time.monotonic()
            record = {
                "event": event,
                "monotonic_seconds": timestamp,
                "elapsed_seconds": timestamp - self.started,
                **facts,
            }
            with self.path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def capture(
        self,
        table,
        attempt: str,
        stage: str,
        annotation: tuple[int, int, int, int] | None = None,
    ) -> Image.Image | None:
        path = self.images_dir / f"{attempt}-{stage}.png"
        try:
            image = render_control(table, path)
            if annotation is not None:
                drawing = ImageDraw.Draw(image)
                drawing.rectangle(annotation, outline=(255, 0, 0), width=3)
                image.save(path)
            self.record(
                "grid_screenshot",
                attempt=attempt,
                stage=stage,
                path=str(path),
                dimensions=[image.width, image.height],
            )
            return image
        except Exception as error:
            self.record(
                "grid_screenshot_failed",
                attempt=attempt,
                stage=stage,
                path=str(path),
                error=f"{type(error).__name__}: {error}",
            )
            return None

    def save_annotated(
        self,
        image: Image.Image | None,
        attempt: str,
        stage: str,
        annotation: tuple[int, int, int, int],
    ) -> None:
        path = self.images_dir / f"{attempt}-{stage}.png"
        try:
            if image is None:
                raise RuntimeError("the source grid image was unavailable")
            annotated = image.copy()
            drawing = ImageDraw.Draw(annotated)
            drawing.rectangle(annotation, outline=(255, 0, 0), width=3)
            annotated.save(path)
            self.record(
                "grid_screenshot",
                attempt=attempt,
                stage=stage,
                path=str(path),
                dimensions=[annotated.width, annotated.height],
            )
        except Exception as error:
            self.record(
                "grid_screenshot_failed",
                attempt=attempt,
                stage=stage,
                path=str(path),
                error=f"{type(error).__name__}: {error}",
            )


_ACTIVE_GRID_CELL_TRACE: GridCellTrace | None = None


def _detailed_trace_enabled() -> bool:
    return os.getenv("FAKTURAMA_DETAILED_TRACE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _safe_next_grid_attempt(
    trace: GridCellTrace, row_index: int, column_name: str
) -> str | None:
    try:
        return trace.next_attempt(row_index, column_name)
    except Exception:
        return None


def _diagnostic_read(operation: Callable[[], object]) -> object:
    try:
        return operation()
    except Exception as error:
        return {"error": f"{type(error).__name__}: {error}"}


def _control_focus(control) -> object:
    return _diagnostic_read(control.has_keyboard_focus)


def _date_control_facts(control) -> dict[str, object]:
    rectangle = _diagnostic_read(control.rectangle)
    if isinstance(rectangle, dict):
        rectangle_facts: object = rectangle
    else:
        rectangle_facts = [
            rectangle.left,
            rectangle.top,
            rectangle.right,
            rectangle.bottom,
        ]
    return {
        "handle": _diagnostic_read(lambda: control.handle),
        "class_name": _diagnostic_read(lambda: control.element_info.class_name),
        "rectangle": rectangle_facts,
        "visible": _diagnostic_read(control.is_visible),
        "enabled": _diagnostic_read(control.is_enabled),
        "focused": _control_focus(control),
        "value": _diagnostic_read(control.get_value),
    }


def _capture_date_control(
    trace: DateEntryTrace | None, control, stage: str
) -> None:
    if trace is None:
        return
    output_path = trace.diagnostics_dir / f"date-entry-{stage}.png"
    try:
        render_control(control, output_path)
        trace.record(
            "date_control_screenshot",
            stage=stage,
            path=str(output_path),
        )
    except Exception as error:
        trace.record(
            "date_control_screenshot_failed",
            stage=stage,
            path=str(output_path),
            error=f"{type(error).__name__}: {error}",
        )


def _copy_text_to_clipboard(text: str) -> None:
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()


def _paste_full_date(
    date_edit, expected_date: str, trace: DateEntryTrace | None = None
) -> None:
    rectangle = date_edit.rectangle()
    relative_coordinates = (rectangle.width() // 2, rectangle.height() // 2)
    absolute_coordinates = (
        rectangle.left + relative_coordinates[0],
        rectangle.top + relative_coordinates[1],
    )
    if trace is not None:
        trace.record(
            "date_click_before",
            stage="whole_date",
            relative_coordinates=list(relative_coordinates),
            absolute_coordinates=list(absolute_coordinates),
            focused=_control_focus(date_edit),
            value=_diagnostic_read(date_edit.get_value),
        )
    date_edit.click_input(coords=relative_coordinates)
    if trace is not None:
        trace.record(
            "date_click_after",
            stage="whole_date",
            relative_coordinates=list(relative_coordinates),
            absolute_coordinates=list(absolute_coordinates),
            focused=_control_focus(date_edit),
            value=_diagnostic_read(date_edit.get_value),
        )
    _copy_text_to_clipboard(expected_date)
    if trace is not None:
        trace.record("date_clipboard_text_set", value=expected_date)
        trace.record(
            "date_type_keys_before",
            stage="whole_date_paste",
            keys="^v",
            focused=_control_focus(date_edit),
            value=_diagnostic_read(date_edit.get_value),
        )
    date_edit.type_keys("^v")
    if trace is not None:
        trace.record(
            "date_type_keys_after",
            stage="whole_date_paste",
            keys="^v",
            focused=_control_focus(date_edit),
            value=_diagnostic_read(date_edit.get_value),
        )
    _capture_date_control(trace, date_edit, "paste")
    if trace is not None:
        trace.record(
            "date_type_keys_before",
            stage="tab_commit",
            keys="{TAB}",
            focused=_control_focus(date_edit),
            value=_diagnostic_read(date_edit.get_value),
        )
    date_edit.type_keys("{TAB}")
    if trace is not None:
        trace.record(
            "date_type_keys_after",
            stage="tab_commit",
            keys="{TAB}",
            focused=_control_focus(date_edit),
            value=_diagnostic_read(date_edit.get_value),
        )
    _capture_date_control(trace, date_edit, "tab-commit")


def wait_for(predicate: Callable[[], T], timeout: float, description: str) -> T:
    deadline = time.monotonic() + timeout
    last_value: object = None
    while time.monotonic() < deadline:
        try:
            last_value = predicate()
        except Exception as error:
            last_value = f"{type(error).__name__}: {error}"
        if last_value:
            return last_value  # type: ignore[return-value]
        time.sleep(0.2)
    raise TimeoutError(
        f"timed out waiting for {description}; last observed value: {last_value!r}"
    )


def _configured_main_windows(config: FakturamaConfig):
    expected_title = f"Fakturama - {config.workspace.resolve()}"
    return [
        window
        for window in Desktop(backend="uia").windows()
        if window.window_text() == expected_title
        and window.element_info.class_name == "SWT_Window0"
        and window.is_visible()
    ]


def connect_or_launch(
    config: FakturamaConfig, trace: DateEntryTrace | None = None
):
    matches = _configured_main_windows(config)
    if len(matches) > 1:
        raise RuntimeError(
            f"configured Fakturama window is ambiguous: found {len(matches)} matches"
        )
    if len(matches) == 1:
        if trace is not None:
            trace.record("main_window_first_detected", launch_required=False)
        return matches[0]

    if not config.executable.is_file():
        raise ValueError(f"Fakturama executable does not exist: {config.executable}")
    if not config.workspace.is_dir():
        raise ValueError(f"Fakturama workspace does not exist: {config.workspace}")
    process = subprocess.Popen(
        [str(config.executable), "--workspace", str(config.workspace.resolve())],
        cwd=str(config.executable.parent),
    )
    if trace is not None:
        trace.record(
            "fakturama_process_launched",
            process_id=_diagnostic_read(lambda: process.pid),
        )

    main_window_detected = False

    def locate_unique_window():
        nonlocal main_window_detected
        current = _configured_main_windows(config)
        if len(current) > 1:
            raise RuntimeError(
                f"configured Fakturama window is ambiguous: found {len(current)} matches"
            )
        if len(current) == 1:
            if trace is not None and not main_window_detected:
                trace.record("main_window_first_detected", launch_required=True)
                main_window_detected = True
            return current[0]
        return None

    return wait_for(locate_unique_window, 120, "configured Fakturama main window")


def send_swt_text(edit, text: str) -> None:
    edit.set_focus()
    win32gui.SendMessage(edit.handle, win32con.EM_SETSEL, 0, -1)
    win32gui.SendMessage(edit.handle, win32con.WM_CLEAR, 0, 0)
    for character in text:
        win32gui.SendMessage(edit.handle, win32con.WM_CHAR, ord(character), 1)
    wait_for(
        lambda: edit.get_value() if edit.get_value() == text else None,
        10,
        f"SWT text readback {text!r}",
    )


def render_control(control, output_path: Path) -> Image.Image:
    left, top, right, bottom = win32gui.GetWindowRect(control.handle)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise RuntimeError(f"cannot render control with size {width}x{height}")

    window_dc = win32gui.GetWindowDC(control.handle)
    source_dc = win32ui.CreateDCFromHandle(window_dc)
    memory_dc = source_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    try:
        bitmap.CreateCompatibleBitmap(source_dc, width, height)
        memory_dc.SelectObject(bitmap)
        result = ctypes.windll.user32.PrintWindow(
            control.handle, memory_dc.GetSafeHdc(), 2
        )
        if result != 1:
            raise RuntimeError(f"PrintWindow failed for {control.window_text()!r}")
        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        image = Image.frombuffer(
            "RGB",
            (info["bmWidth"], info["bmHeight"]),
            bits,
            "raw",
            "BGRX",
            0,
            1,
        ).copy()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(output_path)
        return image
    finally:
        if bitmap.GetHandle():
            win32gui.DeleteObject(bitmap.GetHandle())
        memory_dc.DeleteDC()
        source_dc.DeleteDC()
        win32gui.ReleaseDC(control.handle, window_dc)


def read_persistence(workspace: Path) -> str:
    database_dir = workspace / "Database"
    parts = [
        (database_dir / filename).read_text(encoding="latin-1")
        for filename in ("Database.script", "Database.log")
    ]
    return "\n".join(parts)


def _control_record(control, ordinal: int) -> dict[str, object]:
    info = control.element_info
    rectangle = control.rectangle()
    record: dict[str, object] = {
        "ordinal": ordinal,
        "title": control.window_text(),
        "control_type": info.control_type,
        "automation_id": info.automation_id,
        "class_name": info.class_name,
        "framework_id": info.framework_id,
        "process_id": info.process_id,
        "visible": control.is_visible(),
        "enabled": control.is_enabled(),
        "rectangle": [rectangle.left, rectangle.top, rectangle.right, rectangle.bottom],
    }
    for name, method_name in (
        ("value", "get_value"),
        ("toggle_state", "get_toggle_state"),
        ("selection", "get_selection"),
    ):
        if hasattr(control, method_name):
            try:
                record[name] = str(getattr(control, method_name)())
            except Exception:
                pass
    return record


def capture_diagnostics(
    main_window, name: str, config: FakturamaConfig
) -> None:
    config.diagnostics_dir.mkdir(parents=True, exist_ok=True)
    render_control(main_window, config.diagnostics_dir / f"{name}.png")
    controls = [main_window, *main_window.descendants()]
    tree_path = config.diagnostics_dir / f"{name}.jsonl"
    tree_path.write_text(
        "\n".join(
            json.dumps(_control_record(control, index), ensure_ascii=False)
            for index, control in enumerate(controls)
        )
        + "\n",
        encoding="utf-8",
    )


def _unique(controls: list, description: str):
    if len(controls) != 1:
        raise RuntimeError(f"{description} uniqueness failure: found {len(controls)}")
    return controls[0]


def open_new_order(main_window, trace: DateEntryTrace | None = None):
    dirty_panes = [
        pane
        for pane in main_window.descendants(control_type="Pane")
        if pane.window_text() == "New Order" and pane.is_visible()
    ]
    dirty_tabs = [
        tab
        for tab in main_window.descendants(control_type="TabItem")
        if tab.window_text().lstrip("*") == "New Order"
    ]
    if dirty_panes or dirty_tabs:
        raise RuntimeError("an unsaved New Order is already open")
    button = _unique(
        [
            candidate
            for candidate in main_window.descendants(control_type="Button")
            if candidate.window_text() == "Create: New Order"
            and candidate.is_visible()
        ],
        "Create: New Order button",
    )
    if trace is not None:
        trace.record(
            "new_order_button_first_observed",
            visible=_diagnostic_read(button.is_visible),
            enabled=_diagnostic_read(button.is_enabled),
        )
    if not button.is_enabled():
        raise RuntimeError("Create: New Order button is disabled")
    button.invoke()

    editor_detected = False

    def locate_editor():
        nonlocal editor_detected
        editor = next(
            (
                pane
                for pane in main_window.descendants(control_type="Pane")
                if pane.window_text() == "New Order" and pane.is_visible()
            ),
            None,
        )
        if editor is not None and trace is not None and not editor_detected:
            trace.record("new_order_editor_detected")
            editor_detected = True
        return editor

    return wait_for(
        locate_editor,
        15,
        "one visible New Order editor",
    )


def verify_document_currency(editor, currency: str) -> None:
    if currency != "USD":
        raise ValueError(f"unsupported document currency: {currency!r}; expected 'USD'")
    total_net = _unique(
        [
            edit
            for edit in editor.descendants(control_type="Edit")
            if edit.window_text() in {"Total Gross", "Total Net"}
            and edit.is_visible()
        ],
        "visible document-total currency Edit",
    )
    _money_from_ui(total_net.get_value(), currency)


def _english_date(value) -> str:
    return f"{value.strftime('%b')} {value.day}, {value.year}"


def set_order_header(
    editor, order: OrderData, trace: DateEntryTrace | None = None
) -> str:
    date_label = _unique(
        [
            text
            for text in editor.descendants(control_type="Text")
            if text.window_text() == "Date"
        ],
        "Date label",
    )
    center = (date_label.rectangle().top + date_label.rectangle().bottom) / 2
    row_edits = [
        edit
        for edit in editor.descendants(control_type="Edit")
        if edit.rectangle().top <= center <= edit.rectangle().bottom
    ]
    date_edit = _unique(
        [edit for edit in row_edits if edit.rectangle().left > date_label.rectangle().right],
        "Order date Edit",
    )
    number_edit = _unique(
        [edit for edit in row_edits if edit.rectangle().right < date_label.rectangle().left],
        "proposed Order number Edit",
    )
    reference_edit = _unique(
        [
            edit
            for edit in editor.descendants(control_type="Edit")
            if edit.window_text() == "Cust.Ref."
        ],
        "Cust.Ref. Edit",
    )
    proposed_number = number_edit.get_value()
    if not re.fullmatch(r"PO\d+", proposed_number):
        raise RuntimeError(f"unexpected proposed Order number: {proposed_number!r}")

    expected_date = _set_date_edit(
        date_edit,
        order.order_date,
        trace,
        "Order date",
    )
    reference_edit.set_edit_text(order.external_reference)

    price_mode = _unique(
        [
            combo
            for combo in editor.descendants(control_type="ComboBox")
            if not combo.window_text() and combo.selected_text() in {"Gross", "Net"}
        ],
        "Order price-mode ComboBox",
    )
    if price_mode.selected_text() != "Net":
        price_mode.expand()

        def net_item():
            matches = [
                item
                for item in price_mode.descendants(control_type="ListItem")
                if item.window_text() == "Net" and item.is_visible()
            ]
            return matches[0] if len(matches) == 1 else None

        wait_for(net_item, 3, "exact Net price-mode item").select()
        wait_for(
            lambda: price_mode.selected_text() == "Net",
            3,
            "Net price mode selection",
        )

    vat_mode = _unique(
        [
            combo
            for combo in editor.descendants(control_type="ComboBox")
            if combo.window_text() == "VAT"
        ],
        "VAT mode ComboBox",
    )
    actual = {
        "number": number_edit.get_value(),
        "date": date_edit.get_value(),
        "reference": reference_edit.get_value(),
        "price_mode": price_mode.selected_text(),
        "vat_mode": vat_mode.selected_text(),
    }
    expected = {
        "number": proposed_number,
        "date": expected_date,
        "reference": order.external_reference,
        "price_mode": "Net",
        "vat_mode": "With VAT",
    }
    if actual != expected:
        raise RuntimeError(f"Order header readback mismatch: expected={expected}, actual={actual}")
    return proposed_number


def open_selector(editor, label: str, dialog_title: str):
    label_control = _unique(
        [
            text
            for text in editor.descendants(control_type="Text")
            if text.window_text() == label
        ],
        f"{label} label",
    )
    images = sorted(
        [image for image in label_control.parent().descendants(control_type="Image") if image.is_visible()],
        key=lambda image: image.rectangle().top,
    )
    if label == "Addresses":
        if len(images) != 2:
            raise RuntimeError(f"Addresses selector Image count: {len(images)}")
        trigger = images[0]
    elif label == "Items":
        if len(images) != 4:
            raise RuntimeError(f"Items selector Image count: {len(images)}")
        trigger = images[0]
    else:
        trigger = _unique(images, f"{label} selector Image")
    rectangle = trigger.rectangle()
    Desktop(backend="win32").window(handle=trigger.handle).click(
        coords=(rectangle.width() // 2, rectangle.height() // 2)
    )
    main_window = editor.top_level_parent()
    return wait_for(
        lambda: next(
            (
                window
                for window in main_window.descendants(control_type="Window")
                if window.window_text() == dialog_title and window.is_visible()
            ),
            None,
        ),
        10,
        f"{dialog_title} selector",
    )


def _shared_debtor_address(order: OrderData) -> Address:
    billing = order.debtor.billing_address
    if billing != order.debtor.delivery_address:
        raise RuntimeError(
            "supported Fakturama happy path requires identical billing and delivery addresses"
        )
    return billing


def _matching_debtor_id(order: OrderData, persistence: str) -> int:
    def stored_text(value: str) -> str:
        return value.removesuffix(r"\u0009")

    contact_rows = _persistence_rows(persistence, "FKT_CONTACT")
    matching_ids = {
        row[0]
        for row in contact_rows
        if len(row) > 20
        and row[1] == "Debitor"
        and stored_text(row[3]) == order.debtor.company
        and row[6] == "FALSE"
        and row[8] == order.debtor.contact_first_name
        and row[14] == order.debtor.contact_last_name
        and row[19] == order.debtor.alias
    }
    address_rows = _persistence_rows(persistence, "FKT_ADDRESS")
    role_rows = _persistence_rows(persistence, "FKT_ADDRESS_CONTACTTYPES")
    roles = {(row[0], row[1]) for row in role_rows if len(row) == 2}
    country_codes = {"United States": "US"}

    address = _shared_debtor_address(order)

    def exact_shared_address(contact_id: str) -> bool:
        matches = [
            row
            for row in address_rows
            if len(row) > 20
            and row[2] == address.name
            and row[3] == address.city
            and row[5] == country_codes.get(address.country)
            and row[7] == "FALSE"
            and row[16] == address.street
            and row[19] == address.postal_code
            and row[20] == contact_id
            and (row[0], "BILLING") in roles
            and (row[0], "DELIVERY") in roles
        ]
        return len(matches) == 1

    exact_ids = {
        contact_id
        for contact_id in matching_ids
        if exact_shared_address(contact_id)
    }
    if len(exact_ids) != 1:
        raise RuntimeError(
            f"expected one exact current Debtor in persistence, found IDs {sorted(exact_ids)}"
        )
    return int(next(iter(exact_ids)))


def _stable_descendants(control) -> bool:
    previous = None
    stable = 0
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = len(control.descendants())
        if current == previous:
            stable += 1
        else:
            stable = 0
            previous = current
        if stable >= 3:
            return True
        time.sleep(0.2)
    return False


def _edge_runs(values: list[int]) -> list[tuple[int, int]]:
    runs: list[list[int]] = []
    for value in values:
        if not runs or value > runs[-1][-1] + 1:
            runs.append([value])
        else:
            runs[-1].append(value)
    return [(run[0], run[-1]) for run in runs]


def _collapse_scaled_edges(centers: list[int]) -> list[int]:
    if len(centers) < 3:
        return centers
    gaps = [right - left for left, right in zip(centers, centers[1:])]
    typical_gap = sorted(gaps)[len(gaps) // 2]
    groups: list[list[int]] = []
    for center in centers:
        if not groups or center - groups[-1][-1] >= typical_gap * 0.40:
            groups.append([center])
        else:
            groups[-1].append(center)
    return [round(sum(group) / len(group)) for group in groups]


def _selector_result_rows(image: Image.Image) -> list[tuple[int, int]]:
    pixels = image.convert("RGB")
    width, height = pixels.size
    horizontal_edges = []
    for y in range(1, height):
        changed = sum(
            max(abs(a - b) for a, b in zip(pixels.getpixel((x, y)), pixels.getpixel((x, y - 1)))) > 25
            for x in range(width)
        )
        if changed >= width * 0.55:
            horizontal_edges.append(y)
    boundaries = _collapse_scaled_edges(
        [
            round((start + end) / 2)
            for start, end in _edge_runs(horizontal_edges)
            if height * 0.01 < round((start + end) / 2) < height * 0.85
        ]
    )
    if not boundaries:
        raise RuntimeError("could not derive selector header boundary")
    if len(boundaries) == 1:
        return []
    row_gap = boundaries[1] - boundaries[0]
    if not height * 0.02 <= row_gap <= height * 0.30:
        raise RuntimeError(f"implausible selector row gap: {row_gap}")
    repeated = [boundaries[0]]
    for boundary in boundaries[1:]:
        gap = boundary - repeated[-1]
        if not row_gap * 0.80 <= gap <= row_gap * 1.20:
            break
        repeated.append(boundary)
    return [
        (top + 1, bottom - 1)
        for top, bottom in zip(repeated, repeated[1:])
        if top + 1 < bottom - 1
    ]


def _selector_first_row_point(image: Image.Image) -> tuple[int, int]:
    pixels = image.convert("RGB")
    width, _ = pixels.size
    rows = _selector_result_rows(image)
    if len(rows) != 1:
        raise RuntimeError(
            f"expected exactly one populated selector result row, found {len(rows)}"
        )
    row_top, row_bottom = rows[0]

    vertical_edges = []
    for x in range(1, width):
        changed = sum(
            max(abs(a - b) for a, b in zip(pixels.getpixel((x, y)), pixels.getpixel((x - 1, y)))) > 25
            for y in range(row_top, row_bottom + 1)
        )
        if changed >= (row_bottom - row_top + 1) * 0.75:
            vertical_edges.append(x)
    separators = _collapse_scaled_edges(
        [
            round((start + end) / 2)
            for start, end in _edge_runs(vertical_edges)
        ]
    )
    if len(separators) < 2:
        raise RuntimeError(
            f"could not derive selector identity column: {separators}"
        )
    return (
        (separators[0] + separators[1]) // 2,
        (row_top + row_bottom) // 2,
    )


def select_existing_debtor(
    editor, order: OrderData, config: FakturamaConfig
) -> None:
    persistence = read_persistence(config.workspace)
    debtor_id = _matching_debtor_id(order, persistence)
    dialog = open_selector(editor, "Addresses", "Select the address")
    search = _unique(dialog.descendants(control_type="Edit"), "Debtor selector Search Edit")
    send_swt_text(search, order.debtor.company)
    if not _stable_descendants(dialog):
        raise TimeoutError("Debtor selector results did not stabilize")
    ok = _unique(
        [button for button in dialog.descendants(control_type="Button") if button.window_text() == "OK"],
        "Debtor selector OK button",
    )
    panes = [
        pane
        for pane in dialog.descendants(control_type="Pane")
        if pane.rectangle().top > search.rectangle().bottom
        and pane.rectangle().bottom < ok.rectangle().top
    ]
    results = _unique(panes, "Debtor selector results Pane")
    image_path = config.diagnostics_dir / "debtor-selector-results.png"
    image = render_control(results, image_path)
    point = _selector_first_row_point(image)
    Desktop(backend="win32").window(handle=results.handle).click(coords=point)
    if debtor_id < 1:
        raise RuntimeError("invalid exact Debtor primary key")
    ok.invoke()
    wait_for(
        lambda: not any(
            window.window_text() == "Select the address" and window.is_visible()
            for window in editor.top_level_parent().descendants(control_type="Window")
        ),
        10,
        "Debtor selector to close",
    )
    wait_for(
        lambda: any(
            order.debtor.billing_address.postal_code in edit.get_value()
            and order.debtor.billing_address.city in edit.get_value()
            for edit in editor.descendants(control_type="Edit")
        ),
        10,
        "selected Debtor address to populate",
    )


def _normalized_multiline(value: str) -> str:
    return "\n".join(line.strip() for line in value.replace("\r\n", "\n").split("\n") if line.strip())


def _address_components(address: Address) -> dict[str, str]:
    return {
        "name": address.name,
        "street": address.street,
        "postal_code": address.postal_code,
        "city": address.city,
        "country": address.country,
    }


def _verify_address_text(actual: str, address: Address, description: str) -> None:
    components = _address_components(address)
    lines = _normalized_multiline(actual).splitlines()
    postal_lines = {
        f'{components["postal_code"]} {components["city"]}',
        f'US-{components["postal_code"]} {components["city"]}',
    }
    checks = {
        "name": components["name"] in lines,
        "street": components["street"] in lines,
        "postal_code_and_city": any(line in postal_lines for line in lines),
    }
    if not all(checks.values()):
        raise RuntimeError(
            f"{description} address mismatch: expected={components}, "
            f"checks={checks}, actual={_normalized_multiline(actual)!r}"
        )


def _address_tab_edit(editor, tab_name: str):
    tab = _unique(
        [tab for tab in editor.descendants(control_type="TabItem") if tab.window_text() == tab_name],
        f"{tab_name} TabItem",
    )
    if not tab.is_selected():
        rectangle = tab.rectangle()
        main_window = editor.top_level_parent()
        main_rectangle = main_window.rectangle()
        main_window.click_input(
            coords=(
                (rectangle.left + rectangle.right) // 2 - main_rectangle.left,
                (rectangle.top + rectangle.bottom) // 2 - main_rectangle.top,
            )
        )
        wait_for(lambda: tab.is_selected(), 3, f"{tab_name} tab selection")
    multiline = [
        edit
        for edit in editor.descendants(control_type="Edit")
        if edit.is_visible() and "\n" in edit.get_value().replace("\r\n", "\n")
    ]
    return _unique(multiline, f"{tab_name} multiline Edit")


def _read_address_tab(editor, tab_name: str) -> str:
    return _address_tab_edit(editor, tab_name).get_value()


def verify_order_addresses(editor, order: OrderData) -> None:
    address = _shared_debtor_address(order)
    billing = _read_address_tab(editor, "Invoice address")
    _verify_address_text(billing, address, "billing")
    delivery = _read_address_tab(editor, "Delivery address")
    _verify_address_text(delivery, address, "delivery")
    restored_billing = _read_address_tab(editor, "Invoice address")
    _verify_address_text(
        restored_billing,
        address,
        "billing after restoring the Invoice address tab",
    )


def _matching_product_id(item: LineItem, persistence: str) -> int:
    vat_ids = {
        row[0]
        for row in _persistence_rows(persistence, "FKT_VAT")
        if len(row) > 8
        and row[2] == "FALSE"
        and row[6] == "VAT 19%"
        and Decimal(row[8]) == Decimal("0.19")
    }
    if len(vat_ids) != 1 or item.vat_percent != Decimal("19"):
        raise RuntimeError(f"expected exact standard VAT 19% identity, found IDs {sorted(vat_ids)}")
    vat_id = next(iter(vat_ids))
    product_ids = {
        row[0]
        for row in _persistence_rows(persistence, "FKT_PRODUCT")
        if len(row) > 32
        and row[11] == "FALSE"
        and row[14] == item.sku
        and row[17] == item.description
        and row[32] == vat_id
    }
    if len(product_ids) != 1:
        raise RuntimeError(
            f"expected one exact current Product {item.sku!r}, found IDs {sorted(product_ids)}"
        )
    return int(next(iter(product_ids)))


def select_existing_product(
    editor, item: LineItem, currency: str, config: FakturamaConfig
) -> None:
    product_id = _matching_product_id(item, read_persistence(config.workspace))
    total_net_edit = _unique(
        [
            edit
            for edit in editor.descendants(control_type="Edit")
            if edit.window_text() == "Total Net"
        ],
        "Total Net Edit before Product selection",
    )
    total_before = _money_from_ui(total_net_edit.get_value(), currency)
    dialog = open_selector(editor, "Items", "Select a product")
    search = _unique(dialog.descendants(control_type="Edit"), "Product selector Search Edit")
    send_swt_text(search, item.sku)
    if not _stable_descendants(dialog):
        raise TimeoutError("Product selector results did not stabilize")
    current_dialogs = [
        window
        for window in editor.top_level_parent().descendants(control_type="Window")
        if window.window_text() == "Select a product" and window.is_visible()
    ]
    if not current_dialogs:
        wait_for(
            lambda: _money_from_ui(total_net_edit.get_value(), currency)
            != total_before,
            10,
            f"auto-selected Product {item.sku} to update Total Net",
        )
        return
    dialog = _unique(current_dialogs, "Select a product dialog after filtering")
    ok = _unique(
        [button for button in dialog.descendants(control_type="Button") if button.window_text() == "OK"],
        "Product selector OK button",
    )
    results = _unique(
        [
            pane
            for pane in dialog.descendants(control_type="Pane")
            if pane.rectangle().top > search.rectangle().bottom
            and pane.rectangle().bottom < ok.rectangle().top
        ],
        "Product selector results Pane",
    )
    image = render_control(
        results, config.diagnostics_dir / f"product-selector-{item.sku}.png"
    )
    point = _selector_first_row_point(image)
    Desktop(backend="win32").window(handle=results.handle).click(coords=point)
    if product_id < 1:
        raise RuntimeError("invalid exact Product primary key")
    ok.invoke()
    wait_for(
        lambda: not any(
            window.window_text() == "Select a product" and window.is_visible()
            for window in editor.top_level_parent().descendants(control_type="Window")
        ),
        10,
        f"Product selector for {item.sku} to close",
    )


def find_items_table(editor):
    main_window = editor.top_level_parent()
    main_window.maximize()
    editor_bottom = editor.rectangle().bottom
    lower_minimize = []
    for button in main_window.descendants(control_type="Button"):
        if button.window_text() != "Minimize":
            continue
        toolbar = button.parent()
        tab = toolbar.parent() if toolbar else None
        if (
            tab
            and tab.element_info.control_type == "Tab"
            and tab.rectangle().top >= editor_bottom
        ):
            lower_minimize.append(button)
    if len(lower_minimize) > 1:
        raise RuntimeError(f"lower panel Minimize ambiguity: {len(lower_minimize)}")
    if lower_minimize:
        before_height = editor.rectangle().height()
        lower_minimize[0].invoke()
        wait_for(
            lambda: editor.rectangle().height() > before_height,
            5,
            "Order editor to expand after lower panel minimize",
        )
    label = _unique(
        [
            text
            for text in editor.descendants(control_type="Text")
            if text.window_text() == "Items"
        ],
        "Items label",
    )
    center = (label.rectangle().top + label.rectangle().bottom) / 2
    candidates = [
        pane
        for pane in editor.descendants(control_type="Pane")
        if pane.rectangle().left > label.rectangle().right
        and pane.rectangle().top <= center <= pane.rectangle().bottom
        and pane.rectangle().width() > editor.rectangle().width() * 0.5
    ]
    if not candidates:
        raise RuntimeError("Items table semantic locator returned no candidates")
    return min(
        candidates,
        key=lambda pane: pane.rectangle().width() * pane.rectangle().height(),
    )


def detect_item_grid(
    image: Image.Image,
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    pixels = image.convert("RGB")
    width, height = pixels.size
    if width < 100 or height < 40:
        raise ValueError(f"item-grid render is too small: {width}x{height}")

    horizontal_edges = []
    for y in range(1, height):
        changed = sum(
            max(
                abs(channel - previous)
                for channel, previous in zip(
                    pixels.getpixel((x, y)), pixels.getpixel((x, y - 1))
                )
            )
            > 25
            for x in range(width)
        )
        if changed >= width * 0.55:
            horizontal_edges.append(y)
    horizontal_centers = _collapse_scaled_edges(
        [
            round((start + end) / 2)
            for start, end in _edge_runs(horizontal_edges)
        ]
    )
    if len(horizontal_centers) < 2:
        raise RuntimeError(
            f"could not derive item-grid header/row boundaries: {horizontal_centers}"
        )
    row_gap = horizontal_centers[1] - horizontal_centers[0]
    if not height * 0.02 <= row_gap <= height * 0.30:
        raise RuntimeError(f"implausible item-grid row gap: {row_gap}")
    repeated_boundaries = [horizontal_centers[0]]
    for boundary in horizontal_centers[1:]:
        gap = boundary - repeated_boundaries[-1]
        if not row_gap * 0.80 <= gap <= row_gap * 1.20:
            break
        repeated_boundaries.append(boundary)
    rows = [
        (top + 1, bottom - 1)
        for top, bottom in zip(
            repeated_boundaries, repeated_boundaries[1:]
        )
        if top + 1 < bottom - 1
    ]
    if not rows:
        raise RuntimeError("could not derive any item-grid rows")

    scan_top, scan_bottom = rows[0]
    vertical_edges = []
    scan_height = scan_bottom - scan_top + 1
    for x in range(1, width):
        changed = sum(
            max(
                abs(channel - previous)
                for channel, previous in zip(
                    pixels.getpixel((x, y)), pixels.getpixel((x - 1, y))
                )
            )
            > 25
            for y in range(scan_top, scan_bottom + 1)
        )
        if changed >= scan_height * 0.75:
            vertical_edges.append(x)
    separators = _collapse_scaled_edges(
        [
            round((start + end) / 2)
            for start, end in _edge_runs(vertical_edges)
        ]
    )
    boundaries = [0, *separators]
    if len(boundaries) != 11 or any(
        left >= right for left, right in zip(boundaries, boundaries[1:])
    ):
        raise RuntimeError(f"unexpected item-grid column boundaries: {boundaries}")
    columns = list(zip(boundaries, boundaries[1:]))
    return columns, rows


_ITEM_COLUMNS = {
    "Pos.": 0,
    "Qty.": 1,
    "Item No.": 2,
    "Picture": 3,
    "Name": 4,
    "Description": 5,
    "VAT": 6,
    "U.Price": 7,
    "Discount": 8,
    "Price": 9,
}


def _current_grid(table):
    path = Path(tempfile.gettempdir()) / "fakturama-current-item-grid.png"
    return detect_item_grid(render_control(table, path))


class _GuiThreadInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


def _foreground_and_focus() -> dict[str, object]:
    foreground = win32gui.GetForegroundWindow()
    thread_id = ctypes.windll.user32.GetWindowThreadProcessId(foreground, None)
    info = _GuiThreadInfo()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
        raise RuntimeError("GetGUIThreadInfo failed")
    return {
        "foreground_window_handle": foreground,
        "foreground_window_title": win32gui.GetWindowText(foreground),
        "focused_control_handle": int(info.hwndFocus or 0),
    }


def _active_document_control(table):
    top_level = table.top_level_parent()
    current = table
    seen: set[int] = set()
    while current is not None:
        handle = int(current.handle)
        if handle in seen:
            break
        seen.add(handle)
        if current is not table and current.window_text():
            return current
        if handle == int(top_level.handle):
            break
        current = current.parent()
    return top_level


def _item_cell_geometry(
    columns: list[tuple[int, int]],
    rows: list[tuple[int, int]],
    row_index: int,
    column_index: int,
) -> tuple[tuple[int, int, int, int], tuple[int, int]]:
    left, right = columns[column_index]
    top, bottom = rows[row_index]
    return (
        (left, top, right, bottom),
        ((left + right) // 2, (top + bottom) // 2),
    )


def _absolute_screen_coordinates(
    rectangle, relative_coordinates: tuple[int, int]
) -> tuple[int, int]:
    return (
        rectangle.left + relative_coordinates[0],
        rectangle.top + relative_coordinates[1],
    )


def _edit_diagnostic_record(edit) -> dict[str, object]:
    rectangle = _diagnostic_read(edit.rectangle)
    return {
        "handle": _diagnostic_read(lambda: edit.handle),
        "rectangle": (
            rectangle
            if isinstance(rectangle, dict)
            else [rectangle.left, rectangle.top, rectangle.right, rectangle.bottom]
        ),
        "value": _diagnostic_read(edit.get_value),
        "visible": _diagnostic_read(edit.is_visible),
        "enabled": _diagnostic_read(edit.is_enabled),
        "parent_handle": _diagnostic_read(lambda: edit.parent().handle),
        "parent_title": _diagnostic_read(lambda: edit.parent().window_text()),
        "parent_class_name": _diagnostic_read(
            lambda: edit.parent().element_info.class_name
        ),
    }


def _grid_visual_changes(
    before: Image.Image | None,
    current: Image.Image | None,
    row: tuple[int, int],
) -> tuple[object, object]:
    if before is None or current is None:
        return None, None
    if before.size != current.size:
        return True, True
    table_changed = ImageChops.difference(before, current).getbbox() is not None
    top, bottom = row
    row_changed = (
        ImageChops.difference(
            before.crop((0, top, before.width, bottom + 1)),
            current.crop((0, top, current.width, bottom + 1)),
        ).getbbox()
        is not None
    )
    return table_changed, row_changed


def _record_grid_observation(
    trace: GridCellTrace,
    attempt: str,
    stage: str,
    table,
    before_image: Image.Image | None,
    row: tuple[int, int],
    before_handles: set[int],
    candidates: list,
) -> None:
    try:
        current_image = trace.capture(table, attempt, stage)
        table_changed, row_changed = _grid_visual_changes(
            before_image, current_image, row
        )
        trace.record(
            "grid_cell_observation",
            attempt=attempt,
            stage=stage,
            **_foreground_and_focus(),
            candidates=[_edit_diagnostic_record(edit) for edit in candidates],
            new_edits_anywhere=[
                _edit_diagnostic_record(edit)
                for edit in table.top_level_parent().descendants(control_type="Edit")
                if edit.handle not in before_handles
            ],
            table_render_changed=table_changed,
            target_row_render_changed=row_changed,
        )
    except Exception as error:
        trace.record(
            "grid_cell_observation_failed",
            attempt=attempt,
            stage=stage,
            error=f"{type(error).__name__}: {error}",
        )


def _open_item_cell(table, row_index: int, column_name: str):
    columns, rows = _current_grid(table)
    if column_name not in _ITEM_COLUMNS:
        raise ValueError(f"unknown item column: {column_name}")
    if not 0 <= row_index < len(rows):
        raise ValueError(f"item row {row_index} is outside detected rows {rows}")
    cell_boundaries, click_coordinates = _item_cell_geometry(
        columns, rows, row_index, _ITEM_COLUMNS[column_name]
    )
    top_level = table.top_level_parent()
    before_edits = top_level.descendants(control_type="Edit")
    before = {edit.handle for edit in before_edits}
    table_rectangle = table.rectangle()
    trace = _ACTIVE_GRID_CELL_TRACE
    attempt = None
    before_image = None
    if trace is not None:
        attempt = _safe_next_grid_attempt(trace, row_index, column_name)
    if trace is not None and attempt is not None:
        before_image = trace.capture(table, attempt, "before-double-click")
        trace.save_annotated(
            before_image,
            attempt,
            "target-annotated",
            cell_boundaries,
        )
        try:
            document = _active_document_control(table)
            document_edits_before = document.descendants(control_type="Edit")
            trace.record(
                "grid_cell_attempt",
                attempt=attempt,
                active_document_editor_title=_diagnostic_read(
                    document.window_text
                ),
                main_window_title=_diagnostic_read(top_level.window_text),
                row_index=row_index,
                row_number=row_index + 1,
                column_name=column_name,
                table={
                    "handle": _diagnostic_read(lambda: table.handle),
                    "class_name": _diagnostic_read(
                        lambda: table.element_info.class_name
                    ),
                    "rectangle": [
                        table_rectangle.left,
                        table_rectangle.top,
                        table_rectangle.right,
                        table_rectangle.bottom,
                    ],
                    "visible": _diagnostic_read(table.is_visible),
                    "enabled": _diagnostic_read(table.is_enabled),
                },
                rendered_grid_dimensions=(
                    [before_image.width, before_image.height]
                    if before_image is not None
                    else None
                ),
                detected_rows=[list(row) for row in rows],
                detected_columns=[list(column) for column in columns],
                chosen_cell_boundaries=list(cell_boundaries),
                relative_click_coordinates=list(click_coordinates),
                absolute_click_coordinates=list(
                    _absolute_screen_coordinates(table_rectangle, click_coordinates)
                ),
                document_edit_handles_before=[
                    edit.handle for edit in document_edits_before
                ],
                top_level_edit_handles_before=[edit.handle for edit in before_edits],
                **_foreground_and_focus(),
            )
        except Exception as error:
            trace.record(
                "grid_cell_attempt_details_failed",
                attempt=attempt,
                error=f"{type(error).__name__}: {error}",
            )
    Desktop(backend="win32").window(handle=table.handle).double_click(
        coords=click_coordinates
    )

    click_time = time.monotonic()

    def candidate_edits():
        return [
            edit
            for edit in top_level.descendants(control_type="Edit")
            if edit.handle not in before
            and table_rectangle.left <= edit.rectangle().left < table_rectangle.right
            and table_rectangle.top <= edit.rectangle().top < table_rectangle.bottom
        ]

    def diagnostic_candidates():
        try:
            return candidate_edits()
        except Exception as error:
            if trace is not None and attempt is not None:
                trace.record(
                    "grid_candidate_collection_failed",
                    attempt=attempt,
                    error=f"{type(error).__name__}: {error}",
                )
            return []

    observed_offsets: set[float] = set()
    observation_offsets = (0.2, 1.0, 3.0)

    if trace is not None and attempt is not None:
        try:
            trace.record(
                "grid_double_click_after",
                attempt=attempt,
                **_foreground_and_focus(),
            )
        except Exception:
            pass
        immediate_matches = diagnostic_candidates()
        _record_grid_observation(
            trace,
            attempt,
            "after-double-click",
            table,
            before_image,
            rows[row_index],
            before,
            immediate_matches,
        )

    def new_cell_edit():
        matches = candidate_edits()
        if trace is not None and attempt is not None:
            elapsed = time.monotonic() - click_time
            for offset in observation_offsets:
                if offset not in observed_offsets and elapsed >= offset:
                    observed_offsets.add(offset)
                    _record_grid_observation(
                        trace,
                        attempt,
                        f"observation-{round(offset * 1000):04d}ms",
                        table,
                        before_image,
                        rows[row_index],
                        before,
                        matches,
                    )
        return matches[0] if len(matches) == 1 else None

    try:
        result = wait_for(
            new_cell_edit,
            5,
            f"{column_name} in-place Edit for row {row_index + 1}",
        )
    except Exception:
        if trace is not None and attempt is not None:
            _record_grid_observation(
                trace,
                attempt,
                "wait-failed",
                table,
                before_image,
                rows[row_index],
                before,
                diagnostic_candidates(),
            )
        raise
    if trace is not None and attempt is not None:
        _record_grid_observation(
            trace,
            attempt,
            "wait-succeeded",
            table,
            before_image,
            rows[row_index],
            before,
            diagnostic_candidates(),
        )
        pending_offsets = [
            offset for offset in observation_offsets if offset not in observed_offsets
        ]
        if pending_offsets:
            trace.record(
                "grid_observations_not_reached_before_success",
                attempt=attempt,
                offsets_seconds=pending_offsets,
            )
    return result


def _display_decimal(value: str) -> Decimal:
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", value)
    if not match:
        raise RuntimeError(f"no decimal value in item cell readback {value!r}")
    return Decimal(match.group(0).replace(",", ""))


def edit_item_cell(
    table,
    row_index: int,
    column_name: str,
    value: str,
    currency: str,
) -> None:
    expected = _display_decimal(value)
    expected_readback = -expected if column_name == "Discount" else expected
    total_net_edits = [
        edit
        for edit in table.top_level_parent().descendants(control_type="Edit")
        if edit.window_text() == "Total Net"
    ]
    total_before = (
        _money_from_ui(total_net_edits[0].get_value(), currency)
        if len(total_net_edits) == 1
        else None
    )
    cell = _open_item_cell(table, row_index, column_name)
    original = _display_decimal(cell.get_value())
    if original == expected_readback:
        handle = cell.handle
        win32gui.SendMessage(handle, win32con.WM_KEYDOWN, win32con.VK_ESCAPE, 0)
        win32gui.SendMessage(handle, win32con.WM_KEYUP, win32con.VK_ESCAPE, 0)
        wait_for(
            lambda: not any(
                edit.handle == handle
                for edit in table.top_level_parent().descendants(control_type="Edit")
            ),
            5,
            f"already-correct {column_name} Edit to close",
        )
        return
    committed_text = (
        format(expected_readback, "f") if column_name == "Discount" else value
    )
    handle = cell.handle
    cell.click_input()
    cell.type_keys(f"^a{committed_text}{{ENTER}}")
    wait_for(
        lambda: not any(
            edit.handle == handle
            for edit in table.top_level_parent().descendants(control_type="Edit")
        ),
        10,
        f"{column_name} in-place Edit to commit",
    )
    if (
        column_name in {"Qty.", "U.Price", "Discount"}
        and original != expected_readback
        and total_before is not None
    ):
        wait_for(
            lambda: _money_from_ui(total_net_edits[0].get_value(), currency)
            != total_before,
            10,
            f"{column_name} to update Total Net",
        )
    confirmed = _open_item_cell(table, row_index, column_name)
    actual = _display_decimal(confirmed.get_value())
    confirm_handle = confirmed.handle
    win32gui.SendMessage(confirm_handle, win32con.WM_KEYDOWN, win32con.VK_ESCAPE, 0)
    win32gui.SendMessage(confirm_handle, win32con.WM_KEYUP, win32con.VK_ESCAPE, 0)
    wait_for(
        lambda: not any(
            edit.handle == confirm_handle
            for edit in table.top_level_parent().descendants(control_type="Edit")
        ),
        5,
        f"confirmed {column_name} Edit to close",
    )
    if actual != expected_readback:
        raise RuntimeError(
            f"{column_name} cell readback mismatch: expected={expected_readback}, actual={actual}"
        )


def populate_item(
    editor,
    row_index: int,
    item: LineItem,
    order_currency: str,
    config: FakturamaConfig,
    expected_total_net: Decimal,
) -> None:
    total_net = _unique(
        [
            edit
            for edit in editor.descendants(control_type="Edit")
            if edit.window_text() == "Total Net"
        ],
        "Total Net Edit before item population",
    )
    select_existing_product(editor, item, order_currency, config)
    table = find_items_table(editor)
    edit_item_cell(table, row_index, "Qty.", format(item.quantity, "f"), order_currency)
    edit_item_cell(table, row_index, "U.Price", f"{item.unit_net:.2f}", order_currency)
    edit_item_cell(table, row_index, "VAT", format(item.vat_percent, "f"), order_currency)
    edit_item_cell(
        table,
        row_index,
        "Discount",
        format(item.discount_percent, "f"),
        order_currency,
    )
    actual_total = _money_from_ui(total_net.get_value(), order_currency)
    if actual_total != expected_total_net:
        raise RuntimeError(
            f"row {row_index + 1} cumulative net mismatch: "
            f"expected total={expected_total_net}, actual={actual_total}"
        )
    verify_item_row(table, row_index, item, order_currency)
    render_control(
        table, config.diagnostics_dir / f"item-row-{row_index + 1}-verified.png"
    )


def _money_from_ui(value: str, expected_currency: str) -> Decimal:
    if expected_currency != "USD":
        raise ValueError(
            f"unsupported document currency: {expected_currency!r}; expected 'USD'"
        )
    if "$" not in value:
        raise RuntimeError(
            f"money currency mismatch: expected USD marker, actual={value!r}"
        )
    return _display_decimal(value)


def _money_or_decimal_from_ui(value: str, expected_currency: str) -> Decimal:
    if "$" in value:
        return _money_from_ui(value, expected_currency)
    return _display_decimal(value)


def verify_order_totals(editor, order: OrderData) -> None:
    expected = {
        "Total Net": order.totals.net,
        "VAT": order.totals.vat,
        "Total": order.totals.gross,
    }
    actual = {}
    for name in expected:
        edit = _unique(
            [
                candidate
                for candidate in editor.descendants(control_type="Edit")
                if candidate.window_text() == name
            ],
            f"{name} total Edit",
        )
        actual[name] = _money_from_ui(edit.get_value(), order.currency)
    if actual != expected:
        raise RuntimeError(f"Order totals mismatch: expected={expected}, actual={actual}")


def create_followup_invoice(saved_order, order: OrderData):
    group = _unique(
        [
            candidate
            for candidate in saved_order.descendants(control_type="Group")
            if candidate.window_text() == "Create a follow-up document"
            and candidate.is_visible()
        ],
        "Create a follow-up document Group",
    )
    invoice = _unique(
        [
            candidate
            for candidate in group.descendants(control_type="Button")
            if candidate.window_text() == "Invoice" and candidate.is_visible()
        ],
        "follow-up Invoice button",
    )
    if not invoice.is_enabled():
        raise RuntimeError("follow-up Invoice button is disabled")
    main_window = saved_order.top_level_parent()
    invoice.invoke()
    return wait_for(
        lambda: next(
            (
                pane
                for pane in main_window.descendants(control_type="Pane")
                if pane.window_text() == "New Invoice" and pane.is_visible()
            ),
            None,
        ),
        15,
        "one visible New Invoice editor",
    )


def _edit_immediately_right_of_label(editor, label_name: str):
    label = _unique(
        [
            candidate
            for candidate in editor.descendants(control_type="Text")
            if candidate.window_text() == label_name and candidate.is_visible()
        ],
        f"{label_name} label",
    )
    label_rectangle = label.rectangle()
    matches = [
        edit
        for edit in editor.descendants(control_type="Edit")
        if edit.is_visible()
        and edit.rectangle().left >= label_rectangle.right
        and edit.rectangle().top < label_rectangle.bottom
        and edit.rectangle().bottom > label_rectangle.top
    ]
    if not matches:
        return _unique(matches, f"Edit immediately right of {label_name}")
    nearest_left = min(edit.rectangle().left for edit in matches)
    return _unique(
        [edit for edit in matches if edit.rectangle().left == nearest_left],
        f"Edit immediately right of {label_name}",
    )


def _combo_immediately_right_of_label(editor, label_name: str):
    labels = [
        candidate
        for candidate in editor.descendants(control_type="Text")
        if candidate.window_text() == label_name and candidate.is_visible()
    ]
    matches = []
    for label in labels:
        rectangle = label.rectangle()
        matches.extend(
            combo
            for combo in editor.descendants(control_type="ComboBox")
            if combo.is_visible()
            and combo.rectangle().left >= rectangle.right
            and combo.rectangle().top < rectangle.bottom
            and combo.rectangle().bottom > rectangle.top
        )
    return _unique(matches, f"ComboBox immediately right of {label_name}")


def _read_item_cell(table, row_index: int, column_name: str) -> str:
    cell = _open_item_cell(table, row_index, column_name)
    value = cell.get_value()
    handle = cell.handle
    win32gui.SendMessage(handle, win32con.WM_KEYDOWN, win32con.VK_ESCAPE, 0)
    win32gui.SendMessage(handle, win32con.WM_KEYUP, win32con.VK_ESCAPE, 0)
    wait_for(
        lambda: not any(
            edit.handle == handle
            for edit in table.top_level_parent().descendants(control_type="Edit")
        ),
        5,
        f"verified {column_name} Edit to close",
    )
    return value


def _read_item_row(
    table, row_index: int
) -> dict[str, Decimal | str]:
    actual = {
        "sku": _read_item_cell(table, row_index, "Item No."),
        "name": _read_item_cell(table, row_index, "Name"),
        "quantity": _display_decimal(
            _read_item_cell(table, row_index, "Qty.")
        ),
        "unit_net": _display_decimal(
            _read_item_cell(table, row_index, "U.Price")
        ),
        "vat": _display_decimal(_read_item_cell(table, row_index, "VAT")),
        "discount": _display_decimal(
            _read_item_cell(table, row_index, "Discount")
        ),
    }
    actual["price"] = round_money(
        actual["quantity"]
        * actual["unit_net"]
        * (Decimal("1") + actual["discount"] / Decimal("100"))
    )
    return actual


def _rendered_currency_glyph(
    image: Image.Image, rectangle: tuple[int, int, int, int]
) -> tuple[tuple[bool, ...], ...]:
    left, top, right, bottom = rectangle
    crop = image.convert("RGB").crop((left + 1, top + 1, right, bottom))
    colors = crop.getcolors(maxcolors=crop.width * crop.height)
    if not colors:
        raise RuntimeError("could not derive item-cell background color")
    background = max(colors, key=lambda entry: entry[0])[1]
    mask = [
        [
            max(
                abs(channel - background_channel)
                for channel, background_channel in zip(
                    crop.getpixel((x, y)), background
                )
            )
            > 80
            for x in range(crop.width)
        ]
        for y in range(crop.height)
    ]
    ink_columns = [
        x
        for x in range(crop.width)
        if sum(row[x] for row in mask) >= 2
    ]
    runs = _edge_runs(ink_columns)
    if not runs:
        raise RuntimeError("rendered money cell contains no readable glyphs")
    glyph_left, glyph_right = runs[0]
    ink_rows = [
        y
        for y, row in enumerate(mask)
        if any(row[glyph_left : glyph_right + 1])
    ]
    if not ink_rows:
        raise RuntimeError("rendered currency glyph contains no ink")
    glyph_top, glyph_bottom = ink_rows[0], ink_rows[-1]
    return tuple(
        tuple(row[glyph_left : glyph_right + 1])
        for row in mask[glyph_top : glyph_bottom + 1]
    )


def _verify_rendered_price_currency(
    table, row_index: int, currency: str
) -> None:
    if currency != "USD":
        raise ValueError(f"unsupported document currency: {currency!r}; expected 'USD'")
    image_path = Path(tempfile.gettempdir()) / "fakturama-current-item-grid.png"
    image = render_control(table, image_path)
    columns, rows = detect_item_grid(image)
    if not 0 <= row_index < len(rows):
        raise RuntimeError(
            f"item row {row_index} is outside detected rows {rows}"
        )
    top, bottom = rows[row_index]
    unit_left, unit_right = columns[_ITEM_COLUMNS["U.Price"]]
    price_left, price_right = columns[_ITEM_COLUMNS["Price"]]
    unit_glyph = _rendered_currency_glyph(
        image, (unit_left, top, unit_right, bottom)
    )
    price_glyph = _rendered_currency_glyph(
        image, (price_left, top, price_right, bottom)
    )
    if price_glyph != unit_glyph:
        raise RuntimeError(
            f"row {row_index + 1} calculated Price currency marker mismatch"
        )


def _expected_item_row(item: LineItem) -> dict[str, Decimal | str]:
    return {
        "sku": item.sku,
        "name": item.description,
        "quantity": item.quantity,
        "unit_net": item.unit_net,
        "vat": item.vat_percent,
        "discount": -item.discount_percent,
        "price": item.source_line_net,
    }


def verify_item_row(
    table,
    row_index: int,
    item: LineItem,
    currency: str,
) -> None:
    actual = _read_item_row(table, row_index)
    expected = _expected_item_row(item)
    if actual != expected:
        raise RuntimeError(
            f"item row {row_index + 1} mismatch: expected={expected}, actual={actual}"
        )
    _verify_rendered_price_currency(table, row_index, currency)


def verify_document_charges(editor, order: OrderData) -> None:
    discount = _unique(
        [
            edit
            for edit in editor.descendants(control_type="Edit")
            if edit.window_text() == "Discount" and edit.is_visible()
        ],
        "overall Discount Edit",
    )
    shipping = _unique(
        [
            edit
            for edit in editor.descendants(control_type="Edit")
            if edit.window_text() == "Shipping" and edit.is_visible()
        ],
        "Shipping selection Edit",
    )
    shipping_rectangle = shipping.rectangle()
    shipping_value = _unique(
        [
            edit
            for edit in editor.descendants(control_type="Edit")
            if edit.is_visible()
            and edit.rectangle().left >= shipping_rectangle.right
            and edit.rectangle().top < shipping_rectangle.bottom
            and edit.rectangle().bottom > shipping_rectangle.top
        ],
        "Shipping value Edit",
    )
    actual = {
        "discount": _display_decimal(discount.get_value()),
        "shipping": shipping.get_value(),
        "shipping_value": _money_from_ui(
            shipping_value.get_value(), order.currency
        ),
    }
    expected = {
        "discount": Decimal("0"),
        "shipping": "Free of shipping costs",
        "shipping_value": Decimal("0"),
    }
    if actual != expected:
        raise RuntimeError(
            f"document charges mismatch: expected={expected}, actual={actual}"
        )


def verify_copied_invoice(invoice_editor, order: OrderData) -> str:
    number = _edit_immediately_right_of_label(invoice_editor, "No.").get_value()
    if not re.fullmatch(r"INV\d+", number):
        raise RuntimeError(f"unexpected proposed Invoice number: {number!r}")
    reference = _unique(
        [
            edit
            for edit in invoice_editor.descendants(control_type="Edit")
            if edit.window_text() == "Cust.Ref."
        ],
        "Invoice Cust.Ref. Edit",
    ).get_value()
    if reference != order.external_reference:
        raise RuntimeError(
            f"Invoice reference mismatch: expected={order.external_reference!r}, actual={reference!r}"
        )
    order_date = _edit_immediately_right_of_label(invoice_editor, "Order Date").get_value()
    if order_date != _english_date(order.order_date):
        raise RuntimeError(
            f"Invoice Order Date mismatch: expected={_english_date(order.order_date)!r}, actual={order_date!r}"
        )
    document_mode = _combo_immediately_right_of_label(invoice_editor, "Date").selected_text()
    vat_mode = _combo_immediately_right_of_label(invoice_editor, "VAT").selected_text()
    if document_mode != "Net" or vat_mode != "With VAT":
        raise RuntimeError(
            f"Invoice modes mismatch: document={document_mode!r}, VAT={vat_mode!r}"
        )
    verify_order_addresses(invoice_editor, order)
    table = find_items_table(invoice_editor)
    for row_index, item in enumerate(order.items):
        verify_item_row(table, row_index, item, order.currency)
    verify_document_charges(invoice_editor, order)
    verify_order_totals(invoice_editor, order)
    return number


def _set_date_edit(
    date_edit,
    value,
    trace: DateEntryTrace | None = None,
    description: str = "date",
) -> str:
    expected = _english_date(value)
    initial_value = date_edit.get_value()
    if trace is not None:
        trace.record(
            "date_control_discovered",
            description=description,
            expected_value=expected,
            **_date_control_facts(date_edit),
        )
    _capture_date_control(trace, date_edit, "initial")
    if initial_value != expected:
        _paste_full_date(date_edit, expected, trace)
    try:
        wait_for(
            lambda: date_edit.get_value() == expected,
            3,
            f"committed {description} {expected}",
        )
    finally:
        if trace is not None:
            trace.record(
                "date_final_value",
                description=description,
                expected_value=expected,
                focused=_control_focus(date_edit),
                value=_diagnostic_read(date_edit.get_value),
            )
    return expected


def apply_invoice_payment(
    invoice_editor,
    payment: PaymentData,
    total: Decimal,
    currency: str,
    trace: DateEntryTrace | None = None,
) -> None:
    paid = _unique(
        [
            candidate
            for candidate in invoice_editor.descendants(control_type="CheckBox")
            if candidate.window_text() == "paid" and candidate.is_visible()
        ],
        "paid CheckBox",
    )
    paid_rectangle = paid.rectangle()
    payment_combo = _unique(
        [
            combo
            for combo in invoice_editor.descendants(control_type="ComboBox")
            if combo.is_visible()
            and combo.rectangle().left >= paid_rectangle.right
            and combo.rectangle().top < paid_rectangle.bottom
            and combo.rectangle().bottom > paid_rectangle.top
        ],
        "payment ComboBox immediately right of paid",
    )
    if payment_combo.selected_text() != payment.method:
        payment_combo.expand()

        def exact_method():
            matches = [
                item
                for item in payment_combo.descendants(control_type="ListItem")
                if item.window_text() == payment.method and item.is_visible()
            ]
            return matches[0] if len(matches) == 1 else None

        wait_for(exact_method, 3, f"exact payment method {payment.method}").select()
    wait_for(
        lambda: payment_combo.selected_text() == payment.method,
        3,
        f"payment method {payment.method}",
    )

    expected_paid = 1 if payment.status == "PAID" else 0
    if paid.get_toggle_state() != expected_paid:
        paid.toggle()
    wait_for(
        lambda: next(
            (
                candidate
                for candidate in invoice_editor.descendants(control_type="CheckBox")
                if candidate.window_text() == "paid"
                and candidate.is_visible()
                and candidate.get_toggle_state() == expected_paid
            ),
            None,
        ),
        5,
        f"paid state {expected_paid}",
    )
    if payment.status != "PAID":
        return

    assert payment.payment_date is not None
    date_edit = _edit_immediately_right_of_label(invoice_editor, "at")
    value_edit = _edit_immediately_right_of_label(invoice_editor, "Value")
    _set_date_edit(date_edit, payment.payment_date, trace, "payment date")
    if _money_or_decimal_from_ui(value_edit.get_value(), currency) != total:
        value_edit.click_input()
        value_edit.type_keys(f"^a{total:.2f}{{ENTER}}")
    wait_for(
        lambda: _money_or_decimal_from_ui(value_edit.get_value(), currency) == total,
        5,
        f"paid value {total:.2f}",
    )
    actual_paid = _unique(
        [
            candidate
            for candidate in invoice_editor.descendants(control_type="CheckBox")
            if candidate.window_text() == "paid" and candidate.is_visible()
        ],
        "paid CheckBox after payment entry",
    )
    if (
        payment_combo.selected_text() != payment.method
        or actual_paid.get_toggle_state() != 1
        or date_edit.get_value() != _english_date(payment.payment_date)
        or _money_or_decimal_from_ui(value_edit.get_value(), currency) != total
    ):
        raise RuntimeError("Invoice payment readback mismatch")


def open_documents_view(main_window):
    panes = [
        pane
        for pane in main_window.descendants(control_type="Pane")
        if pane.window_text() == "Documents" and pane.is_visible()
    ]
    if len(panes) == 1:
        return panes[0]
    main_window.menu_select("Data->Documents")
    opened = wait_for(
        lambda: next(
            (
                pane
                for pane in main_window.descendants(control_type="Pane")
                if pane.window_text() == "Documents" and pane.is_visible()
            ),
            None,
        ),
        5,
        "Documents Pane from Data menu",
    )
    if opened is not None:
        return opened
    tab = _unique(
        [
            item
            for item in main_window.descendants(control_type="TabItem")
            if item.window_text() == "Documents" and item.is_visible()
        ],
        "Documents TabItem",
    )
    tab.select()
    return wait_for(
        lambda: next(
            (
                pane
                for pane in main_window.descendants(control_type="Pane")
                if pane.window_text() == "Documents" and pane.is_visible()
            ),
            None,
        ),
        5,
        "visible Documents Pane",
    )


def _documents_search_and_grid(documents):
    search_label = _unique(
        [
            text
            for text in documents.descendants(control_type="Text")
            if text.window_text() == "Search:" and text.is_visible()
        ],
        "Documents Search label",
    )
    label_rectangle = search_label.rectangle()
    search = _unique(
        [
            edit
            for edit in documents.descendants(control_type="Edit")
            if edit.is_visible()
            and edit.rectangle().left >= label_rectangle.right
            and edit.rectangle().top < label_rectangle.bottom
            and edit.rectangle().bottom > label_rectangle.top
        ],
        "Documents Search Edit",
    )
    documents_rectangle = documents.rectangle()
    grid = _unique(
        [
            pane
            for pane in documents.descendants(control_type="Pane")
            if not pane.window_text()
            and pane.is_visible()
            and pane.rectangle().width() > documents_rectangle.width() * 0.50
            and pane.rectangle().top > search.rectangle().bottom
        ],
        "Documents owner-drawn grid Pane",
    )
    return search, grid


def _documents_result_rows(image: Image.Image) -> list[tuple[int, int]]:
    candidate_rows = _selector_result_rows(image)
    pixels = image.convert("RGB")
    width, _ = pixels.size
    return [
        (top, bottom)
        for top, bottom in candidate_rows
        if sum(
            1
            for y in range(top, bottom + 1)
            for x in range(width)
            if max(pixels.getpixel((x, y))) < 100
            or max(pixels.getpixel((x, y)))
            - min(pixels.getpixel((x, y)))
            > 30
        )
        >= 3
    ]


def _filter_documents(
    main_window, document_type: str, query: str, output_path: Path
) -> tuple[object, Image.Image, list[tuple[int, int]]]:
    documents = open_documents_view(main_window)
    category_name = f"{document_type}s"
    categories = [
        item
        for item in documents.descendants(control_type="TreeItem")
        if item.window_text() == category_name and item.is_visible()
    ]
    if len(categories) != 1:
        raise RuntimeError(
            f"Documents {category_name} category uniqueness failure: "
            f"found {len(categories)}"
        )
    current_headings = [
        text
        for text in documents.descendants(control_type="Text")
        if text.window_text() == category_name and text.is_visible()
    ]
    if len(current_headings) != 1:
        categories[0].click_input()
        wait_for(
            lambda: next(
                (
                    text
                    for text in documents.descendants(control_type="Text")
                    if text.window_text() == category_name and text.is_visible()
                ),
                None,
            ),
            5,
            f"Documents {category_name} heading",
        )
        if not _stable_descendants(documents):
            raise TimeoutError(
                f"Documents {category_name} category did not stabilize"
            )
        time.sleep(1)
    search, grid = _documents_search_and_grid(documents)
    search.click_input()
    try:
        send_swt_text(search, query)
    except TimeoutError:
        refreshed_main = Desktop(backend="uia").window(handle=main_window.handle)
        documents = open_documents_view(refreshed_main)
        search, grid = _documents_search_and_grid(documents)
        time.sleep(1)
        search.click_input()
        send_swt_text(search, query)
    if not _stable_descendants(documents):
        raise TimeoutError("Documents results did not stabilize")
    image = render_control(grid, output_path)
    return grid, image, _documents_result_rows(image)


def _saved_document_editor(main_window, number: str):
    panes = [
        pane
        for pane in main_window.descendants(control_type="Pane")
        if pane.window_text() == number and pane.is_visible()
    ]
    tabs = [
        tab
        for tab in main_window.descendants(control_type="TabItem")
        if tab.window_text() == number
    ]
    return panes[0] if len(panes) == 1 and len(tabs) == 1 else None


def _rebind_saved_document(
    main_window,
    document_type: str,
    number: str,
    config: FakturamaConfig,
):
    current = _saved_document_editor(main_window, number)
    if current is not None:
        return current
    grid, image, rows = _filter_documents(
        main_window,
        document_type,
        number,
        config.diagnostics_dir / f"documents-{number}.png",
    )
    if len(rows) != 1:
        raise RuntimeError(
            f"expected one rendered Documents row for {number}, found {len(rows)}"
        )
    row_top, row_bottom = rows[0]
    point = (round(image.width * 0.15), (row_top + row_bottom) // 2)
    Desktop(backend="win32").window(handle=grid.handle).double_click(
        coords=point
    )
    return wait_for(
        lambda: _saved_document_editor(main_window, number),
        15,
        f"saved document editor {number}",
    )


def verify_order_in_documents(
    main_window,
    order: OrderData,
    order_number: str,
    config: FakturamaConfig,
) -> None:
    verify_saved_order(config.workspace, order, order_number)
    _, _, rows = _filter_documents(
        main_window,
        "Order",
        order_number,
        config.diagnostics_dir / "documents-order.png",
    )
    if len(rows) != 1:
        raise RuntimeError(
            f"expected one rendered Documents Order row, found {len(rows)}"
        )
    _rebind_saved_document(main_window, "Order", order_number, config)


def verify_final_documents(
    main_window,
    order: OrderData,
    order_number: str,
    invoice_number: str,
    config: FakturamaConfig,
) -> None:
    verify_saved_invoice(
        config.workspace, order, order_number, invoice_number
    )
    for document_type, number in (
        ("Order", order_number),
        ("Invoice", invoice_number),
    ):
        _, _, rows = _filter_documents(
            main_window,
            document_type,
            number,
            config.diagnostics_dir
            / f"documents-final-{document_type.lower()}.png",
        )
        if len(rows) != 1:
            raise RuntimeError(
                f"expected one rendered Documents {document_type} row, "
                f"found {len(rows)}"
            )


def _save_document_once(
    main_window,
    editor,
    proposed_number: str,
    document_type: str,
    external_reference: str,
    workspace: Path,
) -> str:
    save = _unique(
        [
            button
            for button in main_window.descendants(control_type="Button")
            if button.window_text() == "Save the current contents"
        ],
        "Save the current contents button",
    )
    if not save.is_enabled():
        raise RuntimeError(
            f"{document_type} Save button is disabled before the sole Save"
        )
    save.invoke()

    def fail_on_please_verify() -> None:
        verification_dialogs = [
            window
            for window in main_window.descendants(control_type="Window")
            if window.window_text() == "Please verify" and window.is_visible()
        ]
        if verification_dialogs:
            raise RuntimeError(
                "unexpected Please verify dialog: identical billing and delivery "
                "addresses should be inherited from the saved Order"
            )

    fail_on_please_verify()

    def saved_editor():
        fail_on_please_verify()
        current_save = [
            button
            for button in main_window.descendants(control_type="Button")
            if button.window_text() == "Save the current contents"
        ]
        editor_match = _saved_document_editor(main_window, proposed_number)
        return (
            editor_match
            if len(current_save) == 1
            and not current_save[0].is_enabled()
            and editor_match is not None
            else None
        )

    try:
        wait_for(
            saved_editor,
            20,
            f"disabled Save and saved {document_type} {proposed_number}",
        )
    except TimeoutError as error:
        persistence = read_persistence(workspace)
        versions = [
            row
            for row in _persistence_rows(persistence, "FKT_DOCUMENT")
            if len(row) > 17
            and row[1] == document_type
            and row[5] == external_reference
            and row[7] == "FALSE"
            and row[17] == proposed_number
        ]
        durable_ids = {row[0] for row in versions}
        recovery_config = FakturamaConfig(
            Path(), workspace, Path(tempfile.gettempdir())
        )
        try:
            _, _, rows = _filter_documents(
                main_window,
                document_type,
                proposed_number,
                Path(tempfile.gettempdir())
                / "fakturama-save-ambiguity-documents.png",
            )
            rebound = _rebind_saved_document(
                main_window, document_type, proposed_number, recovery_config
            )
        except Exception:
            rows = []
            rebound = None
        if len(durable_ids) == 1 and len(rows) == 1 and rebound is not None:
            return proposed_number
        raise RuntimeError(
            f"{document_type} Save is ambiguous; Save was not retried: "
            f"persistence IDs={sorted(durable_ids)}, Documents rows={len(rows)}, "
            f"editor rebound={rebound is not None}; {error}"
        ) from error
    return proposed_number


def save_order(
    main_window,
    editor,
    proposed_number: str,
    external_reference: str,
    workspace: Path,
) -> str:
    return _save_document_once(
        main_window,
        editor,
        proposed_number,
        "Order",
        external_reference,
        workspace,
    )


def save_invoice(
    main_window,
    invoice_editor,
    proposed_number: str,
    external_reference: str,
    workspace: Path,
) -> str:
    return _save_document_once(
        main_window,
        invoice_editor,
        proposed_number,
        "Invoice",
        external_reference,
        workspace,
    )


def _persistence_rows(persistence: str, table: str) -> list[list[str]]:
    rows = []
    for line in persistence.splitlines():
        if not re.match(
            rf'^INSERT INTO (?:"{re.escape(table)}"|{re.escape(table)}) VALUES\(',
            line,
        ):
            continue
        raw = line.split("VALUES(", 1)[1]
        if not raw.endswith(")"):
            continue
        rows.append(
            next(csv.reader([raw[:-1]], quotechar="'", skipinitialspace=True))
        )
    return rows


def _verify_document_receivers(
    persistence: str,
    document_id: str,
    order: OrderData,
) -> None:
    address = _shared_debtor_address(order)
    country_codes = {"United States": "US"}
    rows = [
        row
        for row in _persistence_rows(persistence, "FKT_DOCUMENTRECEIVER")
        if len(row) > 33 and row[9] == "FALSE" and row[33] == document_id
    ]

    matches = [
        row
        for row in rows
        if (
            (
                row[34]
                if len(row) > 34 and row[34] not in {"", "NULL"}
                else row[4].removesuffix(r"\u0009")
            )
            == address.name
            and row[2] == address.city
            and row[6] == country_codes.get(address.country)
            and row[25] == address.street
            and row[32] == address.postal_code
        )
    ]

    checks = {
        "billing_and_delivery_receiver_count": len(rows) == 2,
        "billing_and_delivery_exact_address": len(matches) == 2,
        "distinct_receiver_ids": len({row[0] for row in matches}) == 2,
    }
    if not all(checks.values()):
        raise RuntimeError(
            f"document {document_id} receiver persistence mismatch: {checks}"
        )


def verify_saved_order(
    workspace: Path, order: OrderData, order_number: str
) -> None:
    persistence = read_persistence(workspace)
    versions = [
        row
        for row in _persistence_rows(persistence, "FKT_DOCUMENT")
        if len(row) > 48
        and row[1] == "Order"
        and row[5] == order.external_reference
        and row[17] == order_number
    ]
    document_ids = {row[0] for row in versions}
    if len(document_ids) != 1:
        raise RuntimeError(
            f"saved Order primary-key uniqueness failure: IDs={sorted(document_ids)}"
        )
    latest = versions[-1]
    checks = {
        "type": latest[1] == "Order",
        "reference": latest[5] == order.external_reference,
        "not_deleted": latest[7] == "FALSE",
        "date": latest[9] == order.order_date.isoformat(),
        "number": latest[17] == order_number,
        "net_mode": latest[18] == "1",
        "order_date": latest[20] == order.order_date.isoformat(),
        "open": latest[27] == "0",
        "total": Decimal(latest[31]) == order.totals.gross,
    }
    if not all(checks.values()):
        raise RuntimeError(f"saved Order persistence mismatch: {checks}")

    document_id = next(iter(document_ids))
    _verify_document_receivers(persistence, document_id, order)
    item_rows = [
        row
        for row in _persistence_rows(persistence, "FKT_DOCUMENTITEM")
        if len(row) > 25 and row[25] == document_id and row[2] == "FALSE"
    ]
    if len(item_rows) != len(order.items):
        raise RuntimeError(
            f"saved Order item count mismatch: expected={len(order.items)}, actual={len(item_rows)}"
        )
    item_rows.sort(key=lambda row: int(row[14]))
    for expected_position, (row, item) in enumerate(zip(item_rows, order.items), 1):
        expected_rebate = -(item.discount_percent / Decimal("100"))
        item_checks = {
            "position": row[14] == str(expected_position),
            "sku": row[5] == item.sku,
            "name": row[10] == item.description,
            "rebate": Decimal(row[6]) == expected_rebate,
            "unit_net": Decimal(row[15]) == item.unit_net,
            "quantity": Decimal(row[16]) == item.quantity,
            "vat_identity": row[23] == "2",
        }
        if not all(item_checks.values()):
            raise RuntimeError(
                f"saved Order item {expected_position} persistence mismatch: {item_checks}"
            )


def verify_saved_invoice(
    workspace: Path,
    order: OrderData,
    order_number: str,
    invoice_number: str,
) -> None:
    persistence = read_persistence(workspace)
    document_rows = _persistence_rows(persistence, "FKT_DOCUMENT")
    order_versions = [
        row
        for row in document_rows
        if len(row) > 48
        and row[1] == "Order"
        and row[5] == order.external_reference
        and row[17] == order_number
    ]
    invoice_versions = [
        row
        for row in document_rows
        if len(row) > 48
        and row[1] == "Invoice"
        and row[5] == order.external_reference
        and row[17] == invoice_number
    ]
    order_ids = {row[0] for row in order_versions}
    invoice_ids = {row[0] for row in invoice_versions}
    if len(order_ids) != 1 or len(invoice_ids) != 1:
        raise RuntimeError(
            "saved document primary-key uniqueness failure: "
            f"Order IDs={sorted(order_ids)}, Invoice IDs={sorted(invoice_ids)}"
        )
    order_id = next(iter(order_ids))
    invoice_id = next(iter(invoice_ids))
    latest_order = order_versions[-1]
    latest_invoice = invoice_versions[-1]
    payment_ids = {
        row[0]
        for row in _persistence_rows(persistence, "FKT_PAYMENT")
        if len(row) > 10 and row[3] == "FALSE" and row[10] == order.payment.method
    }
    checks = {
        "order_type": latest_order[1] == "Order",
        "order_reference": latest_order[5] == order.external_reference,
        "order_not_deleted": latest_order[7] == "FALSE",
        "order_number": latest_order[17] == order_number,
        "order_open": latest_order[27] == "0",
        "order_total": Decimal(latest_order[31]) == order.totals.gross,
        "invoice_type": latest_invoice[1] == "Invoice",
        "invoice_reference": latest_invoice[5] == order.external_reference,
        "invoice_not_deleted": latest_invoice[7] == "FALSE",
        "invoice_not_deposit": latest_invoice[8] in {"FALSE", ""},
        "invoice_number": latest_invoice[17] == invoice_number,
        "invoice_paid": latest_invoice[21] == ("TRUE" if order.payment.status == "PAID" else "FALSE"),
        "invoice_paid_value": Decimal(latest_invoice[22]) == (
            order.totals.gross if order.payment.status == "PAID" else Decimal("0")
        ),
        "invoice_pay_date": latest_invoice[23] == (
            order.payment.payment_date.isoformat()
            if order.payment.payment_date is not None
            else ""
        ),
        "invoice_total": Decimal(latest_invoice[31]) == order.totals.gross,
        "payment_identity": len(payment_ids) == 1 and latest_invoice[44] in payment_ids,
        "order_invoice_reference": latest_order[42] == invoice_id,
        "invoice_source_document": latest_invoice[46] == order_id,
    }
    forbidden = {
        row[1]
        for row in document_rows
        if len(row) > 7
        and row[5] == order.external_reference
        and row[7] == "FALSE"
        and row[1] not in {"Order", "Invoice"}
    }
    checks["no_forbidden_followup"] = not forbidden
    if not all(checks.values()):
        raise RuntimeError(
            f"saved Invoice persistence mismatch: checks={checks}, forbidden={sorted(forbidden)}"
        )
    _verify_document_receivers(persistence, order_id, order)
    _verify_document_receivers(persistence, invoice_id, order)


def automate_order(order: OrderData, config: FakturamaConfig) -> AutomationResult:
    global _ACTIVE_GRID_CELL_TRACE

    _shared_debtor_address(order)
    detailed_trace_enabled = _detailed_trace_enabled()
    trace = (
        DateEntryTrace(config.diagnostics_dir)
        if detailed_trace_enabled
        else None
    )
    previous_grid_trace = _ACTIVE_GRID_CELL_TRACE
    if detailed_trace_enabled:
        try:
            _ACTIVE_GRID_CELL_TRACE = GridCellTrace(config.diagnostics_dir)
        except Exception:
            _ACTIVE_GRID_CELL_TRACE = None
    else:
        _ACTIVE_GRID_CELL_TRACE = None
    main_window = None
    try:
        main_window = connect_or_launch(config, trace)
        editor = open_new_order(main_window, trace)
        verify_document_currency(editor, order.currency)
        proposed_number = set_order_header(editor, order, trace)
        select_existing_debtor(editor, order, config)
        verify_order_addresses(editor, order)
        for row_index, item in enumerate(order.items):
            expected_total_net = calculate_order_totals(
                order.items[: row_index + 1]
            ).net
            populate_item(
                editor,
                row_index,
                item,
                order.currency,
                config,
                expected_total_net,
            )
        verify_document_charges(editor, order)
        verify_order_totals(editor, order)
        order_number = save_order(
            main_window,
            editor,
            proposed_number,
            order.external_reference,
            config.workspace,
        )
        verify_order_in_documents(
            main_window, order, order_number, config
        )
        saved_order = _rebind_saved_document(
            main_window, "Order", order_number, config
        )
        invoice_editor = create_followup_invoice(saved_order, order)
        proposed_invoice_number = verify_copied_invoice(
            invoice_editor, order
        )
        apply_invoice_payment(
            invoice_editor,
            order.payment,
            order.totals.gross,
            order.currency,
            trace,
        )
        invoice_number = save_invoice(
            main_window,
            invoice_editor,
            proposed_invoice_number,
            order.external_reference,
            config.workspace,
        )
        verify_final_documents(
            main_window,
            order,
            order_number,
            invoice_number,
            config,
        )
        return AutomationResult(order_number, invoice_number)
    except Exception:
        if main_window is not None:
            try:
                capture_diagnostics(
                    main_window, "automation-failure", config
                )
            except Exception:
                pass
        raise
    finally:
        _ACTIVE_GRID_CELL_TRACE = previous_grid_trace
