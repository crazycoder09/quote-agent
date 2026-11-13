"""
memory.py
---------
All of Quote Agent's memory, built on LangGraph's persistence layer:

    Short-term (per conversation)  -> SqliteSaver checkpointer
        every message, tool call and result in a chat, restored when the chat
        is reopened (storage/conversations.db)

    Long-term (across conversations) -> SqliteStore (storage/long_term_memory.db)
        ("customers",)          customer profiles: contact details + facts/preferences
        ("quotes", <customer>)  every quote created for that customer
        ("learned_rules",)      business rules staff taught in chat (procedural memory)
        ("handoffs",)           chats the agent handed to a human designer ("Needs you")
        ("threads",)            chat list + which customer each chat belongs to

The graph's `remember` node uses the LLM to pull new facts out of every turn
into the store, so details a customer gives are not lost in later chats.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.store.sqlite import SqliteStore

from agent.agents import CHECKPOINT_DB_PATH, STORAGE_DIR, STORE_DB_PATH

CUSTOMERS = ("customers",)
LEARNED_RULES = ("learned_rules",)
THREADS = ("threads",)
HANDOFFS = ("handoffs",)

_lock = threading.Lock()
# Read-modify-write of one record (thread, customer) must be atomic: several users' sessions and the
# background memory writer run in parallel threads of the same process.
_rw_lock = threading.RLock()
# Quote numbers are allocated + saved under one lock, so two quotes made at the same moment never share a number.
quote_lock = threading.Lock()
_singletons: Dict[str, Any] = {}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def get_checkpointer() -> SqliteSaver:
    with _lock:
        if "saver" not in _singletons:
            STORAGE_DIR.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(CHECKPOINT_DB_PATH), check_same_thread=False)
            saver = SqliteSaver(conn)
            saver.setup()
            _singletons["saver"] = saver
        return _singletons["saver"]


def get_store() -> SqliteStore:
    with _lock:
        if "store" not in _singletons:
            STORAGE_DIR.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(STORE_DB_PATH), check_same_thread=False, isolation_level=None)
            store = SqliteStore(conn)
            store.setup()
            _singletons["store"] = store
        return _singletons["store"]


def customer_key(phone: Optional[str]) -> Optional[str]:
    """Customers are keyed by phone digits (last 10), so '+1 (407) 555-0100' == '4075550100'."""
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 7 else None


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

def get_customer(key: str) -> Optional[Dict[str, Any]]:
    item = get_store().get(CUSTOMERS, key)
    return item.value if item else None


def upsert_customer(phone: str, name: Optional[str] = None, email: Optional[str] = None,
                    address: Optional[str] = None, facts: Optional[List[str]] = None) -> Optional[str]:
    key = customer_key(phone)
    if not key:
        return None
    with _rw_lock:
        return _upsert_customer(key, phone, name, email, address, facts)


def _upsert_customer(key: str, phone: str, name: Optional[str], email: Optional[str], address: Optional[str],
                     facts: Optional[List[str]]) -> str:
    current = get_customer(key) or {"phone": phone, "facts": [], "created_at": now()}
    for field, value in (("name", name), ("email", email), ("address", address), ("phone", phone)):
        if value:
            current[field] = value
    known = {f.lower() for f in current.get("facts", [])}
    for fact in facts or []:
        if fact and fact.lower() not in known:
            current.setdefault("facts", []).append(fact)
            known.add(fact.lower())
    current["updated_at"] = now()
    get_store().put(CUSTOMERS, key, current)
    return key


def find_customers_by_name(name: str, limit: int = 5) -> List[Dict[str, Any]]:
    needle = name.strip().lower()
    out = []
    for item in get_store().search(CUSTOMERS, limit=1000):
        if needle and needle in str(item.value.get("name", "")).lower():
            out.append({"customer_key": item.key, **item.value})
            if len(out) >= limit:
                break
    return out


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------

def save_quote(customer: str, quote: Dict[str, Any]) -> None:
    get_store().put(("quotes", customer), quote["quote_number"], quote)
    get_store().put(("quote_index",), quote["quote_number"], {"customer": customer})


def get_quote(quote_number: str) -> Optional[Dict[str, Any]]:
    idx = get_store().get(("quote_index",), quote_number.strip().upper())
    if not idx:
        return None
    item = get_store().get(("quotes", idx.value["customer"]), quote_number.strip().upper())
    return item.value if item else None


def quotes_for_customer(customer: str, limit: int = 5) -> List[Dict[str, Any]]:
    items = get_store().search(("quotes", customer), limit=100)
    items = sorted(items, key=lambda i: i.value.get("created_at", ""), reverse=True)[:limit]
    return [i.value for i in items]


def latest_quote_for_thread(thread_id: str) -> Optional[Dict[str, Any]]:
    """The most recent quote created in this conversation (the one 'add/remove/change' refers to)."""
    customer = get_thread(thread_id).get("customer")
    if not customer:
        return None
    for q in quotes_for_customer(customer, limit=50):
        if q.get("thread_id") == thread_id:
            return q
    return None


# ---------------------------------------------------------------------------
# Handoffs (chats that need a human designer)
# ---------------------------------------------------------------------------

def add_handoff(thread_id: str, reason: str, summary: str, urgency: str) -> None:
    get_store().put(HANDOFFS, thread_id, {"reason": reason, "summary": summary, "urgency": urgency,
                                          "status": "open", "created_at": now()})
    update_thread(thread_id, status="Needs you")


def open_handoffs() -> List[Dict[str, Any]]:
    items = get_store().search(HANDOFFS, limit=500)
    rows = [{"thread_id": i.key, **i.value} for i in items if i.value.get("status") == "open"]
    return sorted(rows, key=lambda r: r["created_at"], reverse=True)


def resolve_handoff(thread_id: str) -> None:
    item = get_store().get(HANDOFFS, thread_id)
    if item:
        get_store().put(HANDOFFS, thread_id, {**item.value, "status": "resolved", "resolved_at": now()})
    update_thread(thread_id, status="Resolved")


def next_quote_number(prefix: str) -> str:
    count = len(get_store().search(("quote_index",), limit=100000))
    return f"{prefix}{1001 + count}".upper()


# ---------------------------------------------------------------------------
# Learned rules (procedural memory taught by staff)
# ---------------------------------------------------------------------------

def add_learned_rule(rule: str) -> None:
    key = f"rule-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    get_store().put(LEARNED_RULES, key, {"rule": rule.strip(), "created_at": now()})


def learned_rules() -> List[Dict[str, Any]]:
    items = get_store().search(LEARNED_RULES, limit=500)
    return sorted(({"id": i.key, **i.value} for i in items), key=lambda r: r["created_at"])


def delete_learned_rule(rule_id: str) -> None:
    get_store().delete(LEARNED_RULES, rule_id)


# ---------------------------------------------------------------------------
# Threads (chat list + chat -> customer link)
# ---------------------------------------------------------------------------

def get_thread(thread_id: str) -> Dict[str, Any]:
    item = get_store().get(THREADS, thread_id)
    return item.value if item else {}


def update_thread(thread_id: str, **fields: Any) -> None:
    with _rw_lock:
        current = get_thread(thread_id) or {"created_at": now()}
        current.update({k: v for k, v in fields.items() if v is not None})
        current["updated_at"] = now()
        get_store().put(THREADS, thread_id, current)


def list_threads(limit: int = 25, owner: Optional[str] = None) -> List[Dict[str, Any]]:
    """Most recent chats. With owner: only the chats started in that browser (customers never see
    other customers' chats); without: every chat (staff)."""
    items = get_store().search(THREADS, limit=1000)
    rows = [{"thread_id": i.key, **i.value} for i in items if owner is None or i.value.get("owner") == owner]
    return sorted(rows, key=lambda r: r.get("updated_at", ""), reverse=True)[:limit]


def delete_thread(thread_id: str) -> None:
    """Delete a conversation: its messages, its uploaded files and any open hand-off flag.
    Quotes are kept - they belong to the customer's history."""
    import shutil

    get_store().delete(THREADS, thread_id)
    get_store().delete(HANDOFFS, thread_id)
    get_checkpointer().delete_thread(thread_id)
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in thread_id)
    shutil.rmtree(STORAGE_DIR / "uploads" / safe, ignore_errors=True)
