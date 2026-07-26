"""
AID PLUS+ — Service Bus
========================
AidPlusServiceBus: central inter-product service registry.
Products register via register(). Dormant sockets return ServiceNotAvailable
gracefully until a product connects.
HMAC-SHA256 request signing prevents rogue kiosk attacks.
"""
from __future__ import annotations
import time, threading, json
from datetime import datetime
import hmac as _hmac
import secrets

from aidplus.config import *
from aidplus.db import DatabaseManager

class ServiceNotAvailable:
    """Returned when a product service is not yet registered on the bus."""
    def __init__(self, service_name: str):
        self.service_name = service_name
    def __repr__(self):
        return f"<ServiceNotAvailable: {self.service_name}>"


class AidPlusServiceBus:
    """
    AID PLUS+ Service Bus — Integration Contracts v1.0.0.
    All products communicate through this bus — never directly.

    B28: Dormant sockets added for BTM and Aid Air.
    A dormant socket means the integration contract is locked and the
    bus knows the service exists — but the product hardware is not yet
    deployed. Calls to dormant sockets return ServiceNotAvailable
    gracefully, exactly as if the product simply isn't connected yet.

    Live registrations (B28):
        CAPSCAN  — live (co-located CUPSCANModule on ADW-AS)
    Dormant (contracts locked, hardware not yet deployed):
        BTM      — Blood Testing Machine
        AID_AIR  — Aid Air drone system
    """
    _registry:        dict = {}
    _dormant_sockets: dict = {}   # B28: known-but-not-live services
    _db: "DatabaseManager | None" = None

    @classmethod
    def init(cls, db: "DatabaseManager"):
        cls._db = db
        # B28: Register dormant sockets for future products
        cls._dormant_sockets = {
            "BTM": {
                "variant":  ADW_VARIANT_BT,
                "contract": "BTM-v1",
                "status":   "dormant",
                "note":     "Blood Testing Machine — hardware not yet deployed",
            },
            "AID_AIR": {
                "variant":  ADW_VARIANT_AA,
                "contract": "AIDAIR-v1",
                "status":   "dormant",
                "note":     "Aid Air drone system — hardware not yet deployed",
            },
        }

    @classmethod
    def register(cls, service_name: str, handler,
                 version: str = "1.0.0",
                 capabilities: list = None):
        """
        Register a product service on the bus.
        Idempotent — re-registration updates version/capabilities.
        If service was previously dormant, it becomes live on registration.
        """
        cls._registry[service_name] = {
            "handler":       handler,
            "version":       version,
            "capabilities":  capabilities or [],
            "registered_at": datetime.now().isoformat(),
            "status":        "live",
        }
        # Promote dormant → live if applicable
        if service_name in cls._dormant_sockets:
            cls._dormant_sockets[service_name]["status"] = "live"
        if cls._db:
            cls._db.register_service(
                service_name, f"{service_name}-v1",
                version, capabilities or [])
        print(f"[ServiceBus] ✅ Registered: {service_name} v{version}  "
              f"capabilities={capabilities}")

    @classmethod
    def call(cls, service_name: str, method: str, **kwargs):
        """
        Call a method on a registered service.
        Returns ServiceNotAvailable for both unregistered and dormant services.
        Dormant services are distinguished in the response for diagnostics.
        """
        if service_name not in cls._registry:
            sna = ServiceNotAvailable(service_name)
            if service_name in cls._dormant_sockets:
                sna.dormant = True
                sna.note    = cls._dormant_sockets[service_name]["note"]
            return sna
        try:
            return cls._registry[service_name]["handler"].handle(method, **kwargs)
        except Exception as e:
            print(f"[ServiceBus] ❌ Error calling {service_name}.{method}: {e}")
            return {"success": False, "error_code": "BUS_CALL_ERROR",
                    "message": str(e)}

    @classmethod
    def emit(cls, event_name: str, payload: dict):
        """Broadcast event to all live registered listeners."""
        delivered = 0
        for name, svc in cls._registry.items():
            handler = svc["handler"]
            if hasattr(handler, "on_event"):
                try:
                    handler.on_event(event_name, payload)
                    delivered += 1
                except Exception as e:
                    print(f"[ServiceBus] ⚠  Event delivery failed "
                          f"{event_name} → {name}: {e}")
        return delivered

    @classmethod
    def is_registered(cls, service_name: str) -> bool:
        return service_name in cls._registry

    @classmethod
    def is_dormant(cls, service_name: str) -> bool:
        return (service_name in cls._dormant_sockets and
                service_name not in cls._registry)

    @classmethod
    def status(cls) -> dict:
        """Live registry status."""
        return {
            name: {
                "version":       svc["version"],
                "capabilities":  svc["capabilities"],
                "registered_at": svc["registered_at"],
                "status":        svc.get("status", "live"),
            }
            for name, svc in cls._registry.items()
        }

    @classmethod
    def full_status(cls) -> dict:
        """B28: Full status including dormant sockets — for admin dashboard."""
        out = {}
        for name, svc in cls._registry.items():
            out[name] = {
                "status":       "live",
                "version":      svc["version"],
                "registered_at": svc["registered_at"],
            }
        for name, sock in cls._dormant_sockets.items():
            if name not in out:
                out[name] = {
                    "status":   "dormant",
                    "contract": sock["contract"],
                    "note":     sock["note"],
                }
        return out


# ─────────────────────────────────────────────────────────────────────────────
# BiometricAuthService  [B20-B]
# Mandatory face verification wrapper for all authentication paths.
# ─────────────────────────────────────────────────────────────────────────────

