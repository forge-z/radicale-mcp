# Radicale MCP

A thin MCP server for an existing Radicale CalDAV/CardDAV installation. It exposes calendars, tasks, and contacts over Streamable HTTP at `/mcp`. Radicale remains the data store and DAV server; this package reads and writes through DAV, with no separate database or item cache. The supplied image runs Radicale 3.8.0 and the MCP server as two processes in one container.

## Configuration

| Variable | Purpose | Default |
| --- | --- | --- |
| `DAV_URL` | Radicale HTTP base URL used by MCP | `http://127.0.0.1:5232/` |
| `DAV_USERNAME` | Existing Radicale user for DAV Basic auth | Required |
| `DAV_PASSWORD` | Password for that user | Required |
| `MCP_TOKEN` | Single Bearer token for `/mcp`; startup fails if empty | Required |
| `MCP_TIMEZONE` | IANA zone for new date-times without an offset | `America/Sao_Paulo` |
| `PORT` | MCP HTTP listen port in the container | `8080` in Docker, `8000` with `python -m radicale_mcp.server` |

`GET /health` is public and returns 200 only when a DAV PROPFIND succeeds with the configured credentials. It returns 503 without revealing DAV data when Radicale is unavailable. Every other MCP request requires `Authorization: Bearer <MCP_TOKEN>`. The MCP server listens on `0.0.0.0` inside the container. Compose publishes both ports on host loopback by default; put an HTTPS reverse proxy in front of `/mcp` for remote clients.

The included Radicale config uses its native bcrypt htpasswd authentication and `owner_only` rights. MCP does not create users or collections. Create calendars, task lists, and address books in the Radicale web UI or with a DAV client before using the item tools. The username in `DAV_USERNAME` must be allowed to access those collections. A calendar that supports both VEVENT and VTODO appears in both `list_calendars` and `list_task_lists`.

## Run on a Railway development VM

Build the image, create a bcrypt htpasswd entry, and keep the password and token in an uncommitted `.env` on the VM. The hash command prompts for the password, so it need not appear in shell history:

```sh
docker build -t radicale-mcp:dev .
docker run --rm -it --entrypoint python radicale-mcp:dev -c 'import bcrypt,getpass; print(bcrypt.hashpw(getpass.getpass("Radicale password: ").encode(), bcrypt.gensalt()).decode())'
```

Put `username:generated-hash` in a private users file. Set `RADICALE_USERS_FILE` to its absolute path. Add these values to `.env` (use actual secrets, not the placeholders):

```dotenv
RADICALE_USERS_FILE=/absolute/path/to/private/users
DAV_USERNAME=username
DAV_PASSWORD=<same Radicale password>
MCP_TOKEN=<long random token>
```

Then run `docker compose up --build -d` in the Railway VM. Check `docker compose ps` and `curl -fsS http://127.0.0.1:8080/health`. The bundled image also exposes Radicale on host loopback port 5232 for its UI and DAV clients. `MCP_BIND_ADDRESS`, `MCP_PUBLISHED_PORT`, `RADICALE_BIND_ADDRESS`, and `RADICALE_PUBLISHED_PORT` can change the host bindings. Do not expose either HTTP port directly to the Internet without a TLS and access-control boundary.

For an already-running Radicale, run only the Python MCP package with `DAV_URL`, `DAV_USERNAME`, `DAV_PASSWORD`, `MCP_TOKEN`, and optionally `PORT`; `python -m radicale_mcp.server` starts the MCP listener. The ASGI entry point is `radicale_mcp.server:app`.

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

## Existing data and verification

Before replacing a Radicale deployment, back up its collection storage, config, and htpasswd file. Point the new container at the same verified data volume (`RADICALE_VOLUME`) or mount the existing collections directory at `/var/lib/radicale/collections`, and mount the existing Radicale config and users file read-only. Check that UID 10001 can write the storage volume. Keep the original backup until calendars, tasks, contacts, and their UIDs are visible through DAV and MCP after a restart. This package has no schema migration and should not rewrite stored `.ics` or `.vcf` files during transfer.

On the Railway VM, the relevant checks are:

```sh
.venv/bin/python -m unittest discover -s tests -v
docker build -t radicale-mcp:dev .
```

The integration test starts a temporary container and Radicale volume, checks MCP authentication, CRUD, ETag conflicts, DAV persistence after restart, and then removes its test resources. Run it only where Docker execution is permitted.
