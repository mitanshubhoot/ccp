---
document_type: access_review_report
title: Q2 2026 User Access Review
issued_at: 2026-07-15T00:00:00Z
reviewer: Dana Okafor
approver: Priya Natarajan
---
# Q2 2026 User Access Review

Prepared by Dana Okafor (Security Lead). Approved by Priya Natarajan (CTO) on 2026-07-15.

## 1. Purpose

This review confirms that access to Acme's in-scope systems is limited to personnel with
a current business need, that privileged roles are justified, and that leavers have been
deprovisioned within the 24-hour service level.

## 2. Scope

The systems below were in scope for this quarter. Each row records the owner who
performed the review, the date, the number of accounts examined, and the result.

| System | Owner | Reviewed on | Accounts reviewed | Result |
|---|---|---|---|---|
| GitHub | Priya Natarajan | 2026-07-10 | 12 | 1 removal (bkim), 0 role changes |
| Okta | Dana Okafor | 2026-07-11 | 41 | no changes |
| AWS-Prod | Priya Natarajan | 2026-07-12 | 9 | 2 role reductions |
| Datadog | Lee Park | 2026-07-14 | 15 | no changes |

## 3. Method

Each system owner received the list of accounts and roles for their system, confirmed
or removed each one, and recorded the outcome in the compliance tracker. Removals were
executed the same day and verified by a second reviewer.

## 4. Findings

One dormant GitHub account (bkim) was removed. Two AWS-Prod administrators were reduced
to read-only. No shared account without a named owner was examined in this cycle.

## 5. Next review

Q3 2026, due 2026-10-15.
