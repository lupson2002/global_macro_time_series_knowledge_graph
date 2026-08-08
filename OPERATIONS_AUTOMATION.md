# Automation operations

## Installed topology

Project jobs run as `mikey` user-systemd timers. User lingering is enabled, so timers
continue without an interactive Linux login. Every timer uses `Persistent=true`, which
runs a missed calendar event when the WSL distribution next starts.

| Timer | Schedule (Asia/Seoul) | Work |
|---|---|---|
| `macro-pipeline.timer` | every 6 hours | RSS ingestion |
| `macro-daily-report.timer` | daily 07:00 | Daily report |
| `macro-auto-blog.timer` | daily 08:30 | Blog workflow |
| `macro-insight-report.timer` | Friday 05:00 | Insight report |
| `macro-cio-report.timer` | Monday 08:00 | 주간 통합 투자인텔리전스 리포트 (구 CIO) |
| `macro-narrative-report.timer` | Wednesday/Sunday 06:00 | Narrative report |
| `macro-watchdog.timer` | every 15 minutes | Timer and journal health |

The Daily wrapper uses `python -m src.report_generator` so package imports remain valid
inside the minimal systemd environment.

The units are versioned under `deploy/systemd/user/` and installed in
`~/.config/systemd/user/`. Journal-aware wrappers write to
`logs/pipeline-events.jsonl`. Wrapper failures propagate to systemd; report content,
transcripts and exception messages are excluded from the journal.

The former five project crontab entries were removed while unrelated entries remain.
The former system-level `market-narrative.timer` is disabled to prevent duplicate runs.

## WSL reboot behavior

Windows Task Scheduler contains `GlobalMacro-WSL-Autostart`, an interactive task for
Windows user `mikey`. At user logon it runs:

```text
wsl.exe -d Ubuntu -u mikey --exec /bin/true
```

This starts Ubuntu/systemd after a Windows reboot. Linux user lingering starts the
enabled timers, and `Persistent=true` catches up work missed while WSL was stopped.
Because a WSL distribution belongs to a Windows user, automation starts at that user's
logon rather than before Windows logon.

Recreate the Windows task from a non-elevated PowerShell session if needed:

```powershell
$a = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\wsl.exe" -Argument "-d Ubuntu -u mikey --exec /bin/true"
$t = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$p = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "GlobalMacro-WSL-Autostart" -Action $a -Trigger $t -Principal $p -Force
```

## Health checks

```bash
systemctl --user list-timers --all | grep macro-
systemctl --user --failed
systemctl --user status macro-watchdog.service
journalctl --user -u macro-watchdog.service -n 50
python scripts/summarize_run_events.py --limit 20
python scripts/check_automation_health.py
```

The watchdog verifies all six work timers are active and checks the newest journal run
per pipeline. It returns exit code 2 for a failed latest run, an execution still running
after eight hours, or malformed journal lines. Before the first event it reports
`awaiting_first_event` as healthy.

## Reinstall

```bash
install -d -m 0755 ~/.config/systemd/user
install -m 0644 deploy/systemd/user/macro-*.service \
  deploy/systemd/user/macro-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now macro-pipeline.timer macro-daily-report.timer \
  macro-auto-blog.timer macro-insight-report.timer macro-cio-report.timer \
  macro-narrative-report.timer macro-watchdog.timer
```

## Recovery

Pre-migration crontab and system Narrative unit copies are under
`backups/operations/` locally (the directory is gitignored). To roll back, disable the
seven `macro-*.timer` units, restore the saved crontab with `crontab <file>`, and restore
the system Narrative unit only after removing the user Narrative timer.
