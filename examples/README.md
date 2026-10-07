# examples/

Board session (each line is a real command; see README):
```
companyos board "CEO, research the market for a SaaS opportunity in Indonesia."
#  CEO: Understood. I will run a bounded market discovery ... [intent MARKET_DISCOVERY]
companyos run                  # research -> problems/segments/competitors -> opportunities -> CTO/CFO/CMO/CRO/legal in parallel -> CPO -> CEO decision
companyos inbox                # BUILD / VALIDATE_MORE / DEFER / REJECT with the ranked opportunities
companyos board "CEO, build an AI invoice reconciliation SaaS for SMEs."
#  Observed live (2026-10-07, opus): the CEO chose EVALUATE first because company state held no demand evidence,
#  and offered to build directly if the Board has committed customers.
companyos approvals ; companyos approve apr-... --note "go"
companyos status
```
End-to-end behavior of both primary workflows is exercised in `tests/orchestration/test_scenarios.py`.
