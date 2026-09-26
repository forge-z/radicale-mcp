# Contributing

Thanks for considering a contribution to Radicale MCP.

The project intentionally stays small: it should remain a thin MCP-to-CalDAV/CardDAV adapter rather than becoming a second calendar server, CRM, scheduler, or user-management system.

## Development setup

Requirements:

- Python 3.11+
- Docker
- Docker Compose

Install and run the test suite:

```sh
python -m pip install .
python -m unittest discover -s tests -v
```

Build the container:

```sh
docker build -t radicale-mcp:dev .
```

## Contribution guidelines

Please keep changes focused and prefer existing standards and DAV behavior over project-specific abstractions.

Before adding a component or dependency, ask:

> Is this necessary to translate MCP to CalDAV/CardDAV safely and reliably?

Avoid adding databases, caches, queues, schedulers, dashboards, user stores, or direct access to Radicale internals.

For behavior changes:

1. add or update tests;
2. keep existing DAV data compatible;
3. preserve ETag-based concurrency behavior;
4. avoid exposing credentials or raw internal errors;
5. verify the sidecar still runs independently from Radicale storage.

## Pull requests

Keep pull requests small enough to review easily. Explain:

- what changed;
- why it is needed;
- how it was tested;
- whether it changes the MCP tool contract or deployment behavior.

Do not include secrets, production URLs, real credentials, or private calendar/contact data in tests or examples.
