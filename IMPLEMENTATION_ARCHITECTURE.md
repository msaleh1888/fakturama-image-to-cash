# Fakturama Image-to-Cash Architecture

## 1. System Purpose

The system is a synchronous Windows command-line application that converts one order image into:

1. a validated structured order;
2. a saved Fakturama Order; and
3. a saved Invoice created as a follow-up document from that Order.

The Invoice is updated with the payment state extracted from the image. The system verifies the important values before each save and verifies the persisted Order, Invoice, and their relationship at the end.

The accepted submission scope is the US/USD, same-address happy scenario on Fakturama 2.2.0. A prepared workspace fixture is stored in the repository and copied to a unique runtime directory for every `automate` and `run` invocation. The required Debtor, payment method, VAT rate, and Products already exist in that fixture. The system selects them exactly; a missing or ambiguous master record is unsupported and stops the run. All five supplied image fixtures have completed the full extraction-to-persistence path successfully.

## 2. System Context

```text
                         +-------------------+
Order image ------------>| Extraction        |
                         | OpenAI Responses  |
                         +---------+---------+
                                   |
                                   v
                         +-------------------+
Reviewed order JSON ---->| OrderData         |
                         | validation        |
                         +---------+---------+
                                   |
                                   v
                         +-------------------+
                         | Fakturama adapter |
                         | UIA + narrow      |
                         | Win32 fallbacks   |
                         +---------+---------+
                                   |
                                   v
                         +-------------------+
                         | Fakturama 2.2.0   |
                         | isolated runtime  |
                         +---------+---------+
                                   |
                                   v
                         +-------------------+
                         | Verification      |
                         | UI + read-only DB |
                         +-------------------+
```

`OrderData` is the only data contract shared between extraction and Fakturama automation. There are no intermediate DTOs, service layers, queues, application database, or background workers.

## 3. Runtime Components

The application consists of four top-level Python modules.

### `main.py` - command-line entry point

`main.py` owns argument parsing and straight-line orchestration. It supports:

```powershell
python main.py extract IMAGE --output JSON
python main.py automate JSON [--fakturama-executable PATH]
python main.py run IMAGE --output JSON [--fakturama-executable PATH]
```

- `extract` converts an image to validated JSON.
- `automate` loads validated JSON, creates an isolated runtime copy of the repository workspace fixture, and performs the Fakturama workflow. The executable argument defaults to `C:\Program Files\Fakturama2\Fakturama.exe`.
- `run` extracts an image, writes the reviewed JSON representation, creates the same kind of isolated workspace copy as `automate`, and passes the same validated object to Fakturama. It accepts the same executable override.

The module contains no image interpretation or desktop-control logic.

### `models.py` - domain contract and validation

`models.py` defines the Pydantic models for:

- order identity and date;
- Debtor identity, contact details, billing address, and delivery address;
- payment method, paid state, and payment date;
- ordered items, quantities, net unit prices, VAT, discounts, and source line totals; and
- source net, VAT, and gross totals.

All monetary values use `Decimal`. Validation recalculates every line and the document totals with two-decimal `ROUND_HALF_UP` rounding. Invalid, incomplete, or arithmetically inconsistent data never reaches Fakturama.

### `extract.py` - image extraction adapter

`extract.py`:

1. validates the image type;
2. encodes the image as a data URL;
3. sends one structured extraction request to the OpenAI Responses API;
4. parses the response directly into `OrderData`; and
5. returns the validated model and request usage.

The OpenAI client is constructed by `main.py` after environment loading and is passed into the adapter. The prompt requests only values visible in the source. Missing or unreadable fields remain null and are rejected by happy-path validation instead of being invented. The extraction adapter has no knowledge of Fakturama controls.

### `fakturama.py` - Fakturama application adapter

`fakturama.py` owns all Fakturama-specific behavior:

- connecting to or launching the configured Fakturama workspace;
- locating and operating UI controls;
- selecting exact existing master records;
- creating and completing the Order;
- creating the linked Invoice from the saved Order;
- applying payment state; and
- verifying saved data through UI readback and read-only persistence inspection.

The workflow remains a direct sequence of functions in this module. A generalized desktop-automation framework or workflow engine is unnecessary for one application and one scenario.

## 4. Component Boundaries

Only two functions are called across adapter boundaries:

```python
# extract.py
def extract_order(
    client: OpenAI,
    image_path: Path,
) -> tuple[OrderData, object | None]: ...

# fakturama.py
def automate_order(
    order: OrderData,
    config: FakturamaConfig,
) -> AutomationResult: ...
```

