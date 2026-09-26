# Radicale MCP

A thin MCP sidecar for an existing Radicale CalDAV/CardDAV server. The MCP image runs only the Python adapter and exposes calendars, tasks, and contacts over Streamable HTTP at `/mcp`. Radicale runs separately and owns its configuration, accounts, collections, and storage. The adapter uses authenticated DAV requests and needs no data volume, local database, or item cache.

## Add MCP to an existing Compose stack

Copy only the `radicale-mcp` service from [compose.yaml](compose.yaml) into the existing Compose file and connect it to the same network as Radicale. Keep the existing project/stack name, Radicale service, port, configuration, and volume definitions. The reference file uses `name: radicale`; do not rename an existing deployment to match it.

The complete example retains the upstream image `ghcr.io/kozea/radicale:stable`, its `TZ=America/Sao_Paulo`, port `5232:5232`, and the `config:/etc/radicale` and `data:/var/lib/radicale` mounts. The named bind volumes remain `radicale-config` (`./config`) and `radicale-data` (`./data`). The MCP service has no volumes and does not read those directories.

Set the credentials of an existing Radicale account and a separate MCP token in the deployment environment or an uncommitted `.env`:

```dotenv
DAV_USERNAME=<existing Radicale username>
DAV_PASSWORD=<existing Radicale password>
MCP_TOKEN=<long random token>
```

`DAV_URL` must use the reachable Radicale service name on that network. The example uses `http://radicale:5232/`. Keep Radicale's existing authentication and rights configuration; the MCP adapter uses the same DAV access granted to that account.

Once the configured MCP image tag is available, start only the sidecar:

```sh
docker compose up -d --no-deps radicale-mcp
docker compose ps radicale-mcp
docker compose exec -T radicale-mcp python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"
```

Adding MCP does not require recreating or migrating volumes, changing Radicale configuration, or restarting Radicale. Avoid a stack-wide `up`, `down`, or volume operation for this addition. The example consumes `ghcr.io/forge-z/radicale-mcp:latest`, a multiarch image for `linux/amd64` and `linux/arm64`; Docker selects the native architecture automatically. It is also possible to build the source with that tag in an authorized development environment.

## Configuration

| Variable | Purpose | Default |
| --- | --- | --- |
| `DAV_URL` | Radicale HTTP base URL used by MCP | `http://radicale:5232/` |
| `DAV_USERNAME` | Existing Radicale user for DAV Basic auth | Required |
| `DAV_PASSWORD` | Password for that user | Required |
| `MCP_TOKEN` | Single Bearer token for `/mcp`; startup fails if empty | Required |
| `MCP_TIMEZONE` | IANA zone for new date-times without an offset | `America/Sao_Paulo` |
| `PORT` | MCP HTTP listen port | `8080` in Docker/Compose, `8000` when run directly |

`GET /health` is public and returns 200 only when a DAV PROPFIND succeeds with the configured credentials. It returns 503 without revealing DAV data when Radicale is unavailable. Every other MCP request requires `Authorization: Bearer <MCP_TOKEN>`. The adapter listens on `0.0.0.0` inside its container. The example uses only `expose: ["8080"]` with `PORT: "8080"`; it does not publish an MCP port on the host. Connect Coolify/Traefik or the existing HTTPS reverse proxy to the same Docker network and route to `radicale-mcp:8080` (`/mcp` for MCP clients).

Accounts, calendars, task lists, and address books are managed by Radicale and DAV clients. MCP does not create users or collections. Existing data is immediately available through DAV according to that user's rights. A calendar supporting both VEVENT and VTODO appears in both `list_calendars` and `list_task_lists`.

Outside Docker, `python -m radicale_mcp.server` starts only MCP using the same six environment variables. The ASGI entry point is `radicale_mcp.server:app`.

## Tools and IDs

Collections and items use the DAV href relative to `DAV_URL` as their `id`, for example `alice/work/` and `alice/work/4c1f.ics`. Use IDs returned by the tools. IDs are checked against the configured origin and base path; redirects and escaped paths are rejected.

| Resource | Tools |
| --- | --- |
| Events | `list_calendars`, `list_events`, `search_events`, `get_event`, `create_event`, `update_event`, `delete_event` |
| Tasks | `list_task_lists`, `list_tasks`, `search_tasks`, `get_task`, `create_task`, `update_task`, `complete_task`, `delete_task` |
| Contacts | `list_addressbooks`, `list_contacts`, `search_contacts`, `get_contact`, `create_contact`, `update_contact`, `delete_contact` |

For example, call `create_event` with:

```json
{"calendar_id":"alice/work/","summary":"Review","start":"2026-10-01T09:00:00","end":"2026-10-01T10:00:00"}
```

The result includes `id`, `uid`, and `etag`. Pass that exact ETag to `update_event`:

```json
{"event_id":"alice/work/<id-from-create>.ics","expected_etag":"\"etag-from-create\"","changes":{"summary":"Review moved"}}
```

`update_event`, `update_task`, `update_contact`, `complete_task`, and all deletes require a current single strong ETag. A concurrent DAV change causes HTTP 412 and the tool call fails; get the item again before retrying. `changes` modifies only named fields. An omitted field stays as it was; `null` clears an optional field. UID is never changed by updates. Create uses `If-None-Match: *`, while updates and deletes use `If-Match`.

`get_event` and `get_task` return `raw_ical`; `get_contact` returns `raw_vcard`. Event patches target the master VEVENT and preserve recurrence overrides, alarms, VTIMEZONE, RRULE, EXDATE, and unrecognized properties. Searches and date filters use DAV REPORT. A recurring event or task is returned as a matching **series resource**, not an expanded list of occurrences; `recurrence_overrides` reports how many override VEVENTs are present. List tools return up to 100 items by default and accept `limit` up to 1000. Search tools filter the full REPORT result before applying the limit.

Date-only events keep all-day `DATE` values and an exclusive end date. A date-time without an offset uses `MCP_TIMEZONE`. An explicit ISO offset is normalized to UTC to preserve its instant in iCalendar; existing TZID and floating date-times remain intact when patched with offset-free values. Contacts support name, company, title, multiple emails and phones, and multiline notes. Other vCard fields remain when omitted from an update.

## Build and verification

Run development checks only in an execution environment authorized for the project:

```sh
python -m unittest discover -s tests -v
docker build -t ghcr.io/forge-z/radicale-mcp:latest .
```

The sidecar integration suite uses separate Radicale and MCP containers with temporary test resources. The manual GitHub Actions workflow builds and tests natively on AMD64 and ARM64, then combines the tested image digests into the commit and `latest` multiarch tags. These commands describe the verification procedure; they do not imply a successful run or a published image for a particular commit. Check the CI results and image tag for the commit being deployed.
