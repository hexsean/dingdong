"""Thin wrapper — delegates to dingdong.cli.main()."""
from dingdong.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
