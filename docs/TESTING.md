# Testing

Run the safe local gate:

```bash
make lint
make type
make test
make scan
make secret-scan
make archive-check
python -m trading_bot quality run
python -m trading_bot quality verify
```

The suite is fixture/mock driven and must not contain exchange credentials or
external order submission. PAPER fixture commands use local CSV data. TESTNET
construction tests use mocked transports only.

Quality evidence is generated from real command output and hashes the JUnit
file, coverage JSON, dependency lock files, configuration schema files and
source tree. The verifier recomputes those hashes and rejects zero tests,
missing required safety regressions, stale records, tampering, source changes
and no-repository live qualification evidence.

The configured overall coverage floor is 90%. The quality record also verifies
named safety regressions for trapped exits, partial fills, fail-closed
reconciliation, endpoint isolation, backtest timing, timezone reporting,
database closure, live qualification evidence, and quality-evidence tampering.
