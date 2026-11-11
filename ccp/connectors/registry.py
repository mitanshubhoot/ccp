"""Build a connector by name and source kind. The one place the choice is made."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ccp import credentials
from ccp.config import Settings
from ccp.connectors.aws.connector import AwsConnector
from ccp.connectors.base import Connection, Connector, ConnectorName, SourceKind
from ccp.connectors.github.client import GitHubLiveConnector
from ccp.connectors.github.fixture import GitHubFixtureConnector
from ccp.connectors.hris.fixture import HrisFixtureConnector
from ccp.connectors.manual.pdf_text import PdfLimits
from ccp.connectors.manual.uploads import ManualUploadConnector
from ccp.tenancy import TenantId

# the connectors a tenant pulls by default; aws and idp are pulled through a declared
# connection (A-121), once onboarded (A-139, A-141)
CONNECTOR_NAMES: tuple[ConnectorName, ...] = ("github", "hris", "manual")
ALL_CONNECTORS: tuple[ConnectorName, ...] = (*CONNECTOR_NAMES, "aws", "idp")
SOURCE_KINDS: tuple[SourceKind, ...] = ("fixture", "live")


def declared_scopes() -> dict[str, frozenset[str]]:
    """Every scope each connector can mark in a sync run, fixture or live (A-118); a test's
    required sources (A-123) must name one of these."""
    return {
        "github": frozenset(GitHubLiveConnector.scopes) | frozenset(GitHubFixtureConnector.scopes),
        "hris": frozenset(HrisFixtureConnector.scopes),
        "manual": frozenset(ManualUploadConnector.scopes),
        "aws": frozenset(AwsConnector.scopes),
    }


def pdf_limits(settings: Settings) -> PdfLimits:
    return PdfLimits(
        max_bytes=settings.ccp_pdf_max_bytes,
        max_pages=settings.ccp_pdf_max_pages,
        timeout_seconds=settings.ccp_pdf_timeout_seconds,
    )


def build_connector(
    name: ConnectorName,
    settings: Settings,
    source: SourceKind = "fixture",
    connection: Connection | None = None,
) -> Connector:
    """The connector for a name and source; with a connection (A-121), the one reading that
    connection's account, spending from that connection's rate bucket."""
    if name == "github":
        if source == "live":
            token = credentials.secret(settings, "github:read")
            if token is None:
                raise RuntimeError("GITHUB_TOKEN is not set; the live GitHub client needs it")
            if connection is None:
                return GitHubLiveConnector(token)
            return GitHubLiveConnector(
                token, org=connection.account, connection_id=connection.connection_id
            )
        return GitHubFixtureConnector(settings.ccp_fixtures_dir)
    if name == "hris":
        if source == "live":
            raise RuntimeError("the HRIS connector is fixture-only in this build (spec.md A-3)")
        return HrisFixtureConnector(settings.ccp_fixtures_dir)
    if name == "manual":
        if source == "live":

            def runtime_root(tenant_id: TenantId) -> Path:
                return settings.uploads_dir / tenant_id

            return ManualUploadConnector(runtime_root, pdf_limits(settings), "live")

        def fixture_root(tenant_id: TenantId) -> Path:
            return settings.ccp_fixtures_dir / tenant_id / "uploads"

        return ManualUploadConnector(fixture_root, pdf_limits(settings))
    if name == "aws":
        return _aws(settings, connection)
    raise ValueError(f"unknown connector {name!r}")


def _aws(settings: Settings, connection: Connection | None) -> Connector:
    """The AWS connector reads one onboarded account, through boto3's own credential chain
    (the task role) assuming the customer's role with CCP's external ID (A-139, A-140)."""
    if connection is None or not connection.params.get("role_arn"):
        raise RuntimeError("an AWS connection needs a verified onboarding (`ccp aws verify`)")
    import boto3

    make: Any = boto3.client  # one factory for every service the connector reads

    def client(credentials: dict[str, Any] | None, service: str) -> Any:
        if credentials is None:
            return make(service, region_name=settings.aws_region)
        return make(
            service,
            region_name=settings.aws_region,
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
        )

    return AwsConnector(
        connection.account, connection.params["role_arn"], connection.params["external_id"], client
    )
