"""Constrain a local official fabric-samples checkout before starting it."""

import re
from pathlib import Path

root = Path(__file__).resolve().parents[1]
samples = root / ".runtime/fabric-samples"
network = samples / "test-network"
for name in ["bin", "config"]:
    link = samples / name
    if not link.exists():
        link.symlink_to(root / ".runtime/fabric" / name, target_is_directory=True)
for path in [*network.rglob("*.yaml"), *network.rglob("*.sh")]:
    content = path.read_text()
    content = content.replace("fabric_test", "marine_fabric")
    content = content.replace("hyperledger/fabric-peer:latest", "hyperledger/fabric-peer:2.5.12")
    content = content.replace(
        "hyperledger/fabric-orderer:latest", "hyperledger/fabric-orderer:2.5.12"
    )
    if path.suffix == ".yaml":
        content = re.sub(r"(?m)^(\s*)- (\d+):(\d+)\s*$", r'\1- "127.0.0.1:\2:\3"', content)
        # CCAAS avoids giving a peer access to the host Docker daemon.
        content = (
            "\n".join(
                line
                for line in content.splitlines()
                if "CORE_VM_ENDPOINT=" not in line and "${DOCKER_SOCK}:/host/" not in line
            )
            + "\n"
        )
        if "mem_limit:" not in content:
            content = re.sub(
                r"(?m)^(\s*)container_name: (.+)$",
                r"\1container_name: \2\n\1mem_limit: 640m",
                content,
            )
    if path.name == "network.sh":
        content = content.replace(
            "function networkDown() {",
            'function networkDown() {\n  echo "Destructive sample cleanup disabled for this workspace" >&2\n  return 1',
        )
    path.write_text(content)
print("Prepared loopback-only sample network; Docker socket mount and sample cleanup disabled.")
