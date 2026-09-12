---
title: Information Security Policy
version: 4
approved_by: Priya Natarajan
effective_date: 2026-01-10
---
# Acme Corp Information Security Policy (v4)

## 1. Purpose and scope

This policy applies to all Acme employees, contractors, and systems that store or process
Acme or customer data.

## 2. Access control

Access is granted on the principle of least privilege and reviewed quarterly for every
system tagged critical. Access for leavers is removed within 24 hours of the termination
effective time. Shared accounts are prohibited unless a named individual owner is
recorded in the account registry.

## 3. Authentication

Multi-factor authentication is required for every human identity on every system that
supports it. Service accounts use scoped credentials rotated at least annually.

## 4. Change management

Changes to production code require review by a second engineer through a pull request,
with administrators subject to the same rule.

## 5. Vulnerability management

Acme operates a coordinated vulnerability disclosure process. Externally reported
vulnerabilities are acknowledged within 5 business days and remediated according to
severity: critical within 7 days, high within 30 days, medium within 90 days.

## 6. Security awareness

All personnel complete security awareness training on hire and annually thereafter.

## 7. Secrets

Credentials and API keys are stored in the secrets manager and never committed to source
control. Repositories are scanned for secrets on every push.
