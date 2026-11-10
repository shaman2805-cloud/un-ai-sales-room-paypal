#!/bin/zsh
set -eu
cd -- "${0:A:h}"
if command -v python3 >/dev/null 2>&1; then
  task_python="$(command -v python3)"
elif [[ -x /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 ]]; then
  task_python=/Library/Frameworks/Python.framework/Versions/3.14/bin/python3
else
  print 'Python 3.10 or newer is required. Download it from python.org.'
  read '?Press Enter to close.'
  exit 1
fi
unai_demo_intake=n
if [[ ! -s runtime/launcher-ai-demo.json ]]; then
  print 'Optional: draft the fictional example with Codex before opening the panel.'
  read -r 'unai_demo_intake?Run Codex intake now? [y/N]: '
fi
if [[ "$unai_demo_intake" == [yY] ]]; then
  umask 077
  mkdir -p runtime
  unai_draft=runtime/launcher-ai-demo.json
  if [[ ! -s "$unai_draft" ]]; then
    unai_draft_tmp="$(mktemp runtime/launcher-ai.XXXXXX)"
    if "$task_python" -m salesroom.ai_intake --input examples/lead.demo.txt > "$unai_draft_tmp"; then
      mv -- "$unai_draft_tmp" "$unai_draft"
    else
      rm -- "$unai_draft_tmp"
      print 'Codex intake did not finish. You can use the sample from the New lead form.'
    fi
  fi
  if [[ -s "$unai_draft" ]]; then
    if "$task_python" -m salesroom create --event-id paypal-launcher-demo-v1 --input "$unai_draft" > /dev/null; then
      print 'The fictional AI draft is saved in the owner panel.'
    fi
  fi
fi
for unai_port in {8788..8795}; do
  if ! /usr/sbin/lsof -nP -iTCP:"$unai_port" -sTCP:LISTEN -t >/dev/null 2>&1; then
    exec "$task_python" -m salesroom serve --port "$unai_port" --open
  fi
done
print 'Close an older UN AI Sales Room window and run this launcher again.'
