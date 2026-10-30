# Investment implementation contract

This is the implementation boundary for the user-approved investment system. The
existing finance-video pipeline and its sealed archives remain compatible.

## Product decisions

- USD 20,000 is the portfolio + cash target, not a funded real-account balance.
- USD 1,000–3,000 is EACH buy, not the maximum cumulative position.
- Primary horizon: 12–36 months. Every actionable plan needs a horizon, review
  date, evidenced target scenarios, take-profit schedule, price stop and its
  confirmation mode, thesis invalidation, and time-expiry handling.
- User executes real trades. Real entries are staged and explicitly confirmed;
  paper entries live in a separate account. No brokerage trading API.
- Feishu Bitable + CSV input, portfolio/plan views, significant-change notices,
  daily/weekly reports and quarterly/event-driven reviews. No web frontend v1.
- Drawdown hard limit is UNCONFIGURED; never imply a risk check passed.
- Example TSM 370/800/270 is a format example, never a real recommendation.

## Package and ownership

Create `rhino_invest/` at repository root. Python 3.11+; prefer standard library
and existing requests. Tests use unittest under `tests_invest/`.

Agent core: **init**.py, common.py, store.py, ledger.py, paper.py; its own tests.
Agent research: market.py, research.py, decisions.py, model.py; its own tests.
Agent delivery: feishu.py, reporting.py, runner.py, **main**.py, configuration,
deploy/investment/, investment user docs; its own tests.
Root reviews interfaces, actual diffs, integration and acceptance.

## Shared Python contracts

Use JSON-safe dictionaries (money serialized as decimal strings), UTC ISO-8601
timestamps with explicit timezone, uppercase bare US symbols for the wrapper,
`TSM.US` only at SDK boundaries. Unknown values are None, never zero.

`Store(path)` creates a durable SQLite store, exposing:

- put(kind, key, payload, *, immutable=False) -> bool (changed); immutable
  conflicting writes raise ValueError; exact replay is a no-op.
- get(kind, key) -> dict | None
- items(kind) -> list[tuple[str, dict]]
- append_event(kind, event_id, payload) -> bool; immutable idempotent event
- events(kind) -> list[dict]; event payloads include their own id/timestamp
- close(), context-manager support

`common.py`: utc_now() -> str, parse_time(value) -> aware UTC datetime,
decimal_value(value, name='value') -> Decimal (reject bool/nonfinite),
stable_id(*parts) -> sha256 str, validate_plan(plan) -> list[str].

Plan minimal keys: id, revision, symbol, status (candidate/needs_review/active/
superseded/invalidated), created_at, evidence_refs (list), thesis,
horizon_months, next_review_at, entry {low, high, amount_usd}, targets (list of
{scenario, price, horizon_months, method, assumptions, evidence_refs}),
take_profit (list of {price, fraction}), stop {price, confirmation},
invalidation_conditions (list), time_exit_policy, valuation_basis (dict).
Actionable activation must require verified provenance, currency/unit and ADR
checks when relevant; missing/unparsed conditions do not silently become buys.
Agents may add fields compatibly and must notify peers of contract extensions.

Ledger functions: stage_entry(store, entry) -> dict; confirm_entry(store, id) ->
dict; portfolio(store, account='real', quotes=None, now=None) -> dict.
Entries: id, account(real/paper), type(opening_cash/opening_position/buy/sell/
deposit/withdrawal/dividend/split/correction), timestamp, currency, plus the
applicable symbol/quantity/price/fee/amount fields. Confirmation must validate
the replayed account; cost/fees/cash completeness are explicit.

Market adapter: MarketData(client=None, snapshot_fetcher=None),
snapshots(symbols, now=None) -> list[dict], fundamentals(symbol, now=None) ->
dict; supports fixture injection, official-only queries, fresh/session-aware
business gates. Do not duplicate SDK authentication or quote normalization.

Research entry: ingest_archives(store, archive_db, **options) -> dict; consumes
sealed analysis revisions read-only. compile/review candidates from original
frames + transcript + visual cache through injectable model, immutable inputs
and cached responses, no unknown paid-call retries. Production model use is
explicitly configured, tests offline. Raw extracted levels alone never become
verified long-term plans. Data is untrusted evidence, not model instructions.

Decision entry: evaluate(store, plan, market_snapshot, portfolio_snapshot,
now=None) -> dict. Returns an auditable recommendation with action/status,
reasons/evidence, amount/quantity, horizon, target/stop/expiry and risk context.
No real writes except durable recommendation/evidence records; cash reservation
is planning-only. Active recommendations cannot silently overallocate cash.

Feishu layer must offer injectable HTTP and dry-run preview, explicit execute
for external writes, readback, stable record identities and unknown-outcome
fencing. Existing webhook is one-way: do not pretend it receives user trades.
Use Bitable polling for staged/confirmed ledger inputs. JSON payloads are not
an acceptable sole user interface: expose readable Chinese fields/views.

## Acceptance

Meaningful offline contract, lifecycle, ledger, simulation, degraded-data,
idempotency, Feishu request/readback and runner integration tests; preserve
existing finance tests. SOXX/TSM archived-evidence replay; unsupported target
inputs must remain needs_review. Live quote/field validation and Feishu
permissions are separate acceptance evidence, never fabricated by mocks.
Pin AKShare from Aaronyu29/akshare to a verified merged SHA, no local sys.path
imports as deployment. Never expose or commit secrets. No deployment or real
messages until code is reviewed and concrete runtime configuration is ready.
