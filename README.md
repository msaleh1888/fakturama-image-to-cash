# Fakturama Image-to-Cash Automation

This Windows CLI reads an order image, extracts it into validated structured data, creates a Fakturama Order and its linked Invoice, applies the payment state, and verifies the saved records.

```text
order image
  -> OpenAI structured extraction
  -> OrderData validation
  -> reviewed JSON
  -> Fakturama Order
  -> linked paid Invoice
  -> UI and persistence verification
```

The supported submission path targets Fakturama 2.2.0 with an English UI, USD, identical billing and delivery addresses, and the prepared master records included in this repository. All five supplied image fixtures have passed the complete `run` workflow.

## How it works

1. `extract.py` sends one image to the OpenAI Responses API and parses the structured response directly into `OrderData`.
2. `models.py` rejects missing fields, invalid dates, inconsistent payment data, incorrect line calculations, and incorrect net/VAT/gross totals.
3. `main.py` writes the validated order to reviewed JSON and creates a fresh copy of the prepared Fakturama workspace under `.runtime/fakturama`.
4. `fakturama.py` launches Fakturama against that copy, selects the exact prepared Debtor and Products, creates the Order, and creates the Invoice only through the Order's follow-up action.
5. Values are read back before Save. The saved Order, paid Invoice, receivers, totals, document state, and reciprocal link are verified through the UI and read-only persistence inspection.

The checked-in workspace is never used as the live workspace, and runtime copies are retained for diagnosis.

## Requirements

- Windows with an unlocked, visible desktop session
- Python 3.11 or newer
- Fakturama 2.2.0 with the English UI
- Fakturama installed at `C:\Program Files\Fakturama2\Fakturama.exe`, or supplied with `--fakturama-executable`
- An OpenAI API key for `extract` and `run`

Never run this automation against real accounting data.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Set the API key in the shell or a local `.env` file:

```dotenv
OPENAI_API_KEY=your_api_key_here
```

Optional local settings:

```dotenv
FAKTURAMA_DIAGNOSTICS_DIR=diagnostics
FAKTURAMA_DETAILED_TRACE=false
```

`.env`, outputs, diagnostics, and runtime workspaces are ignored by Git.

## Run the complete workflow

```powershell
python main.py run samples\synthetic_order_01.png --output output\synthetic_order_01.json
```

If Fakturama is installed elsewhere:

```powershell
python main.py run samples\synthetic_order_01.png `
  --output output\synthetic_order_01.json `
  --fakturama-executable "D:\Apps\Fakturama2\Fakturama.exe"
```

A successful run prints the reviewed JSON path, isolated workspace path, and verified document numbers:

```text
Validated order written to ...
Using isolated Fakturama workspace: ...
Verified Fakturama Order PO...
Verified linked Invoice INV...
```

## Other commands

Extract and validate without opening Fakturama:

```powershell
python main.py extract samples\synthetic_order_01.png --output output\synthetic_order_01.json
```

Run Fakturama from reviewed JSON without an OpenAI request:

```powershell
python main.py automate samples\synthetic_order_01.expected.json
```

`automate` accepts the same optional `--fakturama-executable` argument.

## Included fixtures

`samples/` contains five US/USD image and expected-JSON pairs. The prepared Fakturama workspace supplies:

- Debtor: Northstar Office Inc., 100 Market Street, New York, United States
- Billing and delivery: the same prepared address
- Payment method: Bank Transfer
- VAT: VAT 19%
- Products: `CHR-ERG-01` and `MAT-DESK-02`

Missing or ambiguous master data stops the run. The implementation does not create master records automatically.

## Validation and tests

Financial validation uses `Decimal` with two-decimal `ROUND_HALF_UP` rounding:

```text
line net = quantity * unit net * (1 - discount / 100)
VAT      = sum of discounted line values * each line's VAT rate
gross    = net + VAT
```

Run the deterministic suite:

```powershell
python -m compileall -q main.py models.py extract.py fakturama.py tests
python -m unittest discover -s tests
python -m pip check
```

These tests do not call OpenAI or control Fakturama. Real integration requires an API key and an interactive Windows desktop.

## Limitations

- One order is processed per invocation.
- Only the prepared US/USD, same-address happy path is supported.
- Required Debtor, payment method, VAT, and Products must already exist.
- Only Fakturama 2.2.0 with an English UI is targeted.
- There is no batch mode, rollback, resumability, or automatic recovery from a partially completed transaction.
- Alternate currencies, addresses, tax structures, order-level charges, and document types are outside the supported scope.

## If I had three more hours

I would focus on preventing avoidable failures and duplicate documents. Before creating anything, the script would check whether the same purchase order had already been processed. If it found an existing Order or Invoice, it would stop and clearly report what already exists instead of creating another one.

I would also check that the required customer, payment method, VAT rate, and Products exist before opening the Order screen. If anything were missing or duplicated, the script would report every problem together before making changes.

I would add automated tests for these checks and use the remaining time to run one acceptance test at a different Windows display scale. I would not attempt automatic creation of missing data or full continuation after a crash within three hours, because those changes require more time to implement and test safely.

For the design rationale and tradeoffs, see [DESIGN.md](DESIGN.md). For the implemented component structure, see [IMPLEMENTATION_ARCHITECTURE.md](IMPLEMENTATION_ARCHITECTURE.md).
