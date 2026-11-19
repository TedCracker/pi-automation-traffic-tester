#!/bin/bash
cd /home/bob/pi-traffic-tester && ~/.local/bin/claude -p --model claude-fable-5-2 --dangerously-skip-permissions --output-format stream-json --verbose "Read docs/dev/tasks/UX-INSTRUMENT-FABLE-TASK.md and do it fully." > /home/bob/pi-traffic-tester/docs/dev/tasks/ux-instrument.jsonl 2>&1
