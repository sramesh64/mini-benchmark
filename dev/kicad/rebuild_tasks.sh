#!/bin/bash
# Rebuild the task from its sources: fetch each board at its pinned commit, strip its tracks, run the
# grader's tests on it, and write tasks/route-unseen-boards.
#
#   dev/kicad/rebuild_tasks.sh               the eight boards of route-unseen-boards, then the task
#   dev/kicad/rebuild_tasks.sh <slug> ...    only fetch, strip and test these boards of dev/kicad/boards.tsv
#
# Needs Docker, the kicad-dev image (docker build -t kicad-dev dev/kicad), curl and Python 3. Working
# files go to jobs/_kicad/, which git ignores.
#
# KiCad gives some items a new id each time it saves a board. A rebuilt board can therefore differ
# from the committed one in its (uuid ...) lines. Nothing else changes.
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"
mkdir -p jobs/_kicad/src jobs/_kicad/made
task_boards=$(cd dev/kicad && python3 -c "import package_unseen_task as p; c = p.CONFIGS['unseen-boards']; print(' '.join(dict.fromkeys(c['seen'] + c['run'])))")
want=" ${*:-$task_boards} "
while IFS=$'\t' read -r slug name url source licence; do
  case "$slug" in \#*|"") continue ;; esac
  case "$want" in *" $slug "*) ;; *) continue ;; esac
  dir="jobs/_kicad/src/$slug"
  mkdir -p "$dir"
  for ext in kicad_pcb kicad_pro; do
    [ -s "$dir/$name.$ext" ] || curl -fsSL -o "$dir/$name.$ext" "$url/$name.$ext"
  done
  # a project may have its own custom rules file; most do not
  [ -s "$dir/$name.kicad_dru" ] || curl -fsSL -o "$dir/$name.kicad_dru" "$url/$name.kicad_dru" 2>/dev/null || rm -f "$dir/$name.kicad_dru"
  echo "== $slug"
  docker run --rm --network none -v "$ROOT/jobs/_kicad":/work -v "$ROOT/dev/kicad":/tools:ro -w /work kicad-dev sh -c \
    "python3 /tools/make_route_task.py /work/src/$slug '$name' /work/made/$slug 2>/dev/null | tail -1 | cut -c1-200 && python3 /tools/test_boardcheck.py /work/made/$slug 2>/dev/null | grep -E '^FAIL|checks passed'"
done < dev/kicad/boards.tsv
if [ $# -eq 0 ]; then
  (cd dev/kicad && python3 package_unseen_task.py unseen-boards)
fi
