"""Closed vocabularies shared across layers (spec.md Layers 3, 6, 7).

This module imports nothing from the package so any layer may use it.
"""

from __future__ import annotations

from enum import StrEnum


class EvidenceType(StrEnum):
    policy_document = "policy_document"
    training_record = "training_record"
    access_review_report = "access_review_report"
    critical_systems_list = "critical_systems_list"
    repo_settings = "repo_settings"
    org_membership = "org_membership"
    repo_file_listing = "repo_file_listing"
    hr_roster = "hr_roster"
    identity_mfa_status = "identity_mfa_status"
    account_registry = "account_registry"


class AccountKind(StrEnum):
    human = "human"
    service = "service"
    shared = "shared"
    unknown = "unknown"


class EmploymentStatus(StrEnum):
    active = "active"
    terminated = "terminated"
    unknown = "unknown"


class GrantState(StrEnum):
    active = "active"
    revoked = "revoked"


class VerdictStatus(StrEnum):
    passed = "pass"
    failed = "fail"
    partial = "partial"
    needs_review = "needs_review"
