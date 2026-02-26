import os

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
    def bundle(self):
        self.client.post(
            "/v1/context_bundle",
            json={"task": "fix failing migration", "app_context": {"domain": "coding"}, "constraints": {"k": 8}},
            headers=self.auth_headers,
        )
