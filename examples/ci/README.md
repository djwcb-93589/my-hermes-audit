# CI safe configuration placeholders

This directory contains only non-secret examples. It is not a runnable
production Subject configuration and must not be replaced with a copied local
configuration.

Representative CI receives credentials only through GitHub Secrets:

- `MYHERMES_API_KEY` — required model credential.
- `MYHERMES_MODEL` — required safe model identifier.
- `MYHERMES_BASE_URL` — optional endpoint, never printed or uploaded.
- `MYHERMES_READ_TOKEN` — optional private Subject repository read token.

Use a reviewed Subject-compatible configuration file that resolves credentials
from the CI environment. Never commit a key, endpoint credentials, prompt,
Memory state, Langfuse secret, Judge secret, or full local configuration.
`regression-policy.example.yaml` is a safe policy template; it controls only
existing Regression decisions and does not contain pricing or model credentials.
