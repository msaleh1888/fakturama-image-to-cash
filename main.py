from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from extract import extract_order, format_usage
from fakturama import AutomationResult, FakturamaConfig, automate_order
from models import OrderData


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_FAKTURAMA_EXECUTABLE = Path(r"C:\Program Files\Fakturama2\Fakturama.exe")
PREPARED_FAKTURAMA_WORKSPACE = PROJECT_ROOT / "samples" / "fakturama_workspace"
RUNTIME_FAKTURAMA_ROOT = PROJECT_ROOT / ".runtime" / "fakturama"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract validated orders and enter them in Fakturama."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract_parser = subparsers.add_parser(
        "extract", help="extract one image to reviewed JSON"
    )
    extract_parser.add_argument("image", type=Path, metavar="IMAGE")
    extract_parser.add_argument("--output", required=True, type=Path, metavar="JSON")

    automate_parser = subparsers.add_parser(
        "automate", help="enter one reviewed JSON order in Fakturama"
    )
    automate_parser.add_argument("json", type=Path, metavar="JSON")
    automate_parser.add_argument(
        "--fakturama-executable",
        type=Path,
        default=DEFAULT_FAKTURAMA_EXECUTABLE,
        metavar="PATH",
        help="Fakturama executable (default: %(default)s)",
    )

    run_parser = subparsers.add_parser(
        "run", help="extract one image and enter the order in Fakturama"
    )
    run_parser.add_argument("image", type=Path, metavar="IMAGE")
    run_parser.add_argument("--output", required=True, type=Path, metavar="JSON")
    run_parser.add_argument(
        "--fakturama-executable",
        type=Path,
        default=DEFAULT_FAKTURAMA_EXECUTABLE,
        metavar="PATH",
        help="Fakturama executable (default: %(default)s)",
    )

    return parser.parse_args(argv)


def load_order_json(path: Path) -> OrderData:
    if not path.is_file():
        raise ValueError(f"reviewed JSON file does not exist: {path}")
    return OrderData.model_validate_json(path.read_text(encoding="utf-8"))


def copy_prepared_fakturama_workspace(
    fixture: Path = PREPARED_FAKTURAMA_WORKSPACE,
    runtime_root: Path = RUNTIME_FAKTURAMA_ROOT,
) -> Path:
    database_script = fixture / "Database" / "Database.script"
    if not database_script.is_file():
        raise ValueError(
            "prepared Fakturama workspace is missing its database: "
            f"{database_script}"
        )
    runtime_root.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="workspace-", dir=runtime_root))
    shutil.copytree(fixture, workspace, dirs_exist_ok=True)
    return workspace


def build_automate_fakturama_config(executable: Path) -> FakturamaConfig:
    executable = executable.resolve()
    if not executable.is_file():
        raise ValueError(f"Fakturama executable does not exist: {executable}")
    workspace = copy_prepared_fakturama_workspace()
    diagnostics = Path(os.getenv("FAKTURAMA_DIAGNOSTICS_DIR", "diagnostics"))
    return FakturamaConfig(executable, workspace, diagnostics)


def build_openai_client(api_key: str | None) -> OpenAI:
    if not api_key:
        raise ValueError("OPENAI_API_KEY is required")
    return OpenAI(api_key=api_key, max_retries=0)


def write_reviewed_json(path: Path, order: OrderData) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(order.model_dump_json(indent=2) + "\n", encoding="utf-8")


def command_extract(args: argparse.Namespace, api_key: str | None) -> None:
    order, usage = extract_order(build_openai_client(api_key), args.image)
    write_reviewed_json(args.output, order)
    print(f"Validated order written to {args.output}{format_usage(usage)}")


def print_automation_result(result: AutomationResult) -> None:
    print(f"Verified Fakturama Order {result.order_number}")
    print(f"Verified linked Invoice {result.invoice_number}")


def command_automate(args: argparse.Namespace) -> None:
    order = load_order_json(args.json)
    config = build_automate_fakturama_config(args.fakturama_executable)
    print(f"Using isolated Fakturama workspace: {config.workspace}")
    result = automate_order(order, config)
    print_automation_result(result)


def command_run(args: argparse.Namespace, api_key: str | None) -> None:
    order, usage = extract_order(build_openai_client(api_key), args.image)
    write_reviewed_json(args.output, order)
    print(f"Validated order written to {args.output}{format_usage(usage)}")
    config = build_automate_fakturama_config(args.fakturama_executable)
    print(f"Using isolated Fakturama workspace: {config.workspace}")
    result = automate_order(order, config)
    print_automation_result(result)


def run(argv: list[str] | None = None) -> int:
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    try:
        args = parse_args(argv)
        if args.command == "extract":
            command_extract(args, api_key)
        elif args.command == "automate":
            command_automate(args)
        else:
            command_run(args, api_key)
    except Exception as error:
        message = str(error)
        if api_key:
            message = message.replace(api_key, "[REDACTED]")
        print(f"Failed: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