`main.py` constructs the OpenAI client and `FakturamaConfig`, validates CLI paths, copies the prepared workspace for both automation commands, reads or writes JSON, calls these functions, and formats terminal output. It does not call individual Fakturama screen functions.

The JSON representation uses the same field names as `OrderData`, ISO dates, and decimal strings. Consequently, `extract`, `automate`, and `run` all cross the same validation boundary.

### Extraction functions

`extract.py` contains three direct functions:

```python
def encode_image(image_path: Path) -> str: ...

def extract_order(
    client: OpenAI,
    image_path: Path,
) -> tuple[OrderData, object | None]: ...

def format_usage(usage: object | None) -> str: ...
```

`encode_image` returns a MIME-qualified data URL. `extract_order` makes the single API request and returns the validated model plus optional SDK usage. `format_usage` converts that optional usage object to concise CLI text. No extraction class or provider interface is introduced.

### Fakturama data objects

`fakturama.py` exposes two small runtime result/configuration data objects:

```python
@dataclass
class FakturamaConfig:
    executable: Path
    workspace: Path
    diagnostics_dir: Path

@dataclass
class AutomationResult:
    order_number: str
    invoice_number: str
```

`FakturamaConfig` contains environment-specific paths, never order data. `AutomationResult` contains only the two verified generated document numbers required by the CLI.

### Fakturama runtime functions

These functions isolate behavior shared by multiple screens:

```python
def wait_for(predicate, timeout: float, description: str): ...
def connect_or_launch(config: FakturamaConfig): ...
def send_swt_text(edit, text: str) -> None: ...
def render_control(control, output_path: Path) -> Image.Image: ...
def read_persistence(workspace: Path) -> str: ...
def capture_diagnostics(main_window, name: str, config: FakturamaConfig) -> None: ...
```

- `wait_for` polls an observable predicate and reports its last value on timeout.
- `connect_or_launch` returns the one exact configured Fakturama main window.
- `send_swt_text` writes to an already-located SWT Edit while firing its listeners.
- `render_control` captures the current pixels of an already-located control.
- `read_persistence` reads `Database.script` and `Database.log` without modifying them.
- `capture_diagnostics` writes a screenshot and UIA tree after a failed operation.

These are simple module functions, not a reusable desktop-automation layer.

### Order functions

The Order portion of `automate_order` is composed from:

```python
def open_new_order(main_window): ...
def verify_document_currency(editor, currency: str) -> None: ...
def set_order_header(editor, order: OrderData) -> str: ...
def open_selector(editor, label: str, dialog_title: str): ...
def select_existing_debtor(editor, order: OrderData, config: FakturamaConfig) -> None: ...
def verify_order_addresses(editor, order: OrderData) -> None: ...

def select_existing_product(editor, item: LineItem, currency: str, config: FakturamaConfig) -> None: ...
def find_items_table(editor): ...
def detect_item_grid(
    image: Image.Image,
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]: ...
def edit_item_cell(table, row_index: int, column_name: str, value: str, currency: str) -> None: ...
def verify_item_row(table, row_index: int, item: LineItem, currency: str) -> None: ...
def populate_item(editor, row_index: int, item: LineItem, currency: str, config: FakturamaConfig) -> None: ...
def verify_document_charges(editor, order: OrderData) -> None: ...
def verify_order_totals(editor, order: OrderData) -> None: ...

def save_order(main_window, editor, proposed_number: str, external_reference: str, workspace: Path) -> str: ...
def verify_order_in_documents(main_window, order: OrderData, order_number: str, config: FakturamaConfig) -> None: ...
def verify_saved_order(workspace: Path, order: OrderData, order_number: str) -> None: ...
```

The selection functions own exact-match proof and selector interaction. The grid functions own current-render geometry and in-place cell commits. The verification functions compare UI or read-only persisted state with `OrderData`. `save_order` is the only Order function allowed to invoke Save.

### Invoice functions

The Invoice portion is composed from:

```python
def create_followup_invoice(saved_order, order: OrderData): ...
def verify_copied_invoice(invoice_editor, order: OrderData) -> str: ...
def apply_invoice_payment(
    invoice_editor,
    payment: PaymentData,
    total: Decimal,
) -> None: ...
def save_invoice(main_window, invoice_editor, proposed_number: str, external_reference: str, workspace: Path) -> str: ...
def verify_saved_invoice(
    workspace: Path,
    order: OrderData,
    order_number: str,
    invoice_number: str,
) -> None: ...
def verify_final_documents(main_window, order: OrderData, order_number: str, invoice_number: str, config: FakturamaConfig) -> None: ...
```

