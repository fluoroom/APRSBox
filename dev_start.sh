#!/usr/bin/env bash
set -e
export APRSBOX_INSTALL_ROOT=/home/nic0/projects/APRSBox/dev_data
export APRSBOX_DB_PATH=/home/nic0/projects/APRSBox/dev_data/aprsbox.db

cd /home/nic0/projects/APRSBox

# Start core service in background
.venv/bin/uvicorn app.core_main:app --host 127.0.0.1 --port 18081 &
CORE_PID=$!

# Start web service
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 &
WEB_PID=$!

echo "Core PID: $CORE_PID  Web PID: $WEB_PID"
echo "Web UI: http://127.0.0.1:8000  login: admin / aprs"
wait
