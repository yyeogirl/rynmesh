from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from rynmesh.provider_gateway import create_app as create_gateway_app
from rynmesh.provider_gateway import create_peer_proxy_app
from rynmesh.video_package.service import VideoGenerationService, create_app


class FakeVideoBackend:
    model_id = "fake-video"

    def health(self):
        return {"ok": True, "backend": "fake", "model": self.model_id}

    def generate(self, *, output_path: Path, **kwargs):
        del kwargs
        output_path.write_bytes(b"0" * 2048)
        return {"model": self.model_id, "sha256": "fake", "bytes": 2048, "duration_seconds": 1.0}


def _client(tmp_path):
    service = VideoGenerationService(backend=FakeVideoBackend(), output_dir=tmp_path)
    return TestClient(create_app(service=service, api_token="test-secret"))


def test_video_provider_auth_and_generation(tmp_path):
    client = _client(tmp_path)
    assert client.get("/health").status_code == 200
    assert client.get("/v1/models").status_code == 404
    headers = {"Authorization": "Bearer test-secret"}
    response = client.post(
        "/v1/videos/generations",
        headers=headers,
        json={"prompt": "a blue cube rotates", "width": 128, "height": 128, "num_frames": 5, "num_inference_steps": 1},
    )
    assert response.status_code == 202
    job_id = response.json()["id"]
    for _ in range(100):
        status = client.get(f"/v1/videos/generations/{job_id}", headers=headers).json()
        if status["state"] == "succeeded":
            break
    assert status["state"] == "succeeded"
    assert "prompt" not in status
    content = client.get(f"/v1/videos/generations/{job_id}/content", headers=headers)
    assert content.status_code == 200
    assert len(content.content) == 2048


def test_video_provider_validates_wan_dimensions(tmp_path):
    client = _client(tmp_path)
    headers = {"Authorization": "Bearer test-secret"}
    response = client.post(
        "/v1/videos/generations",
        headers=headers,
        json={"prompt": "x", "width": 130, "height": 128, "num_frames": 5},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "width_and_height_must_be_multiples_of_16"


def test_gateway_mounts_registry_and_video_on_one_port(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("RYNMESH_VIDEO_OUTPUT_DIR", str(tmp_path / "videos"))
    monkeypatch.setenv("RYNMESH_VIDEO_MODEL_PATH", str(tmp_path / "missing-model"))
    monkeypatch.setenv("RYNMESH_VIDEO_API_TOKEN", "test-secret")
    client = TestClient(create_gateway_app())
    assert client.get("/health").json()["kind"] == "rynmesh-registry"
    video_health = client.get("/video/health")
    assert video_health.status_code == 200
    assert video_health.json()["status"] == "degraded"
    assert client.get("/video/v1/models").status_code == 404
    assert client.get(
        "/video/v1/models", headers={"Authorization": "Bearer test-secret"}
    ).json()["data"][0]["id"] == "Wan2.1-T2V-1.3B"


def test_gateway_peer_proxy_forwards_only_signed_task_surface():
    seen = []

    def forward(body):
        seen.append(body)
        return 200, b'{"state":"succeeded"}', "application/json"

    client = TestClient(create_peer_proxy_app(forward=forward))
    response = client.post("/api/peer/llm/tasks", json={"alg": "ed25519"})
    assert response.json()["state"] == "succeeded"
    assert seen == [b'{"alg":"ed25519"}']
    assert client.get("/api/peer/llm/tasks").status_code == 405
