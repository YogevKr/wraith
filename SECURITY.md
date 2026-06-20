# Security Policy

## Supported Versions

Wraith is in active early development. Security fixes are applied to the latest
released version and to `main`.

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |
| < 0.1   | :x:                |

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
discussions, or pull requests.**

Instead, report them privately through GitHub's
[private vulnerability reporting](https://github.com/YogevKr/wraith/security/advisories/new)
("Report a vulnerability" under the repository's **Security** tab). This keeps
the report confidential until a fix is available.

When reporting, please include as much of the following as you can:

- A description of the issue and its potential impact.
- Steps to reproduce, or a proof of concept.
- The affected version / commit.
- Any suggested remediation.

**Do not include real secrets in your report** — no live cookies, harvested
sessions, proxy credentials, or tokens. Redact them or use synthetic values.

## Response

We aim to acknowledge a report within a few days and to keep you informed as we
investigate and prepare a fix. Once a fix is released, we are happy to credit
reporters who wish to be named.

## Scope and Responsible Use

Wraith is a **dual-use** security/automation tool (a stealth, identity-borrowing
agent browser). Reports about how the project itself can be misused are best
directed at its design and documentation via a normal issue; this policy is for
**vulnerabilities in Wraith's own code** (e.g. credential handling, injection,
unsafe deserialization, accidental secret leakage).

Please read the **Responsible Use & Legal** section of the [README](README.md)
before using Wraith. Use it only for legitimate purposes: accessing your own
accounts and data, authorized security testing, research, and personal
automation.
