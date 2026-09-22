"""Deletes result CSVs older than `config.RESULT_RETENTION_DAYS` so
OUTPUT_DIR doesn't grow unbounded.

Run on a schedule via `python -m leadorbyt.cleanup` (or the `leadorbyt-cleanup`
console script) -- an external cron owns scheduling; the server process does
NOT purge files on its own. See docs/cleanup.md for the actual crontab entry
used on the deployment host.
"""

import logging
import time

from . import config

logger = logging.getLogger("leadorbyt.cleanup")


def purge_old_result_files(now: float | None = None) -> int:
    """Delete every `*.csv` under `OUTPUT_DIR/<user_id>/` whose mtime is
    older than `config.RESULT_RETENTION_DAYS`. Returns the count deleted.

    A file that vanishes between the listing and the delete (e.g. a job
    finishing an export at the same moment) is not an error -- just skipped.
    """
    if not config.OUTPUT_DIR.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - config.RESULT_RETENTION_DAYS * 86400
    deleted = 0
    for csv_path in config.OUTPUT_DIR.glob("*/*.csv"):
        try:
            if csv_path.stat().st_mtime < cutoff:
                csv_path.unlink()
                deleted += 1
        except FileNotFoundError:
            continue
    return deleted


def main() -> None:
    logging.basicConfig(level=config.LOG_LEVEL, format="[%(asctime)s] %(levelname)s %(name)s: %(message)s")
    deleted = purge_old_result_files()
    logger.info(f"Purged {deleted} result CSV(s) older than {config.RESULT_RETENTION_DAYS} days")


if __name__ == "__main__":
    main()
