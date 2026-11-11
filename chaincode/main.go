package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"github.com/hyperledger/fabric-chaincode-go/shim"
	"os"
	"regexp"
	"sort"
	"strings"

	"github.com/hyperledger/fabric-contract-api-go/contractapi"
)

type Contract struct{ contractapi.Contract }
type Batch struct {
	ID       string   `json:"id"`
	Species  string   `json:"species"`
	Area     string   `json:"area"`
	Mass     int64    `json:"massGrams"`
	Owner    string   `json:"ownerMSP"`
	State    string   `json:"state"`
	Revision int      `json:"revision"`
	Parents  []string `json:"parents"`
}
type Evidence struct {
	ID         string `json:"id"`
	BatchID    string `json:"batchId"`
	Commitment string `json:"commitment"`
	Actor      string `json:"actorMSP"`
}
type Receipt struct {
	Hash   string `json:"payloadHash"`
	Result string `json:"result"`
	TxID   string `json:"txId"`
}
type Proposal struct {
	ID      string `json:"id"`
	BatchID string `json:"batchId"`
	From    string `json:"fromMSP"`
	To      string `json:"toMSP"`
	Used    bool   `json:"used"`
}
type Output struct {
	ID   string `json:"id"`
	Mass int64  `json:"massGrams"`
}

var validID = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)
var validHash = regexp.MustCompile(`^[0-9a-f]{64}$`)

