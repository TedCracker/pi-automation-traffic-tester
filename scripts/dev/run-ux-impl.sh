#!/bin/bash
# Phases 0-4 of the instrument redesign, in order; stop at the first failure.
cd /home/bob/pi-traffic-tester || exit 1
for n in 0 1 2 3 4; do
  ~/bin/claude-phase-run /home/bob/pi-traffic-tester docs/dev/tasks/UX-IMPL-P$n-TASK.md ux-impl-p$n.log \
    || { ~/bin/tg_notify.sh "⚠️ Pi tester redesign stopped in phase $n — see ux-impl-p$n.log"; exit 1; }
done
~/bin/tg_notify.sh "✅ Pi tester redesign: all phases 0-4 done"
sed -i '/ux-impl/d' ~/.claude/pending-work.txt
