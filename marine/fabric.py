"""Restricted local Fabric CLI adapter. No Docker socket is exposed to the API."""

import json
import os
import subprocess
from pathlib import Path

from marine.storage import digest


class Fabric:
    def __init__(self, organization="Org1MSP"):
        root = os.getenv("MARINE_FABRIC_ROOT")
        if not root:
            raise RuntimeError("Fabric is not configured")
        self.root = Path(root).resolve()
        self.peer = Path(os.environ["MARINE_PEER_BINARY"]).resolve()
        self.channel = os.getenv("MARINE_FABRIC_CHANNEL", "trace-common")
        self.env = {
            **os.environ,
            "FABRIC_CFG_PATH": os.getenv("FABRIC_CFG_PATH", str(self.root / "../config")),
            "CORE_PEER_LOCALMSPID": "Org1MSP",
            "CORE_PEER_ADDRESS": "localhost:7051",
            "CORE_PEER_TLS_ENABLED": "true",
            "CORE_PEER_MSPCONFIGPATH": str(
                self.root
                / "organizations/peerOrganizations/org1.example.com/users/User1@org1.example.com/msp"
            ),
            "CORE_PEER_TLS_ROOTCERT_FILE": str(
                self.root
                / "organizations/peerOrganizations/org1.example.com/peers/peer0.org1.example.com/tls/ca.crt"
            ),
        }
        if organization not in {"Org1MSP", "Org2MSP"}:
            raise ValueError("Unsupported organization")
        if organization == "Org2MSP":
            self.env.update(
                CORE_PEER_LOCALMSPID="Org2MSP",
                CORE_PEER_ADDRESS="localhost:9051",
                CORE_PEER_MSPCONFIGPATH=str(
                    self.root
                    / "organizations/peerOrganizations/org2.example.com/users/User1@org2.example.com/msp"
                ),
                CORE_PEER_TLS_ROOTCERT_FILE=str(
                    self.root
                    / "organizations/peerOrganizations/org2.example.com/peers/peer0.org2.example.com/tls/ca.crt"
                ),
            )
        self.org1_ca = str(
            self.root
            / "organizations/peerOrganizations/org1.example.com/peers/peer0.org1.example.com/tls/ca.crt"
        )
        self.orderer_ca = (
            self.root
            / "organizations/ordererOrganizations/example.com/orderers/orderer.example.com/msp/tlscacerts/tlsca.example.com-cert.pem"
        )
        self.org2_ca = (
            self.root
            / "organizations/peerOrganizations/org2.example.com/peers/peer0.org2.example.com/tls/ca.crt"
        )

        self.orderer_address = "localhost:7050"
        self.orderer_hostname = "orderer.example.com"
        self.endorsers = [("localhost:7051", self.org1_ca), ("localhost:9051", str(self.org2_ca))]
        connection_file = os.getenv("MARINE_FABRIC_CONNECTION_FILE")
        if connection_file:
            config = json.loads(Path(connection_file).read_text())
            member = config["organizations"][organization]
            self.env.update(
                CORE_PEER_ADDRESS=member["peerAddress"],
                CORE_PEER_MSPCONFIGPATH=member["mspPath"],
                CORE_PEER_TLS_ROOTCERT_FILE=member["tlsRootCert"],
            )
            self.orderer_address = config["orderer"]["address"]
            self.orderer_hostname = config["orderer"]["hostname"]
            self.orderer_ca = Path(config["orderer"]["tlsRootCert"])
            self.endorsers = [
                (m["peerAddress"], m["tlsRootCert"]) for m in config["organizations"].values()
            ]
            if not 1 <= len(self.endorsers) <= 10:
                raise ValueError("Invalid endorser configuration")
        elif os.getenv("MARINE_MODE") == "production":
            raise RuntimeError("Production Fabric connection profile is required")

    def query(self, function, *args):
        command = [
            str(self.peer),
            "chaincode",
            "query",
            "-C",
            self.channel,
            "-n",
            "marine",
            "-c",
            json.dumps({"Args": [function, *map(str, args)]}),
        ]
        try:
            result = subprocess.run(command, env=self.env, capture_output=True, timeout=15)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Fabric query timed out") from None
        if result.returncode:
            # Only the chaincode's explicit missing-state error permits creation.
            if (
                b"message=not found" in result.stderr
                or b"message: not found" in result.stderr
                or b'message:"not found"' in result.stderr
            ):
                return None
            raise RuntimeError("Fabric query failed; state is unknown")
        return json.loads(result.stdout)

    def invoke(self, function, *args):
        command = [
            str(self.peer),
            "chaincode",
            "invoke",
            "-o",
            self.orderer_address,
            "--ordererTLSHostnameOverride",
            self.orderer_hostname,
            "--tls",
            "--cafile",
            str(self.orderer_ca),
            "-C",
            self.channel,
            "-n",
            "marine",
            "--waitForEvent",
            "--waitForEventTimeout",
            "30s",
            "-c",
            json.dumps({"Args": [function, *map(str, args)]}),
        ]
        for address, certificate in self.endorsers:
            command += ["--peerAddresses", address, "--tlsRootCertFiles", certificate]
        try:
            result = subprocess.run(command, env=self.env, capture_output=True, timeout=40)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Commit outcome unknown; reconcile by request ID") from None
        if result.returncode:
            raise RuntimeError("Fabric did not confirm VALID commit; reconcile before retry")

    def anchor(self, batch, manifest):
        chain_id = "b_" + digest({"tenant": manifest["tenant"], "batchId": batch["batchId"]})[:48]
        evidence_id = manifest["evidenceId"]
        request_id = "anchor_" + evidence_id
        commitment = digest(manifest)
        existing = self.query("GetEvidence", chain_id, evidence_id)
        if existing:
            if existing["commitment"] != commitment:
                raise ValueError("Conflicting anchored manifest")
            receipt = self.query("GetReceipt", request_id)
            if not receipt:
                raise RuntimeError("Anchor receipt unavailable")
            return {
                **receipt,
                "channelId": self.channel,
                "chainBatchId": chain_id,
                "validationCode": "VALID",
            }
        current = self.query("GetBatch", chain_id)
        if current is None:
            self.invoke(
                "CreateBatch",
                "create_" + chain_id,
                chain_id,
                batch["speciesId"],
                batch["areaCode"],
                batch["massGrams"],
            )
            current = self.query("GetBatch", chain_id)
        if current is None:
            raise RuntimeError("Batch commit unavailable")
        self.invoke(
            "AppendEvidence", request_id, chain_id, evidence_id, commitment, current["revision"]
        )
        receipt = self.query("GetReceipt", request_id)
        anchored = self.query("GetEvidence", chain_id, evidence_id)
        if not receipt or not anchored or anchored["commitment"] != commitment:
            raise RuntimeError("Cannot reconcile anchor")
        return {
            **receipt,
            "channelId": self.channel,
            "chainBatchId": chain_id,
            "validationCode": "VALID",
        }
