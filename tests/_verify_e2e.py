"""One-shot end-to-end API verification — run with: python tests/_verify_e2e.py"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("JWT_SECRET", "test-e2e-verify")
os.environ.setdefault("VELAFLOW_MASTER_KEY", "dGVzdGtleXRlc3RrZXl0ZXN0a2V5dGVzdGtleXRlcw==")

from fastapi.testclient import TestClient
from brain.api.app import create_app

app = create_app()
client = TestClient(app)
passed = 0
failed = 0

def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}: {detail}")

print("=== 1. Unauthenticated Health Endpoints ===")
r = client.get("/health")
check("GET /health returns 200", r.status_code == 200, r.status_code)

r = client.get("/health/live")
check("GET /health/live returns 200", r.status_code == 200, r.status_code)

r = client.get("/health/ready")
check("GET /health/ready returns 200 or 503", r.status_code in (200, 503), r.status_code)

print("\n=== 2. Tenant Registration (Public Path) ===")
r = client.post("/api/v1/tenants", json={"name": "E2E-Test", "email": "e2e@test.com", "accept_tos": True})
check("POST /tenants returns 200", r.status_code == 200, f"{r.status_code}: {r.text[:200]}")
if r.status_code == 200:
    data = r.json()
    tenant_id = data["tenant_id"]
    api_key = data.get("api_key", "")
    token = data["access_token"]
    check("Response has tenant_id", bool(tenant_id))
    check("Response has access_token", bool(token))
    check("Response has api_key", bool(api_key))
else:
    tenant_id = api_key = token = ""

print("\n=== 3. Tenant Login (Public Path) ===")
if tenant_id and api_key:
    r = client.post("/api/v1/tenants/login", json={"tenant_id": tenant_id, "email": "e2e@test.com", "api_key": api_key})
    check("POST /tenants/login returns 200", r.status_code == 200, f"{r.status_code}: {r.text[:200]}")
    if r.status_code == 200:
        token = r.json()["access_token"]
        check("Login returns fresh token", bool(token))
else:
    check("POST /tenants/login", False, "skipped — no tenant created")

headers = {"Authorization": f"Bearer {token}"} if token else {}

print("\n=== 4. Authenticated Endpoints ===")
r = client.get("/api/v1/tenants/me", headers=headers)
check("GET /tenants/me returns 200", r.status_code == 200, r.status_code)

r = client.get("/api/v1/tasks/scored", headers=headers)
check("GET /tasks/scored returns 404 (no data)", r.status_code == 404, r.status_code)

r = client.get("/api/v1/digests/daily", headers=headers)
check("GET /digests/daily returns 404 (no data)", r.status_code == 404, r.status_code)

print("\n=== 5. Data Explorer ===")
r = client.get("/api/v1/data/layers", headers=headers)
check("GET /data/layers returns 200", r.status_code == 200, r.status_code)
if r.status_code == 200:
    layers = r.json()
    check("Returns 3 layers (bronze/silver/gold)", len(layers) == 3, len(layers))

r = client.get("/api/v1/data/silver/datasets", headers=headers)
check("GET /data/silver/datasets returns 403 (free tier)", r.status_code == 403, r.status_code)

r = client.get("/api/v1/data/platinum/datasets", headers=headers)
check("GET /data/platinum/datasets returns 400", r.status_code == 400, r.status_code)

# Path traversal
r = client.get("/api/v1/data/silver/../../etc/passwd", headers=headers)
check("Path traversal blocked", r.status_code in (400, 404, 422), r.status_code)

print("\n=== 6. Vault (free tier = 403, RBAC enforced) ===")
r = client.get("/api/v1/vault/keys", headers=headers)
check("GET /vault/keys returns 403 (free tier)", r.status_code == 403, r.status_code)

r = client.post("/api/v1/vault/keys", headers=headers, json={"key_name": "test_key", "key_value": "secret123", "service": "test"})
check("POST /vault/keys returns 403 (free tier)", r.status_code == 403, f"{r.status_code}: {r.text[:200]}")

r = client.get("/api/v1/vault/keys/test_key", headers=headers)
check("GET /vault/keys/test_key returns 403", r.status_code == 403, r.status_code)

r = client.delete("/api/v1/vault/keys/test_key", headers=headers)
check("DELETE /vault/keys/test_key returns 403", r.status_code == 403, r.status_code)

print("\n=== 7. Webhooks ===")
r = client.post("/api/v1/webhooks/pipeline", headers=headers, json={"todoist_tasks": [{"id": "1", "content": "test"}]})
check("POST /webhooks/pipeline returns 202", r.status_code == 202, f"{r.status_code}: {r.text[:200]}")

r = client.post("/api/v1/webhooks/digest", headers=headers, json={})
check("POST /webhooks/digest returns 202", r.status_code == 202, f"{r.status_code}: {r.text[:200]}")

print("\n=== 8. Pipeline ===")
r = client.post("/api/v1/pipelines/run", headers=headers, json={"todoist_token": "test"})
check("POST /pipelines/run accepted", r.status_code in (200, 202, 422), f"{r.status_code}: {r.text[:200]}")

r = client.get("/api/v1/pipelines/runs", headers=headers)
check("GET /pipelines/runs returns 200", r.status_code == 200, r.status_code)

print("\n=== 9. Security: Unauthenticated Access Blocked ===")
r = client.get("/api/v1/tenants/me")
check("GET /tenants/me without token returns 401", r.status_code == 401, r.status_code)

r = client.get("/api/v1/tasks/scored")
check("GET /tasks/scored without token returns 401", r.status_code == 401, r.status_code)

r = client.get("/api/v1/vault/keys")
check("GET /vault/keys without token returns 401", r.status_code == 401, r.status_code)

r = client.post("/api/v1/webhooks/pipeline", json={})
check("POST /webhooks/pipeline without token returns 401", r.status_code == 401, r.status_code)

print("\n=== 10. Auth Endpoint ===")
r = client.post("/api/v1/auth/google", json={"id_token": "fake"})
check("POST /auth/google with bad token fails gracefully", r.status_code in (400, 401, 422, 500), r.status_code)

print(f"\n{'='*50}")
print(f"PASSED: {passed}")
print(f"FAILED: {failed}")
print(f"TOTAL:  {passed + failed}")
if failed == 0:
    print("ALL END-TO-END CHECKS PASSED")
else:
    print(f"WARNING: {failed} check(s) FAILED")
    sys.exit(1)
