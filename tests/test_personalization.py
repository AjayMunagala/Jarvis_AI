"""v5.6 preference management, inference control, privacy and safety invariance."""

import itertools
from uuid import UUID

import pytest

from database import runner
from jarvis.personalization.models import CATALOG, Observation
from jarvis.personalization.service import MIN_EVIDENCE, PersonalizationError, PersonalizationService
from jarvis.personalization.stores import MemoryPreferenceStore, PostgresPreferenceStore
from jarvis.routing.models import ModelDescriptor, RegistryConfig, TaskRequirements
from jarvis.routing.registry import ModelRegistry, load_policies
from jarvis.routing.router import ModelRouter
from jarvis.security.policy import Capability, CapabilityPolicy

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def service():
    return PersonalizationService(MemoryPreferenceStore())


def descriptor(key, provider, locality):
    return ModelDescriptor(model_key=key, provider=provider, model_id=key, display_name=key, task_types=("general",),
                           input_modalities=("text",), locality=locality, capability_source="operator_declared")


ROUTER = ModelRouter(ModelRegistry(RegistryConfig(version=1, models=(
    descriptor("remote-a", "acme", "remote"), descriptor("remote-b", "beta", "remote"),
    descriptor("local-c", "local", "local")))), load_policies(), {"acme": object(), "beta": object(), "local": object()})


# ------------------------------------------------------------ management

def test_explicit_preferences_validate_against_the_catalog():
    prefs = service()
    assert prefs.set_preference(OWNER, "response_detail", "brief").provenance == "explicit"
    assert prefs.set_preference(OWNER, "language", "en-GB").value == "en-GB"
    assert prefs.set_preference(OWNER, "preferred_providers", ["beta", "beta", "acme"]).value == ("beta", "acme")
    for key, value in [("response_detail", "verbose"), ("language", "English!"), ("preferred_providers", []),
                       ("preferred_providers", ["Bad Name"]), ("allow_tools", True), ("tools_enabled", "true"),
                       ("approval_execution", "on")]:
        with pytest.raises(PersonalizationError) as raised:
            prefs.set_preference(OWNER, key, value)
        assert raised.value.status_code == 422
    with pytest.raises(PersonalizationError):
        prefs.set_preference(OWNER, "response_detail", "brief", scope="banking")
    assert set(CATALOG) == {"response_detail", "output_format", "language", "coding_explanation",
                            "preferred_providers", "latency_quality", "privacy_routing"}


def test_task_specific_preferences_override_global():
    prefs = service()
    prefs.set_preference(OWNER, "response_detail", "brief")
    prefs.set_preference(OWNER, "response_detail", "detailed", scope="coding")
    prefs.set_preference(OWNER, "output_format", "bullets")
    coding = prefs.resolve(OWNER, "coding")
    assert coding.values == {"response_detail": "detailed", "output_format": "bullets"}
    assert coding.sources["response_detail"] == "explicit:coding"
    assert prefs.resolve(OWNER, "summarization").values["response_detail"] == "brief"


def test_disable_enable_remove_and_reset():
    prefs = service()
    prefs.set_preference(OWNER, "output_format", "table")
    prefs.set_enabled(OWNER, False)
    disabled = prefs.resolve(OWNER)
    assert disabled.enabled is False and disabled.values == {}
    assert prefs.apply_to_presentation(disabled).instruction() == ""
    assert prefs.preferences(OWNER)  # disabling keeps data for inspection
    prefs.set_enabled(OWNER, True)
    assert prefs.resolve(OWNER).values == {"output_format": "table"}
    prefs.remove_preference(OWNER, "output_format")
    with pytest.raises(PersonalizationError) as raised:
        prefs.remove_preference(OWNER, "output_format")
    assert raised.value.status_code == 404
    prefs.set_preference(OWNER, "language", "fr")
    prefs.propose_from_observations(OWNER, [Observation(kind="requested_shorter")] * MIN_EVIDENCE)
    prefs.set_enabled(OWNER, False)
    assert prefs.reset(OWNER) == 2 and prefs.preferences(OWNER) == () and prefs.proposals(OWNER) == ()
    assert prefs.enabled(OWNER) is True


def test_privacy_isolation_between_owners():
    prefs = service()
    prefs.set_preference(OWNER, "privacy_routing", "local_only")
    proposal = prefs.propose_from_observations(OWNER, [Observation(kind="requested_table")] * 3)[0]
    assert prefs.resolve(OTHER).values == {} and prefs.proposals(OTHER) == ()
    with pytest.raises(PersonalizationError) as raised:
        prefs.decide(OTHER, proposal.proposal_id, accept=True)
    assert raised.value.status_code == 404
    assert prefs.reset(OTHER) == 0 and prefs.preferences(OWNER)


# ------------------------------------------------------------- inference

def test_inferred_preferences_are_proposals_until_confirmed():
    prefs = service()
    assert prefs.propose_from_observations(OWNER, [Observation(kind="requested_shorter")] * 2) == ()
    proposals = prefs.propose_from_observations(OWNER, [Observation(kind="requested_shorter")] * 3)
    assert len(proposals) == 1 and proposals[0].confidence < 1 and proposals[0].evidence_count == 3
    assert "response_detail" not in prefs.resolve(OWNER).values  # nothing changes yet
    assert prefs.propose_from_observations(OWNER, [Observation(kind="requested_shorter")] * 3) == ()  # no duplicates
    accepted = prefs.decide(OWNER, proposals[0].proposal_id, accept=True)
    assert accepted.status == "accepted"
    effective = prefs.resolve(OWNER)
    assert effective.values["response_detail"] == "brief" and effective.sources["response_detail"] == "inferred_confirmed:global"
    with pytest.raises(PersonalizationError) as raised:
        prefs.decide(OWNER, proposals[0].proposal_id, accept=False)
    assert raised.value.status_code == 409


