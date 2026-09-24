"""Privacy-preserving visitor linking for paid-generation trial quotas."""
import hashlib
import re
import time


def visitor_keys(request) -> list[str]:
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    host = forwarded or (request.client.host if request.client else "unknown")
    keys = ["net:" + hashlib.sha256(host.encode("utf-8")).hexdigest()[:32]]
    visitor = request.headers.get("x-visitor-id", "").strip().lower()
    if re.fullmatch(r"[a-z0-9_-]{20,80}", visitor):
        keys.insert(0, "dev:" + hashlib.sha256(visitor.encode("utf-8")).hexdigest()[:32])
    return keys


def quote(state: dict, keys: list[str], model_cost: float, free_limit: float,
          daily_budget: float, service_fee: float) -> dict:
    today = time.strftime("%Y-%m-%d")
    if state.get("date") != today:
        state["date"], state["daily_cny"] = today, 0.0
    clients = state.setdefault("clients", {})
    used = round(max([0.0] + [float(clients.get(key, 0.0)) for key in keys]), 2)
    daily = round(float(state.get("daily_cny") or 0.0), 2)
    credits = state.get("credits") or {}
    paid_credits = max([0] + [int(credits.get(key, 0)) for key in keys])
    user_allowed = used + model_cost <= free_limit + 1e-9
    global_allowed = daily + model_cost <= daily_budget + 1e-9
    return {
        "allowed": bool((user_allowed and global_allowed) or paid_credits > 0),
        "reason": "user_limit" if not user_allowed else ("daily_budget" if not global_allowed else ""),
        "used_cny": used, "remaining_cny": round(max(0.0, free_limit - used), 2),
        "free_limit_cny": round(free_limit, 2), "model_cost_cny": model_cost,
        "service_fee_cny": round(service_fee, 2),
        "payable_cny": round(model_cost + service_fee, 2), "paid_credits": paid_credits,
    }


def reserve(state: dict, keys: list[str], model_cost: float, free_limit: float,
            daily_budget: float, service_fee: float) -> dict:
    result = quote(state, keys, model_cost, free_limit, daily_budget, service_fee)
    free_allowed = result["used_cny"] + model_cost <= free_limit + 1e-9 and \
        float(state.get("daily_cny") or 0.0) + model_cost <= daily_budget + 1e-9
    if not free_allowed and result["paid_credits"] <= 0:
        return {**result, "reservation": "denied"}
    if free_allowed:
        linked_total = round(result["used_cny"] + model_cost, 2)
        clients = state.setdefault("clients", {})
        for key in keys:
            clients[key] = linked_total
        state["daily_cny"] = round(float(state.get("daily_cny") or 0.0) + model_cost, 2)
        reservation = "free"
    else:
        credits = state.setdefault("credits", {})
        credit_key = max(keys, key=lambda key: int(credits.get(key, 0)))
        credits[credit_key] = max(0, int(credits.get(credit_key, 0)) - 1)
        reservation = "credit:" + credit_key
    return {**quote(state, keys, model_cost, free_limit, daily_budget, service_fee),
            "reservation": reservation}


def release(state: dict, keys: list[str], model_cost: float, reservation: str) -> None:
    if reservation.startswith("credit:"):
        credit_key = reservation.split(":", 1)[1]
        credits = state.setdefault("credits", {})
        credits[credit_key] = int(credits.get(credit_key, 0)) + 1
        return
    clients = state.setdefault("clients", {})
    linked_total = round(max(0.0, max([0.0] + [float(clients.get(key, 0.0)) for key in keys]) - model_cost), 2)
    for key in keys:
        clients[key] = linked_total
    state["daily_cny"] = round(max(0.0, float(state.get("daily_cny") or 0.0) - model_cost), 2)
