---
document_type: critical_systems_list
title: Critical Systems Inventory
issued_at: 2026-08-03T00:00:00Z
owner: Dana Okafor
---
# Critical Systems Inventory

Systems tagged `critical` in the Acme asset inventory as of 2026-08-03. Every system on
this list must be covered by the quarterly user access review.

| System | Tag | Business owner | Why critical |
|---|---|---|---|
| GitHub | critical | Priya Natarajan | Source code and deployment pipeline |
| Okta | critical, tier0 | Dana Okafor | Identity provider for every other system |
| AWS-Prod | critical | Priya Natarajan | Production workloads and customer data |
| Payroll-SaaS | critical | Marco Ribeiro | Employee personal and financial data |
| Datadog | critical | Lee Park | Production telemetry and secrets in logs |

Systems not tagged critical (Slack, Jira) are reviewed annually.
