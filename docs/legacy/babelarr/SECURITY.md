# Security Policy

## Reporting

Please report sensitive vulnerabilities through GitHub's private vulnerability
reporting for this repository when available. If private reporting is not
available, open an issue with a minimal description and omit exploit details,
tokens, private paths, and account identifiers.

## Supported Scope

Babelarr is a local CLI and adapter-facing automation tool. Security-sensitive
areas include token handling, path mapping, generated subtitle write-back,
provider downloads, and MCP/Runtime job dispatch.

The project should not store API keys, Telegram bot tokens, Plex tokens, or
model provider credentials in job JSON. Keep those values in environment
variables or ignored local files.
