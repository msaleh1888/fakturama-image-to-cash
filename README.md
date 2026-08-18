# Fakturama Image-to-Cash Automation

This project converts one order image into validated structured data, creates a Fakturama Order, creates its linked Invoice, applies the source payment status, and verifies the saved records.

The implementation targets the supplied happy-path order, Fakturama 2.2.0 with an English UI, and a disposable prepared workspace. It is intentionally a small synchronous Python CLI rather than a general desktop-automation framework.

## Current status

Image extraction, the `OrderData` contract, deterministic financial validation, reviewed-JSON loading, and the three-command CLI boundary are implemented. The Fakturama adapter currently exposes its final entry point but still raises `NotImplementedError`; desktop automation is not yet complete.

Local status at this stage:

- The local extraction, CLI, serialization, validation, and pure Fakturama-runtime tests pass.
- Both synthetic order fixtures validate.
- Fakturama master data for the acceptance fixture has been prepared in the disposable development workspace.

This section must be updated after the final Fakturama acceptance run.

## Repository structure

```text
main.py                         CLI and straight-line orchestration
models.py                       Pydantic OrderData contract and validation
extract.py                      one OpenAI image-extraction request
fakturama.py                    Fakturama UI automation and verification
samples/                        runnable images and reviewed JSON fixtures
tests/                          local deterministic tests
DESIGN.md                       assessment design document
IMPLEMENTATION_ARCHITECTURE.md  final solution architecture
```

## Requirements

- Windows with a visible interactive desktop session
- Python 3.11 or newer
- Fakturama 2.2.0 with the English UI
- A disposable Fakturama workspace for automation
- An OpenAI API key for `extract` and `run`; `automate` does not require one

Never run the desktop automation against real accounting data. The application uses Fakturama's UI for writes and reads its persistence files only for verification.

## Installation

Create and activate a virtual environment, then install the pinned dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Create a local `.env` file in the repository root:

```dotenv
OPENAI_API_KEY=your_api_key_here
FAKTURAMA_EXECUTABLE=C:\Program Files\Fakturama2\Fakturama.exe
FAKTURAMA_WORKSPACE=D:\path\to\disposable\fakturama_workspace
FAKTURAMA_DIAGNOSTICS_DIR=diagnostics
```

`OPENAI_API_KEY` is needed only for commands that process an image. The Fakturama executable and workspace variables are needed only for commands that automate Fakturama. `.env` and diagnostic output are local files and must not be committed.

Desktop-automation dependencies will be added to `requirements.txt` with the Fakturama implementation. At the current stage, the pinned requirements cover extraction and validation only.

## Prepared Fakturama workspace

The happy-path implementation assumes these exact master records already exist:

- Debtor: Northstar Office GmbH, including the fixture billing and delivery addresses
- Payment method: Bank Transfer
- VAT: VAT 19%
- Product: `CHR-ERG-01` / Ergonomic Desk Chair
- Product: `MAT-DESK-02` / Anti-Fatigue Desk Mat

Missing or ambiguous master data stops the submitted implementation. The full design for conditional master creation is described in [DESIGN.md](DESIGN.md), but those creation branches are outside the timeboxed implementation scope.

## Commands

Extract one image to validated reviewed JSON:

```powershell
python main.py extract samples\synthetic_order_01.png --output output\synthetic_order_01.json
```

Automate Fakturama from reviewed JSON without making an OpenAI request:

```powershell
python main.py automate samples\synthetic_order_01.expected.json
```

Extract an image, write its reviewed JSON, and automate the same validated order:

```powershell
python main.py run samples\synthetic_order_01.png --output output\synthetic_order_01.json
```

At the current repository stage, `extract` works and the two Fakturama commands stop at the unimplemented desktop adapter.

## Validation

The extraction response is parsed directly into `OrderData`. Before JSON is written or Fakturama is touched, Python validation checks required fields, dates, ranges, payment consistency, every line net, and the source net, VAT, and gross totals using exact Decimal arithmetic.

The first fixture must validate to:

- net: EUR 570.00
- VAT: EUR 108.30
- gross: EUR 678.30

Invalid or inconsistent source data exits nonzero and does not produce a reviewed JSON file or perform a desktop action.

## Tests

The default test suite is local and does not call OpenAI or control Fakturama:

```powershell
python -m unittest discover -s tests
```

Real extraction and Fakturama acceptance runs are explicit integration checks because they require an API key or an interactive Windows application.

## Safety and verification

- The automation uses only the configured disposable workspace.
- It does not write Fakturama database files directly.
- Debtor and Product selection requires one exact prepared record.
- UI values and totals are verified before Save.
- The Invoice is created only through the saved Order's follow-up action.
- If a Save result is unclear, UI and read-only persistence are inspected before any further financial action.

## Known limitations

- Only the supplied happy-path data shape is supported.
- Required master records must already exist.
- Only Fakturama 2.2.0 with the English UI is targeted.
- The automation requires an unlocked visible Windows desktop.
- There is no batch mode, resumable execution, rollback, or automatic recovery from a partially completed transaction.
- Alternate currencies, tax structures, order-level charges, and other Fakturama document types are not supported.

## Evidence

The final submission will include a small set of annotated screenshots under `evidence/` showing the completed two-line Order, linked paid Invoice, and final document verification. Internal discovery screenshots, trial scripts, workspace databases, and backups are not submission artifacts.

## If I had three more hours

I would first implement and rehearse the missing-master branches for Debtor, payment method, VAT, and Product while preserving the open Order. Next I would add interruption-safe identity checks around saved documents so a failed run could resume without creating duplicates. Finally, I would repeat the end-to-end flow from a freshly restored workspace, tighten any remaining brittle control locators, and capture the final annotated evidence from that repeatable run.
