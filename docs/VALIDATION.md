# Sidecar validation

This candidate replaces the single-container setup with an MCP sidecar and a separate Radicale server. No tests have been executed for this candidate; the previous single-container results do not validate this topology. GitHub Actions execution is pending authorization.

Run the unit and container integration suites from the repository root:

```sh
python -m unittest tests.test_unit -v
python -m unittest tests.test_container -v
RADICALE_MCP_TEST_IMAGE=radicale-mcp:dev python -m unittest tests.test_container -v
```

The integration suite uses the upstream `ghcr.io/kozea/radicale:stable` image on a private Docker network with the alias `radicale`. It creates temporary DAV collections and seeds an event, VTODO, and vCard directly through DAV before starting MCP. It verifies their UIDs through MCP, all 22 tools, authentication, ETag conflicts, recurrence and DUE round-trips, no-extension resources, and restart persistence. It inspects the MCP container for only its own `/tmp` tmpfs, no Radicale executable/package/config/storage, and no exposed DAV port. It also checks that health becomes 503 while Radicale is stopped, returns 200 after recovery, and that restarting MCP leaves Radicale running.

When `RADICALE_MCP_TEST_IMAGE` is supplied, the test requires that image to exist locally and neither builds nor removes it, so CI can test and then publish the same image. Without it, the suite builds and removes a uniquely tagged test image. The test removes its containers, private network, and Radicale-only volume. See [GitHub Actions](https://github.com/forge-z/radicale-mcp/actions) for the pending CI run.
