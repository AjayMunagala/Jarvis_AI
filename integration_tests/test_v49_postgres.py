"""v4.9 live PostgreSQL validation: durable proposals, evaluations, owner-scoped snapshots, persisted prompts."""

from uuid import UUID

PERSONA = "prompts/assistant_persona.md"


def test_proposals_snapshots_and_prompt_changes_survive_a_restart(isolated_postgres):
    with isolated_postgres.client() as client:
        service = client.app.state.improvement_service
        before = service.get_target_content(PERSONA)
        content = before + "\nPrefer short, cited answers."
        proposal = client.post("/improvement/proposals", json={
            "target_name": PERSONA, "proposed_content": content, "rationale": "live"}).json()
        decide = f"/improvement/proposals/{proposal['id']}/decide"
        approval_id = client.post(decide, json={"decision": "approved"}).json()["approval"]["approval_id"]
        client.post(f"/approvals/{approval_id}/approve", json={})
        applied = client.post(decide, json={"decision": "approved"}, params={"approval_id": approval_id}).json()
        assert applied["status"] == "approved" and applied["snapshot_id"]
    with isolated_postgres.client() as restarted:
        stored = restarted.get(f"/improvement/proposals/{proposal['id']}").json()
        assert stored["status"] == "approved" and stored["evaluation"]["acceptable"] is True
        assert restarted.app.state.improvement_service.get_target_content(PERSONA) == content  # durable change
        store = restarted.app.state.improvement_service.store
        other = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        assert store.get_snapshot(other, UUID(stored["snapshot_id"])) is None  # owner-scoped
        restore = f"/improvement/proposals/{proposal['id']}/restore"
        approval_id = restarted.post(restore).json()["approval"]["approval_id"]
        restarted.post(f"/approvals/{approval_id}/approve", json={})
        assert restarted.post(restore, params={"approval_id": approval_id}).json()["status"] == "restored"
        assert restarted.app.state.improvement_service.get_target_content(PERSONA) == before
