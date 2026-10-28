# Alfa profile — `unlock.status: verified`, 2026-10-28

Aggregate evidence only: no code, no operation titles, names, amounts or account data.

**The scheduled run on devicex2 entered the static «Секретный код» end to end on the live
cabinet.** That is what `unlock.status: verified` in `alfa.yaml` means (see
`docs/ADD_A_BANK.md`, "Переключите статус"): the screen was recognised, one code was typed,
the cabinet opened, the report was written, and the process exited 0. The upload of that report
was done by the owner's devicex2 scheduler wrapper (the exiosv-lab-dev-config job), not by
bank-collector: bank-collector itself sends nothing anywhere (see `docs/SECURITY.md`:90). The
top-level profile status (`verified`) still means the numbers were checked against the
cabinet; the unlock status only covers the code entry.

## Timeline (2026-10-28, UTC, from the systemd journal)

```
03:00:22Z  alfa: run started (scheduled daily job)
03:03:58Z  alfa attempt=1 exit=0 UNLOCK: экран «Секретный код» — код введён
           written: …/reports/alfa.json
03:03:59Z  report alfa-2026-10-28.json 30215 bytes
           upload exit=0
```

- One attempt, no retry, no failed-attempt marker written (`alfa-code.failed` absent after the
  run).
- `UNLOCK: экран «Секретный код» — код введён` is printed only when the screen was detected,
  the code was filled, the submit button was pressed and `success_wait` (the dashboard) then
  appeared.
- The report exists and is non-empty (30215 bytes), so the run went on to read the cabinet
  after the unlock; the upload of that report exited 0. That upload was done by the owner's
  devicex2 scheduler wrapper (the exiosv-lab-dev-config job), not by bank-collector.
- The code itself, and every amount in the report, is deliberately not reproduced here.

## What this does not cover

- It is a single observation. It proves the flow works, not that Alfa can never change the
  screen: the selectors are still maintenance, and a screen that no longer matches is reported
  as `UNLOCK: не удалось (<reason>)` instead of being typed into.
- It says nothing about the profile's numeric verification, which is a separate claim.
- A real sign-out (a full logout rather than the static-code screen) has no `signed_out`
  marker in the Alfa profile: such a run still ends with exit 4.
