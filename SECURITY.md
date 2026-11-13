# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue.

Use GitHub's private reporting: open the **Security** tab of this repository and choose
**Report a vulnerability**. You will get a reply within seven days.

Please include what you found, how to reproduce it, and which version (`RELEASE`) you tested.
Do not include real photos, file names or keys.

## What Star is designed for

Star is built for a home network that its owner controls. Knowing the design helps you judge
what is a vulnerability and what is intended.

- **Plain HTTP on the LAN.** Only the web container publishes a port. Do not expose it to the
  internet. Reach it from outside through a VPN.
- **Local accounts.** The web UI has password login with Argon2 hashes and server-side sessions.
- **Workers connect outbound.** They publish no ports. A worker may enrol itself only from the
  networks listed in `STAR_WORKER_AUTO_ENROLL_CIDRS`.
- **Secrets stay in ignored `.env` files** or in encrypted application storage. The Immich key
  and the family roster are encrypted at rest.
- **Models are sandboxed by design.** They receive pixels and a prompt, and return a structured
  answer. They have no tools, shell, database or Immich access, and their output is treated as
  untrusted evidence.
- **No delete capability.** The Immich client has no delete, trash or replace method.

## In scope

- Anything that lets a photo, file name, EXIF value, prompt or key leave the machines.
- A way to make Star delete, replace or lower a rating in Immich without the owner's approval.
- Prompt injection through photo content, captions or metadata that changes what Star does.
- Bypassing login, session or worker-enrolment checks.
- Secrets written to logs, diagnostics or reports.

## Out of scope

- Attacks that need the coordinator to be exposed to the internet against this guidance.
- Attacks by someone who already has administrator access to a machine running Star.
- Vulnerabilities in Immich, Ollama, MLX or the models themselves. Report those upstream.
- The quality of a model's rating. That is a product question, not a security one.

## Supported versions

Only the latest release is supported. Star has no long-term support branches.
