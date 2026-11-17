# Security policy

## Reporting a vulnerability

Please do not report a vulnerability in a public issue, pull request or discussion.

Report it in one of these two ways:

1. **GitHub private vulnerability reporting.** On this repository open the **Security** tab,
   choose **Report a vulnerability** and fill in the form. Only the maintainers see the report.
2. **E-mail.** Write to [security@brutor.ai](mailto:security@brutor.ai).

Use GitHub's form if you can: it keeps the report, the discussion and the fix in one place.

## What to include

- The component: Core Gateway, Control Plane, Admin Console, Skill Runner, witness or the package.
- The version or the commit, and how you run it (the Community package, or built from source).
- What an attacker can do, and what they need to have first (no account, an API key, an
  administrator account, access to the network).
- The steps to reproduce it. A request, a script or a short proof of concept helps most.
- The configuration that matters, with secrets removed.
- Whether you have told anyone else, and whether you plan to publish.

Never send real secrets, API keys or personal data. Replace them with made-up values.

## What happens next

- We acknowledge your report within 3 working days.
- We tell you whether we can reproduce it and how serious we judge it to be, and we keep you
  informed while we work on it.
- We fix it in private, release the fix, and then publish a security advisory. We credit you
  in the advisory unless you ask us not to.
- Please give us reasonable time to release a fix before you publish details.

## Supported versions

Until version 1.0, only the latest release is supported. Security fixes are made on the
latest release and are not back-ported to older ones.

| Version | Supported |
|---|---|
| The latest release | Yes |
| Any older release | No |

## Scope

In scope: the code in this repository and the container images built from it (Core Gateway,
Control Plane, Admin Console, Skill Runner, the witness) and the Community package.

Out of scope:

- Findings that need an administrator account of the installation, or access to its host,
  its database, its Redis or its `.env` file. Those are trusted.
- An installation that was changed to be less safe than the package: for example a weak
  `JWT_SECRET` or `ENCRYPTION_KEY`, the services opened to a network without TLS in front
  of them, or a guardrail switched off.
- Vulnerabilities in third-party software that this repository only starts or depends on
  (PostgreSQL, Valkey, base images, libraries) when our use of it is not what makes it
  exploitable. Report those to their maintainers. Tell us if we ship an affected version.
- The model providers, MCP servers and agents that you connect to the gateway.
- Denial of service by sending a very large amount of traffic.
- Reports from automated scanners with no demonstrated impact.
- Social engineering, and physical attacks.
- The Brutor websites and hosted services. They are not part of this repository.
- The Enterprise Edition. Report an Enterprise Edition vulnerability through your support
  contact, or by the e-mail address above.

If you are not sure whether something is in scope, report it privately anyway.
