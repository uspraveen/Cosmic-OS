import sys

sys.path.insert(0, ".")
from gateway.session_store import SessionStore
from gateway.runtime import GatewayRuntime
from gateway.config import GatewayConfig

cfg = GatewayConfig.from_env()
print("imports OK")
print("passive kinds:", cfg.cosmic_memory_passive_kinds)
print("email thread idle minutes:", cfg.email_thread_summary_idle_minutes)
print("email thread poll sec:", cfg.email_thread_summary_poll_sec)
