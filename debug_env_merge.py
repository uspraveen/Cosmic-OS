"""One-off: merge slide agent .env uniques into agent.env, then retire .env."""
import re
import shutil
import time
from pathlib import Path

root = Path("/home/ubuntu/Cosmic-OS/Backend/agents/slide_agent")
dot_env = root / ".env"
agent_env = root / "agent.env"


def parse(p):
    out = {}
    for line in p.read_text().splitlines():
        m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
        if m and m.group(1) not in out:
            out[m.group(1)] = m.group(2)
    return out


dot = parse(dot_env)
agent = parse(agent_env)

# Keys only .env carries (or where .env is authoritative for the core).
merge_keys = [
    "MODEL_API_KEY", "MODEL_BASE_URL", "MODEL_NAME", "VISION_MODEL_NAME",
    "CATALOGS_DIR", "ICON_DEFAULT_COLOR", "ICON_DEFAULT_SIZE_PX",
    "LIBREOFFICE_PATH", "PDFTOPPM_PATH", "MAX_SLIDES",
    "MAX_GRAPH_STEPS_PER_SLIDE",
]
added = []
for key in merge_keys:
    if key in agent and agent[key]:
        continue
    if key in dot and dot[key]:
        added.append(f"{key}={dot[key]}")

if added:
    agent_env.write_text(agent_env.read_text().rstrip("\n") + "\n" + "\n".join(added) + "\n", encoding="utf-8")

# Retire .env so no stale value can ever shadow agent.env again.
stamp = time.strftime("%Y%m%d%H%M%S")
shutil.move(str(dot_env), str(root / f".env.retired-{stamp}"))
print(f"merged {len(added)} keys, .env retired")
for line in added:
    print("  +", line.split("=")[0])
