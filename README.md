# Radicale MCP

A thin MCP sidecar for an existing [Radicale](https://github.com/Kozea/Radicale) CalDAV/CardDAV server.

Radicale remains responsible for users, permissions, collections, storage, CalDAV and CardDAV. `radicale-mcp` only translates MCP tool calls into authenticated DAV requests. It has no database, no storage volume, no cache, and does not import Radicale internals.

## What it exposes

- Calendars and events: list, search, read, create, update, delete
- Task lists and VTODO tasks: list, search, read, create, update, complete, delete
- Address books and contacts: list, search, read, create, update, delete
- Streamable HTTP MCP endpoint at `/mcp`
- Public readiness endpoint at `/health`
- Multiarch container image for `linux/amd64` and `linux/arm64`

Image:

```text
ghcr.io/forge-z/radicale-mcp:latest
```

## Architecture

```text
MCP client
    |
    | HTTPS + Bearer token
    v
radicale-mcp
    |
    | CalDAV / CardDAV
    v
Radicale
    |
    v
existing Radicale storage
```

The MCP container never mounts or reads Radicale's data or configuration directories.

## Quick start

If Radicale is already running, add only the MCP sidecar to the same Docker network:

```yaml
radicale-mcp:
  image: ghcr.io/forge-z/radicale-mcp:latest
  pull_policy: always
  restart: unless-stopped
  init: true

  environment:
    DAV_URL: http://radicale:5232/
    DAV_USERNAME: ${DAV_USERNAME}
    DAV_PASSWORD: ${DAV_PASSWORD}
    MCP_TOKEN: ${MCP_TOKEN}
    MCP_TIMEZONE: ${MCP_TIMEZONE:-UTC}
    PORT: "8080"

  expose:
    - "8080"

  read_only: true

  tmpfs:
    - /tmp:size=16m

  cap_drop:
    - ALL

  security_opt:
    - no-new-privileges:true
```

Example environment:

```dotenv
DAV_USERNAME=alice
DAV_PASSWORD=change-me
MCP_TOKEN=replace-with-a-long-random-token
MCP_TIMEZONE=Europe/London
```

Generate a token with:

```sh
openssl rand -hex 32
```

Expose `radicale-mcp:8080` through your HTTPS reverse proxy and configure the MCP client with:

```text
https://mcp.example.com/mcp
Authorization: Bearer <MCP_TOKEN>
```

Do not expose the MCP endpoint without HTTPS on an untrusted network.

## Complete Compose example

[compose.yaml](compose.yaml) shows Radicale and the sidecar together. It preserves Radicale as the only DAV/storage service and gives each container its own health check.

If you already have a production Radicale deployment, keep your existing Radicale volumes and configuration. Copy the MCP service rather than replacing or migrating the existing storage.

## Configuration

| Variable | Purpose | Default |
| --- | --- | --- |
| `DAV_URL` | Radicale base URL reachable by the sidecar | `http://radicale:5232/` |
| `DAV_USERNAME` | Existing Radicale user | Required |
| `DAV_PASSWORD` | Password for that Radicale user | Required |
| `MCP_TOKEN` | Bearer token protecting `/mcp` | Required |
| `MCP_TIMEZONE` | IANA timezone used for naive date-times | Image default: `America/Sao_Paulo`; set explicitly for your deployment |
| `PORT` | Internal MCP HTTP port | `8080` in the reference Compose |

The sidecar uses the rights already granted to `DAV_USERNAME`. It does not create Radicale users or collections.

## Health checks

`GET /health` is intentionally public and lightweight. It returns HTTP 200 only when the MCP process can successfully authenticate to the configured Radicale server with a DAV `PROPFIND`. If Radicale is unavailable or the configured DAV credentials fail, it returns HTTP 503.

That makes the MCP health endpoint a functional end-to-end readiness check for the adapter plus its Radicale dependency.

The reference Compose also gives Radicale its own local port health check and starts the MCP sidecar only after Radicale becomes healthy.

For Docker Compose platforms such as Coolify, health is still reported per component, while the overall Service/stack status is aggregated from required components. The MCP health check additionally verifies the real DAV dependency, so a healthy MCP means the complete MCP -> Radicale path is working.

## Multiple users

The current design is intentionally simple:

> One MCP instance = one Radicale identity.

Do not share one MCP token between people who should have different Radicale permissions.

For multiple users, run one sidecar per Radicale account:

```text
Radicale
├── radicale-mcp-alice  -> DAV user alice
├── radicale-mcp-bob    -> DAV user bob
└── radicale-mcp-agent  -> DAV service account
```

Each sidecar has its own `DAV_USERNAME`, `DAV_PASSWORD` and `MCP_TOKEN`. Radicale remains the source of truth for access control and shared calendars/address books.

## MCP tools

| Resource | Tools |
| --- | --- |
| Events | `list_calendars`, `list_events`, `search_events`, `get_event`, `create_event`, `update_event`, `delete_event` |
| Tasks | `list_task_lists`, `list_tasks`, `search_tasks`, `get_task`, `create_task`, `update_task`, `complete_task`, `delete_task` |
| Contacts | `list_addressbooks`, `list_contacts`, `search_contacts`, `get_contact`, `create_contact`, `update_contact`, `delete_contact` |

Collection and item IDs are DAV hrefs relative to `DAV_URL`, for example:

```text
alice/work/
alice/work/4c1f.ics
```

Use IDs returned by the tools rather than constructing them manually.

## Dates and concurrency

- Date-times use ISO 8601.
- Explicit offsets preserve the instant and are normalized safely for iCalendar.
- Date-only events remain all-day `DATE` values.
- Recurring resources are returned as series resources; occurrences are not expanded by the MCP layer.
- Updates and deletes require the current strong ETag.
- Stale ETags fail instead of silently overwriting a concurrent DAV change.

Example event creation:

```json
{
  "calendar_id": "alice/work/",
  "summary": "Review",
  "start": "2026-10-01T09:00:00-03:00",
  "end": "2026-10-01T10:00:00-03:00"
}
```

## Security model

- `/mcp` requires `Authorization: Bearer <MCP_TOKEN>`.
- DAV credentials stay inside the sidecar environment.
- Credentials and tokens are not returned by MCP tools.
- Clients cannot supply arbitrary DAV server URLs.
- Redirects and path traversal are rejected.
- The reference container runs as a non-root user with a read-only filesystem and dropped Linux capabilities.

For internet-facing deployments, terminate TLS at a trusted reverse proxy.

## Development

```sh
python -m pip install .
python -m unittest discover -s tests -v
docker build -t radicale-mcp:dev .
```

The integration suite runs a real Radicale container separately from the MCP container and verifies that the MCP sidecar has no Radicale storage mounts.

GitHub Actions builds and tests natively on both AMD64 and ARM64 before publishing the multiarch image.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE).

Radicale is a separate project with its own license and is not bundled into the `radicale-mcp` image.
