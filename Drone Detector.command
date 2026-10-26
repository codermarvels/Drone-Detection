#!/bin/bash
# Double-click this file in Finder to start the drone detector and open its dashboard.
# It activates the conda environment "dronedet", starts the detector on the HackRF
# (or the simulator if the HackRF tools are missing) and opens http://localhost:8080.
# Close this window, or press "Quit the app" on the Settings page, to stop it.

cd "$(dirname "$0")" || exit 1
ENV_NAME="${DRONEDET_ENV:-dronedet}"
PORT=8080
URL="http://localhost:$PORT"

# Already running? Just open the dashboard.
if curl -s -o /dev/null --max-time 2 "$URL/api/settings"; then
  echo "The drone detector is already running. Opening $URL"
  open "$URL"
  exit 0
fi

# Find conda (Miniforge first, as installed on this Mac), never the old Intel Homebrew one.
CONDA=""
for c in "$HOME/miniforge3/bin/conda" "$HOME/mambaforge/bin/conda" "/opt/homebrew/Caskroom/miniforge/base/bin/conda" \
         "/opt/miniforge3/bin/conda" "$HOME/miniconda3/bin/conda" "$HOME/anaconda3/bin/conda" "/opt/homebrew/bin/conda"; do
  if [ -x "$c" ]; then CONDA="$c"; break; fi
done
if [ -z "$CONDA" ] && command -v conda >/dev/null 2>&1; then CONDA="$(command -v conda)"; fi
if [ -z "$CONDA" ]; then
  echo "Could not find conda. Install Miniforge, then create the '$ENV_NAME' environment (see README)."
  read -r -p "Press Return to close."
  exit 1
fi
eval "$("$CONDA" shell.bash hook)"
if ! conda activate "$ENV_NAME"; then
  echo "The conda environment '$ENV_NAME' does not exist yet (see README: Install)."
  read -r -p "Press Return to close."
  exit 1
fi

# First run after an update: make sure the package is installed from this folder.
if ! python -c "import dronedet.control" >/dev/null 2>&1; then
  echo "Installing the drone detector into '$ENV_NAME'…"
  python -m pip install -q -e . || { read -r -p "Install failed. Press Return to close."; exit 1; }
fi

echo "Starting the drone detector. The dashboard opens in your browser."
echo "Leave this window open while you use it."
python -m dronedet.cli run --source auto --open --port "$PORT"
echo
read -r -p "The drone detector has stopped. Press Return to close this window."
