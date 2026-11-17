from http.client import HTTPException
from time import perf_counter
from typing import NamedTuple
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

REQUEST_COUNT, CHUNK_SIZE, MIB = 10, 64 * 1024, 1024 * 1024

class DownloadError(Exception):
    """A measurement failure, with the original error available as its cause."""

class BenchmarkResult(NamedTuple):
    request_count: int
    total_bytes: int
    total_seconds: float

    @property
    def average_seconds(self) -> float:
        return self.total_seconds / self.request_count

    @property
    def speed_bytes_per_second(self) -> float:
        return self.total_bytes / self.total_seconds

def _read_body(response) -> int:
    length = response.getheader("Content-Length")
    if response.getheader("Transfer-Encoding") is not None or response.status == 204:
        length = None
    if length is not None:
        try:
            length = int(length)
            if length < 0:
                raise ValueError("negative length")
        except ValueError as error:
            raise HTTPException("Invalid Content-Length") from error
    received = 0
    while chunk := response.read(CHUNK_SIZE):
        received += len(chunk)
    if length is not None and received != length:
        raise HTTPException(f"Incomplete response: expected {length}, received {received} bytes")
    return received

class _Redirects(HTTPRedirectHandler):
    def __init__(self):
        super().__init__()
        self.bytes_received = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        request = super().redirect_request(req, fp, code, msg, headers, newurl)
        if request.type not in {"http", "https"}:
            raise HTTPError(newurl, code, "Redirect target must use HTTP(S)", headers, fp)
        with fp:
            self.bytes_received += _read_body(fp)
        return request

def download_once(url: str, timeout: float) -> tuple[int, float]:
    """Return all body bytes, including redirects, and elapsed seconds."""
    redirects = _Redirects()
    opener = build_opener(redirects)
    request = Request(url, headers={"Accept-Encoding": "identity"})
    started = perf_counter()
    try:
        with opener.open(request, timeout=timeout) as response:
            size = redirects.bytes_received + _read_body(response)
            return size, perf_counter() - started
    except HTTPError as error:
        error.close()
        raise

def run_benchmark(url: str, timeout: float) -> BenchmarkResult:
    """Run ten sequential downloads, stopping at the first failure."""
    results = []
    for number in range(1, REQUEST_COUNT + 1):
        try:
            results.append(download_once(url, timeout))
        except (OSError, HTTPException, ValueError) as error:
            detail = f"HTTP {error.code}: {error.reason}" if isinstance(error, HTTPError) else str(error)
            raise DownloadError(f"Request {number}/{REQUEST_COUNT} failed: {detail}") from error
    sizes, times = zip(*results)
    elapsed = sum(times)
    if elapsed <= 0:
        raise DownloadError("Measured duration is too small to calculate a speed")
    return BenchmarkResult(REQUEST_COUNT, sum(sizes), elapsed)

def format_report(result: BenchmarkResult) -> str:
    """Return the report without performing I/O."""
    return (
        f"Average request time: {result.average_seconds:.6f} s\n"
        f"Total downloaded: {result.total_bytes} bytes ({result.total_bytes / MIB:.3f} MiB)\n"
        f"Download speed: {result.speed_bytes_per_second / MIB:.6f} MiB/s"
    )
