import argparse
import sys
from urllib.parse import urlsplit
from core import DownloadError, format_report, run_benchmark

def main(argv: list[str] | None = None) -> None:
    """Validate arguments, run the benchmark and print the result or error."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("url")
    parser.add_argument("--timeout", type=float, default=30, metavar="SECONDS")
    args = parser.parse_args(argv)
    try:
        url = urlsplit(args.url)
        if (url.scheme not in {"http", "https"} or not url.hostname
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in args.url)):
            raise ValueError("Expected an HTTP(S) URL without whitespace/control characters")
        _ = url.port
        if not 0 < args.timeout <= 86400:
            raise ValueError("timeout must be in (0, 86400] seconds")
    except ValueError as error:
        parser.error(str(error))
    try:
        result = run_benchmark(args.url, args.timeout)
    except DownloadError as error:
        print(f"Error: {error}", file=sys.stderr)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
    else:
        print(format_report(result))
