#!/usr/bin/env python3
"""Small dependency-free validation gate for the deployable Odoo addon."""

from __future__ import annotations

import ast
import py_compile
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


class ValidationError(RuntimeError):
    """Raised when an addon is unsafe to deploy."""


def validate_addon(addon: Path) -> dict[str, str]:
    addon = Path(addon)
    manifest_path = addon / "__manifest__.py"
    if not manifest_path.is_file():
        raise ValidationError(f"Missing manifest: {manifest_path}")

    try:
        manifest = ast.literal_eval(manifest_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError) as exc:
        raise ValidationError(f"Invalid manifest: {exc}") from exc

    if not isinstance(manifest, dict) or not manifest.get("name") or not manifest.get("version"):
        raise ValidationError("Invalid manifest: name and version are required")

    for python_file in addon.rglob("*.py"):
        try:
            py_compile.compile(str(python_file), doraise=True)
        except py_compile.PyCompileError as exc:
            raise ValidationError(f"Invalid Python file {python_file}: {exc.msg}") from exc

    for xml_file in addon.rglob("*.xml"):
        try:
            ET.parse(xml_file)
        except (OSError, ET.ParseError) as exc:
            raise ValidationError(f"Invalid XML file {xml_file}: {exc}") from exc

    return {"name": str(manifest["name"]), "version": str(manifest["version"])}


def main() -> int:
    addon = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("hvac_sales_extension")
    try:
        result = validate_addon(addon)
    except ValidationError as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"Validated {result['name']} {result['version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
