# Validation

Validated on the Railway development VM on 2026-09-25 (Python 3.14.7 host, Docker 29.1.2). The test image installs Python 3.13, MCP SDK 2.2.0, and Radicale 3.8.0.

From the repository root, with the project virtual environment and Docker available:

```sh
.venv/bin/python -m unittest tests.test_unit -v
.venv/bin/python -m unittest tests.test_container -v
```

The unit suite passed 5 tests. The container suite passed 1 end-to-end test (52.183s), building and starting the image with temporary credentials and a dedicated volume. It checked direct CalDAV/CardDAV access, public health, MCP SDK HTTP/authentication and all 22 tools, calendar/task/contact CRUD, ETag conflicts, Unicode and recurrence round-trips, resources without file extensions, and data surviving a container restart on the same volume. Test-created containers, volumes, and images are removed during teardown.

The container test uses `docker build` and `docker run`; Docker Compose was not available on the validation VM, so `compose.yaml` was not executed there.
