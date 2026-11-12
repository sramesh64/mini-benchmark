# Write a router

Write a program that routes circuit boards. It is judged on boards you have not seen.

## What you have

- **4 practice boards** in `/home/agent/boards/`, one folder each. Each is a real KiCad board with every
  part placed and the copper tracks missing, with its design rules next to it: the project file
  (`.kicad_pro`) and a rules file (`.kicad_dru`). The rules file sets a minimum track width for some
  nets and, on some boards, a maximum length difference for the two tracks of a differential pair.
- **KiCad 9, without its graphical programs:** `kicad-cli`, and the `pcbnew` Python module
  (`python3 -c "import pcbnew"`). numpy is installed.
- **`boardcheck --board <folder name> <routed file>`:** checks a routed board from `/home/agent/boards/`
  the same way the grader does and prints the result. `boardcheck --help` lists its options. Its
  source is in `/opt/boardcheck/`.
- **No network.** The container can reach the model API and nothing else.

## What to hand in

A folder `/home/agent/work/router/` with a file `route.py` in it. The grader runs

    python3 route.py <board.kicad_pcb> <routed.kicad_pcb>

The first argument is an unrouted board; its `.kicad_pro` and `.kicad_dru` are next to it under the
same name. Your program writes the routed board to the second.

## Rules

- The program adds tracks and vias only. Only tracks, arcs and vias are read from the file it writes.
  Parts, pads, nets, zones, the board outline and the design rules are taken from the board as given.
- Vias go through the whole board. Blind, buried and micro vias are not read.
- The program runs by itself, without you and without a network. It may use what is installed here
  (KiCad 9's Python module and command line, numpy, Python's standard library) and the files in its
  own folder.
- Use only the software in this environment.

## How it is judged

- Your program is run once on each of 4 boards you have not seen. They are real boards of the same
  kind as the practice boards: two copper layers and 38 to 83 nets to route.
- Each run gets 8 minutes on 2 CPUs with 4 GB of memory. The routed file that exists when the run
  ends is the one that is scored; no file scores 0.
- A board is scored the way `boardcheck` scores it: the nets that count, divided by the nets that
  need routing. A net counts if all its pads are connected and none of its new copper is in a rule
  violation.
- On a net with a length rule, a track or via that leads nowhere counts as a violation: the length
  has to be made up with copper the signal runs through.
- Your score is the average over the 4 boards. It is 1.0 only if every board is complete.

## Budget

75 minutes of wall-clock time.