def test_inference_never_overrides_explicit_choices_or_contrary_evidence():
    prefs = service()
    prefs.set_preference(OWNER, "output_format", "prose")
    assert prefs.propose_from_observations(OWNER, [Observation(kind="requested_bullets")] * 5) == ()
    mixed = [Observation(kind="requested_shorter")] * 3 + [Observation(kind="requested_longer")] * 3
    assert prefs.propose_from_observations(OWNER, mixed) == ()
    rejected = prefs.propose_from_observations(OWNER, [Observation(kind="requested_steps", scope="coding")] * 4)[0]
    prefs.decide(OWNER, rejected.proposal_id, accept=False)
    assert "coding_explanation" not in prefs.resolve(OWNER, "coding").values
    with pytest.raises(Exception):
        Observation(kind="mentioned_health_condition")  # only fixed, non-sensitive observation kinds exist


# --------------------------------------------------- safety invariance

def test_routing_preferences_only_tighten_privacy_and_reorder_eligible_models():
    prefs = service()
    base_policy = load_policies().policies[0]
    for privacy, providers, tradeoff, requested in itertools.product(
            ("local_only", "prefer_local", "default", None), (("beta",), ("local",), None),
            ("latency", "quality", "balanced", None), ("any", "local_only", "prefer_local")):
        prefs.reset(OWNER)
        for key, value in (("privacy_routing", privacy), ("preferred_providers", providers), ("latency_quality", tradeoff)):
            if value is not None:
                prefs.set_preference(OWNER, key, list(value) if isinstance(value, tuple) else value)
        requirements = TaskRequirements(privacy=requested)
        policy, adjusted = prefs.apply_to_routing(prefs.resolve(OWNER), base_policy, requirements)
        order = ("any", "prefer_local", "local_only")
        assert order.index(adjusted.privacy) >= order.index(requested)  # never loosened
        assert (policy.allow_remote, policy.allow_demo, policy.max_fallbacks) == (
            base_policy.allow_remote, base_policy.allow_demo, base_policy.max_fallbacks)
        base = ROUTER.decide(requirements, policy=base_policy)
        personalized = ROUTER.decide(adjusted, policy=policy)
        eligible = {item.model_key for item in base.ranked}
        assert {item.model_key for item in personalized.ranked} <= eligible  # never adds models
    private = load_policies().policies[-1]
    prefs.reset(OWNER)
    prefs.set_preference(OWNER, "preferred_providers", ["acme"])
    policy, _ = prefs.apply_to_routing(prefs.resolve(OWNER), private, TaskRequirements())
    assert ROUTER.decide(TaskRequirements(), policy=policy).selected == "local-c"  # private policy still wins


def test_preferred_provider_is_honoured_when_eligible():
    prefs = service()
    prefs.set_preference(OWNER, "preferred_providers", ["beta"])
    policy, requirements = prefs.apply_to_routing(prefs.resolve(OWNER), load_policies().policies[0], TaskRequirements())
    assert ROUTER.decide(requirements, policy=policy).selected == "remote-b"


def test_personalization_cannot_change_permissions():
    prefs = service()
    policy = CapabilityPolicy(OWNER)
    before = {capability: policy.allows(OWNER, capability) for capability in Capability}
    for key, values in CATALOG.items():
        sample = {"bcp47": "de", "provider_list": ["acme"]}.get(values, values[0] if isinstance(values, tuple) else None)
        prefs.set_preference(OWNER, key, sample)
    directives = prefs.apply_to_presentation(prefs.resolve(OWNER))
    assert {capability: policy.allows(OWNER, capability) for capability in Capability} == before
    assert policy.allows(OWNER, Capability.EXTERNAL_ACTION_EXECUTE) is False
    text = directives.instruction()
    assert "never change accuracy, safety rules or permissions" in text and "language: de" in text


# ---------------------------------------------------------------- storage

class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=()):
        self.log.append((" ".join(query.split()), params))
        return FakeCursor([])


def test_postgres_store_is_owner_scoped_and_failures_are_sanitized():
    log = []
    store = PostgresPreferenceStore("postgresql://x", connect=lambda url, connect_timeout: FakeConnection(log))
    prefs = PersonalizationService(store)
    prefs.set_preference(OWNER, "output_format", "table", scope="coding")
    assert prefs.resolve(OWNER).enabled is True and prefs.reset(OWNER) == 0
    for query, params in (entry for entry in log if "set_config" not in entry[0]):
        assert OWNER in params
    insert = next(entry for entry in log if entry[0].startswith("INSERT INTO jarvis_app.personalization_preferences"))
    assert insert[1][:3] == (OWNER, "output_format", "coding") and insert[1][3] == '"table"'

    def failing(url, connect_timeout):
        raise OSError("postgresql://user:" + "secret@host")  # synthetic, split to avoid scanner noise

    broken = PersonalizationService(PostgresPreferenceStore("postgresql://x", connect=failing))
    with pytest.raises(PersonalizationError) as raised:
        broken.resolve(OWNER)
    assert raised.value.status_code == 503 and "secret" not in raised.value.public_message


def test_migration_0056_is_discovered():
    found = {migration_id for migration_id, *_ in runner._migration_files(runner.MIGRATIONS_DIR)}
    assert "0056_personalization" in found
    sql = (runner.MIGRATIONS_DIR / "0056_personalization.sql").read_text(encoding="utf-8")
    assert "personalization_key_allowed" in sql and "tools" not in sql.lower().replace("no security settings", "")