func identity(ctx contractapi.TransactionContextInterface) (string, error) {
	msp, err := ctx.GetClientIdentity().GetMSPID()
	if err != nil {
		return "", err
	}
	if msp != "Org1MSP" && msp != "Org2MSP" {
		return "", fmt.Errorf("unauthorized organization")
	}
	return msp, nil
}
func get(ctx contractapi.TransactionContextInterface, key string, out interface{}) error {
	raw, err := ctx.GetStub().GetState(key)
	if err != nil {
		return err
	}
	if raw == nil {
		return fmt.Errorf("not found")
	}
	return json.Unmarshal(raw, out)
}
func put(ctx contractapi.TransactionContextInterface, key string, value interface{}) error {
	raw, err := json.Marshal(value)
	if err != nil {
		return err
	}
	if err = ctx.GetStub().PutState(key, raw); err != nil {
		return err
	}
	if strings.HasPrefix(key, "batch~") {
		var batch Batch
		if err = json.Unmarshal(raw, &batch); err != nil {
			return err
		}
		historyKey := fmt.Sprintf("revision~%s~%012d", batch.ID, batch.Revision)
		if err = ctx.GetStub().PutState(historyKey, raw); err != nil {
			return err
		}
	}
	return nil
}
func transact(ctx contractapi.TransactionContextInterface, request string, payload interface{}, action func() (string, error)) (string, error) {
	actor, err := identity(ctx)
	if err != nil {
		return "", err
	}
	if !validID.MatchString(request) {
		return "", fmt.Errorf("invalid request ID")
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(raw)
	hash := hex.EncodeToString(sum[:])
	key := "receipt~" + actor + "~" + request
	existing, err := ctx.GetStub().GetState(key)
	if err != nil {
		return "", err
	}
	if existing != nil {
		var receipt Receipt
		if err = json.Unmarshal(existing, &receipt); err != nil {
			return "", err
		}
		if receipt.Hash != hash {
			return "", fmt.Errorf("idempotency conflict")
		}
		return receipt.Result, nil
	}
	result, err := action()
	if err != nil {
		return "", err
	}
	if err = put(ctx, key, Receipt{hash, result, ctx.GetStub().GetTxID()}); err != nil {
		return "", err
	}
	audit, err := json.Marshal(map[string]interface{}{"requestId": request, "actorMSP": actor, "payload": payload, "result": result, "txId": ctx.GetStub().GetTxID()})
	if err != nil {
		return "", err
	}
	if err = ctx.GetStub().PutState("command~"+actor+"~"+request, audit); err != nil {
		return "", err
	}
	if err = ctx.GetStub().SetEvent("BatchCommandCommitted", audit); err != nil {
		return "", err
	}
	return result, nil
}
func (c *Contract) GetBatchRevision(ctx contractapi.TransactionContextInterface, id string, revision int) (*Batch, error) {
	if _, err := identity(ctx); err != nil {
		return nil, err
	}
	if !validID.MatchString(id) || revision < 1 {
		return nil, fmt.Errorf("invalid revision")
	}
	var batch Batch
	if err := get(ctx, fmt.Sprintf("revision~%s~%012d", id, revision), &batch); err != nil {
		return nil, err
	}
	return &batch, nil
}
func (c *Contract) GetBatch(ctx contractapi.TransactionContextInterface, id string) (*Batch, error) {
	if _, err := identity(ctx); err != nil {
		return nil, err
	}
	if !validID.MatchString(id) {
		return nil, fmt.Errorf("invalid ID")
	}
	var batch Batch
	if err := get(ctx, "batch~"+id, &batch); err != nil {
		return nil, err
	}
	return &batch, nil
}
func (c *Contract) CreateBatch(ctx contractapi.TransactionContextInterface, request, id, species, area string, mass int64) (string, error) {
	return transact(ctx, request, []interface{}{"create", id, species, area, mass}, func() (string, error) {
		actor, _ := identity(ctx)
		if actor != "Org1MSP" {
			return "", fmt.Errorf("harvest organization required")
		}
		if !validID.MatchString(id) || len(species) == 0 || len(species) > 128 || len(area) > 64 || mass <= 0 || mass > 1000000000000 {
			return "", fmt.Errorf("invalid batch")
		}
		old, err := ctx.GetStub().GetState("batch~" + id)
		if err != nil {
			return "", err
		}
		if old != nil {
			return "", fmt.Errorf("batch exists")
		}
		batch := Batch{id, species, area, mass, actor, "CREATED", 1, []string{}}
		if err = put(ctx, "batch~"+id, batch); err != nil {
			return "", err
		}
		return id, nil
	})
}
func (c *Contract) Transition(ctx contractapi.TransactionContextInterface, request, id, next string, revision int) (string, error) {
	return transact(ctx, request, []interface{}{"transition", id, next, revision}, func() (string, error) {
		batch, err := c.GetBatch(ctx, id)
		if err != nil {
			return "", err
		}
		actor, _ := identity(ctx)
		if actor != batch.Owner {
			return "", fmt.Errorf("owner required")
		}
		if batch.Revision != revision {
			return "", fmt.Errorf("revision conflict")
		}
		allowed := map[string]map[string]bool{"CREATED": {"LANDED": true, "RECALLED": true}, "LANDED": {"PROCESSED": true, "IN_COLD_CHAIN": true, "RECALLED": true}, "PROCESSED": {"IN_COLD_CHAIN": true, "RECALLED": true}, "IN_COLD_CHAIN": {"DELIVERED": true, "RECALLED": true}, "DELIVERED": {"RECALLED": true}}
		if !allowed[batch.State][next] {
			return "", fmt.Errorf("invalid state")
		}
		batch.State = next
		batch.Revision++
		if err = put(ctx, "batch~"+id, batch); err != nil {
			return "", err
		}
		return id, nil
	})
}
func (c *Contract) AppendEvidence(ctx contractapi.TransactionContextInterface, request, id, evidenceID, commitment string, revision int) (string, error) {
	return transact(ctx, request, []interface{}{"evidence", id, evidenceID, commitment, revision}, func() (string, error) {
		if !validID.MatchString(evidenceID) || !validHash.MatchString(commitment) {
			return "", fmt.Errorf("invalid evidence")
		}
		batch, err := c.GetBatch(ctx, id)
		if err != nil {
			return "", err
		}
		actor, _ := identity(ctx)
		if actor != batch.Owner {
			return "", fmt.Errorf("owner required")
		}
		if batch.Revision != revision {
			return "", fmt.Errorf("revision conflict")
		}
		key := "evidence~" + id + "~" + evidenceID
		old, err := ctx.GetStub().GetState(key)
		if err != nil {
			return "", err
		}
		if old != nil {
			return "", fmt.Errorf("immutable evidence exists")
		}
		evidence := Evidence{evidenceID, id, commitment, actor}
		if err = put(ctx, key, evidence); err != nil {
			return "", err
		}
		batch.Revision++
		if err = put(ctx, "batch~"+id, batch); err != nil {
			return "", err
		}
		encoded, _ := json.Marshal(evidence)
		if err = ctx.GetStub().SetEvent("EvidenceAnchored", encoded); err != nil {
			return "", err
		}
		return evidenceID, nil
	})
}
func (c *Contract) ProposeTransfer(ctx contractapi.TransactionContextInterface, request, id, to string, revision int) (string, error) {
	return transact(ctx, request, []interface{}{"propose", id, to, revision}, func() (string, error) {
		batch, err := c.GetBatch(ctx, id)
		if err != nil {
			return "", err
		}
		actor, _ := identity(ctx)
		if actor != batch.Owner || to == actor || (to != "Org1MSP" && to != "Org2MSP") {
			return "", fmt.Errorf("invalid parties")
		}
		if batch.Revision != revision || batch.State == "RECALLED" || batch.State == "CONSUMED" {
			return "", fmt.Errorf("invalid state or revision")
		}
		existingProposal, err := ctx.GetStub().GetState("proposal~" + request)
		if err != nil {
			return "", err
		}
		if existingProposal != nil {
			return "", fmt.Errorf("proposal ID already exists")
		}
		proposal := Proposal{request, id, actor, to, false}
		if err = put(ctx, "proposal~"+request, proposal); err != nil {
			return "", err
		}
		batch.Revision++
		if err = put(ctx, "batch~"+id, batch); err != nil {
			return "", err
		}
		return request, nil
	})
}
func (c *Contract) AcceptTransfer(ctx contractapi.TransactionContextInterface, request, proposalID string, revision int) (string, error) {
	return transact(ctx, request, []interface{}{"accept", proposalID, revision}, func() (string, error) {
		var p Proposal
		if err := get(ctx, "proposal~"+proposalID, &p); err != nil {
			return "", err
		}
		actor, _ := identity(ctx)
		if p.Used || actor != p.To {
			return "", fmt.Errorf("recipient required")
		}
		batch, err := c.GetBatch(ctx, p.BatchID)
		if err != nil {
			return "", err
		}
		if batch.Owner != p.From || batch.Revision != revision || batch.State == "RECALLED" || batch.State == "CONSUMED" {
			return "", fmt.Errorf("invalid state or revision")
		}
		p.Used = true
		batch.Owner = actor
		batch.Revision++
		if err = put(ctx, "batch~"+batch.ID, batch); err != nil {
			return "", err
		}
		if err = put(ctx, "proposal~"+proposalID, p); err != nil {
			return "", err
		}
		return batch.ID, nil
	})
}
func (c *Contract) TransformBatch(ctx contractapi.TransactionContextInterface, request, parentsJSON, outputsJSON string, loss int64) (string, error) {
	return transact(ctx, request, []interface{}{"transform", parentsJSON, outputsJSON, loss}, func() (string, error) {
		actor, _ := identity(ctx)
		if actor != "Org2MSP" || loss < 0 {
			return "", fmt.Errorf("processor required")
		}
		var parents []string
		var outputs []Output
		if json.Unmarshal([]byte(parentsJSON), &parents) != nil || json.Unmarshal([]byte(outputsJSON), &outputs) != nil || len(parents) == 0 || len(parents) > 100 || len(outputs) == 0 || len(outputs) > 100 {
			return "", fmt.Errorf("invalid transform")
		}
		sort.Strings(parents)
		var total, result int64
		seen := map[string]bool{}
		batches := []*Batch{}
		species := ""
		for _, id := range parents {
			if seen[id] {
				return "", fmt.Errorf("duplicate parent")
			}
			seen[id] = true
			b, err := c.GetBatch(ctx, id)
			if err != nil {
				return "", err
			}
			if b.Owner != actor || (b.State != "LANDED" && b.State != "PROCESSED") {
				return "", fmt.Errorf("parent not available")
			}
			if species == "" {
				species = b.Species
			} else if species != b.Species {
				return "", fmt.Errorf("mixed species unsupported")
			}
			total += b.Mass
			batches = append(batches, b)
		}
		for _, o := range outputs {
			if !validID.MatchString(o.ID) || seen[o.ID] || o.Mass <= 0 || o.Mass > 1000000000000 {
				return "", fmt.Errorf("invalid output")
			}
			seen[o.ID] = true
			old, err := ctx.GetStub().GetState("batch~" + o.ID)
			if err != nil {
				return "", err
			}
			if old != nil {
				return "", fmt.Errorf("output exists")
			}
			result += o.Mass
		}
		if result+loss != total {
			return "", fmt.Errorf("mass conservation violated")
		}
		for _, b := range batches {
			b.State = "CONSUMED"
			b.Revision++
			if err := put(ctx, "batch~"+b.ID, b); err != nil {
				return "", err
			}
		}
		for _, o := range outputs {
			b := Batch{o.ID, species, "processed", o.Mass, actor, "PROCESSED", 1, parents}
			if err := put(ctx, "batch~"+o.ID, b); err != nil {
				return "", err
			}
		}
		return request, nil
	})
}
func (c *Contract) GetEvidence(ctx contractapi.TransactionContextInterface, batch, id string) (*Evidence, error) {
	if _, err := identity(ctx); err != nil {
		return nil, err
	}
	if !validID.MatchString(batch) || !validID.MatchString(id) {
		return nil, fmt.Errorf("invalid ID")
	}
	var evidence Evidence
	if err := get(ctx, "evidence~"+batch+"~"+id, &evidence); err != nil {
		return nil, err
	}
	return &evidence, nil
}
func (c *Contract) GetReceipt(ctx contractapi.TransactionContextInterface, request string) (*Receipt, error) {
	actor, err := identity(ctx)
	if err != nil {
		return nil, err
	}
	var receipt Receipt
	if err = get(ctx, "receipt~"+actor+"~"+request, &receipt); err != nil {
		return nil, err
	}
	return &receipt, nil
}
func main() {
	chaincode, err := contractapi.NewChaincode(&Contract{})
	if err != nil {
		panic(err)
	}
	if address := os.Getenv("CHAINCODE_SERVER_ADDRESS"); address != "" {
		tls := shim.TLSProperties{Disabled: true}
		if os.Getenv("CHAINCODE_TLS_CERT_FILE") != "" {
			cert, e := os.ReadFile(os.Getenv("CHAINCODE_TLS_CERT_FILE"))
			if e != nil {
				panic(e)
			}
			key, e := os.ReadFile(os.Getenv("CHAINCODE_TLS_KEY_FILE"))
			if e != nil {
				panic(e)
			}
			tls = shim.TLSProperties{Disabled: false, Cert: cert, Key: key}
		} else if os.Getenv("MARINE_MODE") == "production" {
			panic("production chaincode requires TLS")
		}
		server := shim.ChaincodeServer{CCID: os.Getenv("CHAINCODE_ID"), Address: address, CC: chaincode, TLSProps: tls}
		if err = server.Start(); err != nil {
			panic(err)
		}
		return
	}
	if err = chaincode.Start(); err != nil {
		panic(err)
	}
}
