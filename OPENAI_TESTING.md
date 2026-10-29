# Testing the OpenAI Provider (office laptop, real key)

This app defaults to Ollama. Nothing below is required to run the
default workflow - these steps are only for trying the OpenAI provider,
on the `feature/openai-provider` branch, with your real OpenAI API key.

Everything here is Windows PowerShell. Replace `sk-...your-key...` with
your real key - never paste the real key into a file that gets
committed, and never run these commands with the key typed directly
into a command you might paste into chat/logs; setting it as shown below
keeps it in your shell session only.

## 1. Activate the existing virtual environment

```powershell
cd C:\folder\Credit-Review-System---01
.\venv\Scripts\Activate.ps1
```
(Adjust the path if your venv folder has a different name.)

## 2. Install the updated dependencies

```powershell
pip install -r requirements.txt
```
This installs `openai` (pinned in `requirements.txt`) alongside the
existing packages. The default Ollama workflow does not need this step
at all - only do this if you're testing the OpenAI provider.

## 3. Configure OPENAI_API_KEY locally (never committed)

Pick ONE of these - both keep the key out of git:

**Option A - this PowerShell session only (simplest, gone when you close the window):**
```powershell
$env:OPENAI_API_KEY = "sk-...your-key..."
```

**Option B - Streamlit's own secrets file (persists across sessions, already gitignored):**
Create/edit `.streamlit\secrets.toml` in the project root:
```toml
OPENAI_API_KEY = "sk-...your-key..."
```
Confirm it's ignored before doing anything else:
```powershell
git check-ignore -v .streamlit\secrets.toml
```
This should print a match against `.gitignore`. If it prints nothing,
stop and ask before proceeding - that would mean the file is NOT
ignored.

## 4. Select OpenAI as the provider

```powershell
$env:AI_PROVIDER = "openai"
```
Optional overrides (defaults shown):
```powershell
$env:OPENAI_MODEL = "gpt-4o-mini"            # must be enabled for your account - checked automatically, never assumed
$env:OPENAI_AVAILABILITY_TIMEOUT = "10"      # seconds
$env:OPENAI_MAX_RETRIES = "0"                # retries per OpenAI request (see "Timeouts and time budgets")
```

`AI_PROVIDER`, `OPENAI_MODEL` and the timeout/budget variables are read
once, when the app starts. Set them in the SAME PowerShell window you run
`streamlit run` from, and restart Streamlit after changing them - an
already-running app keeps the provider it started with.

## Timeouts and time budgets

All three AI stages (validation, dynamic mapping, correction proposals)
use the same provider selected by `AI_PROVIDER`. Each stage works in
batches and has its own overall time budget. The budget is checked
before each batch starts: once it has passed, every batch not yet
started is marked "skipped (time budget exhausted)" and the stage moves
on. A batch already in progress is not interrupted, so a stage can run
for at most its budget plus one request timeout.

| Stage | Budget (seconds) | Per-request timeout (seconds) | Fields per batch |
|---|---|---|---|
| AI validation | `AI_VALIDATION_TIME_BUDGET_SECONDS` = 240 | `OLLAMA_CALL_TIMEOUT` = 150 | `AI_VALIDATION_MAX_FIELDS_PER_BATCH` = 1 |
| AI dynamic mapping | `AI_MAPPING_TIME_BUDGET_SECONDS` = 120 | `AI_MAPPING_CALL_TIMEOUT` = 120 | `AI_MAPPING_MAX_FIELDS_PER_BATCH` = 2 |
| Correction proposals | `AI_CORRECTION_TIME_BUDGET_SECONDS` = 120 | `AI_CORRECTION_CALL_TIMEOUT` = 120 | `AI_CORRECTION_MAX_FIELDS_PER_BATCH` = 2 |

(`OLLAMA_CALL_TIMEOUT` applies to whichever provider is selected,
despite its name.)

- "skipped (time budget exhausted)" means the stage's overall budget ran
  out before some batches could start. A single request that times out
  is reported differently: as a technical error with the reason
  "OpenAI call failed: ... timed out ...".
