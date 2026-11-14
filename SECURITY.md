# Security Policy

## Supported Versions

| Version | Supported |
| :--- | :--- |
| `2.1.x` | Yes |
| `< 2.1` | No |

## Credential & Runtime Security Model

BEL Code is designed to keep credentials and workspace operations isolated:

- **Encrypted Secret Storage**: API keys entered via `POST /api/providers/configure` or Settings are stored via OS keyring / PBKDF2-HMAC-SHA256 encrypted `vault.enc` (`~/.bel_code/vault.enc`), never written in plaintext to `opencode.json` (which uses `{env:...}` references).
- **Path Isolation & Execution Modes**: File and terminal tools enforce workspace path boundaries, security modes (`ask`, `supervised`, `autonomous`), command classification, prompt-injection scanning, and hash-chained audit logs.
- **No Hardcoded Secrets**: Automated CI runs `tests/test_independence.py::test_no_hardcoded_api_credentials` on every change.

## Reporting a Vulnerability

If you discover a security vulnerability in BEL Code:

1. **Do not** open a public GitHub issue with exploit details or exposed secrets.
2. Contact the maintainers privately via GitHub Security Advisories ("Report a vulnerability" in the repository's **Security** tab).
3. Include a clear description, affected file paths or endpoints, steps to reproduce, and potential impact.

Maintainers will acknowledge reports promptly and coordinate a fix before public disclosure.
