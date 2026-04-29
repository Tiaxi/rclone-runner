# Security Policy

## Supported Use

Rclone Runner is intended for single-admin, self-hosted deployments on trusted networks.
Before exposing an instance, set a strong admin password hash, replace the default session
secret, use HTTPS or a trusted reverse proxy, and keep `/data` plus `rclone.conf` backed up
and private.

## Reporting a Vulnerability

Please do not open public issues for suspected security vulnerabilities.

Report vulnerabilities privately using GitHub's private vulnerability reporting if it is
available for this repository. If it is not available, contact the maintainer through the
GitHub profile for this repository with a brief description and reproduction details.

Include:

- The affected version or commit.
- Steps to reproduce the issue.
- Any relevant logs, screenshots, or proof of concept.
- Whether credentials, `rclone.conf`, run logs, or SMTP settings may be exposed.

## Sensitive Data

Do not include real `.env` files, `rclone.conf`, database files, access tokens, SMTP
passwords, or run logs containing secrets in public issues, pull requests, or screenshots.
