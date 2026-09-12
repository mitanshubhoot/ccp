"""Phase 4 gate: every tabled fixture pair's computed score lands in its tabled route band.

Runs entirely in memory: fixtures -> mappers -> merged persons -> blocking -> scoring. If
config/identity.yaml or the fixtures drift, this fails before any database is touched.
"""

from __future__ import annotations

import pytest

from ccp.config import Settings
from ccp.connectors.registry import build_connector
from ccp.identity.blocking import block_keys, candidate_pairs, name_key, normalize_email
from ccp.identity.config import IdentityConfig
from ccp.identity.scoring import evaluate, gate
from ccp.normalize.consumer import map_event
from ccp.normalize.entities import Person
from ccp.normalize.merge import merge_persons
from ccp.tenancy import TenantContext
from ccp.types import AccountKind


def merged_persons(settings: Settings, tenant: str) -> dict[str, Person]:
    observations: list[Person] = []
    for connector in ("github", "hris"):
        conn = build_connector(connector, settings, "fixture")  # type: ignore[arg-type]
        for event in conn.pull(TenantContext.for_id(settings, tenant)):
            observations.extend(map_event(event).of(Person))
    return {p.entity_id: p for p in merge_persons(observations)}


@pytest.fixture(scope="module")
def config() -> IdentityConfig:
    return IdentityConfig.load()


@pytest.fixture(scope="module")
def acme(base_settings: Settings) -> dict[str, Person]:
    return merged_persons(base_settings, "acme")


@pytest.fixture(scope="module")
def globex(base_settings: Settings) -> dict[str, Person]:
    return merged_persons(base_settings, "globex")


def _pair(persons: dict[str, Person], left: str, right: str) -> tuple[Person, Person]:
    return persons[left], persons[right]


TABLE = [
    # tenant, left, right, expected route, expected score band (inclusive bounds) or None when gated
    ("acme", "hris:person:E1042", "github:person:U_kgDOAcme1042", "auto_merge", (0.85, 1.0)),
    ("acme", "hris:person:E1017", "github:person:U_kgDOAcme1017", "review", (0.4001, 0.8499)),
    ("acme", "hris:person:E1023", "github:person:U_kgDOAcmeBot01", "blocked", None),
    ("acme", "hris:account:ops-shared", "github:person:U_kgDOAcmeShar1", "blocked", None),
    ("globex", "hris:person:G2001", "github:person:U_kgDOGlobex2001", "auto_merge", (0.85, 1.0)),
]


@pytest.mark.parametrize(("tenant", "left_id", "right_id", "expected_route", "band"), TABLE)
def test_tabled_pair_lands_in_its_band(
    request: pytest.FixtureRequest,
    config: IdentityConfig,
    tenant: str,
    left_id: str,
    right_id: str,
    expected_route: str,
    band: tuple[float, float] | None,
) -> None:
    persons: dict[str, Person] = request.getfixturevalue(tenant)
    left, right = _pair(persons, left_id, right_id)
    scored = evaluate(left, right, config)
    assert scored.route == expected_route, scored
    if band is None:
        assert scored.score is None and scored.gate_result != "passed"
    else:
        assert scored.score is not None and band[0] <= scored.score <= band[1], scored


def test_tabled_pairs_are_actually_generated_by_blocking(
    acme: dict[str, Person], globex: dict[str, Person]
) -> None:
    acme_pairs = {(p.left.entity_id, p.right.entity_id) for p in candidate_pairs(acme.values())}
    for _, left, right, _, _ in TABLE[:4]:
        assert tuple(sorted((left, right))) in acme_pairs, (left, right)
    globex_pairs = {(p.left.entity_id, p.right.entity_id) for p in candidate_pairs(globex.values())}
    assert ("github:person:U_kgDOGlobex2001", "hris:person:G2001") in globex_pairs


def test_gate_blocks_kind_mismatch_regardless_of_name_similarity(acme: dict[str, Person]) -> None:
    chris, bot = _pair(acme, "hris:person:E1023", "github:person:U_kgDOAcmeBot01")
    assert chris.account_kind is AccountKind.human and bot.account_kind is AccountKind.service
    assert gate(chris, bot) == "blocked_kind_mismatch"
    assert evaluate(chris, bot, IdentityConfig.load()).features == {}  # nothing was scored


def test_gate_blocks_shared_accounts(acme: dict[str, Person]) -> None:
    shared, gh = _pair(acme, "hris:account:ops-shared", "github:person:U_kgDOAcmeShar1")
    assert gate(shared, gh) == "blocked_shared_account"


def test_handle_plus_name_alone_never_merges(
    acme: dict[str, Person], config: IdentityConfig
) -> None:
    jane_hr, jane_gh = _pair(acme, "hris:person:E1042", "github:person:U_kgDOAcme1042")
    jane_gh_no_email = jane_gh.model_copy(update={"emails": ()})
    scored = evaluate(jane_hr, jane_gh_no_email, config)
    assert scored.route == "review" and scored.score is not None and scored.score < 0.85


def test_unknown_kind_caps_at_review(acme: dict[str, Person], config: IdentityConfig) -> None:
    jane_hr, jane_gh = _pair(acme, "hris:person:E1042", "github:person:U_kgDOAcme1042")
    unknown = jane_gh.model_copy(update={"account_kind": AccountKind.unknown})
    assert evaluate(jane_hr, unknown, config).route == "review"


def test_pairs_never_cross_tenants(acme: dict[str, Person], globex: dict[str, Person]) -> None:
    both = list(acme.values()) + list(globex.values())
    for pair in candidate_pairs(both):
        assert pair.left.tenant_id == pair.right.tenant_id
    janes = [p for p in both if p.display_name == "Jane Smith"]
    assert len(janes) == 4  # two per tenant, one per connector


def test_block_key_normalization() -> None:
    assert normalize_email("Jane.Smith+ci@Globex.Example") == "janesmith@globex.example"
    assert name_key("Dana Okafor") == "dokafor" and name_key("D. Okafor") == "dokafor"
    assert name_key("Madonna") is None


def test_every_acme_human_with_a_verified_email_merges(
    acme: dict[str, Person], config: IdentityConfig
) -> None:
    merges = {
        tuple(sorted((p.left.entity_id, p.right.entity_id)))
        for p in candidate_pairs(acme.values())
        if evaluate(p.left, p.right, config).route == "auto_merge"
    }
    for hr, gh in [
        ("E1001", "U_kgDOAcme1001"),
        ("E1031", "U_kgDOAcme1031"),
        ("E1044", "U_kgDOAcme1044"),
        ("E1050", "U_kgDOAcme1050"),
        ("E1058", "U_kgDOAcme1058"),
    ]:
        assert (f"github:person:{gh}", f"hris:person:{hr}") in merges


def test_block_keys_include_handles_and_locals(acme: dict[str, Person]) -> None:
    keys = block_keys(acme["github:person:U_kgDOAcme1017"])
    assert "handle:dokafor-dev" in keys and "name:dokafor" in keys
