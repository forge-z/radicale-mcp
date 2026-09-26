# Security Policy

## Reporting a vulnerability

Please do not open a public issue for a vulnerability that could expose credentials, calendar data, tasks, contacts, or remote MCP access.

Report security issues privately to the repository owner through GitHub's private vulnerability reporting feature when available.

## Deployment notes

- Put the MCP endpoint behind HTTPS when it is reachable over an untrusted network.
- Use a long random `MCP_TOKEN` and a dedicated Radicale account where practical.
- Do not reuse one MCP token for users who require different Radicale permissions.
- Keep `DAV_PASSWORD` and `MCP_TOKEN` out of source control and logs.
- Treat the Radicale account's DAV rights as the authorization boundary for the sidecar.
