"""Allow ``python -m drivefusion`` and serve as the frozen entry point."""

from drivefusion.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
