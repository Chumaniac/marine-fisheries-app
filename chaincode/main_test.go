package main

import (
	"github.com/hyperledger/fabric-chaincode-go/pkg/cid"
	"github.com/hyperledger/fabric-chaincode-go/shimtest"
	"github.com/hyperledger/fabric-contract-api-go/contractapi"
	"strings"
	"testing"
)

type testIdentity struct {
	cid.ClientIdentity
	msp string
}

func (i testIdentity) GetMSPID() (string, error) { return i.msp, nil }
func context(t *testing.T) (*contractapi.TransactionContext, *Contract) {
	stub := shimtest.NewMockStub("marine", nil)
	stub.MockTransactionStart("tx-test")
	ctx := new(contractapi.TransactionContext)
	ctx.SetStub(stub)
	ctx.SetClientIdentity(testIdentity{msp: "Org1MSP"})
	return ctx, &Contract{}
}
func TestTransferAndMassConservation(t *testing.T) {
	ctx, c := context(t)
	if _, err := c.CreateBatch(ctx, "create", "b", "saithe", "A", 1000); err != nil {
		t.Fatal(err)
	}
	if _, err := c.CreateBatch(ctx, "create", "b", "saithe", "A", 1000); err != nil {
		t.Fatal(err)
	}
	if _, err := c.CreateBatch(ctx, "create", "b", "saithe", "A", 2000); err == nil {
		t.Fatal("conflict accepted")
	}
	if _, err := c.Transition(ctx, "land", "b", "LANDED", 1); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ProposeTransfer(ctx, "proposal", "b", "Org2MSP", 2); err != nil {
		t.Fatal(err)
	}
	if _, err := c.AcceptTransfer(ctx, "accept", "proposal", 3); err == nil {
		t.Fatal("sender accepted as recipient")
	}
	ctx.SetClientIdentity(testIdentity{msp: "Org2MSP"})
	if _, err := c.AcceptTransfer(ctx, "accept", "proposal", 3); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ProposeTransfer(ctx, "proposal", "b", "Org1MSP", 4); err == nil {
		t.Fatal("cross-organization proposal overwrite accepted")
	}
	if _, err := c.TransformBatch(ctx, "bad", `["b"]`, `[{"id":"c","massGrams":999}]`, 0); err == nil {
		t.Fatal("mass discrepancy accepted")
	}
	if _, err := c.TransformBatch(ctx, "split", `["b"]`, `[{"id":"c","massGrams":900}]`, 100); err != nil {
		t.Fatal(err)
	}
	if _, err := c.TransformBatch(ctx, "double", `["b"]`, `[{"id":"d","massGrams":900}]`, 100); err == nil {
		t.Fatal("double consumption accepted")
	}
}
func TestEvidenceImmutableAndOrganizationBoundary(t *testing.T) {
	ctx, c := context(t)
	c.CreateBatch(ctx, "create", "b", "saithe", "A", 1000)
	if _, err := c.AppendEvidence(ctx, "e1", "b", "e", strings.Repeat("a", 64), 1); err != nil {
		t.Fatal(err)
	}
	if _, err := c.AppendEvidence(ctx, "e2", "b", "e", strings.Repeat("b", 64), 2); err == nil {
		t.Fatal("overwritten immutable evidence")
	}
	ctx.SetClientIdentity(testIdentity{msp: "UnknownMSP"})
	if _, err := c.GetBatch(ctx, "b"); err == nil {
		t.Fatal("unknown organization allowed")
	}
}

func TestRevisionHistoryRetained(t *testing.T) {
	ctx, c := context(t)
	if _, err := c.CreateBatch(ctx, "history-create", "h", "saithe", "A", 1000); err != nil {
		t.Fatal(err)
	}
	if _, err := c.Transition(ctx, "history-land", "h", "LANDED", 1); err != nil {
		t.Fatal(err)
	}
	old, err := c.GetBatchRevision(ctx, "h", 1)
	if err != nil || old.State != "CREATED" {
		t.Fatal("original revision missing", err)
	}
	latest, err := c.GetBatchRevision(ctx, "h", 2)
	if err != nil || latest.State != "LANDED" {
		t.Fatal("transition revision missing", err)
	}
}
