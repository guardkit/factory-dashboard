"""Gateway / llama-swap / NATS health polls (P6).

STAGE-OWNED: built at S2 (D1). Load-neutral endpoints ONLY, poll <=1/30s (fence 5): LiteLLM
GET /v1/models, llama-swap GET /health + /v1/models, NATS :8222/healthz. LiteLLM /health is
FORBIDDEN (it fires real completions).
"""