`create_followup_invoice` is the only function allowed to create an Invoice and uses the saved Order's follow-up action. `apply_invoice_payment` owns payment method, paid state, date, and value. `save_invoice` is the only Invoice function allowed to invoke Save. `verify_saved_invoice` verifies payment persistence and the reciprocal Order/Invoice relationship.

### Automation composition

`automate_order` is straight-line orchestration inside `fakturama.py`:

```text
connect_or_launch
  -> open_new_order
  -> verify_document_currency
  -> set_order_header
  -> select_existing_debtor
  -> verify_order_addresses
  -> populate_item for each source item
  -> verify document charges
  -> verify_order_totals
  -> save_order
  -> verify_order_in_documents
  -> create_followup_invoice
  -> verify_copied_invoice
  -> apply_invoice_payment
  -> save_invoice
  -> verify_final_documents
  -> AutomationResult
```

The orchestration contains no retry engine or state-machine abstraction. Each called function verifies its postcondition before returning.

## 5. End-to-End Data Flow

### Image path

```text
image
  -> structured OpenAI response
  -> OrderData parsing
  -> deterministic validation
  -> reviewed JSON
  -> Fakturama automation
```

### Reviewed JSON path

```text
JSON
  -> OrderData parsing
  -> deterministic validation
  -> Fakturama automation
```

Both paths converge before any desktop action. This makes the UI workflow independent of OpenAI availability and allows the supplied fixture to exercise Fakturama directly.

## 6. Fakturama Workflow

The adapter performs one continuous Order-first transaction:

```text
Connect to the fresh runtime workspace
  -> open New Order
  -> set date, reference, Net price mode, With VAT
  -> prove the new document displays USD before financial entry
  -> select and verify exact Debtor
  -> select each exact Product in source order
  -> set quantity, unit net price, VAT, and line discount
  -> verify address components, complete rows, charges, net, VAT, and gross
  -> save Order once
  -> verify generated Order number, persisted identity, and one fresh-render Documents row
  -> create Invoice from the Order follow-up action
  -> inherit the identical billing and delivery receivers from the saved Order
  -> verify copied reference, address components, complete rows, charges, and totals
  -> set payment method and paid values
  -> save Invoice once
  -> verify both fresh-render Documents rows, open Order, paid Invoice, receivers, and reciprocal link
```

The New Order remains open while master records are selected. Fakturama's proposed Order and Invoice numbers are never overwritten. The Invoice is created only through the saved Order's follow-up action, never from the top-level New Invoice command, because that relationship must be retained.

## 7. UI Control Grounding

Fakturama 2.2.0 is an SWT desktop application. Some controls expose reliable UI Automation patterns while owner-drawn tables do not. The adapter therefore uses a fixed hierarchy of control techniques.

### Semantic UI Automation

Normal controls are located by a combination of:

- exact accessible name or window title;
- control type and SWT class;
- stable semantic parent;
- visible/enabled state; and
- uniqueness within that parent.

Examples include the main window, toolbar actions, editor fields, dialog buttons, and the Order's follow-up Invoice action. Process IDs, window handles, AutomationIds, and absolute screen coordinates are not persisted because they change between launches.

### SWT-notifying text input

For selector and Documents search fields, UIA value assignment changes the displayed text but does not trigger SWT's live-filter listener. The adapter first locates and activates the exact Edit semantically, then sends `EM_SETSEL`, `WM_CLEAR`, and `WM_CHAR` messages to that control. Date controls receive one complete formatted date paste followed by exact readback because segmented date-field selection proved unreliable. These are control-specific input mechanisms, not locators.

### Runtime-relative owner-drawn interaction

Selector rows, item-grid cells, and Documents rows are not exposed as accessible child objects. For these surfaces the adapter:

1. locates the containing window or table semantically;
2. captures its current rendering;
3. derives the relevant row or column boundaries from that rendering;
4. calculates a click point relative to the current control bounds; and
5. confirms the resulting UI state.

This preserves layout independence: positions are calculated from the current control and current render, never from fixed desktop dimensions or coordinates recorded during discovery. Item columns are identified from their current vertical grid boundaries and known semantic column order.

## 8. Exact Master-Record Resolution

