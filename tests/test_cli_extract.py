import tempfile
import unittest
import base64
from io import BytesIO
from pathlib import Path
from unittest.mock import ANY, patch

import main
from PIL import Image
from extract import encode_image
from fakturama import AutomationResult, FakturamaConfig
from models import OrderData


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "samples" / "synthetic_order_01.expected.json"


class CliAndExtractionTests(unittest.TestCase):
    def test_parse_extract_command(self) -> None:
        args = main.parse_args(["extract", "order.png", "--output", "order.json"])
        self.assertEqual(args.command, "extract")
        self.assertEqual(args.image, Path("order.png"))
        self.assertEqual(args.output, Path("order.json"))

    def test_parse_automate_and_run_commands(self) -> None:
        automate = main.parse_args(["automate", "order.json"])
        automate_custom = main.parse_args(
            [
                "automate",
                "order.json",
                "--fakturama-executable",
                r"D:\Apps\Fakturama.exe",
            ]
        )
        run = main.parse_args(["run", "order.png", "--output", "reviewed.json"])
        run_custom = main.parse_args(
            [
                "run",
                "order.png",
                "--output",
                "reviewed.json",
                "--fakturama-executable",
                r"D:\Apps\Fakturama.exe",
            ]
        )
        self.assertEqual((automate.command, automate.json), ("automate", Path("order.json")))
        self.assertEqual(
            automate.fakturama_executable, main.DEFAULT_FAKTURAMA_EXECUTABLE
        )
        self.assertEqual(
            automate_custom.fakturama_executable, Path(r"D:\Apps\Fakturama.exe")
        )
        self.assertEqual((run.command, run.output), ("run", Path("reviewed.json")))
        self.assertEqual(run.fakturama_executable, main.DEFAULT_FAKTURAMA_EXECUTABLE)
        self.assertEqual(
            run_custom.fakturama_executable, Path(r"D:\Apps\Fakturama.exe")
        )

    def test_prepared_workspace_is_copied_fresh_without_mutating_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "fixture"
            database = fixture / "Database"
            database.mkdir(parents=True)
            script = database / "Database.script"
            script.write_text("prepared", encoding="utf-8")
            (fixture / "settings.ini").write_text("USD", encoding="utf-8")

            first = main.copy_prepared_fakturama_workspace(
                fixture, root / "runtime"
            )
            (first / "Database" / "Database.script").write_text(
                "changed", encoding="utf-8"
            )
            second = main.copy_prepared_fakturama_workspace(
                fixture, root / "runtime"
            )

            self.assertNotEqual(first, second)
            self.assertEqual(script.read_text(encoding="utf-8"), "prepared")
            self.assertEqual(
                (second / "Database" / "Database.script").read_text(
                    encoding="utf-8"
                ),
                "prepared",
            )
            self.assertEqual((second / "settings.ini").read_text(), "USD")

    def test_automate_config_checks_executable_before_copying_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            main, "copy_prepared_fakturama_workspace"
        ) as copy_workspace:
            missing = Path(directory) / "missing.exe"
            with self.assertRaisesRegex(ValueError, "executable does not exist"):
                main.build_automate_fakturama_config(missing)
        copy_workspace.assert_not_called()

    def test_automate_config_uses_requested_executable_and_fresh_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "Fakturama.exe"
            executable.write_bytes(b"")
            workspace = root / "runtime-workspace"
            with patch.object(
                main, "copy_prepared_fakturama_workspace", return_value=workspace
            ):
                config = main.build_automate_fakturama_config(executable)
            self.assertEqual(config.executable, executable.resolve())
            self.assertEqual(config.workspace, workspace)

    def test_encode_image_rejects_unsupported_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "order.gif"
            path.write_bytes(b"GIF")
            with self.assertRaisesRegex(ValueError, "PNG, JPEG, or WEBP"):
                encode_image(path)

    def test_encode_image_rejects_missing_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "does not exist"):
                encode_image(Path(directory) / "missing.png")

    def test_encode_image_downscales_oversized_input_in_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.png"
            Image.effect_noise((1400, 1400), 100).convert("RGB").save(path)
            self.assertGreater(path.stat().st_size, 250_000)
            data_url = encode_image(path)
            prefix, encoded = data_url.split(",", 1)
            self.assertEqual(prefix, "data:image/jpeg;base64")
            with Image.open(BytesIO(base64.b64decode(encoded))) as resized:
                self.assertLessEqual(resized.width, 700)
                self.assertLessEqual(resized.height, 900)

    def test_load_reviewed_json(self) -> None:
        order = main.load_order_json(FIXTURE)
        self.assertIsInstance(order, OrderData)
        self.assertEqual(order.external_reference, "WEB-2026-0714-A17")

    def test_extract_command_writes_returned_validated_model(self) -> None:
        order = OrderData.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "reviewed.json"
            args = main.parse_args(["extract", "unused.png", "--output", str(output)])
            with patch.object(main, "build_openai_client", return_value=object()), patch.object(
                main, "extract_order", return_value=(order, None)
            ) as extraction:
                main.command_extract(args, "test-key")
            extraction.assert_called_once_with(ANY, Path("unused.png"))
            self.assertEqual(main.load_order_json(output), order)

    def test_automate_command_prints_verified_document_numbers(self) -> None:
        args = main.parse_args(["automate", str(FIXTURE)])
        config = FakturamaConfig(Path("fakturama.exe"), Path("workspace"), Path("diagnostics"))
        result = AutomationResult("PO000003", "INV000003")
        with patch.object(
            main, "build_automate_fakturama_config", return_value=config
        ) as build_config, patch.object(
            main, "automate_order", return_value=result
        ) as automate, patch("builtins.print") as output:
            main.command_automate(args)
        build_config.assert_called_once_with(main.DEFAULT_FAKTURAMA_EXECUTABLE)
        automate.assert_called_once_with(main.load_order_json(FIXTURE), config)
        output.assert_any_call("Using isolated Fakturama workspace: workspace")
        output.assert_any_call("Verified Fakturama Order PO000003")
        output.assert_any_call("Verified linked Invoice INV000003")

    def test_run_passes_extracted_order_to_automation(self) -> None:
        order = main.load_order_json(FIXTURE)
        config = FakturamaConfig(Path("fakturama.exe"), Path("workspace"), Path("diagnostics"))
        result = AutomationResult("PO000003", "INV000003")
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "reviewed.json"
            args = main.parse_args(["run", "unused.png", "--output", str(output_path)])
            with patch.object(main, "build_openai_client", return_value=object()), patch.object(
                main, "extract_order", return_value=(order, None)
            ), patch.object(
                main, "build_automate_fakturama_config", return_value=config
            ) as build_config, patch.object(
                main, "automate_order", return_value=result
            ) as automate, patch("builtins.print") as output:
                main.command_run(args, "test-key")
            self.assertEqual(main.load_order_json(output_path), order)
            build_config.assert_called_once_with(main.DEFAULT_FAKTURAMA_EXECUTABLE)
            automate.assert_called_once_with(order, config)
            self.assertIs(automate.call_args.args[0], order)
            output.assert_any_call("Using isolated Fakturama workspace: workspace")

    def test_run_uses_custom_executable_for_isolated_workspace(self) -> None:
        order = main.load_order_json(FIXTURE)
        executable = Path(r"D:\Apps\Fakturama.exe")
        config = FakturamaConfig(executable, Path("fresh-workspace"), Path("diagnostics"))
        args = main.parse_args(
            [
                "run",
                "unused.png",
                "--output",
                "unused.json",
                "--fakturama-executable",
                str(executable),
            ]
        )
        with patch.object(main, "build_openai_client", return_value=object()), patch.object(
            main, "extract_order", return_value=(order, None)
        ), patch.object(main, "write_reviewed_json"), patch.object(
            main, "build_automate_fakturama_config", return_value=config
        ) as build_config, patch.object(
            main, "automate_order", return_value=AutomationResult("PO1", "INV1")
        ), patch("builtins.print"):
            main.command_run(args, "test-key")
        build_config.assert_called_once_with(executable)


if __name__ == "__main__":
    unittest.main()
