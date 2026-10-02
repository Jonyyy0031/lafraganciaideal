"""Write or verify the committed OpenAPI document (`apps/api/openapi.json`).

    uv run just openapi                              # regenerate after changing a contract
    python -m fragancia_api.main.openapi --check     # exit 1 if the file is stale

The document is built from the module routers alone: no settings, database or Valkey needed.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from fragancia_api.container import MODULES
from fragancia_api.main.http import build_app
from fragancia_api.shared.http.services import ServiceRegistry

OPENAPI_FILE = Path(__file__).resolve().parents[3] / "openapi.json"


def document() -> dict[str, Any]:
    routers = [router for module in MODULES for router in module.routers]
    return build_app(ServiceRegistry(), routers).openapi()


def render(spec: dict[str, Any]) -> str:
    return json.dumps(spec, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write or check apps/api/openapi.json")
    parser.add_argument("--check", action="store_true", help="fail if the file is stale")
    args = parser.parse_args(argv)
    expected = render(document())
    if args.check:
        current = OPENAPI_FILE.read_text() if OPENAPI_FILE.exists() else ""
        if current != expected:
            print(f"✘ {OPENAPI_FILE.name} is stale: run `uv run just openapi`", file=sys.stderr)
            return 1
        print(f"✔ {OPENAPI_FILE.name} is up to date")
        return 0
    OPENAPI_FILE.write_text(expected)
    print(f"✔ wrote {OPENAPI_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
