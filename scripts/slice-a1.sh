#!/bin/sh
# Slice a model for the A1 with the bundled BBL profiles; prints the path of the .gcode.3mf.
# Usage: slice-a1.sh model.stl [filament profile or GFxxx id, default "Generic PLA @BBL A1"] [colour #RRGGBB] [bed type]
# Exit 3: the GFxxx id has no A1 profile.
set -eu
[ $# -ge 1 ] || { echo "uso: slice-a1.sh modello.stl [profilo o id filamento] [colore #RRGGBB] [tipo piatto]" >&2; exit 2; }
for app in /Applications/Orca-Cad.app /Applications/OrcaSlicer.app; do
  [ -x "$app/Contents/MacOS/OrcaSlicer" ] && break
done
[ -x "$app/Contents/MacOS/OrcaSlicer" ] || { echo "Orca non trovato" >&2; exit 1; }
p="$app/Contents/Resources/profiles/BBL"
filament="${2:-Generic PLA @BBL A1}"
colour="${3:-}"
bed="${4:-Textured PEI Plate}"
case "$filament" in
  GF*)
    # The printer reports Bambu's id; this table gives the filament type it stands for.
    type=$(python3 -c 'import json,sys; print(next((x["fila_type"] for x in json.load(open(sys.argv[1]))["data"] if x["fila_id"] == sys.argv[2]), ""))' "$p/filament/filaments_color_codes.json" "$filament")
    [ -f "$p/filament/Bambu $type @BBL A1.json" ] || { echo "nessun profilo A1 per $filament" >&2; exit 3; }
    filament="Bambu $type @BBL A1"
    ;;
esac
dir=$(cd "$(dirname "$1")" && pwd)
name="$(basename "${1%.*}").gcode.3mf"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cd "$tmp" # Orca drops a log file in the working directory.
# Orca joins --outputdir and --export-3mf, so the name must stay relative.
"$app/Contents/MacOS/OrcaSlicer" --datadir "$tmp/data" --slice 1 --arrange 1 \
  --curr-bed-type "$bed" ${colour:+--filament-colour "$colour"} \
  --load-settings "$p/machine/Bambu Lab A1 0.4 nozzle.json;$p/process/0.20mm Standard @BBL A1.json" \
  --load-filaments "$p/filament/$filament.json" \
  --export-3mf "$name" --outputdir "$tmp" "$dir/$(basename "$1")" >"$tmp/log.txt" 2>&1 || { tail -5 "$tmp/log.txt" >&2; exit 1; }
[ -s "$tmp/$name" ] || { echo "slicing senza output" >&2; exit 1; }
mv -f "$tmp/$name" "$dir/$name"
echo "$dir/$name"
