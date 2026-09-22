# Result CSV cleanup

Every search/enrichment tool writes a CSV under
`OUTPUT_DIR/<user_id>/...` (see `config.OUTPUT_DIR`, default
`leads_output/`). Nothing in the server process deletes these on its own --
left alone, storage grows without bound as tenants keep running searches.

`leadorbyt/cleanup.py` deletes any `*.csv` under `OUTPUT_DIR/<user_id>/`
whose mtime is older than `config.RESULT_RETENTION_DAYS`
(`LEADORBYT_RESULT_RETENTION_DAYS`, default 7). It does one purge and exits
-- it is not a long-running process and is not started by `leadorbyt`
itself. Run it on a schedule with the deployment host's own cron, via
`docker exec` since the CSVs live inside the container's `/data` volume:

```cron
# /etc/cron.d/leadorbyt-cleanup, or `crontab -e` as the user that runs docker
0 3 * * * root docker exec leadorbyt python3 -m leadorbyt.cleanup >> /var/log/leadorbyt-cleanup.log 2>&1
```

(`leadorbyt-cleanup` also works in place of `python3 -m leadorbyt.cleanup`
-- both run `leadorbyt.cleanup:main`.)

Daily at 03:00 server time is the default choice here, not a requirement --
tune the schedule and `LEADORBYT_RESULT_RETENTION_DAYS` to taste. A shorter
retention window means less storage pressure but less time for a tenant to
notice and download a result before it's gone; `read_result_csv`'s tool
docstring in `server.py` already tells the calling agent to remind the user
to download a result they want to keep, since there is no in-product
warning before a file is deleted.

## Verifying it ran

```bash
docker exec leadorbyt python3 -m leadorbyt.cleanup
# [...] INFO leadorbyt.cleanup: Purged N result CSV(s) older than 7 days
```
