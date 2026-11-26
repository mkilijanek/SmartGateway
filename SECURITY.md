# Security Policy

SmartGateway sits between your clients and your model servers: it holds upstream API keys, sees prompts and answers in transit, and
can call tools through MCP. Reports about it are taken seriously, and a good report is appreciated.

## Supported versions

SmartGateway has no long-term-support branches. Fixes land on `main` and are released from there.

| Version | Supported |
| ------- | --------- |
| `main` and the latest release (2.x) | Yes |
| Anything older | No - please update first |

If you run a build from source, run the current `main`. Check that the problem still exists there before reporting it.

## Reporting a vulnerability

**Please do not open a public issue, pull request or discussion for a vulnerability.**

1. Open the repository's **Security** tab and choose **Report a vulnerability** (GitHub private vulnerability reporting). The report
   is visible only to the maintainers and to you.
2. If that button is missing, private reporting is not enabled on this repository. Open a public issue that says only *"I would
   like to report a security problem, please enable private reporting or give me a contact"* - with no technical details - and
   wait for a reply.

Please include:

- what is affected (component, endpoint or file) and the version or commit you tested;
- what an attacker needs first (network position, a gateway key, admin access, a malicious upstream server, ...);
- steps or a small proof of concept that reproduces it, and what you saw;
- the impact as you understand it. A CVSS 3.1 vector is welcome but not required.

Do not include real secrets, other people's data, or anything from a system you do not own.

### What to expect

SmartGateway is maintained by volunteers, so these are targets, not guarantees:

| Step | Target |
| ---- | ------ |
| Acknowledgement of your report | 7 days |
| First assessment (accepted, needs more information, or declined, with reasons) | 14 days |
| Fix or mitigation for a confirmed high or critical issue | 30 days |
| Fix for a confirmed medium or low issue | next regular release |

We will tell you if a date slips. If a report is declined we will explain why; you are free to disagree.

### Disclosure

We prefer coordinated disclosure: fix first, publish afterwards. Once a fix is released we publish a GitHub security advisory (and
request a CVE where one is warranted) that credits you unless you ask us not to. If a report is not fixed within **90 days** you
may publish your findings; please tell us beforehand.

### Good-faith research

We will not take legal action against research that follows this policy: test only against your own installation, do not access
or keep data that is not yours, do not degrade other people's services, and give us a reasonable time to fix before publishing.

## Scope

**In scope**

- the admin UI and management API (`/`, `/api/*`): authentication, sessions, CSRF/Origin handling, authorisation, audit log;
- the gateway endpoints (`/gw/*`, `/v1/*`, `/unified/*`) and the MCP gateway (`/mcp/*`): API-key checks, per-key limits and allow-lists,
  request/response handling, caching, content filters;
- the MCP client (outbound calls to upstream MCP servers) and the OpenAI-compatible upstream client: SSRF, redirects, credential leakage;
- storage of secrets: encryption of upstream keys, hashing of gateway keys and the admin password, the generated-password flow;
- the container image, `docker-compose.yml`, `deploy/` and the CI workflows in this repository.

**Out of scope**

- vulnerabilities in the model servers or MCP servers you connect, or in third-party services - report those to their authors;
- findings that need an already signed-in admin to do what an admin can do by design (adding an upstream, a filter or a gateway);
- gateways deliberately left without API keys, `SG_ALLOW_OPEN_ADMIN=1`, or exposing the plain-HTTP port to an untrusted network
  without a TLS proxy: these are documented operator choices (see the README section *Exposing it to the internet*);
- denial of service that needs more traffic than any single-host service can absorb, and missing rate limits on endpoints that
  are already behind a login;
- reports produced by an automated scanner without a demonstrated impact, clickjacking on pages without sensitive actions,
  missing best-practice headers with no exploit, and version-banner disclosures;
- social engineering, physical access, and attacks on a host that is already compromised.

If you are unsure whether something is in scope, report it anyway.

## What the project already does

This is the intended security model, so you can tell a bug from a design choice:

- the management API **fails closed**: without `SG_ADMIN_PASSWORD` a random password is generated; open access needs an explicit opt-in;
- gateway and MCP API keys are stored only as hashes and shown once; upstream API keys and MCP headers are encrypted at rest
  (back up `secret.key` / `SG_SECRET_KEY` with the database);
- state-changing admin requests are checked for their origin, failed logins are rate-limited, request bodies and MCP responses are
  size-limited, and public error messages are generic;
- the container runs as a non-root user and its dependencies are pinned.

A deployment checklist lives in the README (*Exposing it to the internet*). Each deployment is still the operator's responsibility:
keep it updated, put TLS in front, and give every public gateway an API key.

## For contributors

- Never commit `.env`, `secret.key`, databases, backups or exported images.
- Fix security issues in a private fork or private advisory first when a report was made privately; do not describe an unpatched
  vulnerability in a public commit message or pull request.
- Security fixes need a test that fails before the change and passes after it.
- Enable *secret scanning* and *push protection* on forks you push to.
