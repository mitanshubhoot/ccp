"""Onboarding an AWS account (spec.md A-139).

1. CCP generates an external ID for the account: random, unguessable, one per account, never
   chosen by the customer, so another CCP customer cannot name this account's role and have
   CCP assume it for them (the confused deputy).
2. The customer deploys infra/cloudformation/ccp-read-role.yaml through the quick-create link
   (or a service-managed StackSet across an organization) and gives CCP the role's ARN.
3. CCP verifies before it reads anything, and again every day:
     - the role can be assumed with the external ID;
     - it cannot be assumed without it (else the trust policy does not require it);
     - ReadOnlyAccess is not attached (it reads data, not just configuration);
     - no write action and no secret-bearing read is allowed: each is simulated against the
       role's policies and must come back denied.
   Any one failing refuses the account, with the reason.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode

ACCOUNT_ID_LENGTH = 12
FORBIDDEN_POLICIES = frozenset({"arn:aws:iam::aws:policy/ReadOnlyAccess"})
# A sample of what the role must never do: writes across the services CCP reads, and the
# reads that would hand CCP a secret or customer data.
MUST_BE_DENIED = (
    "s3:PutObject",
    "s3:DeleteBucket",
    "s3:PutBucketPolicy",
    "iam:CreateUser",
    "iam:AttachRolePolicy",
    "ec2:RunInstances",
    "ec2:AuthorizeSecurityGroupIngress",
    "kms:ScheduleKeyDeletion",
    "cloudtrail:StopLogging",
    "config:DeleteConfigurationRecorder",
    "secretsmanager:GetSecretValue",
    "ssm:GetParameter",
    "kms:Decrypt",
    "s3:GetObject",
)


class Sts(Protocol):
    def assume_role(self, **kwargs: Any) -> dict[str, Any]: ...


class Iam(Protocol):
    def list_attached_role_policies(self, **kwargs: Any) -> dict[str, Any]: ...

    def simulate_principal_policy(self, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Verification:
    account_id: str
    role_arn: str
    findings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.findings


def new_external_id() -> str:
    return "ccp-" + secrets.token_urlsafe(32)


def check_account_id(account_id: str) -> str:
    if len(account_id) != ACCOUNT_ID_LENGTH or not account_id.isdigit():
        raise ValueError(f"{account_id!r} is not a 12-digit AWS account id")
    return account_id


def quick_create_url(
    template_url: str, external_id: str, ccp_principal: str, region: str = "us-east-1"
) -> str:
    query = urlencode(
        {
            "templateURL": template_url,
            "stackName": "CCPReadOnly",
            "param_ExternalId": external_id,
            "param_CcpPrincipal": ccp_principal,
        }
    )
    return (
        f"https://{region}.console.aws.amazon.com/cloudformation/home?region={region}"
        f"#/stacks/quickcreate?{query}"
    )


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code", type(exc).__name__))
    return type(exc).__name__


def verify(
    account_id: str,
    role_arn: str,
    external_id: str,
    sts: Sts,
    iam_for: Any,
) -> Verification:
    """Check the role as onboarding requires. `iam_for(credentials)` builds an IAM client
    from the assumed session's credentials."""
    findings: list[str] = []
    if f":{account_id}:" not in role_arn:
        findings.append(f"the role {role_arn} is not in account {account_id}")
        return Verification(account_id, role_arn, tuple(findings))
    try:
        assumed = sts.assume_role(
            RoleArn=role_arn, RoleSessionName="ccp-onboarding", ExternalId=external_id
        )
    except Exception as exc:
        findings.append(f"the role cannot be assumed with CCP's external ID ({_error_code(exc)})")
        return Verification(account_id, role_arn, tuple(findings))
    try:
        sts.assume_role(RoleArn=role_arn, RoleSessionName="ccp-onboarding-no-id")
    except Exception as exc:
        if _error_code(exc) not in ("AccessDenied", "AccessDeniedException"):
            findings.append(f"assuming without the external ID failed oddly ({_error_code(exc)})")
    else:
        findings.append(
            "the role can be assumed without the external ID: its trust policy must require"
            " sts:ExternalId"
        )
    iam = iam_for(assumed["Credentials"])
    role_name = role_arn.rsplit("/", 1)[-1]
    attached = iam.list_attached_role_policies(RoleName=role_name).get("AttachedPolicies", [])
    for policy in attached:
        if policy.get("PolicyArn") in FORBIDDEN_POLICIES:
            findings.append(
                f"{policy['PolicyArn']} is attached: it reads data, not just configuration"
            )
    simulated = iam.simulate_principal_policy(
        PolicySourceArn=role_arn, ActionNames=list(MUST_BE_DENIED)
    )
    for result in simulated.get("EvaluationResults", []):
        if result.get("EvalDecision") == "allowed":
            findings.append(f"the role may {result['EvalActionName']}: it must not")
    return Verification(account_id, role_arn, tuple(findings))
