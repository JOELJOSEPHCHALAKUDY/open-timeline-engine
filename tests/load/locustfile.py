import os
import uuid

from locust import HttpUser, between, task


class TCEUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self) -> None:
        token = os.getenv("TCE_LOAD_TOKEN", "local-dev-token")
        consumer = os.getenv("TCE_LOAD_CONSUMER", "load-test")
        role = os.getenv("TCE_LOAD_ROLE", "executor")
        workspace = os.getenv("TCE_LOAD_WORKSPACE", "personal")
        user = os.getenv("TCE_LOAD_USER", "load-tester")
        self.auth_headers = {
            "Authorization": f"Bearer {token}",
            "X-TCE-Consumer": consumer,
            "X-TCE-Role": role,
            "X-TCE-Workspace": workspace,
            "X-TCE-User": user,
        }
        self.session_id = os.getenv("TCE_LOAD_SESSION_ID", f"locust-{uuid.uuid4().hex[:8]}")
        self.behavior_pilot_enabled = os.getenv("TCE_LOAD_BEHAVIOR_PILOT", "false").lower() == "true"
        self.pilot_sequence = 0
        self.client.post(
            "/v1/takeover/step",
            json={
                "session_id": self.session_id,
                "message": "beru take over for load test",
                "activation_mode_default": "takeover",
                "allow_fallback": True,
            },
            headers=self.auth_headers,
            name="/v1/takeover/step",
        )

    @task
    def health(self):
        self.client.get("/v1/health")

    @task
    def search(self):
        self.client.post(
            "/v1/search",
            json={"query": "debug", "filters": {}, "k": 5},
            headers=self.auth_headers,
        )

    @task
    def continuity_search(self):
        self.client.post(
            "/v1/search",
            json={"query": "continue codex work on advisor runtime fallback", "filters": {}, "k": 5},
            headers=self.auth_headers,
            name="/v1/search continuity",
        )

    @task
    def behavior_prediction(self):
        self.client.post(
            "/v1/behavior/predict",
            json={
                "situation_type": "prioritization_needed",
                "situation_summary": "Choose scope for a production fix",
                "objective": "Fix the defect without broad regression",
                "candidate_choices": ["minimal verified fix", "broad refactor"],
            },
            headers=self.auth_headers,
        )

    @task
    def behavior_projection_pilot(self):
        if not self.behavior_pilot_enabled:
            return
        self.pilot_sequence += 1
        self.client.post(
            "/v1/behavior/projections/pilot/assign",
            json={
                "trial_key": f"{self.session_id}-pilot-{self.pilot_sequence}",
                "situation_type": "load_test",
                "situation_summary": "Choose a bounded load-test context",
                "objective": "Measure pilot assignment under mixed traffic",
                "candidate_choices": ["bounded context", "no context"],
            },
            headers=self.auth_headers,
        )

    @task
    def bundle(self):
        self.client.post(
            "/v1/context_bundle",
            json={"task": "fix failing migration", "app_context": {"domain": "coding"}, "constraints": {"k": 8}},
            headers=self.auth_headers,
        )

    @task
    def takeover_step(self):
        self.client.post(
            "/v1/takeover/step",
            json={
                "session_id": self.session_id,
                "message": "continue load test objective",
                "activation_mode_default": "takeover",
                "allow_fallback": True,
            },
            headers=self.auth_headers,
        )
