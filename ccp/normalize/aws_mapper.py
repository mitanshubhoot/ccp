"""AWS snapshots to canonical entities (spec.md A-140). Pure: the same event maps to the
same entities.

    aws.account.snapshot    the account as an asset: its own public access block and
                            whether a multi-region trail is logging (AWS-3)
    aws.s3.bucket.snapshot  a bucket as an asset, with the effective public access block
                            the connector computed from the account's and its own (AWS-1)
    aws.iam.user.snapshot   an IAM user as an account, keyed by its immutable user id, and
                            its grant on the account: console access and MFA (AWS-2)
"""

from __future__ import annotations

from typing import Any

from ccp.connectors.base import SourceEvent
from ccp.normalize.entities import AccessGrant, Asset, Entity, MappingResult, Person
from ccp.types import AccountKind, GrantState


def account_asset_id(account_id: str) -> str:
    return f"aws:asset:account:{account_id}"


def _base(event: SourceEvent, entity_id: str) -> dict[str, Any]:
    return {
        "tenant_id": event.tenant_id,
        "entity_id": entity_id,
        "source_connector": "aws",
        "source_event_id": event.source_event_id,
        "observed_at": event.source_ts,
    }


def map_aws_event(event: SourceEvent) -> MappingResult:
    p = event.payload
    kind = event.event_type
    out: list[Entity] = []
    if kind == "aws.account.snapshot":
        out.append(
            Asset(
                **_base(event, account_asset_id(p["account_id"])),
                name=p["account_id"],
                asset_type="aws_account",
                tags=("critical",),
                settings={
                    "account_block": p.get("account_block", {}),
                    "trails": p.get("trails", []),
                    "multi_region_trail_logging": bool(p.get("multi_region_trail_logging")),
                },
            )
        )
    elif kind == "aws.s3.bucket.snapshot":
        effective = p.get("effective_block") or {}
        out.append(
            Asset(
                **_base(event, f"aws:asset:s3:{p['account_id']}:{p['bucket']}"),
                name=p["bucket"],
                asset_type="s3_bucket",
                settings={
                    "account_id": p["account_id"],
                    "effective_block": effective,
                    "bucket_block": p.get("bucket_block", {}),
                    "policy_is_public": p.get("policy_is_public"),
                    # public access is blocked when all four flags hold, whoever set them
                    "public_access_blocked": bool(effective)
                    and all(effective.values())
                    and p.get("policy_is_public") is not True,
                    "read_from": p.get("from"),
                },
            )
        )
    elif kind == "aws.iam.user.snapshot":
        person = f"aws:person:{p['user_id']}"
        out.append(
            Person(
                **_base(event, person),
                display_name=p["user_name"],
                handles={"aws_iam": p["user_name"]},
                account_kind=AccountKind.unknown,
            )
        )
        out.append(
            AccessGrant(
                **_base(event, f"aws:grant:{p['user_id']}:{p['account_id']}"),
                person_entity_id=person,
                asset_entity_id=account_asset_id(p["account_id"]),
                role="iam_user",
                state=GrantState.active,
                state_effective_at=event.source_ts,
                attributes={
                    "source": "aws_iam",
                    "user_name": p["user_name"],
                    "arn": p["arn"],
                    "console_access": bool(p.get("console_access")),
                    "mfa_enabled": bool(p.get("mfa_enabled")),
                },
            )
        )
    return MappingResult(entities=tuple(out))
