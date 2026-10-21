"""Console entry point for the bundled server and existing CLI commands."""
import multiprocessing
import sys


def main():
    multiprocessing.freeze_support()
    if len(sys.argv) == 1:
        sys.argv.append("serve")
    from betafolio.cli import main as cli_main
    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