- The budgets are the same for both providers. They are set per launch,
  so tune them in the same window as `AI_PROVIDER`. Example for a slow
  local Ollama on CPU: `$env:AI_MAPPING_TIME_BUDGET_SECONDS = "300"`.
- `OPENAI_MAX_RETRIES` (default `0`): the OpenAI SDK would otherwise retry
  timeouts, rate limits (429) and server errors twice on its own, so one
  request could take about three times its timeout. With `0`, each
  request takes at most its timeout. Raising it to `N` allows up to
  `N + 1` attempts per request - the worst-case wait grows to match.

## 5. Start Streamlit

```powershell
streamlit run app.py
```
Upload a financial statement PDF as usual. The Data Review page will
show AI Validation / AI Dynamic Field Mapping exactly as before -
Accept/Reject/Keep Original decisions and human approval requirements
are identical regardless of provider.

## 6. Run a small API connectivity test (before trusting a real upload)

```powershell
python -c "import ai_validation as av; print(av.check_ollama_availability())"
```
Expect `reachable=True, model_installed=True`. If it instead shows a
clear detail message (missing key, model not available, etc.), fix that
before uploading a real document - nothing will silently fall back to
Ollama.

## 7. Run the same PDF test cases against OpenAI

The existing automated test suite mocks every provider call (no real
key needed for it), so it won't exercise your real key. To see OpenAI
work against a real document, re-run the SAME manual upload from step 5
with a real PDF, and compare the Data Review page's AI Dynamic Field
Mapping table/summary against a prior Ollama run of the identical file
(see "Comparing providers" below).

## 8. Switch back to Ollama immediately

```powershell
$env:AI_PROVIDER = "ollama"
```
or simply close this PowerShell window/tab - `AI_PROVIDER` was only set
for this session and the app always defaults to `ollama` when it's
unset.

---

## Comparing providers (same PDF, both providers)

1. Upload a PDF with `AI_PROVIDER=ollama` (or unset), note the Data
   Review page's AI Dynamic Field Mapping summary/table and Field-Level
   Review AI Validation column for a few fields.
2. Reset (upload a different financial statement / refresh the
   session), set `AI_PROVIDER=openai`, upload the SAME PDF again.
3. Compare: validation outcomes (CONFIRMED/YEAR_MISMATCH/etc.), which
   source row each provider selected for the same NOT_FOUND field,
   whether deterministic verification passed, which fields stayed
   unresolved and why, and the time each run actually took. OpenAI's
   approximate cost for a run can be checked against your OpenAI
   account's usage dashboard - this app does not track or display cost
   itself.
4. Neither provider's output is ever trusted automatically - every
   mapping still goes through the same deterministic verification and
   requires the same explicit human Accept/Reject/Keep decision either
   way.

## Troubleshooting

- **"OPENAI_API_KEY is not set"** - re-run step 3; confirm `$env:OPENAI_API_KEY`
  prints something in this same PowerShell session (`echo $env:OPENAI_API_KEY`),
  or confirm `.streamlit\secrets.toml` exists and is spelled correctly.
- **"The 'openai' package is not installed"** - re-run step 2 in this
  same venv (`pip show openai` should show the version from
  `requirements.txt`).
- **"model '...' is not available to this account"** - your API key's
  account doesn't have that model enabled; try a different
  `OPENAI_MODEL` value or check your OpenAI account's model access.
- **"N of M field(s) skipped (time budget exhausted)"** - the stage's
  overall budget ran out (see "Timeouts and time budgets"). First check
  which provider actually ran: the app reads `AI_PROVIDER` only at
  startup, so restart Streamlit from the window where you set it. With
  OpenAI, a request usually takes a few seconds; with a local Ollama on
  CPU it can take tens of seconds, which is what usually exhausts the
  120-second mapping budget. Raise the budget if needed.
- Nothing here ever requires deleting or resetting anything in git -
  if something looks wrong, just close the terminal and reopen a fresh
  one; your repository is untouched by any of these steps.
