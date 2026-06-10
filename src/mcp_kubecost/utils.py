import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(tempfile.gettempdir()) / "csv-reports"
OUTPUT_DIR.mkdir(exist_ok=True)


def write_csv(filename: str, csv_str: str) -> Path:
    path = OUTPUT_DIR / filename
    path.write_text(csv_str)
    file_size = path.stat().st_size
    size_kb = file_size / 1024
    size_str = f"{size_kb:.2f} KB" if size_kb < 1024 else f"{size_kb / 1024:.2f} MB"
    logger.info(f"CSV created: {path} ({size_str}, {file_size:,} bytes)")
    return path


def report_url(filename: str) -> str | None:
    base = os.getenv("MCP_KUBECOST_BASE_URL", "").rstrip("/")
    return f"{base}/reports/{filename}" if base else None
