"""Create a real Org1-only channel and verify Org2 cannot query it locally."""

import copy
import json
import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
network = ROOT / ".runtime/fabric-samples/test-network"
binary = ROOT / ".runtime/fabric/bin"
configfile = network / "configtx/configtx.yaml"
config = yaml.safe_load(configfile.read_text())
profile = copy.deepcopy(config["Profiles"]["ChannelUsingRaft"])
profile["Application"]["Organizations"] = [
    o for o in profile["Application"]["Organizations"] if o["ID"] == "Org1MSP"
]
config["Profiles"]["OriginAuditOnly"] = profile
configfile.write_text(yaml.safe_dump(config, sort_keys=False))
channel = "origin-audit"
block = network / "channel-artifacts/origin-audit.block"
orderer = (
    network / "organizations/ordererOrganizations/example.com/orderers/orderer.example.com/tls"
)


def command(args, env=None, allow_failure=False):
    result = subprocess.run(
        [str(binary / args[0]), *map(str, args[1:])],
        cwd=network,
        env={**os.environ, **(env or {})},
        capture_output=True,
        timeout=30,
    )
    if result.returncode and not allow_failure:
        raise RuntimeError(f"{args[0]} {args[1]} failed (exit {result.returncode})")
    return result


def peer_env(org):
    base = network / f"organizations/peerOrganizations/org{org}.example.com"
    return {
        "FABRIC_CFG_PATH": str(ROOT / ".runtime/fabric/config"),
        "CORE_PEER_LOCALMSPID": f"Org{org}MSP",
        "CORE_PEER_MSPCONFIGPATH": str(base / f"users/Admin@org{org}.example.com/msp"),
        "CORE_PEER_TLS_ENABLED": "true",
        "CORE_PEER_ADDRESS": f"localhost:{7051 if org == 1 else 9051}",
        "CORE_PEER_TLS_ROOTCERT_FILE": str(base / f"peers/peer0.org{org}.example.com/tls/ca.crt"),
    }


if command(["peer", "channel", "getinfo", "-c", channel], peer_env(1), True).returncode:
    command(
        [
            "configtxgen",
            "-profile",
            "OriginAuditOnly",
            "-outputBlock",
            block,
            "-channelID",
            channel,
        ],
        {"FABRIC_CFG_PATH": str(network / "configtx")},
    )
    command(
        [
            "osnadmin",
            "channel",
            "join",
            "--channelID",
            channel,
            "--config-block",
            block,
            "-o",
            "localhost:7053",
            "--ca-file",
            orderer / "ca.crt",
            "--client-cert",
            orderer / "server.crt",
            "--client-key",
            orderer / "server.key",
        ]
    )
    command(["peer", "channel", "join", "-b", block], peer_env(1))
org1 = command(["peer", "channel", "getinfo", "-c", channel], peer_env(1), True)
org2 = command(["peer", "channel", "getinfo", "-c", channel], peer_env(2), True)
assert org1.returncode == 0 and org2.returncode != 0
result = {
    "channel": channel,
    "org1Read": True,
    "org2Read": False,
    "scope": "local two-organization isolation, not five-party production governance",
}
(ROOT / "artifacts/channel-isolation.json").write_text(json.dumps(result, indent=2))
print(json.dumps(result))