The prepared repository workspace contains the happy-path Debtor, payment method, VAT rate, and Products. Northstar Office Inc. has one exact US main address carrying both Invoice and Delivery roles; there is no alternate address. The workspace stores a US-region currency locale and the automation supports only its `$` USD display. Selection uses the Order's own Debtor and Product selectors as required by the assignment.

Before selecting an owner-drawn result row, the adapter proves that exactly one current record matches the expected identity using read-only Fakturama persistence data:

- Debtor: company/contact identity and address fields;
- Product: exact SKU, description, and VAT identity;
- payment method: exact name; and
- VAT: exact percentage and expected standard-rate identity.

The filtered selector must also render one corresponding row. Zero matches, multiple matches, or conflicting values stop the run before a row is clicked. There is no fuzzy matching.

## 9. Synchronization and State Verification

Actions normally wait for observable state changes. One narrow one-second settle follows a Documents category change because Fakturama clears and reuses the same native Search handle after exposing the new heading. Typical observable conditions are:

- exactly one configured main window exists;
- the expected editor or selector appears;
- selector input and rendered results stabilize;
- a selected address or Product appears in the Order;
- an in-place item editor opens or closes;
- totals update to the expected values;
- a generated document number replaces the proposed state; and
- the saved document appears uniquely in persistence.

Polling has a bounded timeout and reports the last observed value. One top-level boundary around the complete connected transaction captures a stable screenshot and UIA tree on failure while preserving the original exception. Connection failures do not attempt UI capture.

Verification is deliberately split:

- UI readback proves the application accepted visible edits and transitions. Address text requires the exact rendered recipient, street, and ZIP/city; Fakturama normally omits the US home country from this control.
- Read-only inspection of Fakturama's HSQLDB `Database.script`, `Database.log`, and referenced CLOB receiver text proves exact persisted identities, totals, paid state, receivers, and the Order/Invoice relationship where owner-drawn UI text is inaccessible. This includes the exact `US` country on the selected Debtor and both saved document receivers.

The application never writes Fakturama database files directly.

## 10. Save and Failure Semantics

Validation and all available pre-save checks complete before a save action. The Order is saved once and the Invoice is saved once. If the post-save state is ambiguous, the shared guard inspects the editor, a fresh-render Documents view, and persistence files; it does not blindly invoke Save again. The same-address happy path must not display Fakturama's `Please verify` address-confirmation modal; its appearance is a clear failure, with no manual confirmation behavior.

Expected failures include invalid source data, no unique Fakturama window, missing or ambiguous master data, unavailable controls, unexpected totals, timeout, and failed persistence verification. They propagate to `main.py`, which prints a concise reason and exits nonzero.

The application does not retry business actions, repair source data, roll back Fakturama records, or attempt recovery from a partially completed run. These behaviors would require idempotency and recovery design beyond the chosen happy-path scope.

## 11. Configuration and Runtime Boundary

The runtime environment is:

- Windows;
- Python 3.11 or later;
- Fakturama 2.2.0 with English UI;
- a fresh runtime copy of the prepared repository workspace; and
- one visible interactive desktop session.

For `automate` and `run`, Fakturama configuration includes:

- an optional Fakturama executable argument, with the standard installation path as its default;
- the repository fixture path and generated runtime workspace path; and
- diagnostic artifact directory.

The launcher passes `--workspace <runtime-path>` to Fakturama and matches the main window against the same resolved path. Neither command opens or modifies a user's personal workspace. `FAKTURAMA_EXECUTABLE` and `FAKTURAMA_WORKSPACE` environment variables are not required; the executable uses its standard installation path by default or the explicit CLI override, and the workspace always comes from a fresh repository-fixture copy.

`OPENAI_API_KEY` is required only for `extract` and `run`. It is read from the environment and is never written to JSON, logs, screenshots, or source code. `automate` operates entirely from reviewed JSON and requires no network access.

## 12. Architectural Constraints and Tradeoffs

- The architecture favors a small synchronous process because the workflow handles one image and one interactive desktop application.
- UIA is the primary automation mechanism; narrow Win32 messaging and current-render analysis exist only where SWT does not expose usable semantics.
- Read-only persistence inspection strengthens verification but is not used to create or modify Fakturama records.
- The happy scenario assumes prepared exact master data. Automatic master creation, ambiguous-match review, resumability, rollback, batch processing, alternate Fakturama versions, and alternate UI languages are outside this solution boundary.
- The design avoids fixed screen coordinates, but it intentionally depends on Fakturama 2.2.0's semantic labels and owner-drawn table column order.
