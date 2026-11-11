import copy
import json
from pathlib import Path
import pytest
from marine.field_readiness import field_preflight


def template():
    return json.loads(Path("contracts/field-acceptance-plan.json").read_text())


def test_unconfigured_plan_never_invents_conditions_or_target_metrics():
    plan = template()
    before = copy.deepcopy(plan)
    result = field_preflight(plan)
    assert result["planFieldsComplete"] is False and len(result["missingFields"]) == 14
    assert plan == before and result["fieldAccepted"] is False


def test_complete_synthetic_plan_is_not_an_authorized_or_successful_field_trial():
    plan = template()
    plan["conditions"] = {k: "synthetic" for k in plan["conditions"]}
    for key in ("modelSha256", "datasetSha256"):
        plan["conditions"][key] = "a" * 64
    plan["targets"] = {k: 1 for k in plan["targets"]}
    plan["authorizationReference"] = "synthetic-declaration"
    result = field_preflight(plan)
    assert result["planFieldsComplete"] and len(result["planSha256"]) == 64
    for flag in (
        "authorizationVerified",
        "fieldAccepted",
        "accuracyValidated",
        "productionApproved",
        "automaticProductionCutover",
    ):
        assert result[flag] is False
    assert "synthetic-declaration" not in json.dumps(result)


@pytest.mark.parametrize("invalid", [True, float("nan"), float("inf"), -1])
def test_invalid_target_types_and_ranges_reject_without_echo(invalid):
    plan = template()
    plan["targets"]["map"] = invalid
    with pytest.raises(ValueError, match="Invalid bounded field targets"):
        field_preflight(plan)
